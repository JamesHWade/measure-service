"""Capture a coherent, bounded portable batch without changing scientific state."""

from __future__ import annotations

import json
from typing import Any

from . import analysis_evidence, analysis_runtime, batches
from .storage import canonical, now, registry_connection, sha256


def capture_document(
    owner: str, batch_id: str, expected_revision: int, mode: str
) -> dict[str, Any]:
    """Read one owner/revision-consistent snapshot; caller serializes and validates it."""
    from . import batch_snapshot as codec
    from .import_origins import get_import_origin

    owner = batches._owner(owner)
    batch_id = batches._identifier(batch_id, "batch_id")
    expected_revision = batches._positive_integer(expected_revision, "expected_revision")
    if type(mode) is not str or mode not in {"references", "self_contained"}:
        raise ValueError("Choose references or self_contained snapshot mode explicitly")
    budget = codec.MAX_SNAPSHOT_BYTES - 16_384
    size = 0

    def account(value: Any) -> Any:
        nonlocal size
        size += len(canonical(value).encode("utf-8"))
        if size > budget:
            raise ValueError("Snapshot exceeds its bounded construction budget")
        return value

    with registry_connection() as con:
        for table, maximum in (
            ("analysis_batch_items", 32),
            ("analysis_batch_attempts", 256),
            ("analysis_batch_runs", 256),
            ("analysis_batch_control_events", 1024),
        ):
            count = con.execute(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} WHERE owner=? AND batch_id=? LIMIT ?)",
                (owner, batch_id, maximum + 1),
            ).fetchone()[0]
            if count > maximum:
                raise ValueError("Batch history exceeds the portable snapshot limits")
        invalid_item = con.execute(
            """SELECT 1 FROM analysis_batch_items WHERE owner=? AND batch_id=?
            AND (length(CAST(payload AS BLOB))>? OR length(CAST(compact_result AS BLOB))>?) LIMIT 1""",
            (owner, batch_id, batches.MAX_MANIFEST_BYTES, batches.MAX_COMPACT_RESULT_BYTES),
        ).fetchone()
        if invalid_item is not None:
            raise ValueError("Stored item exceeds portable metadata limits")
        sizes = con.execute(
            """SELECT length(CAST(manifest AS BLOB)), length(CAST(method AS BLOB))
            FROM analysis_batches WHERE id=? AND owner=?""",
            (batch_id, owner),
        ).fetchone()
        if sizes is not None and any(value > batches.MAX_MANIFEST_BYTES for value in sizes):
            raise ValueError("Stored batch metadata exceeds the snapshot limits")
        row = batches._batch_row(con, owner, batch_id)
        summary = batches._batch_public(con, owner, row)
        if summary["revision"] != expected_revision:
            raise ValueError("Batch revision conflict during snapshot capture")
        if summary["counts"]["running"]:
            raise ValueError("Finish or explicitly recover running claims before export")
        document = {
            "profile": "measure-chromatogram-batch-snapshot/1",
            "schema_version": "1.0",
            "mode": mode,
            "captured_at": now(),
            "batch": account(summary),
            "items": [],
            "attempts": [],
            "runs": [],
            "control_events": [],
            "datasets": [],
            "analyses": [],
            "availability": {
                "component_correspondences": "not_linked",
                "calibration_revisions": "not_linked",
                "physical_validation": "not_established",
                "operational_claims": "not_portable",
            },
        }
        items = con.execute(
            "SELECT * FROM analysis_batch_items WHERE owner=? AND batch_id=? ORDER BY ordinal LIMIT 33",
            (owner, batch_id),
        ).fetchall()
        if len(items) != summary["item_count"] or len(items) > 32:
            raise ValueError("Stored batch item count is inconsistent")
        datasets: dict[str, tuple[dict, str, dict | None]] = {}
        raw_points = 0
        for item in items:
            document["items"].append(account(batches._item_public(item)))
            sizes = con.execute(
                """SELECT length(CAST(d.payload AS BLOB)),
                COALESCE(length(CAST(s.content AS BLOB)),0),
                COALESCE(length(CAST(s.mapping AS BLOB)),0)
                FROM datasets d LEFT JOIN dataset_sources s ON d.id=s.dataset_id
                WHERE d.id=? AND d.owner=?""",
                (item["dataset_id"], owner),
            ).fetchone()
            if (
                sizes is None
                or sizes[0] > 4_000_000
                or sizes[1] > 4_000_000
                or sizes[2] > batches.MAX_MANIFEST_BYTES
            ):
                raise ValueError("Stored input exceeds the portable dataset/source limits")
            data, digest, source = batches._live_dataset_and_source(con, owner, item)
            identifier = item["dataset_id"]
            if identifier not in datasets:
                datasets[identifier] = (data, digest, source)
                points = sum(len(trace["axis"]) for trace in data["traces"])
                raw_points += points
                if raw_points > 500_000:
                    raise ValueError("Complete dataset closure exceeds 500000 raw points")
                source_copy = None if source is None else dict(source)
                if mode == "references" and source_copy is not None:
                    source_copy["content"] = None
                created = con.execute(
                    "SELECT created_at FROM datasets WHERE id=? AND owner=?", (identifier, owner)
                ).fetchone()[0]
                document["datasets"].append(
                    account(
                        {
                            "dataset_id": identifier,
                            "input_sha256": digest,
                            "created_at": created,
                            "title": data["title"],
                            "trace_count": len(data["traces"]),
                            "point_count": points,
                            "payload": data if mode == "self_contained" else None,
                            "source": source_copy,
                            "import_origin": get_import_origin(con, owner, "dataset", identifier),
                        }
                    )
                )
            if item["state"] != "completed":
                continue
            sizes = con.execute(
                """SELECT length(CAST(result AS BLOB)),length(CAST(provenance AS BLOB)),
                length(CAST(method AS BLOB)) FROM analyses WHERE id=? AND owner=?""",
                (item["analysis_id"], owner),
            ).fetchone()
            if (
                sizes is None
                or sizes[0] > 16 * 1024 * 1024
                or sizes[1] > batches.MAX_RUNTIME_BYTES
                or sizes[2] > batches.MAX_MANIFEST_BYTES
            ):
                raise ValueError("Stored analysis exceeds the portable result limits")
            stored = con.execute(
                "SELECT * FROM analyses WHERE id=? AND owner=?", (item["analysis_id"], owner)
            ).fetchone()
            if stored is None:
                raise ValueError("Completed batch analysis is no longer available")
            record = {
                "analysis_id": stored["id"],
                "dataset_id": stored["dataset_id"],
                "trace_id": stored["trace_id"],
                "kind": stored["kind"],
                "method": json.loads(stored["method"]),
                "result": batches._decode_json(
                    stored["result"],
                    "Stored chromatogram result",
                    16 * 1024 * 1024,
                    object_only=True,
                ),
                "provenance": json.loads(stored["provenance"]),
                "created_at": stored["created_at"],
            }
            if (
                record["dataset_id"] != identifier
                or record["trace_id"] != item["trace_id"]
                or canonical(record["method"]) != canonical(summary["method"])
                or record["provenance"].get("source_sha256") != item["source_sha256"]
            ):
                raise ValueError("Stored analysis does not match the frozen item")
            evidence = analysis_evidence.build_evidence(record, data, digest)
            result_digest = sha256(record["result"])
            if (
                result_digest != item["result_digest"]
                or evidence["evidence_sha256"] != item["evidence_digest"]
                or canonical(analysis_runtime.compact_analysis(record)) != item["compact_result"]
            ):
                raise ValueError("Stored analysis identity does not match batch completion")
            document["analyses"].append(
                account(
                    record
                    | {
                        "result": record["result"] if mode == "self_contained" else None,
                        "result_sha256": result_digest,
                        "evidence_sha256": evidence["evidence_sha256"],
                        "import_origin": get_import_origin(con, owner, "analysis", stored["id"]),
                    }
                )
            )
        for table, key, maximum, order in (
            ("analysis_batch_attempts", "attempts", 256, "ordinal, attempt_no"),
            ("analysis_batch_runs", "runs", 256, "created_at, id"),
            ("analysis_batch_control_events", "control_events", 1024, "event_no"),
        ):
            fields = {
                "attempts": [
                    ("execution_context", batches.MAX_EXECUTION_CONTEXT_BYTES),
                    ("runtime", batches.MAX_RUNTIME_BYTES),
                    ("error", batches.MAX_ERROR_BYTES),
                    ("compact_result", batches.MAX_COMPACT_RESULT_BYTES),
                ],
                "runs": [("request_key", 400)],
                "control_events": [("payload", batches.MAX_CONTROL_EVENT_BYTES)],
            }[key]
            for field, cap in fields:
                excessive = con.execute(
                    f"SELECT 1 FROM {table} WHERE owner=? AND batch_id=? AND length(CAST({field} AS BLOB))>? LIMIT 1",
                    (owner, batch_id, cap),
                ).fetchone()
                if excessive is not None:
                    raise ValueError(f"Stored {key} exceeds its portable record limit")
            projection = {
                "attempts": "id,batch_id,run_id,ordinal,attempt_no,state,lease_expires_at,execution_context,runtime,error,analysis_id,result_digest,evidence_digest,started_at,finished_at",
                "runs": "id",
                "control_events": "id,event_no,payload,digest,created_at",
            }[key]
            records = con.execute(
                f"SELECT {projection} FROM {table} WHERE owner=? AND batch_id=? ORDER BY {order} LIMIT ?",
                (owner, batch_id, maximum + 1),
            ).fetchall()
            if len(records) > maximum:
                raise ValueError(f"Snapshot exceeds the {key} limit")
            for stored in records:
                if key == "attempts":
                    value = batches._attempt_public(stored)
                elif key == "runs":
                    value = batches._run_public(con, owner, stored["id"])
                else:
                    value = batches._decode_json(
                        stored["payload"],
                        "Control event",
                        batches.MAX_CONTROL_EVENT_BYTES,
                        object_only=True,
                    )
                    if (
                        value.get("event_id") != stored["id"]
                        or value.get("batch_id") != batch_id
                        or value.get("event_no") != stored["event_no"]
                        or value.get("created_at") != stored["created_at"]
                        or sha256(value) != stored["digest"]
                    ):
                        raise ValueError("Stored control event identity is inconsistent")
                    value = value | {"event_sha256": stored["digest"]}
                document[key].append(account(value))
        return document


def capture_snapshot(owner: str, batch_id: str, expected_revision: int, mode: str) -> bytes:
    from .batch_snapshot import encode_snapshot

    return encode_snapshot(capture_document(owner, batch_id, expected_revision, mode))
