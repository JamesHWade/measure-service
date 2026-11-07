"""Owner-scoped durable state for bounded chromatogram analysis batches.

The runner owns process capacity and numerical execution.  This module owns
only the immutable declaration, SQLite claims, attempt history and atomic
result handoff.  No transaction is held across numerical work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from . import analysis_evidence, analysis_runtime
from .analysis_models import Dataset
from .batch_models import (
    MAX_BATCH_ITEMS,
    MAX_BATCH_POINTS,
    MAX_MANIFEST_BYTES,
    PROFILE,
    SCHEMA_VERSION,
    BatchSpec,
)
from .storage import canonical, now, registry_connection, registry_mutation, sha256

LEASE_SECONDS = 150
MAX_ATTEMPTS = 8
MAX_PAGE = 1_000
MAX_ITEM_PAGE = 20
MAX_ATTEMPT_PAGE = 20
MAX_ERROR_BYTES = 4_000
MAX_RUNTIME_BYTES = 4_000
MAX_EXECUTION_CONTEXT_BYTES = 4_000
MAX_COMPACT_RESULT_BYTES = 8_000
MAX_BATCH_RESPONSE_BYTES = MAX_MANIFEST_BYTES + 16_384
MAX_CONTROL_EVENT_BYTES = 8_192

_ITEM_STATES = {"pending", "running", "completed", "failed", "interrupted", "cancelled"}
_RECORD_FIELDS = {
    "analysis_id",
    "dataset_id",
    "trace_id",
    "kind",
    "method",
    "result",
    "provenance",
    "created_at",
}


class ClaimFenceError(ValueError):
    """A worker claim is invalid, expired, or no longer running."""


def _owner(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("Owner must be nonempty text")
    return value


def _key(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 100 or not value.strip():
        raise ValueError("A nonempty idempotency key of at most 100 characters is required")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{label} must be nonempty text")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _ordinal(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("ordinal must be a nonnegative integer")
    return value


def _page_value(value: Any, label: str, *, default: int, maximum: int) -> int:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    if label == "limit" and not 1 <= value <= maximum:
        raise ValueError(f"limit must be an integer from 1 through {maximum}")
    return value


def _validate_json(value: Any, label: str, limit: int, *, object_only: bool = False) -> str:
    if object_only and not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    try:
        serialized = canonical(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must contain strict finite JSON values") from exc
    if len(serialized.encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds its byte limit")
    return serialized


def _decode_json(serialized: Any, label: str, limit: int, *, object_only: bool = False) -> Any:
    if not isinstance(serialized, str) or len(serialized.encode("utf-8")) > limit:
        raise ValueError(f"Stored {label} exceeds its byte limit")
    try:
        value = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Stored {label} is malformed") from exc
    _validate_json(value, f"Stored {label}", limit, object_only=object_only)
    if canonical(value) != serialized:
        raise ValueError(f"Stored {label} is not canonically encoded")
    return value


def _source_content_sha256(content: Any) -> str:
    if not isinstance(content, str):
        raise ValueError("Stored source content is not text")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _token(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("execution token must be nonempty text")
    return value


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _lease_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()


def _lease_is_expired(value: Any, *, at: datetime | None = None) -> bool:
    if not isinstance(value, str):
        raise ValueError("Stored claim lease is malformed")
    try:
        expiry = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Stored claim lease is malformed") from exc
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry <= (datetime.now(timezone.utc) if at is None else at)


def _coerce_spec(spec: BatchSpec | Mapping[str, Any]) -> BatchSpec:
    if isinstance(spec, BatchSpec):
        return spec
    if not isinstance(spec, Mapping):
        raise ValueError("Batch spec must be a BatchSpec or JSON object")
    return BatchSpec.model_validate(spec)


def _batch_row(con: sqlite3.Connection, owner: str, batch_id: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM analysis_batches WHERE id=? AND owner=?", (batch_id, owner)
    ).fetchone()
    if row is None:
        raise ValueError("Batch was not found or is not accessible")
    return row


def _item_row(con: sqlite3.Connection, owner: str, batch_id: str, ordinal: int) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM analysis_batch_items WHERE owner=? AND batch_id=? AND ordinal=?",
        (owner, batch_id, ordinal),
    ).fetchone()
    if row is None:
        raise ValueError("Batch item was not found or is not accessible")
    return row


def _stored_batch_spec(row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any]]:
    method = _decode_json(row["method"], "batch method", MAX_MANIFEST_BYTES, object_only=True)
    manifest = _decode_json(row["manifest"], "batch manifest", MAX_MANIFEST_BYTES, object_only=True)
    if (
        row["profile"] != PROFILE
        or row["schema_version"] != SCHEMA_VERSION
        or row["title"] != manifest.get("title")
        or manifest.get("profile") != PROFILE
        or manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("method") != method
        or row["manifest_digest"] != sha256(manifest)
    ):
        raise ValueError("Stored batch declaration has an unsupported profile or version")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != row["item_count"]:
        raise ValueError("Stored batch manifest item count is inconsistent")
    declaration = {
        "schema_version": SCHEMA_VERSION,
        "profile": PROFILE,
        "title": manifest.get("title"),
        "method": method,
        "inputs": [
            {
                "dataset_id": item.get("dataset_id"),
                "trace_id": item.get("trace_id"),
                "role": item.get("role"),
                "internal_standard_references": item.get("internal_standard_references", []),
            }
            for item in inputs
        ],
    }
    if sha256(declaration) != row["input_digest"]:
        raise ValueError("Stored batch declaration digest does not match its manifest")
    return method, manifest


def _validate_item_binding(row: sqlite3.Row, manifest: dict[str, Any]) -> None:
    inputs = manifest.get("inputs")
    ordinal = row["ordinal"]
    if not isinstance(inputs, list) or not 0 <= ordinal < len(inputs):
        raise ValueError("Stored batch item ordinal is outside its manifest")
    expected = inputs[ordinal]
    actual = _item_payload(row)
    if (
        expected != actual
        or row["dataset_id"] != expected.get("dataset_id")
        or row["trace_id"] != expected.get("trace_id")
        or row["role"] != expected.get("role")
        or row["input_sha256"] != expected.get("dataset_sha256")
        or row["source_sha256"] != expected.get("source_sha256")
    ):
        raise ValueError("Stored batch item no longer matches its immutable manifest")


def _bound_item_public(row: sqlite3.Row, manifest: dict[str, Any]) -> dict[str, Any]:
    _validate_item_binding(row, manifest)
    return _item_public(row)


def _counts(con: sqlite3.Connection, owner: str, batch_id: str) -> dict[str, int]:
    rows = con.execute(
        """SELECT state, COUNT(*) AS count FROM analysis_batch_items
        WHERE owner=? AND batch_id=? GROUP BY state""",
        (owner, batch_id),
    ).fetchall()
    counts = {state: 0 for state in _ITEM_STATES}
    for row in rows:
        if row["state"] not in counts:
            raise ValueError("Stored batch item state is unsupported")
        counts[row["state"]] = int(row["count"])
    return counts


def _derived_status(row: sqlite3.Row, counts: dict[str, int]) -> str:
    if row["cancel_requested"]:
        return "cancelling" if counts["running"] else "cancelled"
    if counts["completed"] == row["item_count"]:
        return "completed"
    return "active"


def _refresh_status(con: sqlite3.Connection, owner: str, batch_id: str, timestamp: str) -> None:
    row = _batch_row(con, owner, batch_id)
    status = _derived_status(row, _counts(con, owner, batch_id))
    con.execute(
        "UPDATE analysis_batches SET status=?, updated_at=? WHERE id=? AND owner=?",
        (status, timestamp, batch_id, owner),
    )


def _item_payload(row: sqlite3.Row) -> dict[str, Any]:
    return _decode_json(row["payload"], "batch item", MAX_MANIFEST_BYTES, object_only=True)


def _compact_from_row(serialized: Any) -> dict[str, Any] | None:
    if serialized is None:
        return None
    value = _decode_json(serialized, "compact analysis", MAX_COMPACT_RESULT_BYTES, object_only=True)
    if any(key in value for key in ("peaks", "processed_trace", "axis", "signal")):
        raise ValueError("Stored compact analysis contains raw arrays")
    return value


def _item_public(row: sqlite3.Row, *, include_token: bool = False) -> dict[str, Any]:
    payload = _item_payload(row)
    value = {
        "item_id": row["id"],
        "batch_id": row["batch_id"],
        "ordinal": row["ordinal"],
        "dataset_id": row["dataset_id"],
        "trace_id": row["trace_id"],
        "role": row["role"],
        "internal_standard_references": payload.get("internal_standard_references", []),
        "dataset_title": payload.get("dataset_title"),
        "trace": payload.get("trace"),
        "point_count": payload.get("point_count"),
        "input_sha256": row["input_sha256"],
        "source_sha256": row["source_sha256"],
        "state": row["state"],
        "attempt_count": row["attempt_count"],
        "analysis_id": row["analysis_id"],
        "result_sha256": row["result_digest"],
        "evidence_sha256": row["evidence_digest"],
        # Item reads carry the bounded compact summary once; attempt history
        # retains the same identity through its digests without duplicating
        # that payload on every historical attempt page.
        "result": _compact_from_row(row["compact_result"]),
    }
    if include_token:
        value["execution_token"] = None
    return value


def _attempt_public(row: sqlite3.Row, *, include_token: bool = False) -> dict[str, Any]:
    value = {
        "attempt_id": row["id"],
        "batch_id": row["batch_id"],
        "run_id": row["run_id"],
        "ordinal": row["ordinal"],
        "attempt_no": row["attempt_no"],
        "state": row["state"],
        "lease_expires_at": row["lease_expires_at"],
        "execution_context": _decode_json(
            row["execution_context"],
            "execution context",
            MAX_EXECUTION_CONTEXT_BYTES,
            object_only=True,
        ),
        "runtime": _decode_json(
            row["runtime"], "attempt runtime", MAX_RUNTIME_BYTES, object_only=True
        ),
        "error": None
        if row["error"] is None
        else _decode_json(row["error"], "attempt error", MAX_ERROR_BYTES),
        "analysis_id": row["analysis_id"],
        "result_sha256": row["result_digest"],
        "evidence_sha256": row["evidence_digest"],
        # The compact result is available on the current item/run selection;
        # attempt history stays bounded by retaining only its identity and
        # evidence digests.
        "result": None,
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
    if include_token:
        value["execution_token"] = row["execution_token"]
    return value


def _run_public(
    con: sqlite3.Connection, owner: str, run_id: str, *, include_tokens: bool = False
) -> dict[str, Any]:
    row = con.execute(
        "SELECT * FROM analysis_batch_runs WHERE id=? AND owner=?", (run_id, owner)
    ).fetchone()
    if row is None:
        raise ValueError("Batch run was not found or is not accessible")
    batch = _batch_row(con, owner, row["batch_id"])
    _batch_method, manifest = _stored_batch_spec(batch)
    selections = []
    token_column = ", ri.execution_token" if include_tokens else ""
    for selected in con.execute(
        f"""SELECT ri.run_id, ri.owner, ri.batch_id, ri.ordinal, ri.attempt_id,
          ri.created_at {token_column}, i.id AS item_id, i.state AS item_state,
          a.attempt_no, a.state AS attempt_state, a.lease_expires_at,
          a.error, a.analysis_id, a.result_digest, a.evidence_digest,
          a.compact_result, a.execution_context, a.runtime, a.started_at,
          a.finished_at
        FROM analysis_batch_run_items ri
        JOIN analysis_batch_items i
          ON i.owner=ri.owner AND i.batch_id=ri.batch_id AND i.ordinal=ri.ordinal
        JOIN analysis_batch_attempts a ON a.owner=ri.owner AND a.id=ri.attempt_id
        WHERE ri.owner=? AND ri.run_id=? ORDER BY ri.ordinal""",
        (owner, run_id),
    ):
        _validate_item_binding(
            _item_row(con, owner, row["batch_id"], selected["ordinal"]), manifest
        )
        selection = {
            "ordinal": selected["ordinal"],
            "item_id": selected["item_id"],
            "attempt_id": selected["attempt_id"],
            # ``state`` belongs to this immutable run claim.  The item may
            # later be retried under a different run, so using its current
            # state here would rewrite this run's historical outcome.
            "state": selected["attempt_state"],
            "item_state": selected["item_state"],
            "attempt_state": selected["attempt_state"],
            "attempt_no": selected["attempt_no"],
            "lease_expires_at": selected["lease_expires_at"],
            "analysis_id": selected["analysis_id"],
            "result_sha256": selected["result_digest"],
            "evidence_sha256": selected["evidence_digest"],
            "result": _compact_from_row(selected["compact_result"]),
            "error": None
            if selected["error"] is None
            else _decode_json(selected["error"], "attempt error", MAX_ERROR_BYTES),
            "execution_context": _decode_json(
                selected["execution_context"],
                "execution context",
                MAX_EXECUTION_CONTEXT_BYTES,
                object_only=True,
            ),
            "runtime": _decode_json(
                selected["runtime"], "attempt runtime", MAX_RUNTIME_BYTES, object_only=True
            ),
            "started_at": selected["started_at"],
            "finished_at": selected["finished_at"],
        }
        if include_tokens:
            selection["execution_token"] = selected["execution_token"]
        selections.append(selection)
    states = [item["state"] for item in selections]
    if not states:
        status = "empty"
    elif all(state == "completed" for state in states):
        status = "completed"
    elif any(state == "running" for state in states):
        status = "running"
    elif any(state == "failed" for state in states):
        status = "failed"
    elif any(state == "interrupted" for state in states):
        status = "interrupted"
    elif any(state == "cancelled" for state in states):
        status = "cancelled"
    else:
        status = "active"
    result = {
        "run_id": row["id"],
        "batch_id": row["batch_id"],
        "expected_revision": row["expected_revision"],
        "max_items": row["max_items"],
        "selected_count": row["selected_count"],
        "request_sha256": row["request_digest"],
        "request_key_sha256": _token_digest(row["request_key"]),
        "status": status,
        "created_at": row["created_at"],
        "items": selections,
    }
    _validate_json(result, "batch run response", 256 * 1024, object_only=True)
    return result


def _validated_batch(
    con: sqlite3.Connection, owner: str, row: sqlite3.Row
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    """Validate the declaration and every item binding in the caller's transaction."""
    method, manifest = _stored_batch_spec(row)
    for item in con.execute(
        "SELECT * FROM analysis_batch_items WHERE owner=? AND batch_id=? ORDER BY ordinal",
        (owner, row["id"]),
    ):
        _validate_item_binding(item, manifest)
    counts = _counts(con, owner, row["id"])
    if sum(counts.values()) != row["item_count"]:
        raise ValueError("Stored batch item count is inconsistent")
    return method, manifest, counts


def _batch_public(con: sqlite3.Connection, owner: str, row: sqlite3.Row) -> dict[str, Any]:
    method, manifest, counts = _validated_batch(con, owner, row)
    result = {
        "batch_id": row["id"],
        "schema_version": row["schema_version"],
        "profile": row["profile"],
        "title": row["title"],
        "method": method,
        "manifest": manifest,
        "input_sha256": row["input_digest"],
        "manifest_sha256": row["manifest_digest"],
        "point_count": row["point_count"],
        "item_count": row["item_count"],
        "revision": row["revision"],
        "status": _derived_status(row, counts),
        "cancel_requested": bool(row["cancel_requested"]),
        "counts": counts,
        "control_event_count": con.execute(
            "SELECT COUNT(*) FROM analysis_batch_control_events WHERE owner=? AND batch_id=?",
            (owner, row["id"]),
        ).fetchone()[0],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    _validate_json(result, "batch response", MAX_BATCH_RESPONSE_BYTES, object_only=True)
    return result


def _freeze_inputs(
    con: sqlite3.Connection, owner: str, spec: BatchSpec
) -> tuple[list[dict[str, Any]], int]:
    frozen: list[dict[str, Any]] = []
    grains: set[tuple[str, str, str, str]] = set()
    point_count = 0
    for ordinal, selected in enumerate(spec.inputs):
        row = con.execute(
            "SELECT digest, payload FROM datasets WHERE id=? AND owner=?",
            (selected.dataset_id, owner),
        ).fetchone()
        if row is None:
            raise ValueError("Dataset was not found or is not accessible")
        dataset = _decode_json(row["payload"], "dataset", 4_000_000, object_only=True)
        if sha256(dataset) != row["digest"]:
            raise ValueError("Stored dataset digest does not match its contents")
        try:
            Dataset.model_validate(dataset)
        except Exception as exc:
            raise ValueError("Stored dataset is invalid") from exc
        traces = [
            trace
            for trace in dataset.get("traces", [])
            if trace.get("trace_id") == selected.trace_id
        ]
        if len(traces) != 1:
            raise ValueError("Trace was not found in the dataset")
        trace = traces[0]
        grain = (selected.dataset_id, trace["sample_id"], trace["injection_id"], trace["channel"])
        if grain in grains:
            raise ValueError("Batch inputs contain a duplicate sample/injection/channel grain")
        grains.add(grain)
        point_count += len(trace["axis"])
        if point_count > MAX_BATCH_POINTS:
            raise ValueError("Batch exceeds the total point limit")

        source_row = con.execute(
            """SELECT s.kind, s.digest, s.content, s.mapping FROM dataset_sources s
            JOIN datasets d ON d.id=s.dataset_id
            WHERE s.dataset_id=? AND d.owner=?""",
            (selected.dataset_id, owner),
        ).fetchone()
        source: dict[str, Any] | None
        if source_row is None:
            source = None
        else:
            source_mapping = _decode_json(
                source_row["mapping"], "source import mapping", MAX_MANIFEST_BYTES, object_only=True
            )
            source_digest = source_row["digest"]
            if (
                not isinstance(source_digest, str)
                or len(source_digest) != 64
                or any(character not in "0123456789abcdef" for character in source_digest)
                or _source_content_sha256(source_row["content"]) != source_digest
            ):
                raise ValueError("Stored source digest does not match its contents")
            source = {
                "kind": source_row["kind"],
                "source_sha256": source_digest,
                "mapping": source_mapping,
            }
        metadata = {
            key: trace[key]
            for key in (
                "trace_id",
                "sample_id",
                "injection_id",
                "channel",
                "separation",
                "detector",
                "axis_unit",
                "signal_unit",
                "sample_type",
                "batch_id",
                "run_order",
            )
        }
        frozen.append(
            {
                "ordinal": ordinal,
                "dataset_id": selected.dataset_id,
                "trace_id": selected.trace_id,
                "role": selected.role,
                "internal_standard_references": list(selected.internal_standard_references),
                "dataset_title": dataset["title"],
                "dataset_sha256": row["digest"],
                "source": source,
                "source_sha256": None if source is None else source["source_sha256"],
                "trace": metadata,
                "point_count": len(trace["axis"]),
            }
        )
    return frozen, point_count


def _create_batch_record(con: sqlite3.Connection, owner: str, spec: BatchSpec) -> dict[str, Any]:
    """Create the same pending declaration inside a caller-owned transaction."""
    requested = spec.model_dump(mode="json")
    frozen, point_count = _freeze_inputs(con, owner, spec)
    batch_id = str(uuid.uuid4())
    timestamp = now()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "profile": PROFILE,
        "title": spec.title,
        "method": spec.method.model_dump(mode="json"),
        "inputs": frozen,
        "point_count": point_count,
    }
    manifest_serialized = _validate_json(
        manifest, "Batch manifest", MAX_MANIFEST_BYTES, object_only=True
    )
    method_serialized = _validate_json(
        manifest["method"], "Batch method", MAX_MANIFEST_BYTES, object_only=True
    )
    con.execute(
        """INSERT INTO analysis_batches(
          id, owner, profile, schema_version, title, method, manifest,
          manifest_digest, input_digest, point_count, item_count, revision, status,
          cancel_requested, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', 0, ?, ?)""",
        (
            batch_id,
            owner,
            PROFILE,
            SCHEMA_VERSION,
            spec.title,
            method_serialized,
            manifest_serialized,
            sha256(manifest),
            sha256(requested),
            point_count,
            len(frozen),
            timestamp,
            timestamp,
        ),
    )
    for item in frozen:
        item_serialized = _validate_json(
            item, "Batch item manifest", MAX_MANIFEST_BYTES, object_only=True
        )
        con.execute(
            """INSERT INTO analysis_batch_items(
              id, owner, batch_id, ordinal, dataset_id, trace_id, role,
              input_sha256, source_sha256, payload, state, attempt_count,
              analysis_id, result_digest, evidence_digest, compact_result,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL, ?, ?)""",
            (
                str(uuid.uuid4()),
                owner,
                batch_id,
                item["ordinal"],
                item["dataset_id"],
                item["trace_id"],
                item["role"],
                item["dataset_sha256"],
                item["source_sha256"],
                item_serialized,
                timestamp,
                timestamp,
            ),
        )
    return {"batch_id": batch_id}


def create_batch(owner: str, spec: BatchSpec | Mapping[str, Any], key: str) -> dict[str, Any]:
    """Freeze an owned bounded batch declaration with an idempotent receipt."""
    owner = _owner(owner)
    key = _key(key)
    spec = _coerce_spec(spec)
    requested = spec.model_dump(mode="json")
    request = {"operation": "create_batch", "spec": requested}

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        return _create_batch_record(con, owner, spec)

    response = registry_mutation(owner, key, request, write)
    batch_id = response.get("batch_id") if isinstance(response, dict) else None
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("Stored batch creation receipt is malformed")
    return get_batch(owner, batch_id)


def get_batch(owner: str, batch_id: str) -> dict[str, Any]:
    owner = _owner(owner)
    batch_id = _identifier(batch_id, "batch_id")
    with registry_connection(write=False) as con:
        con.row_factory = sqlite3.Row
        return _batch_public(con, owner, _batch_row(con, owner, batch_id))


def read_items(owner: str, batch_id: str, offset: int = 0, limit: int = 20) -> dict[str, Any]:
    owner = _owner(owner)
    batch_id = _identifier(batch_id, "batch_id")
    offset = _page_value(offset, "offset", default=0, maximum=MAX_PAGE)
    limit = _page_value(limit, "limit", default=20, maximum=MAX_ITEM_PAGE)
    with registry_connection(write=False) as con:
        con.row_factory = sqlite3.Row
        batch = _batch_row(con, owner, batch_id)
        _batch_method, manifest = _stored_batch_spec(batch)
        rows = con.execute(
            """SELECT * FROM analysis_batch_items
            WHERE owner=? AND batch_id=? ORDER BY ordinal LIMIT ? OFFSET ?""",
            (owner, batch_id, limit + 1, offset),
        ).fetchall()
        total = con.execute(
            "SELECT COUNT(*) FROM analysis_batch_items WHERE owner=? AND batch_id=?",
            (owner, batch_id),
        ).fetchone()[0]
        result = {
            "batch_id": batch_id,
            "revision": batch["revision"],
            "offset": offset,
            "limit": limit,
            "count": 0,
            "total": total,
            "next_offset": None,
            "items": [],
        }
        for row in rows[:limit]:
            count = len(result["items"]) + 1
            candidate = result | {
                "count": count,
                "next_offset": offset + count if offset + count < total else None,
                "items": result["items"] + [_bound_item_public(row, manifest)],
            }
            if len(canonical(candidate).encode("utf-8")) > 256 * 1024:
                if not result["items"]:
                    raise ValueError("One batch item exceeds the page byte limit")
                break
            result = candidate
        _validate_json(result, "Batch item page", 256 * 1024, object_only=True)
        return result


def read_attempts(
    owner: str, batch_id: str, ordinal: int, offset: int = 0, limit: int = 20
) -> dict[str, Any]:
    owner = _owner(owner)
    batch_id = _identifier(batch_id, "batch_id")
    ordinal = _ordinal(ordinal)
    offset = _page_value(offset, "offset", default=0, maximum=MAX_PAGE)
    limit = _page_value(limit, "limit", default=20, maximum=MAX_ATTEMPT_PAGE)
    with registry_connection(write=False) as con:
        con.row_factory = sqlite3.Row
        batch = _batch_row(con, owner, batch_id)
        _batch_method, manifest = _stored_batch_spec(batch)
        item = _item_row(con, owner, batch_id, ordinal)
        _validate_item_binding(item, manifest)
        rows = con.execute(
            """SELECT * FROM analysis_batch_attempts
            WHERE owner=? AND batch_id=? AND ordinal=?
            ORDER BY attempt_no LIMIT ? OFFSET ?""",
            (owner, batch_id, ordinal, limit + 1, offset),
        ).fetchall()
        page = rows[:limit]
        result = {
            "batch_id": batch_id,
            "ordinal": ordinal,
            "offset": offset,
            "limit": limit,
            "count": len(page),
            "total": con.execute(
                """SELECT COUNT(*) FROM analysis_batch_attempts
                WHERE owner=? AND batch_id=? AND ordinal=?""",
                (owner, batch_id, ordinal),
            ).fetchone()[0],
            "next_offset": offset + limit if len(rows) > limit else None,
            "attempts": [_attempt_public(row) for row in page],
        }
        _validate_json(result, "Batch attempt page", 256 * 1024, object_only=True)
        return result


def _reserve_request(batch_id: str, expected_revision: int, max_items: int) -> dict[str, Any]:
    return {
        "operation": "reserve_batch_run",
        "batch_id": batch_id,
        "expected_revision": expected_revision,
        "max_items": max_items,
    }


def _execution_context(value: Mapping[str, Any] | None) -> str:
    if value is None:
        value = {"runtime_status": "unknown"}
    if not isinstance(value, Mapping):
        raise ValueError("execution_context must be a JSON object")
    return _validate_json(
        dict(value), "execution_context", MAX_EXECUTION_CONTEXT_BYTES, object_only=True
    )


def _reserve_action(
    con: sqlite3.Connection,
    owner: str,
    batch_id: str,
    request_key: str,
    request_digest: str,
    expected_revision: int,
    max_items: int,
    capacity: int,
    execution_context: str,
) -> dict[str, Any]:
    batch = _batch_row(con, owner, batch_id)
    _validated_batch(con, owner, batch)
    if batch["revision"] != expected_revision:
        raise ValueError("Batch revision conflict")
    if batch["cancel_requested"]:
        raise ValueError("Batch cancellation has been requested")
    rows = con.execute(
        """SELECT * FROM analysis_batch_items
        WHERE owner=? AND batch_id=? AND state='pending'
        ORDER BY ordinal LIMIT ?""",
        (owner, batch_id, min(max_items, capacity)),
    ).fetchall()
    if not rows:
        raise ValueError("Batch has no pending items")
    run_id = str(uuid.uuid4())
    timestamp = now()
    con.execute(
        """INSERT INTO analysis_batch_runs(
          id, owner, batch_id, request_key, request_digest, expected_revision,
          max_items, selected_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            owner,
            batch_id,
            request_key,
            request_digest,
            expected_revision,
            max_items,
            len(rows),
            timestamp,
        ),
    )
    for item in rows:
        attempt_count = con.execute(
            """SELECT COUNT(*) FROM analysis_batch_attempts
            WHERE owner=? AND batch_id=? AND ordinal=?""",
            (owner, batch_id, item["ordinal"]),
        ).fetchone()[0]
        if attempt_count >= MAX_ATTEMPTS:
            raise ValueError("Batch item has reached the maximum attempt count")
        attempt_id = str(uuid.uuid4())
        token = str(uuid.uuid4())
        con.execute(
            """INSERT INTO analysis_batch_attempts(
              id, owner, batch_id, run_id, ordinal, attempt_no,
              execution_token, state, lease_expires_at, execution_context,
              runtime, error, analysis_id, result_digest, evidence_digest,
              compact_result, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, NULL)""",
            (
                attempt_id,
                owner,
                batch_id,
                run_id,
                item["ordinal"],
                attempt_count + 1,
                token,
                _lease_expiry(),
                execution_context,
                canonical({"runtime_status": "unknown"}),
                timestamp,
            ),
        )
        con.execute(
            """INSERT INTO analysis_batch_run_items(
              run_id, owner, batch_id, ordinal, attempt_id, execution_token, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, owner, batch_id, item["ordinal"], attempt_id, token, timestamp),
        )
        changed = con.execute(
            """UPDATE analysis_batch_items
            SET state='running', attempt_count=?, updated_at=?
            WHERE owner=? AND batch_id=? AND ordinal=? AND state='pending'""",
            (attempt_count + 1, timestamp, owner, batch_id, item["ordinal"]),
        ).rowcount
        if changed != 1:
            raise ValueError("Batch item state changed while reserving its claim")
    con.execute(
        "UPDATE analysis_batches SET revision=revision+1, updated_at=? WHERE id=? AND owner=?",
        (timestamp, batch_id, owner),
    )
    return {"run_id": run_id, "batch_id": batch_id}


def reserve_run(
    owner: str,
    batch_id: str,
    key: str,
    expected_revision: int,
    max_items: int = 2,
    *,
    capacity: int = 2,
    execution_context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Durably claim up to two pending items, returning ``(run, created)``.

    ``capacity`` and ``execution_context`` are trusted runtime inputs.  They
    are intentionally excluded from the caller request digest so a replay is
    recognized before a runner acquires process capacity.
    """
    owner = _owner(owner)
    batch_id = _identifier(batch_id, "batch_id")
    key = _key(key)
    expected_revision = _positive_integer(expected_revision, "expected_revision")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 2:
        raise ValueError("max_items must be an integer from 1 through 2")
    if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 2:
        raise ValueError("capacity must be an integer from 1 through 2")
    context = _execution_context(execution_context)
    request = _reserve_request(batch_id, expected_revision, max_items)
    request_digest = sha256(request)
    created = False

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        nonlocal created
        result = _reserve_action(
            con,
            owner,
            batch_id,
            key,
            request_digest,
            expected_revision,
            max_items,
            capacity,
            context,
        )
        created = True
        return result

    response = registry_mutation(owner, key, request, write)
    run_id = response.get("run_id") if isinstance(response, dict) else None
    if not isinstance(run_id, str) or response.get("batch_id") != batch_id:
        raise ValueError("Stored batch-run receipt is malformed")
    with registry_connection(write=False) as con:
        con.row_factory = sqlite3.Row
        return _run_public(con, owner, run_id, include_tokens=True), created


def lookup_run_request(
    owner: str,
    batch_id: str,
    key: str,
    expected_revision: int,
    max_items: int = 2,
) -> dict[str, Any] | None:
    """Read a retained reserve receipt before a runner acquires capacity."""
    owner = _owner(owner)
    batch_id = _identifier(batch_id, "batch_id")
    key = _key(key)
    expected_revision = _positive_integer(expected_revision, "expected_revision")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 2:
        raise ValueError("max_items must be an integer from 1 through 2")
    request = _reserve_request(batch_id, expected_revision, max_items)
    digest = sha256(request)
    with registry_connection(write=False) as con:
        con.row_factory = sqlite3.Row
        receipt = con.execute(
            "SELECT digest, response FROM registry_receipts WHERE owner=? AND request_key=?",
            (owner, key),
        ).fetchone()
        if receipt is None:
            return None
        if receipt["digest"] != digest:
            raise ValueError("Idempotency key was already used for a different registry request")
        response = _decode_json(receipt["response"], "batch-run receipt", 64_000, object_only=True)
        run_id = response.get("run_id")
        if response.get("batch_id") != batch_id or not isinstance(run_id, str):
            raise ValueError("Stored batch-run receipt does not match its request")
        return _run_public(con, owner, run_id, include_tokens=False)


def get_run(owner: str, run_id: str) -> dict[str, Any]:
    owner = _owner(owner)
    run_id = _identifier(run_id, "run_id")
    with registry_connection(write=False) as con:
        con.row_factory = sqlite3.Row
        return _run_public(con, owner, run_id, include_tokens=False)


def claim_input(
    owner: str,
    batch_id: str,
    run_id: str,
    ordinal: int,
    token: str,
) -> dict[str, Any]:
    """Return the frozen raw input for one live internal runner claim.

    This is intentionally separate from public reads: it requires the
    execution token, checks the lease and revalidates live dataset/source
    identity in the same read transaction used to assemble the work item.
    """
    owner = _owner(owner)
    batch_id = _identifier(batch_id, "batch_id")
    run_id = _identifier(run_id, "run_id")
    ordinal = _ordinal(ordinal)
    token = _token(token)
    with registry_connection(write=False) as con:
        con.row_factory = sqlite3.Row
        claim, item, batch = _claim_row(con, owner, batch_id, run_id, ordinal, token)
        if claim["state"] != "running" or item["state"] != "running":
            raise ClaimFenceError("Batch claim is stale or no longer running")
        if _lease_is_expired(claim["lease_expires_at"]):
            raise ClaimFenceError("Batch claim lease has expired")
        method, _manifest = _stored_batch_spec(batch)
        dataset, digest, _source = _live_dataset_and_source(con, owner, item)
        trace = next(trace for trace in dataset["traces"] if trace["trace_id"] == item["trace_id"])
        return {
            "batch_id": batch_id,
            "run_id": run_id,
            "ordinal": ordinal,
            "attempt_id": claim["id"],
            "dataset_id": item["dataset_id"],
            "trace_id": item["trace_id"],
            "trace": trace,
            "method": method,
            "input_sha256": digest,
            "source_sha256": item["source_sha256"],
            "execution_context": _decode_json(
                claim["execution_context"],
                "execution context",
                MAX_EXECUTION_CONTEXT_BYTES,
                object_only=True,
            ),
        }


def _claim_row(
    con: sqlite3.Connection,
    owner: str,
    batch_id: str,
    run_id: str,
    ordinal: int,
    token: str,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    row = con.execute(
        """SELECT a.*, i.state AS item_state, i.id AS item_id,
          i.dataset_id, i.trace_id, i.input_sha256, i.source_sha256,
          i.payload AS item_payload, i.analysis_id AS item_analysis_id,
          b.method AS batch_method, b.manifest AS batch_manifest
        FROM analysis_batch_attempts a
        JOIN analysis_batch_items i
          ON i.owner=a.owner AND i.batch_id=a.batch_id AND i.ordinal=a.ordinal
        JOIN analysis_batches b ON b.owner=a.owner AND b.id=a.batch_id
        JOIN analysis_batch_run_items ri
          ON ri.owner=a.owner AND ri.batch_id=a.batch_id AND ri.ordinal=a.ordinal
         AND ri.run_id=a.run_id AND ri.attempt_id=a.id
        WHERE a.owner=? AND a.batch_id=? AND a.run_id=? AND a.ordinal=?""",
        (owner, batch_id, run_id, ordinal),
    ).fetchone()
    if row is None:
        raise ValueError("Batch claim was not found or is not accessible")
    if not hmac.compare_digest(row["execution_token"], token):
        raise ClaimFenceError("Batch claim token is stale or invalid")
    item = _item_row(con, owner, batch_id, ordinal)
    batch = _batch_row(con, owner, batch_id)
    _batch_method, manifest = _stored_batch_spec(batch)
    _validate_item_binding(item, manifest)
    return row, item, batch


def _live_dataset_and_source(
    con: sqlite3.Connection, owner: str, item: sqlite3.Row
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    dataset_row = con.execute(
        "SELECT payload, digest FROM datasets WHERE id=? AND owner=?",
        (item["dataset_id"], owner),
    ).fetchone()
    if dataset_row is None:
        raise ValueError("Frozen dataset is no longer available to this owner")
    dataset = _decode_json(dataset_row["payload"], "dataset", 4_000_000, object_only=True)
    if dataset_row["digest"] != item["input_sha256"] or sha256(dataset) != item["input_sha256"]:
        raise ValueError("Frozen dataset digest no longer matches the stored dataset")
    try:
        Dataset.model_validate(dataset)
    except Exception as exc:
        raise ValueError("Stored dataset is invalid") from exc
    traces = [
        trace for trace in dataset.get("traces", []) if trace.get("trace_id") == item["trace_id"]
    ]
    if len(traces) != 1:
        raise ValueError("Frozen trace is no longer selected exactly once")
    payload = _item_payload(item)
    metadata = {
        key: traces[0][key]
        for key in (
            "trace_id",
            "sample_id",
            "injection_id",
            "channel",
            "separation",
            "detector",
            "axis_unit",
            "signal_unit",
            "sample_type",
            "batch_id",
            "run_order",
        )
    }
    if payload.get("trace") != metadata:
        raise ValueError("Frozen trace metadata no longer matches the stored dataset")
    source_row = con.execute(
        """SELECT s.kind, s.digest, s.content, s.mapping FROM dataset_sources s
        JOIN datasets d ON d.id=s.dataset_id
        WHERE s.dataset_id=? AND d.owner=?""",
        (item["dataset_id"], owner),
    ).fetchone()
    expected_source = item["source_sha256"]
    if source_row is None:
        if expected_source is not None:
            raise ValueError("Frozen source is no longer available")
        return dataset, dataset_row["digest"], None
    mapping = _decode_json(
        source_row["mapping"], "source import mapping", MAX_MANIFEST_BYTES, object_only=True
    )
    actual_content_digest = _source_content_sha256(source_row["content"])
    frozen_source = payload.get("source")
    if (
        expected_source is None
        or source_row["digest"] != expected_source
        or actual_content_digest != expected_source
        or not isinstance(frozen_source, dict)
        or frozen_source.get("kind") != source_row["kind"]
        or frozen_source.get("source_sha256") != source_row["digest"]
        or frozen_source.get("mapping") != mapping
    ):
        raise ValueError("Frozen source digest or import mapping no longer matches")
    return (
        dataset,
        dataset_row["digest"],
        {
            "kind": source_row["kind"],
            "source_sha256": source_row["digest"],
            "content": source_row["content"],
            "mapping": mapping,
        },
    )


def _record_for_completion(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping) or set(record) != _RECORD_FIELDS:
        raise ValueError("Analysis record has an unexpected shape")
    try:
        owned = json.loads(canonical(dict(record)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Analysis record must contain strict finite JSON values") from exc
    if not isinstance(owned, dict):
        raise ValueError("Analysis record must be a JSON object")
    return owned


def _compact_record(record: dict[str, Any]) -> tuple[dict[str, Any], str]:
    compact = analysis_runtime.compact_analysis(record)
    serialized = _validate_json(
        compact, "compact analysis", MAX_COMPACT_RESULT_BYTES, object_only=True
    )
    if any(key in compact for key in ("peaks", "processed_trace", "axis", "signal")):
        raise ValueError("Compact analysis must not contain raw arrays")
    return compact, serialized


def complete_item(
    owner: str,
    batch_id: str,
    run_id: str,
    ordinal: int,
    token: str,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate evidence and atomically retain one successful analysis result."""
    owner = _owner(owner)
    batch_id = _identifier(batch_id, "batch_id")
    run_id = _identifier(run_id, "run_id")
    ordinal = _ordinal(ordinal)
    token = _token(token)
    record = _record_for_completion(record)
    with registry_connection(write=True) as con:
        con.row_factory = sqlite3.Row
        claim, item, batch = _claim_row(con, owner, batch_id, run_id, ordinal, token)
        if claim["state"] == "completed" and item["state"] == "completed":
            return _item_public(item)
        if claim["state"] != "running" or item["state"] != "running":
            raise ClaimFenceError("Batch claim is stale or no longer running")
        if _lease_is_expired(claim["lease_expires_at"]):
            raise ClaimFenceError("Batch claim lease has expired")
        method, _ = _stored_batch_spec(batch)
        if (
            record["dataset_id"] != item["dataset_id"]
            or record["trace_id"] != item["trace_id"]
            or record["kind"] != "chromatogram"
            or canonical(record["method"]) != canonical(method)
        ):
            raise ValueError("Analysis record does not match the frozen batch item")
        provenance = record.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError("Analysis record provenance must be an object")
        if (
            provenance.get("input_sha256") != item["input_sha256"]
            or provenance.get("source_sha256") != item["source_sha256"]
        ):
            raise ValueError("Analysis record evidence does not match the frozen input")
        runtime_serialized = _validate_json(
            provenance, "attempt runtime", MAX_RUNTIME_BYTES, object_only=True
        )
        dataset, digest, _source = _live_dataset_and_source(con, owner, item)
        evidence = analysis_evidence.build_evidence(record, dataset, digest)
        result_digest = sha256(record["result"])
        evidence_digest = evidence["evidence_sha256"]
        _compact, compact_serialized = _compact_record(record)
        current = con.execute(
            """SELECT state, execution_token, lease_expires_at
            FROM analysis_batch_attempts WHERE id=? AND owner=?""",
            (claim["id"], owner),
        ).fetchone()
        if (
            current is None
            or current["state"] != "running"
            or not hmac.compare_digest(current["execution_token"], token)
            or _lease_is_expired(current["lease_expires_at"])
        ):
            raise ClaimFenceError("Batch claim became stale or expired before completion")
        analysis_runtime.insert_analysis(con, owner, record)
        finished = now()
        con.execute(
            """UPDATE analysis_batch_attempts SET state='completed', runtime=?, error=NULL,
              analysis_id=?, result_digest=?, evidence_digest=?, compact_result=?, finished_at=?
            WHERE id=? AND owner=? AND state='running' AND execution_token=?""",
            (
                runtime_serialized,
                record["analysis_id"],
                result_digest,
                evidence_digest,
                compact_serialized,
                finished,
                claim["id"],
                owner,
                token,
            ),
        )
        changed = con.execute(
            """UPDATE analysis_batch_items SET state='completed', analysis_id=?,
              result_digest=?, evidence_digest=?, compact_result=?, updated_at=?
            WHERE owner=? AND batch_id=? AND ordinal=? AND state='running'""",
            (
                record["analysis_id"],
                result_digest,
                evidence_digest,
                compact_serialized,
                finished,
                owner,
                batch_id,
                ordinal,
            ),
        ).rowcount
        if changed != 1:
            raise ValueError("Batch item state changed while committing its result")
        con.execute(
            "UPDATE analysis_batches SET revision=revision+1, updated_at=? WHERE id=? AND owner=?",
            (finished, batch_id, owner),
        )
        _refresh_status(con, owner, batch_id, finished)
        return _item_public(_item_row(con, owner, batch_id, ordinal))


def _error_payload(error: Any) -> str:
    if isinstance(error, str):
        if not error.strip() or len(error) > 1_000:
            raise ValueError("error must be nonblank text of at most 1000 characters")
        value: Any = {"message": error}
    elif isinstance(error, Mapping):
        value = dict(error)
    else:
        raise ValueError("error must be nonblank text or a JSON object")
    return _validate_json(value, "attempt error", MAX_ERROR_BYTES, object_only=True)


def fail_item(
    owner: str,
    batch_id: str,
    run_id: str,
    ordinal: int,
    token: str,
    error: Any,
) -> dict[str, Any]:
    """Fence one failed computation and retain bounded error evidence."""
    owner = _owner(owner)
    batch_id = _identifier(batch_id, "batch_id")
    run_id = _identifier(run_id, "run_id")
    ordinal = _ordinal(ordinal)
    token = _token(token)
    error_serialized = _error_payload(error)
    with registry_connection(write=True) as con:
        con.row_factory = sqlite3.Row
        claim, item, _batch = _claim_row(con, owner, batch_id, run_id, ordinal, token)
        if claim["state"] == "failed" and item["state"] == "failed":
            return _item_public(item)
        if claim["state"] != "running" or item["state"] != "running":
            raise ClaimFenceError("Batch claim is stale or no longer running")
        if _lease_is_expired(claim["lease_expires_at"]):
            raise ClaimFenceError("Batch claim lease has expired")
        finished = now()
        con.execute(
            """UPDATE analysis_batch_attempts SET state='failed', runtime=?, error=?, finished_at=?
            WHERE id=? AND owner=? AND state='running' AND execution_token=?""",
            (
                canonical({"runtime_status": "unknown"}),
                error_serialized,
                finished,
                claim["id"],
                owner,
                token,
            ),
        )
        changed = con.execute(
            """UPDATE analysis_batch_items SET state='failed', updated_at=?
            WHERE owner=? AND batch_id=? AND ordinal=? AND state='running'""",
            (finished, owner, batch_id, ordinal),
        ).rowcount
        if changed != 1:
            raise ValueError("Batch item state changed while recording failure")
        con.execute(
            "UPDATE analysis_batches SET revision=revision+1, updated_at=? WHERE id=? AND owner=?",
            (finished, batch_id, owner),
        )
        _refresh_status(con, owner, batch_id, finished)
        return _item_public(_item_row(con, owner, batch_id, ordinal))


def _record_control_event(
    con: sqlite3.Connection,
    owner: str,
    batch_id: str,
    key: str,
    kind: str,
    before: int,
    after: int,
    affected: list[dict[str, Any]],
    timestamp: str,
) -> None:
    number = con.execute(
        "SELECT COALESCE(MAX(event_no), 0) + 1 FROM analysis_batch_control_events WHERE batch_id=?",
        (batch_id,),
    ).fetchone()[0]
    event = {
        "profile": "measure-chromatogram-batch-control/1",
        "event_id": str(uuid.uuid4()),
        "batch_id": batch_id,
        "event_no": number,
        "kind": kind,
        "revision_before": before,
        "revision_after": after,
        "idempotency_key_sha256": _token_digest(key),
        "items": affected,
        "created_at": timestamp,
    }
    encoded = _validate_json(
        event, "Batch control event", MAX_CONTROL_EVENT_BYTES, object_only=True
    )
    con.execute(
        "INSERT INTO analysis_batch_control_events VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event["event_id"], owner, batch_id, number, encoded, sha256(event), timestamp),
    )


def read_control_events(
    owner: str, batch_id: str, offset: int = 0, limit: int = 20
) -> dict[str, Any]:
    """Read immutable control outcomes, including cancellation after item retry."""
    owner = _owner(owner)
    batch_id = _identifier(batch_id, "batch_id")
    offset = _page_value(offset, "offset", default=0, maximum=MAX_PAGE)
    limit = _page_value(limit, "limit", default=20, maximum=20)
    with registry_connection() as con:
        _stored_batch_spec(_batch_row(con, owner, batch_id))
        rows = con.execute(
            """SELECT * FROM analysis_batch_control_events WHERE owner=? AND batch_id=?
            ORDER BY event_no LIMIT ? OFFSET ?""",
            (owner, batch_id, limit + 1, offset),
        ).fetchall()
        events = []
        for row in rows[:limit]:
            event = _decode_json(
                row["payload"], "Batch control event", MAX_CONTROL_EVENT_BYTES, object_only=True
            )
            if (
                event.get("profile") != "measure-chromatogram-batch-control/1"
                or event.get("event_id") != row["id"]
                or event.get("batch_id") != batch_id
                or event.get("event_no") != row["event_no"]
                or event.get("created_at") != row["created_at"]
                or sha256(event) != row["digest"]
            ):
                raise ValueError("Stored batch control event does not match its identity")
            events.append(event | {"event_sha256": row["digest"]})
        return {
            "batch_id": batch_id,
            "offset": offset,
            "count": len(events),
            "total": con.execute(
                "SELECT COUNT(*) FROM analysis_batch_control_events WHERE owner=? AND batch_id=?",
                (owner, batch_id),
            ).fetchone()[0],
            "next_offset": offset + limit if len(rows) > limit else None,
            "events": events,
        }


def _mutation_request(
    operation: str, batch_id: str, expected_revision: int, **fields: Any
) -> dict[str, Any]:
    request = {"operation": operation, "batch_id": batch_id, "expected_revision": expected_revision}
    request.update(fields)
    _validate_json(request, "batch mutation request", 64_000, object_only=True)
    return request


def _mutation_result(
    owner: str,
    key: str,
    request: dict[str, Any],
    action: Any,
    batch_id: str,
) -> dict[str, Any]:
    def validated_action(con: sqlite3.Connection) -> dict[str, Any]:
        _validated_batch(con, owner, _batch_row(con, owner, batch_id))
        return action(con)

    response = registry_mutation(owner, key, request, validated_action)
    if not isinstance(response, dict) or response.get("batch_id") != batch_id:
        raise ValueError("Stored batch mutation receipt is malformed")
    return get_batch(owner, batch_id)


def cancel_batch(owner: str, batch_id: str, key: str, expected_revision: int) -> dict[str, Any]:
    """Cancel pending items while allowing already-running work to finish."""
    owner = _owner(owner)
    batch_id = _identifier(batch_id, "batch_id")
    key = _key(key)
    expected_revision = _positive_integer(expected_revision, "expected_revision")
    request = _mutation_request("cancel_batch", batch_id, expected_revision)

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        row = _batch_row(con, owner, batch_id)
        if row["revision"] != expected_revision:
            raise ValueError("Batch revision conflict")
        if row["status"] == "completed":
            raise ValueError("Completed batches cannot be cancelled")
        timestamp = now()
        pending = con.execute(
            "SELECT ordinal FROM analysis_batch_items WHERE owner=? AND batch_id=? AND state='pending' ORDER BY ordinal",
            (owner, batch_id),
        ).fetchall()
        changed_rows = con.execute(
            """UPDATE analysis_batch_items SET state='cancelled', updated_at=?
            WHERE owner=? AND batch_id=? AND state='pending'""",
            (timestamp, owner, batch_id),
        ).rowcount
        con.execute(
            """UPDATE analysis_batches SET cancel_requested=1, revision=revision+1,
              updated_at=? WHERE owner=? AND id=?""",
            (timestamp, owner, batch_id),
        )
        _refresh_status(con, owner, batch_id, timestamp)
        _record_control_event(
            con,
            owner,
            batch_id,
            key,
            "cancel_requested",
            row["revision"],
            row["revision"] + 1,
            [
                {"ordinal": item["ordinal"], "from_state": "pending", "to_state": "cancelled"}
                for item in pending
            ],
            timestamp,
        )
        return {"batch_id": batch_id, "cancelled_count": changed_rows}

    return _mutation_result(owner, key, request, write, batch_id)


def recover_batch(owner: str, batch_id: str, key: str, expected_revision: int) -> dict[str, Any]:
    """Mark only expired running claims interrupted; recovery never retries."""
    owner = _owner(owner)
    batch_id = _identifier(batch_id, "batch_id")
    key = _key(key)
    expected_revision = _positive_integer(expected_revision, "expected_revision")
    request = _mutation_request("recover_batch", batch_id, expected_revision)

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        row = _batch_row(con, owner, batch_id)
        if row["revision"] != expected_revision:
            raise ValueError("Batch revision conflict")
        timestamp = now()
        recovered: list[int] = []
        running = con.execute(
            """SELECT * FROM analysis_batch_attempts
            WHERE owner=? AND batch_id=? AND state='running' ORDER BY ordinal""",
            (owner, batch_id),
        ).fetchall()
        for attempt in running:
            if not _lease_is_expired(attempt["lease_expires_at"]):
                continue
            error = {
                "kind": "lease_expired",
                "message": "The claim lease expired before completion.",
            }
            con.execute(
                """UPDATE analysis_batch_attempts SET state='interrupted', runtime=?, error=?, finished_at=?
                WHERE id=? AND owner=? AND state='running'""",
                (
                    canonical({"runtime_status": "unknown"}),
                    canonical(error),
                    timestamp,
                    attempt["id"],
                    owner,
                ),
            )
            con.execute(
                """UPDATE analysis_batch_items SET state='interrupted', updated_at=?
                WHERE owner=? AND batch_id=? AND ordinal=? AND state='running'""",
                (timestamp, owner, batch_id, attempt["ordinal"]),
            )
            recovered.append(attempt["ordinal"])
        if recovered:
            con.execute(
                "UPDATE analysis_batches SET revision=revision+1, updated_at=? WHERE owner=? AND id=?",
                (timestamp, owner, batch_id),
            )
        _refresh_status(con, owner, batch_id, timestamp)
        _record_control_event(
            con,
            owner,
            batch_id,
            key,
            "recover_expired",
            row["revision"],
            row["revision"] + bool(recovered),
            [
                {"ordinal": ordinal, "from_state": "running", "to_state": "interrupted"}
                for ordinal in recovered
            ],
            timestamp,
        )
        return {"batch_id": batch_id, "recovered_ordinals": recovered}

    return _mutation_result(owner, key, request, write, batch_id)


def retry_items(
    owner: str,
    batch_id: str,
    key: str,
    expected_revision: int,
    ordinals: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """Reset selected failed/interrupted/cancelled items to pending."""
    owner = _owner(owner)
    batch_id = _identifier(batch_id, "batch_id")
    key = _key(key)
    expected_revision = _positive_integer(expected_revision, "expected_revision")
    if not isinstance(ordinals, (list, tuple)) or not ordinals or len(ordinals) > MAX_BATCH_ITEMS:
        raise ValueError("ordinals must be a nonempty list of at most 32 item ordinals")
    normalized = [_ordinal(value) for value in ordinals]
    if len(set(normalized)) != len(normalized):
        raise ValueError("ordinals must be unique")
    normalized.sort()
    request = _mutation_request(
        "retry_batch_items", batch_id, expected_revision, ordinals=normalized
    )

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        row = _batch_row(con, owner, batch_id)
        if row["revision"] != expected_revision:
            raise ValueError("Batch revision conflict")
        running = con.execute(
            """SELECT 1 FROM analysis_batch_items
            WHERE owner=? AND batch_id=? AND state='running' LIMIT 1""",
            (owner, batch_id),
        ).fetchone()
        if running is not None:
            raise ValueError("Batch has running items; recover or finish them before retrying")
        rows = con.execute(
            "SELECT * FROM analysis_batch_items WHERE owner=? AND batch_id=? ORDER BY ordinal",
            (owner, batch_id),
        ).fetchall()
        by_ordinal = {item["ordinal"]: item for item in rows}
        missing = [ordinal for ordinal in normalized if ordinal not in by_ordinal]
        if missing:
            raise ValueError("Retry ordinals are not batch items")
        selected = [by_ordinal[ordinal] for ordinal in normalized]
        if not selected:
            raise ValueError("Batch has no retryable selected items")
        for item in selected:
            if item["state"] not in {"failed", "interrupted", "cancelled"}:
                raise ValueError("Only failed, interrupted or cancelled items can be retried")
            if item["attempt_count"] >= MAX_ATTEMPTS:
                raise ValueError("Batch item has reached the maximum attempt count")
        timestamp = now()
        for item in selected:
            con.execute(
                """UPDATE analysis_batch_items SET state='pending', analysis_id=NULL,
                  result_digest=NULL, evidence_digest=NULL, compact_result=NULL, updated_at=?
                WHERE owner=? AND batch_id=? AND ordinal=?""",
                (timestamp, owner, batch_id, item["ordinal"]),
            )
        con.execute(
            """UPDATE analysis_batches SET cancel_requested=0, revision=revision+1,
              status='active', updated_at=? WHERE owner=? AND id=?""",
            (timestamp, owner, batch_id),
        )
        _refresh_status(con, owner, batch_id, timestamp)
        _record_control_event(
            con,
            owner,
            batch_id,
            key,
            "retry_selected",
            row["revision"],
            row["revision"] + 1,
            [
                {"ordinal": item["ordinal"], "from_state": item["state"], "to_state": "pending"}
                for item in selected
            ],
            timestamp,
        )
        return {
            "batch_id": batch_id,
            "retried_ordinals": [item["ordinal"] for item in selected],
        }

    return _mutation_result(owner, key, request, write, batch_id)
