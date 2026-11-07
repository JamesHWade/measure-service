"""Strict, portable documents for completed chromatogram batch snapshots.

Snapshot validation is intentionally independent of SQLite, MCP and numerical
execution.  A snapshot is a content-addressed record of one coherent batch
view; it is not an execution authority and never contains claim tokens or
retry keys.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any

from . import analysis_evidence, analysis_runtime, import_origins
from .analysis_models import ChromatogramMethod, Dataset, TraceMetadata
from .batch_models import (
    MAX_BATCH_ITEMS,
    MAX_BATCH_POINTS,
    MAX_INTERNAL_STANDARD_REFERENCE_BYTES,
    MAX_INTERNAL_STANDARD_REFERENCES,
    MAX_MANIFEST_BYTES,
)
from .batch_models import (
    PROFILE as BATCH_PROFILE,
)
from .batch_models import (
    SCHEMA_VERSION as BATCH_SCHEMA_VERSION,
)
from .storage import canonical, sha256

SNAPSHOT_PROFILE = "measure-chromatogram-batch-snapshot/1"
SNAPSHOT_SCHEMA_VERSION = "1.0"
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_SNAPSHOT_BYTES + 256 * 1024
MAX_JSON_DEPTH = 64
MAX_SNAPSHOT_ITEMS = MAX_BATCH_ITEMS
MAX_SNAPSHOT_DATASETS = MAX_BATCH_ITEMS
MAX_SNAPSHOT_ANALYSES = MAX_BATCH_ITEMS
MAX_SNAPSHOT_ATTEMPTS = 256
MAX_SNAPSHOT_RUNS = 256
MAX_SNAPSHOT_CONTROLS = 1_024
MAX_STRING_BYTES = 4_000_000
MAX_DATASET_BYTES = 4_000_000
MAX_ANALYSIS_RESULT_BYTES = 16 * 1024 * 1024
MAX_COMPACT_RESULT_BYTES = 8_000
MAX_RUNTIME_BYTES = 4_000
MAX_ERROR_BYTES = 4_000
MAX_CONTROL_EVENT_BYTES = 8 * 1024

AVAILABILITY = {
    "component_correspondences": "not_linked",
    "calibration_revisions": "not_linked",
    "physical_validation": "not_established",
    "operational_claims": "not_portable",
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODES = {"references", "self_contained"}
_ITEM_STATES = {"pending", "running", "completed", "failed", "interrupted", "cancelled"}
_ATTEMPT_STATES = {"running", "completed", "failed", "interrupted", "cancelled"}
_CONTROL_KINDS = {"cancel_requested", "recover_expired", "retry_selected"}
_FORBIDDEN_KEYS = {"token", "execution_token", "request_key", "idempotency_key"}

_TOP_FIELDS = {
    "profile",
    "schema_version",
    "mode",
    "captured_at",
    "batch",
    "items",
    "attempts",
    "runs",
    "control_events",
    "datasets",
    "analyses",
    "availability",
}
_BATCH_FIELDS = {
    "batch_id",
    "schema_version",
    "profile",
    "title",
    "method",
    "manifest",
    "input_sha256",
    "manifest_sha256",
    "point_count",
    "item_count",
    "revision",
    "status",
    "cancel_requested",
    "counts",
    "control_event_count",
    "created_at",
    "updated_at",
}
_MANIFEST_FIELDS = {"schema_version", "profile", "title", "method", "inputs", "point_count"}
_MANIFEST_INPUT_FIELDS = {
    "ordinal",
    "dataset_id",
    "trace_id",
    "role",
    "internal_standard_references",
    "dataset_title",
    "dataset_sha256",
    "source",
    "source_sha256",
    "trace",
    "point_count",
}
_TRACE_FIELDS = {
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
}
_SOURCE_MANIFEST_FIELDS = {"kind", "source_sha256", "mapping"}
_ITEM_FIELDS = {
    "item_id",
    "batch_id",
    "ordinal",
    "dataset_id",
    "trace_id",
    "role",
    "internal_standard_references",
    "dataset_title",
    "trace",
    "point_count",
    "input_sha256",
    "source_sha256",
    "state",
    "attempt_count",
    "analysis_id",
    "result_sha256",
    "evidence_sha256",
    "result",
}
_ATTEMPT_FIELDS = {
    "attempt_id",
    "batch_id",
    "run_id",
    "ordinal",
    "attempt_no",
    "state",
    "lease_expires_at",
    "execution_context",
    "runtime",
    "error",
    "analysis_id",
    "result_sha256",
    "evidence_sha256",
    "result",
    "started_at",
    "finished_at",
}
_RUN_FIELDS = {
    "run_id",
    "batch_id",
    "expected_revision",
    "max_items",
    "selected_count",
    "request_sha256",
    "request_key_sha256",
    "status",
    "created_at",
    "items",
}
_RUN_ITEM_FIELDS = {
    "ordinal",
    "item_id",
    "attempt_id",
    "state",
    "item_state",
    "attempt_state",
    "attempt_no",
    "lease_expires_at",
    "analysis_id",
    "result_sha256",
    "evidence_sha256",
    "result",
    "error",
    "execution_context",
    "runtime",
    "started_at",
    "finished_at",
}
_CONTROL_FIELDS = {
    "profile",
    "event_id",
    "batch_id",
    "event_no",
    "kind",
    "revision_before",
    "revision_after",
    "idempotency_key_sha256",
    "items",
    "created_at",
    "event_sha256",
}
_CONTROL_ITEM_FIELDS = {"ordinal", "from_state", "to_state"}
_DATASET_FIELDS = {
    "dataset_id",
    "input_sha256",
    "created_at",
    "title",
    "trace_count",
    "point_count",
    "payload",
    "source",
    "import_origin",
}
_SOURCE_FIELDS = {"kind", "source_sha256", "mapping", "content"}
_ANALYSIS_FIELDS = {
    "analysis_id",
    "dataset_id",
    "trace_id",
    "kind",
    "method",
    "result",
    "provenance",
    "created_at",
    "result_sha256",
    "evidence_sha256",
    "import_origin",
}
_ORIGIN_FIELDS = {
    "profile",
    "import_id",
    "snapshot_sha256",
    "source_batch_id",
    "source_record_id",
    "local_record_id",
    "record_kind",
    "imported_at",
    "original_record_sha256",
    "preserved_content_sha256",
}
_PROVENANCE_FIELDS = {
    "input_sha256",
    "method_sha256",
    "worker_sha256",
    "source_sha256",
    "packages",
    "installed_r_code_md5",
    "r_version",
    "measure_revision",
    "measure_sec_revision",
}
_COMPACT_FIELDS = {
    "analysis_id",
    "dataset_id",
    "trace_id",
    "kind",
    "status",
    "qualification",
    "summary",
    "diagnostics",
    "warnings",
    "notes",
    "provenance",
    "created_at",
    "artifact",
}


def _fail(message: str) -> None:
    raise ValueError(message)


def _strict_tree(value: Any, *, depth: int = 0) -> None:
    """Check a JSON tree iteratively so hostile depth cannot overflow Python."""
    stack: list[tuple[Any, int, bool]] = [(value, depth, False)]
    active: set[int] = set()
    while stack:
        current, current_depth, closing = stack.pop()
        if current_depth > MAX_JSON_DEPTH:
            _fail("snapshot JSON exceeds the maximum nesting depth")
        if closing:
            active.discard(id(current))
            continue
        if type(current) is dict:
            identity = id(current)
            if identity in active:
                _fail("snapshot JSON must not contain cyclic objects")
            active.add(identity)
            stack.append((current, current_depth, True))
            for key, nested in reversed(list(current.items())):
                if type(key) is not str:
                    _fail("snapshot object keys must be strings")
                if key in _FORBIDDEN_KEYS:
                    _fail("snapshot contains execution authority")
                stack.append((nested, current_depth + 1, False))
        elif type(current) is list:
            identity = id(current)
            if identity in active:
                _fail("snapshot JSON must not contain cyclic objects")
            active.add(identity)
            stack.append((current, current_depth, True))
            stack.extend((nested, current_depth + 1, False) for nested in reversed(current))
        elif type(current) is float:
            if not math.isfinite(current):
                _fail("snapshot JSON numbers must be finite")
        elif current is None or type(current) in {str, int, bool}:
            continue
        else:
            _fail("snapshot contains a value that is not strict JSON")


def _canonical_bytes(value: Any) -> bytes:
    _strict_tree(value)
    try:
        return canonical(value).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise ValueError("snapshot JSON is not canonical strict JSON") from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value} is not allowed")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON object keys are not allowed")
        result[key] = value
    return result


def _decode_json(data: bytes, label: str, limit: int) -> dict[str, Any]:
    if type(data) is not bytes:
        _fail(f"{label} must be UTF-8 JSON bytes")
    if len(data) > limit:
        _fail(f"{label} exceeds its byte limit")
    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("duplicate JSON"):
            raise
        raise ValueError(f"{label} must be strict UTF-8 JSON") from exc
    if type(value) is not dict:
        _fail(f"{label} must be a JSON object")
    _strict_tree(value)
    if _canonical_bytes(value) != data:
        _fail(f"{label} must use canonical JSON encoding")
    return value


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        _fail(f"{label} has unexpected fields")
    return value


def _text(value: Any, label: str, *, maximum: int = MAX_STRING_BYTES, nonblank: bool = True) -> str:
    if type(value) is not str:
        _fail(f"{label} must be text")
    if nonblank and not value.strip():
        _fail(f"{label} must be nonblank")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 text") from exc
    if length > maximum:
        _fail(f"{label} exceeds its byte limit")
    return value


def _identifier(value: Any, label: str) -> str:
    return _char_text(value, label, maximum=100)


def _char_text(
    value: Any,
    label: str,
    *,
    maximum: int,
    nonblank: bool = True,
    byte_maximum: int = MAX_STRING_BYTES,
) -> str:
    value = _text(value, label, maximum=byte_maximum, nonblank=nonblank)
    if len(value) > maximum:
        _fail(f"{label} exceeds its character limit")
    return value


def _integer(
    value: Any, label: str, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    if type(value) is not int:
        _fail(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        _fail(f"{label} is below its lower bound")
    if maximum is not None and value > maximum:
        _fail(f"{label} exceeds its upper bound")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        _fail(f"{label} must be a strict boolean")
    return value


def _sha(value: Any, label: str) -> str:
    value = _text(value, label, maximum=64)
    if _SHA256.fullmatch(value) is None:
        _fail(f"{label} must be a lowercase SHA-256 digest")
    return value


def _nullable_sha(value: Any, label: str) -> str | None:
    return None if value is None else _sha(value, label)


def _timestamp(value: Any, label: str) -> str:
    value = _text(value, label, maximum=100)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    return value


def _bounded_json(value: Any, label: str, limit: int, *, object_only: bool = False) -> None:
    if object_only and type(value) is not dict:
        _fail(f"{label} must be a JSON object")
    encoded = _canonical_bytes(value)
    if len(encoded) > limit:
        _fail(f"{label} exceeds its byte limit")


def _no_raw_arrays(value: Any, label: str) -> None:
    if type(value) is dict:
        if set(value) & {
            "axis",
            "signal",
            "peaks",
            "processed_trace",
            "raw_signal",
            "processed_signal",
        }:
            _fail(f"{label} contains raw analytical arrays")
        for nested in value.values():
            _no_raw_arrays(nested, label)
    elif type(value) is list:
        for nested in value:
            _no_raw_arrays(nested, label)


def _method(value: Any, label: str = "method") -> dict[str, Any]:
    if type(value) is not dict:
        _fail(f"{label} must be a JSON object")
    try:
        parsed = ChromatogramMethod.model_validate(value)
    except Exception as exc:
        raise ValueError(f"{label} is not a valid chromatogram method") from exc
    normalized = parsed.model_dump(mode="json")
    if _canonical_bytes(normalized) != _canonical_bytes(value):
        _fail(f"{label} is not normalized")
    return value


def _trace_metadata(value: Any, label: str = "trace metadata") -> dict[str, Any]:
    if type(value) is not dict:
        _fail(f"{label} must be a JSON object")
    try:
        parsed = TraceMetadata.model_validate(value)
    except Exception as exc:
        raise ValueError(f"{label} is invalid") from exc
    if _canonical_bytes(parsed.model_dump(mode="json")) != _canonical_bytes(value):
        _fail(f"{label} is not normalized")
    return value


def _dataset_payload(value: Any, label: str = "dataset payload") -> dict[str, Any]:
    if type(value) is not dict:
        _fail(f"{label} must be a JSON object")
    _bounded_json(value, label, MAX_DATASET_BYTES, object_only=True)
    try:
        parsed = Dataset.model_validate(value)
    except Exception as exc:
        raise ValueError(f"{label} is invalid") from exc
    if _canonical_bytes(parsed.model_dump(mode="json")) != _canonical_bytes(value):
        _fail(f"{label} is not normalized")
    return value


def _references(value: Any, label: str) -> list[str]:
    if type(value) is not list or len(value) > MAX_INTERNAL_STANDARD_REFERENCES:
        _fail(f"{label} must be a bounded list")
    total = 0
    result = []
    for entry in value:
        entry = _text(entry, f"{label} label", maximum=200)
        if entry in result:
            _fail(f"{label} must contain unique labels")
        total += len(entry.encode("utf-8"))
        result.append(entry)
    if total > MAX_INTERNAL_STANDARD_REFERENCE_BYTES:
        _fail(f"{label} exceeds its byte limit")
    return result


def _runtime_mapping(value: Any, label: str, *, digest_values: bool = False) -> None:
    if value is None:
        return
    if type(value) is not dict:
        _fail(f"{label} must be null or an object")
    for key, entry in value.items():
        _text(key, f"{label} key", maximum=200)
        if entry is None:
            continue
        if type(entry) is not str:
            _fail(f"{label} values must be text or null")
        if digest_values:
            if re.fullmatch(r"[0-9a-f]{32}", entry) is None:
                _fail(f"{label} values must be lowercase MD5 digests or null")
        else:
            _text(entry, f"{label} value", maximum=200)
    _bounded_json(value, label, MAX_RUNTIME_BYTES, object_only=True)


def _mapping(value: Any, trace_ids: set[str], *, require_exact: bool = True) -> dict[str, Any]:
    """Validate known importer mapping shape without reparsing source text."""
    if type(value) is not dict:
        _fail("source mapping must be a JSON object")
    delimiter = value.get("delimiter")
    if delimiter not in {",", "\t", ";"}:
        _fail("source mapping has an unsupported delimiter")
    axis = _char_text(
        value.get("axis_column"), "source axis_column", maximum=MAX_STRING_BYTES, nonblank=False
    )
    if "signal_columns" in value:
        if set(value) != {"delimiter", "axis_column", "signal_columns"}:
            _fail("wide source mapping has unexpected fields")
        channels = value["signal_columns"]
        if type(channels) is not list or not 1 <= len(channels) <= MAX_BATCH_ITEMS:
            _fail("wide source mapping has an invalid signal_columns list")
        seen_columns: set[str] = set()
        seen_traces: set[str] = set()
        for channel in channels:
            channel = _exact(channel, {"column", "trace_id"}, "source signal column")
            column = _char_text(
                channel["column"], "source signal column", maximum=200, nonblank=False
            )
            if column == "":
                _fail("source signal column must contain one character")
            trace_id = _identifier(channel["trace_id"], "source trace_id")
            if column == axis or column in seen_columns or trace_id in seen_traces:
                _fail("source mapping contains duplicate columns or traces")
            seen_columns.add(column)
            seen_traces.add(trace_id)
        covered = seen_traces == trace_ids if require_exact else trace_ids <= seen_traces
        if not covered:
            _fail("source mapping does not cover the dataset traces")
    else:
        if set(value) != {"delimiter", "axis_column", "signal_column", "group_columns", "groups"}:
            _fail("long source mapping has unexpected fields")
        signal = _char_text(
            value["signal_column"], "source signal_column", maximum=MAX_STRING_BYTES, nonblank=False
        )
        groups = value["group_columns"]
        if type(groups) is not list or not 1 <= len(groups) <= 4:
            _fail("long source mapping has an invalid group_columns list")
        group_names = [
            _char_text(entry, "source group column", maximum=MAX_STRING_BYTES, nonblank=False)
            for entry in groups
        ]
        if (
            len(set(group_names)) != len(group_names)
            or axis in group_names
            or signal in group_names
        ):
            _fail("long source mapping has duplicate columns")
        entries = value["groups"]
        if type(entries) is not list or not 1 <= len(entries) <= MAX_BATCH_ITEMS:
            _fail("long source mapping has an invalid groups list")
        seen_traces: set[str] = set()
        seen_values: set[tuple[str, ...]] = set()
        for entry in entries:
            entry = _exact(entry, {"values", "trace_id"}, "source group")
            values = entry["values"]
            if type(values) is not list or len(values) != len(group_names):
                _fail("source group values do not match group_columns")
            normalized_values = tuple(
                _char_text(item, "source group value", maximum=MAX_STRING_BYTES, nonblank=False)
                for item in values
            )
            trace_id = _identifier(entry["trace_id"], "source trace_id")
            if trace_id in seen_traces or normalized_values in seen_values:
                _fail("source mapping contains duplicate groups or traces")
            seen_traces.add(trace_id)
            seen_values.add(normalized_values)
        covered = seen_traces == trace_ids if require_exact else trace_ids <= seen_traces
        if not covered:
            _fail("source mapping does not cover the dataset traces")
    return value


def _source(value: Any, mode: str, trace_ids: set[str], expected: dict[str, Any] | None) -> None:
    if value is None:
        if expected is not None:
            _fail("dataset source is missing from the snapshot")
        return
    value = _exact(value, _SOURCE_FIELDS, "dataset source")
    if expected is None:
        _fail("structured dataset cannot have a fabricated source")
    kind = _text(value["kind"], "source kind", maximum=100)
    if kind != expected["kind"]:
        _fail("dataset source kind does not match the frozen batch source")
    source_sha256 = _sha(value["source_sha256"], "source_sha256")
    if source_sha256 != expected["source_sha256"]:
        _fail("dataset source digest does not match the frozen batch source")
    if value["mapping"] != expected["mapping"]:
        _fail("dataset source mapping does not match the frozen batch source")
    _mapping(value["mapping"], trace_ids, require_exact=mode == "self_contained")
    if mode == "self_contained":
        content = _text(
            value["content"], "source content", maximum=MAX_STRING_BYTES, nonblank=False
        )
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != value["source_sha256"]:
            _fail("source content digest does not match source_sha256")
    elif value["content"] is not None:
        _fail("reference snapshots must omit source content")


def _validate_source_replay(source: dict[str, Any], payload: dict[str, Any]) -> None:
    """Re-run the service importer against retained source bytes.

    The parser and CSV models are loaded lazily from the service module so
    this check uses the same importer semantics without opening a store or
    invoking R.  It is only possible for self-contained snapshots, where the
    complete normalized dataset is available for comparison.
    """
    try:
        from .server import CSVChannel, CSVGroup, parse_csv, parse_long_csv

        mapping = source["mapping"]
        traces = {trace["trace_id"]: trace for trace in payload["traces"]}
        if "signal_columns" in mapping:
            channels = [
                CSVChannel(
                    column=entry["column"],
                    metadata=TraceMetadata.model_validate(
                        {key: traces[entry["trace_id"]][key] for key in _TRACE_FIELDS}
                    ),
                )
                for entry in mapping["signal_columns"]
            ]
            parsed, parsed_source = parse_csv(
                source["content"], mapping["axis_column"], channels, mapping["delimiter"]
            )
        else:
            groups = [
                CSVGroup(
                    values=entry["values"],
                    metadata=TraceMetadata.model_validate(
                        {key: traces[entry["trace_id"]][key] for key in _TRACE_FIELDS}
                    ),
                )
                for entry in mapping["groups"]
            ]
            parsed, parsed_source = parse_long_csv(
                source["content"],
                mapping["axis_column"],
                mapping["signal_column"],
                mapping["group_columns"],
                groups,
                mapping["delimiter"],
            )
        parsed_payload = {
            "title": payload["title"],
            "traces": [trace.model_dump(mode="json") for trace in parsed],
        }
        if parsed_source["mapping"] != mapping or parsed_payload != payload:
            _fail("source content and mapping do not reproduce the frozen dataset")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("source content cannot reproduce the frozen dataset") from exc


def _origin(value: Any, kind: str, local_id: str, snapshot_sha: str | None = None) -> None:
    if value is None:
        return
    value = _exact(value, _ORIGIN_FIELDS, "import origin")
    if value["profile"] != "measure-analytical-import-origin/1":
        _fail("unsupported import-origin profile")
    for key in ("import_id", "source_batch_id", "source_record_id", "local_record_id"):
        _identifier(value[key], f"import origin {key}")
    if value["local_record_id"] != local_id or value["record_kind"] != kind:
        _fail("import origin record binding is inconsistent")
    if value["record_kind"] not in {"dataset", "analysis"}:
        _fail("import origin record_kind is unsupported")
    # This is the digest of the source archive from which the record was
    # imported.  It intentionally need not equal this newly captured
    # snapshot's digest; capture preserves import history verbatim.
    _sha(value["snapshot_sha256"], "import origin snapshot_sha256")
    _timestamp(value["imported_at"], "import origin imported_at")
    _sha(value["original_record_sha256"], "import origin original_record_sha256")
    _sha(value["preserved_content_sha256"], "import origin preserved_content_sha256")


def _compact(value: Any, label: str) -> None:
    if value is None:
        return
    value = _exact(value, _COMPACT_FIELDS, label)
    _identifier(value["analysis_id"], f"{label} analysis_id")
    _identifier(value["dataset_id"], f"{label} dataset_id")
    _identifier(value["trace_id"], f"{label} trace_id")
    if value["kind"] != "chromatogram":
        _fail(f"{label} kind is unsupported")
    for key in ("status", "qualification"):
        _text(value[key], f"{label} {key}", maximum=100)
    _bounded_json(value["summary"], f"{label} summary", MAX_COMPACT_RESULT_BYTES)
    if value["diagnostics"] is not None:
        _bounded_json(value["diagnostics"], f"{label} diagnostics", MAX_COMPACT_RESULT_BYTES)
    if type(value["warnings"]) is not list or any(
        type(entry) is not str for entry in value["warnings"]
    ):
        _fail(f"{label} warnings must be a list of text")
    notes = value["notes"]
    if type(notes) is str:
        note_values = [notes]
    elif type(notes) is list and all(type(entry) is str for entry in notes):
        note_values = notes
    else:
        _fail(f"{label} notes must be text or a list of text")
    for entry in value["warnings"] + note_values:
        _text(entry, f"{label} warning/note", maximum=MAX_COMPACT_RESULT_BYTES)
    _bounded_json(value["provenance"], f"{label} provenance", MAX_RUNTIME_BYTES, object_only=True)
    _timestamp(value["created_at"], f"{label} created_at")
    _boolean(value["artifact"], f"{label} artifact")
    _no_raw_arrays(value, label)
    _bounded_json(value, label, MAX_COMPACT_RESULT_BYTES, object_only=True)


def _validate_batch(batch: Any) -> tuple[str, dict[str, Any], dict[int, dict[str, Any]]]:
    batch = _exact(batch, _BATCH_FIELDS, "batch")
    batch_id = _identifier(batch["batch_id"], "batch_id")
    if batch["profile"] != BATCH_PROFILE or batch["schema_version"] != BATCH_SCHEMA_VERSION:
        _fail("batch profile or schema version is unsupported")
    _char_text(batch["title"], "batch title", maximum=200)
    method = _method(batch["method"], "batch method")
    _sha(batch["input_sha256"], "batch input_sha256")
    _sha(batch["manifest_sha256"], "batch manifest_sha256")
    _integer(batch["point_count"], "batch point_count", minimum=0, maximum=MAX_BATCH_POINTS)
    item_count = _integer(
        batch["item_count"], "batch item_count", minimum=1, maximum=MAX_BATCH_ITEMS
    )
    _integer(batch["revision"], "batch revision", minimum=1)
    _text(batch["status"], "batch status", maximum=20)
    if batch["status"] not in {"active", "cancelling", "cancelled", "completed"}:
        _fail("batch status is unsupported")
    _boolean(batch["cancel_requested"], "batch cancel_requested")
    counts = batch["counts"]
    if type(counts) is not dict or set(counts) != _ITEM_STATES:
        _fail("batch counts have unexpected fields")
    for state, count in counts.items():
        _integer(count, f"batch count {state}", minimum=0, maximum=MAX_BATCH_ITEMS)
    if sum(counts.values()) != item_count:
        _fail("batch counts do not sum to item_count")
    _integer(
        batch["control_event_count"],
        "batch control_event_count",
        minimum=0,
        maximum=MAX_SNAPSHOT_CONTROLS,
    )
    _timestamp(batch["created_at"], "batch created_at")
    _timestamp(batch["updated_at"], "batch updated_at")

    manifest = _exact(batch["manifest"], _MANIFEST_FIELDS, "batch manifest")
    _bounded_json(manifest, "batch manifest", MAX_MANIFEST_BYTES, object_only=True)
    if manifest["profile"] != BATCH_PROFILE or manifest["schema_version"] != BATCH_SCHEMA_VERSION:
        _fail("batch manifest profile or schema version is unsupported")
    if manifest["title"] != batch["title"] or _canonical_bytes(
        manifest["method"]
    ) != _canonical_bytes(method):
        _fail("batch manifest declaration does not match batch metadata")
    if sha256(manifest) != batch["manifest_sha256"]:
        _fail("batch manifest digest does not match its contents")
    inputs = manifest["inputs"]
    if (
        type(inputs) is not list
        or len(inputs) != item_count
        or not 1 <= len(inputs) <= MAX_BATCH_ITEMS
    ):
        _fail("batch manifest inputs are outside their bounds")
    manifest_inputs: dict[int, dict[str, Any]] = {}
    declaration_inputs = []
    total_points = 0
    for expected_ordinal, raw_input in enumerate(inputs):
        item = _exact(raw_input, _MANIFEST_INPUT_FIELDS, "batch manifest input")
        ordinal = _integer(
            item["ordinal"], "manifest ordinal", minimum=0, maximum=MAX_BATCH_ITEMS - 1
        )
        if ordinal != expected_ordinal or ordinal in manifest_inputs:
            _fail("batch manifest ordinals are not contiguous and unique")
        dataset_id = _identifier(item["dataset_id"], "manifest dataset_id")
        trace_id = _identifier(item["trace_id"], "manifest trace_id")
        role = _text(item["role"], "manifest role", maximum=20)
        if role not in {"unknown", "blank", "standard", "qc", "reference"}:
            _fail("manifest role is unsupported")
        refs = _references(
            item["internal_standard_references"], "manifest internal_standard_references"
        )
        if refs != item["internal_standard_references"]:
            _fail("manifest internal-standard labels are not normalized")
        _char_text(item["dataset_title"], "manifest dataset_title", maximum=200, nonblank=False)
        _sha(item["dataset_sha256"], "manifest dataset_sha256")
        _sha_or_none = _nullable_sha(item["source_sha256"], "manifest source_sha256")
        source = item["source"]
        if source is not None:
            source = _exact(source, _SOURCE_MANIFEST_FIELDS, "manifest source")
            _text(source["kind"], "manifest source kind", maximum=100)
            if source["kind"] != "delimited_text":
                _fail("manifest source kind is unsupported")
            if _sha(source["source_sha256"], "manifest source_sha256") != _sha_or_none:
                _fail("manifest source digest is inconsistent")
            if _sha_or_none is None:
                _fail("manifest source_sha256 is missing")
            _mapping(source["mapping"], {trace_id}, require_exact=False)
        elif _sha_or_none is not None:
            _fail("structured manifest input cannot have a source digest")
        trace = _trace_metadata(item["trace"], "manifest trace")
        if trace["trace_id"] != trace_id:
            _fail("manifest trace identity is inconsistent")
        point_count = _integer(
            item["point_count"], "manifest point_count", minimum=3, maximum=50_000
        )
        total_points += point_count
        manifest_inputs[ordinal] = item
        declaration_inputs.append(
            {
                "dataset_id": dataset_id,
                "trace_id": trace_id,
                "role": role,
                "internal_standard_references": refs,
            }
        )
    source_by_dataset: dict[str, Any] = {}
    grains: set[tuple[str, str, str]] = set()
    for item in manifest_inputs.values():
        prior = source_by_dataset.setdefault(item["dataset_id"], item["source"])
        if prior != item["source"]:
            _fail("manifest source declarations for one dataset disagree")
        trace = item["trace"]
        grain = (
            trace["sample_id"],
            trace["injection_id"],
            trace["channel"],
        )
        if grain in grains:
            _fail("batch manifest contains a duplicate sample/injection/channel grain")
        grains.add(grain)
    if total_points != batch["point_count"] or total_points > MAX_BATCH_POINTS:
        _fail("manifest point count does not match the batch budget")
    if manifest["point_count"] != total_points:
        _fail("manifest point_count does not match its inputs")
    declaration = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "profile": BATCH_PROFILE,
        "title": batch["title"],
        "method": method,
        "inputs": declaration_inputs,
    }
    if sha256(declaration) != batch["input_sha256"]:
        _fail("batch input digest does not match its normalized declaration")
    return batch_id, {"batch": batch, "method": method, "manifest": manifest}, manifest_inputs


def _validate_items(
    items: Any,
    batch_id: str,
    manifest_inputs: dict[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    if type(items) is not list or len(items) != len(manifest_inputs):
        _fail("snapshot items do not exactly cover the batch")
    by_ordinal: dict[int, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    for raw in items:
        item = _exact(raw, _ITEM_FIELDS, "snapshot item")
        item_id = _identifier(item["item_id"], "item_id")
        if item_id in seen_ids:
            _fail("snapshot item IDs must be unique")
        seen_ids.add(item_id)
        if item["batch_id"] != batch_id:
            _fail("snapshot item batch binding is inconsistent")
        ordinal = _integer(item["ordinal"], "item ordinal", minimum=0, maximum=MAX_BATCH_ITEMS - 1)
        if ordinal not in manifest_inputs or ordinal in by_ordinal:
            _fail("snapshot item ordinals are not an exact set")
        frozen = manifest_inputs[ordinal]
        for key in (
            "dataset_id",
            "trace_id",
            "role",
            "dataset_title",
            "point_count",
            "input_sha256",
            "source_sha256",
            "trace",
        ):
            if item[key] != frozen[key if key != "input_sha256" else "dataset_sha256"]:
                _fail(f"snapshot item {key} does not match its frozen manifest input")
        if item["internal_standard_references"] != frozen["internal_standard_references"]:
            _fail("snapshot item role declarations do not match the manifest")
        if item["state"] not in _ITEM_STATES:
            _fail("snapshot item state is unsupported")
        if item["state"] == "running":
            _fail("snapshots cannot contain running items")
        _integer(item["attempt_count"], "item attempt_count", minimum=0, maximum=8)
        _nullable_sha(item["result_sha256"], "item result_sha256")
        _nullable_sha(item["evidence_sha256"], "item evidence_sha256")
        _identifier(item["analysis_id"], "item analysis_id") if item[
            "analysis_id"
        ] is not None else None
        _compact(item["result"], "item compact result")
        if item["state"] == "completed":
            if any(
                item[key] is None
                for key in ("analysis_id", "result_sha256", "evidence_sha256", "result")
            ):
                _fail("completed item is missing its retained success")
        elif any(
            item[key] is not None
            for key in ("analysis_id", "result_sha256", "evidence_sha256", "result")
        ):
            _fail("non-completed item retains a success result")
        by_ordinal[ordinal] = item
    return by_ordinal


def _validate_attempts(
    attempts: Any,
    batch_id: str,
    items: dict[int, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    if type(attempts) is not list or len(attempts) > MAX_SNAPSHOT_ATTEMPTS:
        _fail("snapshot attempts exceed their bound")
    by_id: dict[str, dict[str, Any]] = {}
    by_ordinal: dict[int, list[dict[str, Any]]] = {ordinal: [] for ordinal in items}
    for raw in attempts:
        attempt = _exact(raw, _ATTEMPT_FIELDS, "snapshot attempt")
        attempt_id = _identifier(attempt["attempt_id"], "attempt_id")
        if attempt_id in by_id:
            _fail("snapshot attempt IDs must be unique")
        by_id[attempt_id] = attempt
        if attempt["batch_id"] != batch_id:
            _fail("snapshot attempt batch binding is inconsistent")
        ordinal = _integer(
            attempt["ordinal"], "attempt ordinal", minimum=0, maximum=MAX_BATCH_ITEMS - 1
        )
        if ordinal not in items:
            _fail("snapshot attempt ordinal is not a batch item")
        _integer(attempt["attempt_no"], "attempt_no", minimum=1, maximum=8)
        state = attempt["state"]
        if state not in _ATTEMPT_STATES or state == "running":
            _fail("snapshots cannot contain running or unknown attempts")
        _identifier(attempt["run_id"], "attempt run_id")
        _timestamp(attempt["lease_expires_at"], "attempt lease_expires_at")
        _bounded_json(
            attempt["execution_context"],
            "attempt execution_context",
            MAX_RUNTIME_BYTES,
            object_only=True,
        )
        _bounded_json(attempt["runtime"], "attempt runtime", MAX_RUNTIME_BYTES, object_only=True)
        if state in {"failed", "interrupted", "cancelled"} and attempt["runtime"] != {
            "runtime_status": "unknown"
        }:
            _fail("failed or interrupted attempts must retain unknown runtime evidence")
        if attempt["error"] is not None:
            _bounded_json(attempt["error"], "attempt error", MAX_ERROR_BYTES)
        _nullable_sha(attempt["result_sha256"], "attempt result_sha256")
        _nullable_sha(attempt["evidence_sha256"], "attempt evidence_sha256")
        if attempt["result"] is not None:
            _compact(attempt["result"], "attempt compact result")
        if attempt["result"] is not None:
            _fail("attempt history must omit duplicated compact results")
        if state == "completed":
            if any(
                attempt[key] is None
                for key in ("analysis_id", "result_sha256", "evidence_sha256", "finished_at")
            ):
                _fail("completed attempt is missing its retained success")
        elif any(
            attempt[key] is not None for key in ("analysis_id", "result_sha256", "evidence_sha256")
        ):
            _fail("non-completed attempt retains a success result")
        if attempt["finished_at"] is not None:
            _timestamp(attempt["finished_at"], "attempt finished_at")
        else:
            _fail("terminal attempts must retain a finished_at timestamp")
        if state == "completed" and attempt["error"] is not None:
            _fail("completed attempts must not retain an error")
        if state in {"failed", "interrupted"} and type(attempt["error"]) is not dict:
            _fail("failed or interrupted attempts must retain an error object")
        _timestamp(attempt["started_at"], "attempt started_at")
        by_ordinal[ordinal].append(attempt)
    for ordinal, history in by_ordinal.items():
        history.sort(key=lambda entry: entry["attempt_no"])
        numbers = [entry["attempt_no"] for entry in history]
        if numbers != list(range(1, len(numbers) + 1)):
            _fail("attempt history must have contiguous attempt numbers")
        if items[ordinal]["attempt_count"] != len(history):
            _fail("item attempt_count does not match retained attempt history")
        if history:
            latest = history[-1]["state"]
            current = items[ordinal]["state"]
            if current == "completed" and latest != "completed":
                _fail("completed item does not bind its latest successful attempt")
            if sum(entry["state"] == "completed" for entry in history) > 1:
                _fail("one item may retain only one successful attempt")
        elif items[ordinal]["state"] not in {"pending", "cancelled"}:
            _fail("item without attempts must remain pending")
    return by_id, by_ordinal


def _run_status(states: list[str]) -> str:
    if not states:
        return "empty"
    if all(state == "completed" for state in states):
        return "completed"
    if any(state == "running" for state in states):
        return "running"
    if any(state == "failed" for state in states):
        return "failed"
    if any(state == "interrupted" for state in states):
        return "interrupted"
    if any(state == "cancelled" for state in states):
        return "cancelled"
    return "active"


def _validate_runs(
    runs: Any,
    batch_id: str,
    batch_revision: int,
    items: dict[int, dict[str, Any]],
    attempts: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if type(runs) is not list or len(runs) > MAX_SNAPSHOT_RUNS:
        _fail("snapshot runs exceed their bound")
    by_id: dict[str, dict[str, Any]] = {}
    claimed_attempts: set[str] = set()
    for raw in runs:
        run = _exact(raw, _RUN_FIELDS, "snapshot run")
        run_id = _identifier(run["run_id"], "run_id")
        if run_id in by_id:
            _fail("snapshot run IDs must be unique")
        by_id[run_id] = run
        if run["batch_id"] != batch_id:
            _fail("snapshot run batch binding is inconsistent")
        expected_revision = _integer(run["expected_revision"], "run expected_revision", minimum=1)
        if expected_revision > batch_revision:
            _fail("run expected_revision is newer than the batch")
        max_items = _integer(run["max_items"], "run max_items", minimum=1, maximum=2)
        selected_count = _integer(run["selected_count"], "run selected_count", minimum=1, maximum=2)
        if (
            selected_count > max_items
            or type(run["items"]) is not list
            or len(run["items"]) != selected_count
        ):
            _fail("run selected_count does not match its selections")
        _sha(run["request_sha256"], "run request_sha256")
        _sha(run["request_key_sha256"], "run request_key_sha256")
        expected_request = {
            "operation": "reserve_batch_run",
            "batch_id": batch_id,
            "expected_revision": expected_revision,
            "max_items": max_items,
        }
        if run["request_sha256"] != sha256(expected_request):
            _fail("run request digest does not match its normalized request")
        _timestamp(run["created_at"], "run created_at")
        run_items = []
        ordinals: set[int] = set()
        states = []
        for raw_item in run["items"]:
            selection = _exact(raw_item, _RUN_ITEM_FIELDS, "run selection")
            ordinal = _integer(
                selection["ordinal"],
                "run selection ordinal",
                minimum=0,
                maximum=MAX_BATCH_ITEMS - 1,
            )
            if ordinal in ordinals or ordinal not in items:
                _fail("run selections are not a unique batch subset")
            ordinals.add(ordinal)
            attempt_id = _identifier(selection["attempt_id"], "run selection attempt_id")
            if attempt_id in claimed_attempts or attempt_id not in attempts:
                _fail("run selection attempt closure is inconsistent")
            attempt = attempts[attempt_id]
            if attempt["run_id"] != run_id or attempt["ordinal"] != ordinal:
                _fail("run selection is not bound to its attempt")
            claimed_attempts.add(attempt_id)
            for key in ("item_id", "item_state"):
                if selection[key] != (
                    items[ordinal]["item_id"] if key == "item_id" else items[ordinal]["state"]
                ):
                    _fail(f"run selection {key} is inconsistent")
            for key in ("state", "attempt_state"):
                if selection[key] != attempt["state"]:
                    _fail("run selection state is inconsistent with its attempt")
            for key in (
                "attempt_no",
                "lease_expires_at",
                "analysis_id",
                "result_sha256",
                "evidence_sha256",
                "error",
                "execution_context",
                "runtime",
                "started_at",
                "finished_at",
            ):
                if selection[key] != attempt[key]:
                    _fail(f"run selection {key} is inconsistent with its attempt")
            if selection["result"] is not None:
                _compact(selection["result"], "run selection result")
            if attempt["state"] == "completed" and selection["result"] != items[ordinal]["result"]:
                _fail("run selection compact result does not match its item")
            if attempt["state"] != "completed" and selection["result"] is not None:
                _fail("non-completed run selection retains a compact result")
            states.append(attempt["state"])
            run_items.append(selection)
        if run["status"] != _run_status(states):
            _fail("run status does not describe its bound attempt states")
    if set(attempts) != claimed_attempts:
        _fail("every retained attempt must be bound to exactly one run")
    return by_id


def _validate_controls(
    controls: Any,
    batch_id: str,
    batch_revision: int,
    item_ordinals: set[int],
) -> None:
    if type(controls) is not list or len(controls) > MAX_SNAPSHOT_CONTROLS:
        _fail("snapshot control events exceed their bound")
    previous_after = 0
    seen_ids: set[str] = set()
    for expected_no, raw in enumerate(controls, start=1):
        event = _exact(raw, _CONTROL_FIELDS, "control event")
        event_id = _identifier(event["event_id"], "control event_id")
        if event_id in seen_ids:
            _fail("control event IDs must be unique")
        seen_ids.add(event_id)
        if (
            event["profile"] != "measure-chromatogram-batch-control/1"
            or event["batch_id"] != batch_id
        ):
            _fail("control event profile or batch binding is inconsistent")
        if _integer(event["event_no"], "control event_no", minimum=1) != expected_no:
            _fail("control event numbers must be contiguous")
        kind = _text(event["kind"], "control event kind", maximum=40)
        if kind not in _CONTROL_KINDS:
            _fail("control event kind is unsupported")
        before = _integer(event["revision_before"], "control revision_before", minimum=1)
        after = _integer(event["revision_after"], "control revision_after", minimum=before)
        if before < previous_after:
            _fail("control event revisions are not ordered")
        previous_after = after
        _sha(event["idempotency_key_sha256"], "control idempotency_key_sha256")
        _timestamp(event["created_at"], "control created_at")
        affected = event["items"]
        if type(affected) is not list or len(affected) > MAX_BATCH_ITEMS:
            _fail("control event affected items are outside their bound")
        if kind == "retry_selected" and not affected:
            _fail("retry control events must identify at least one item")
        ordinals: set[int] = set()
        for raw_item in affected:
            item = _exact(raw_item, _CONTROL_ITEM_FIELDS, "control affected item")
            ordinal = _integer(
                item["ordinal"], "control affected ordinal", minimum=0, maximum=MAX_BATCH_ITEMS - 1
            )
            if ordinal in ordinals or ordinal not in item_ordinals:
                _fail("control affected ordinals are not a batch subset")
            ordinals.add(ordinal)
            allowed = {
                "cancel_requested": ({"pending"}, "cancelled"),
                "recover_expired": ({"running"}, "interrupted"),
                "retry_selected": ({"failed", "interrupted", "cancelled"}, "pending"),
            }[kind]
            if item["from_state"] not in allowed[0] or item["to_state"] != allowed[1]:
                _fail("control event state transition is unsupported")
        delta = after - before
        if kind in {"cancel_requested", "retry_selected"} and delta != 1:
            _fail("control event revision does not match its operation")
        if kind == "recover_expired" and delta not in {0, 1}:
            _fail("recovery control event revision is invalid")
        unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
        if sha256(unsigned) != event["event_sha256"]:
            _fail("control event digest does not match its contents")
        _bounded_json(event, "control event", MAX_CONTROL_EVENT_BYTES, object_only=True)
    if previous_after > batch_revision:
        _fail("control events are newer than the batch")


def _validate_control_coverage(
    controls: list[dict[str, Any]],
    items: dict[int, dict[str, Any]],
    attempts_by_ordinal: dict[int, list[dict[str, Any]]],
    runs: dict[str, dict[str, Any]],
    batch_revision: int,
    batch_cancel_requested: bool,
) -> None:
    """Replay the durable revision ledger and every retained state transition."""
    anchors: list[dict[str, Any]] = []
    for event_no, event in enumerate(controls, start=1):
        anchors.append(
            {
                "before": event["revision_before"],
                "after": event["revision_after"],
                "kind": "control",
                "event_no": event_no,
                "event": event,
            }
        )
    for run in runs.values():
        anchors.append(
            {
                "before": run["expected_revision"],
                "after": run["expected_revision"] + 1,
                "kind": "run",
                "event_no": 0,
                "run": run,
            }
        )

    positive_before: set[int] = set()
    for anchor in anchors:
        if anchor["after"] > anchor["before"]:
            if anchor["before"] in positive_before:
                _fail("multiple positive revision anchors share a revision")
            positive_before.add(anchor["before"])
        elif anchor["kind"] != "control" or anchor["event"]["kind"] != "recover_expired":
            _fail("only empty recovery controls may have zero revision delta")
    anchors.sort(
        key=lambda anchor: (
            anchor["before"],
            0
            if anchor["kind"] == "control"
            and anchor["event"]["kind"] == "recover_expired"
            and anchor["after"] == anchor["before"]
            else 1,
            anchor["event_no"] or 0,
        )
    )

    states = {ordinal: "pending" for ordinal in items}
    attempt_positions = {ordinal: 0 for ordinal in items}
    active: dict[int, dict[str, Any]] = {}
    current_revision = 1
    cancel_requested = False

    def terminal_candidates() -> list[tuple[str, int, int, dict[str, Any]]]:
        candidates = []
        for ordinal, attempt in active.items():
            if attempt["state"] in {"completed", "failed"}:
                candidates.append((attempt["finished_at"], ordinal, attempt["attempt_no"], attempt))
        candidates.sort(key=lambda value: (value[0], value[1], value[2]))
        return candidates

    def advance_to(target: int) -> None:
        nonlocal current_revision
        if target < current_revision:
            _fail("revision anchors are out of order")
        while current_revision < target:
            candidates = terminal_candidates()
            if not candidates:
                _fail("revision gap is not explained by a terminal attempt")
            _finished_at, ordinal, _attempt_no, attempt = candidates[0]
            states[ordinal] = attempt["state"]
            del active[ordinal]
            current_revision += 1

    def apply_run(run: dict[str, Any]) -> None:
        nonlocal current_revision
        if cancel_requested:
            _fail("a cancelled batch cannot claim a new run")
        pending = [ordinal for ordinal in sorted(states) if states[ordinal] == "pending"]
        selected = [selection["ordinal"] for selection in run["items"]]
        if selected != pending[: run["selected_count"]]:
            _fail("run selections are not the first currently pending items")
        for selection in run["items"]:
            ordinal = selection["ordinal"]
            if ordinal in active:
                _fail("run selection claims an already-running item")
            history = attempts_by_ordinal[ordinal]
            position = attempt_positions[ordinal]
            if position >= len(history):
                _fail("run selection has no next retained attempt")
            attempt = history[position]
            if (
                attempt["attempt_id"] != selection["attempt_id"]
                or attempt["run_id"] != run["run_id"]
                or attempt["state"] not in {"completed", "failed", "interrupted"}
                or attempt["attempt_no"] != position + 1
            ):
                _fail("run selection does not start the next retained attempt")
            active[ordinal] = attempt
            states[ordinal] = "running"
            attempt_positions[ordinal] += 1
        current_revision += 1

    def apply_control(event: dict[str, Any]) -> None:
        nonlocal cancel_requested, current_revision
        kind = event["kind"]
        affected = event["items"]
        ordinals = [item["ordinal"] for item in affected]
        if kind == "cancel_requested":
            if not cancel_requested and all(state == "completed" for state in states.values()):
                _fail("completed batches cannot be cancelled")
            pending = sorted(ordinal for ordinal, state in states.items() if state == "pending")
            if sorted(ordinals) != pending:
                _fail("cancellation must cover every currently pending item")
            for item in affected:
                ordinal = item["ordinal"]
                if states[ordinal] != item["from_state"]:
                    _fail("cancellation state does not match the revision ledger")
                states[ordinal] = "cancelled"
            cancel_requested = True
            current_revision += 1
            return
        if kind == "retry_selected":
            if active:
                _fail("retry cannot occur while an item is running")
            if not affected:
                _fail("retry must select at least one item")
            for item in affected:
                ordinal = item["ordinal"]
                if states[ordinal] != item["from_state"] or states[ordinal] not in {
                    "failed",
                    "interrupted",
                    "cancelled",
                }:
                    _fail("retry state does not match the revision ledger")
                states[ordinal] = "pending"
            cancel_requested = False
            current_revision += 1
            return
        if kind == "recover_expired":
            if not affected:
                if event["revision_after"] != event["revision_before"]:
                    _fail("empty recovery controls must be no-ops")
                return
            if event["revision_after"] != event["revision_before"] + 1:
                _fail("recovery of running items must advance the revision")
            for item in affected:
                ordinal = item["ordinal"]
                attempt = active.get(ordinal)
                if (
                    states[ordinal] != "running"
                    or attempt is None
                    or attempt["state"] != "interrupted"
                    or item["from_state"] != "running"
                ):
                    _fail("recovery state does not match the revision ledger")
                states[ordinal] = "interrupted"
                del active[ordinal]
            current_revision += 1
            return
        _fail("unsupported control anchor")

    for anchor in anchors:
        advance_to(anchor["before"])
        if current_revision != anchor["before"]:
            _fail("revision anchors are out of order")
        if anchor["kind"] == "run":
            apply_run(anchor["run"])
        else:
            apply_control(anchor["event"])
        if current_revision != anchor["after"]:
            _fail("revision anchor did not produce its declared revision")

    while terminal_candidates():
        advance_to(current_revision + 1)
    if active:
        _fail("retained running attempts are missing recovery controls")
    if current_revision != batch_revision:
        _fail("batch revision does not match the replayed revision ledger")
    if cancel_requested != batch_cancel_requested:
        _fail("batch cancellation flag does not match the retained control ledger")
    for ordinal, item in items.items():
        if attempt_positions[ordinal] != len(attempts_by_ordinal[ordinal]):
            _fail("retained attempts are not all consumed by their run anchors")
        if states[ordinal] != item["state"]:
            _fail("item state does not match the replayed revision ledger")


def _validate_datasets(
    datasets: Any,
    mode: str,
    batch_id: str,
    manifest_inputs: dict[int, dict[str, Any]],
    snapshot_sha: str | None,
) -> dict[str, dict[str, Any]]:
    if type(datasets) is not list or len(datasets) != len(
        {item["dataset_id"] for item in manifest_inputs.values()}
    ):
        _fail("snapshot datasets do not exactly cover the batch dataset closure")
    expected_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in manifest_inputs.values():
        expected_by_id.setdefault(item["dataset_id"], []).append(item)
    by_id: dict[str, dict[str, Any]] = {}
    total_points = 0
    for raw in datasets:
        dataset = _exact(raw, _DATASET_FIELDS, "snapshot dataset")
        dataset_id = _identifier(dataset["dataset_id"], "dataset_id")
        if dataset_id in by_id or dataset_id not in expected_by_id:
            _fail("snapshot dataset closure is not an exact set")
        by_id[dataset_id] = dataset
        _sha(dataset["input_sha256"], "dataset input_sha256")
        _timestamp(dataset["created_at"], "dataset created_at")
        _char_text(dataset["title"], "dataset title", maximum=200, nonblank=False)
        trace_count = _integer(dataset["trace_count"], "dataset trace_count", minimum=1, maximum=32)
        point_count = _integer(
            dataset["point_count"], "dataset point_count", minimum=3, maximum=150_000
        )
        payload = dataset["payload"]
        trace_ids = {item["trace_id"] for item in expected_by_id[dataset_id]}
        selected_points = sum(item["point_count"] for item in expected_by_id[dataset_id])
        if trace_count < len(trace_ids) or point_count < selected_points:
            _fail("dataset counts are smaller than the selected trace closure")
        if mode == "self_contained":
            payload = _dataset_payload(payload)
            if sha256(payload) != dataset["input_sha256"]:
                _fail("dataset payload digest does not match input_sha256")
            if payload["title"] != dataset["title"]:
                _fail("dataset title does not match its payload")
            traces = {trace["trace_id"]: trace for trace in payload["traces"]}
            if (
                len(traces) != trace_count
                or sum(len(trace["axis"]) for trace in traces.values()) != point_count
            ):
                _fail("dataset counts do not match its payload")
            if not trace_ids <= set(traces):
                _fail("dataset payload is missing a selected trace")
            for item in expected_by_id[dataset_id]:
                trace = traces[item["trace_id"]]
                metadata = {key: trace[key] for key in _TRACE_FIELDS}
                if metadata != item["trace"] or len(trace["axis"]) != item["point_count"]:
                    _fail("selected trace metadata does not match the frozen batch")
        elif payload is not None:
            _fail("reference snapshots must omit dataset payloads")
        for item in expected_by_id[dataset_id]:
            if (
                dataset["title"] != item["dataset_title"]
                or dataset["input_sha256"] != item["dataset_sha256"]
            ):
                _fail("dataset metadata does not match the frozen batch")
        if any(
            item["source"] != expected_by_id[dataset_id][0]["source"]
            for item in expected_by_id[dataset_id]
        ):
            _fail("dataset inputs disagree about their frozen source")
        expected_source = expected_by_id[dataset_id][0]["source"]
        _source(
            dataset["source"],
            mode,
            trace_ids
            if mode == "references"
            else trace_ids
            | ({trace["trace_id"] for trace in payload["traces"]} if payload else set()),
            expected_source,
        )
        if mode == "references" and expected_source is not None:
            mapping = expected_source["mapping"]
            mapped_trace_ids = {
                entry["trace_id"]
                for entry in (
                    mapping["signal_columns"] if "signal_columns" in mapping else mapping["groups"]
                )
            }
            if dataset["trace_count"] != len(mapped_trace_ids):
                _fail("reference dataset trace_count does not cover its source mapping")
            if dataset["point_count"] < selected_points + 3 * (
                len(mapped_trace_ids) - len(trace_ids)
            ):
                _fail("reference dataset point_count is below its source closure")
        if mode == "self_contained" and dataset["source"] is not None:
            _validate_source_replay(dataset["source"], payload)
        _origin(dataset["import_origin"], "dataset", dataset_id, snapshot_sha)
        if dataset["import_origin"] is not None:
            origin = dataset["import_origin"]
            if origin["preserved_content_sha256"] != dataset["input_sha256"]:
                _fail("dataset import origin content digest is inconsistent")
            if mode == "self_contained":
                expected_record = import_origins.dataset_core_sha256(
                    origin["source_record_id"],
                    dataset["input_sha256"],
                    dataset["created_at"],
                    dataset["payload"],
                    dataset["source"],
                )
                if origin["original_record_sha256"] != expected_record:
                    _fail("dataset import origin record digest is inconsistent")
        total_points += point_count
    if total_points > MAX_BATCH_POINTS:
        _fail("dataset closure exceeds the total point budget")
    return by_id


def _validate_provenance(
    value: Any, dataset_sha: str, source_sha: str | None, method: dict[str, Any]
) -> None:
    value = _exact(value, _PROVENANCE_FIELDS, "analysis provenance")
    _bounded_json(value, "analysis provenance", MAX_RUNTIME_BYTES, object_only=True)
    if value["input_sha256"] != dataset_sha or value["source_sha256"] != source_sha:
        _fail("analysis provenance input/source binding is inconsistent")
    if value["method_sha256"] != sha256(method):
        _fail("analysis provenance method digest is inconsistent")
    _sha(value["input_sha256"], "analysis provenance input_sha256")
    _sha(value["method_sha256"], "analysis provenance method_sha256")
    _sha(value["worker_sha256"], "analysis provenance worker_sha256")
    _nullable_sha(value["source_sha256"], "analysis provenance source_sha256")
    _runtime_mapping(value["packages"], "analysis provenance packages")
    _runtime_mapping(
        value["installed_r_code_md5"],
        "analysis provenance installed_r_code_md5",
        digest_values=True,
    )
    for key in ("r_version", "measure_revision", "measure_sec_revision"):
        if value[key] is not None:
            _text(value[key], f"analysis provenance {key}", maximum=1_000)


def _validate_analyses(
    analyses: Any,
    mode: str,
    batch_id: str,
    batch_method: dict[str, Any],
    items: dict[int, dict[str, Any]],
    datasets: dict[str, dict[str, Any]],
    snapshot_sha: str | None,
) -> dict[str, dict[str, Any]]:
    if type(analyses) is not list or len(analyses) > MAX_SNAPSHOT_ANALYSES:
        _fail("snapshot analyses exceed their bound")
    completed = {
        item["analysis_id"]: item
        for item in items.values()
        if item["state"] == "completed" and item["analysis_id"] is not None
    }
    if len(analyses) != len(completed):
        _fail("snapshot analyses do not exactly close completed items")
    by_id: dict[str, dict[str, Any]] = {}
    for raw in analyses:
        analysis = _exact(raw, _ANALYSIS_FIELDS, "snapshot analysis")
        analysis_id = _identifier(analysis["analysis_id"], "analysis_id")
        if analysis_id in by_id or analysis_id not in completed:
            _fail("snapshot analysis closure is not an exact set")
        by_id[analysis_id] = analysis
        item = completed[analysis_id]
        if analysis["dataset_id"] != item["dataset_id"] or analysis["trace_id"] != item["trace_id"]:
            _fail("analysis identity does not match its completed item")
        if analysis["kind"] != "chromatogram" or _canonical_bytes(
            analysis["method"]
        ) != _canonical_bytes(batch_method):
            _fail("analysis kind or method does not match the batch")
        _timestamp(analysis["created_at"], "analysis created_at")
        dataset = datasets[analysis["dataset_id"]]
        source_sha = item["source_sha256"]
        _validate_provenance(
            analysis["provenance"], dataset["input_sha256"], source_sha, batch_method
        )
        result = analysis["result"]
        if mode == "self_contained":
            if type(result) is not dict:
                _fail("self-contained analyses must include results")
            _bounded_json(result, "analysis result", MAX_ANALYSIS_RESULT_BYTES, object_only=True)
            if sha256(result) != analysis["result_sha256"]:
                _fail("analysis result digest does not match its result")
            record = {
                "analysis_id": analysis_id,
                "dataset_id": analysis["dataset_id"],
                "trace_id": analysis["trace_id"],
                "kind": analysis["kind"],
                "method": analysis["method"],
                "result": result,
                "provenance": analysis["provenance"],
                "created_at": analysis["created_at"],
            }
            payload = datasets[analysis["dataset_id"]]["payload"]
            evidence = analysis_evidence.build_evidence(record, payload, dataset["input_sha256"])
            if evidence["evidence_sha256"] != analysis["evidence_sha256"]:
                _fail("analysis evidence digest does not match the retained record")
            if item["result"] != analysis_runtime.compact_analysis(record):
                _fail("completed item compact result does not match its analysis")
        elif result is not None:
            _fail("reference snapshots must omit analysis results")
        _sha(analysis["result_sha256"], "analysis result_sha256")
        _sha(analysis["evidence_sha256"], "analysis evidence_sha256")
        _origin(analysis["import_origin"], "analysis", analysis_id, snapshot_sha)
        compact = item["result"]
        if (
            compact["analysis_id"] != analysis_id
            or compact["dataset_id"] != analysis["dataset_id"]
            or compact["trace_id"] != analysis["trace_id"]
            or compact["kind"] != analysis["kind"]
            or compact["created_at"] != analysis["created_at"]
            or compact["provenance"] != analysis["provenance"]
        ):
            _fail("completed item compact metadata does not match its analysis")
        if (
            item["result_sha256"] != analysis["result_sha256"]
            or item["evidence_sha256"] != analysis["evidence_sha256"]
        ):
            _fail("completed item digests do not match its analysis")
        if analysis["import_origin"] is not None:
            origin = analysis["import_origin"]
            if origin["preserved_content_sha256"] != analysis["result_sha256"]:
                _fail("analysis import origin content digest is inconsistent")
            dataset_origin = datasets[analysis["dataset_id"]]["import_origin"]
            if (
                dataset_origin is None
                or dataset_origin["import_id"] != origin["import_id"]
                or dataset_origin["source_batch_id"] != origin["source_batch_id"]
                or dataset_origin["snapshot_sha256"] != origin["snapshot_sha256"]
                or dataset_origin["imported_at"] != origin["imported_at"]
            ):
                _fail("analysis import origin is not bound to its dataset import")
            if mode == "self_contained":
                source_dataset_id = dataset_origin["source_record_id"]
                expected_record = import_origins.analysis_core_sha256(
                    origin["source_record_id"],
                    source_dataset_id,
                    analysis["trace_id"],
                    analysis["kind"],
                    analysis["method"],
                    analysis["result"],
                    analysis["provenance"],
                    analysis["created_at"],
                )
                if origin["original_record_sha256"] != expected_record:
                    _fail("analysis import origin record digest is inconsistent")
    return by_id


def _validate_success_bindings(
    items: dict[int, dict[str, Any]],
    attempts_by_ordinal: dict[int, list[dict[str, Any]]],
    analyses: dict[str, dict[str, Any]],
) -> None:
    for ordinal, item in items.items():
        history = attempts_by_ordinal[ordinal]
        successful = [attempt for attempt in history if attempt["state"] == "completed"]
        if item["state"] == "completed":
            if len(successful) != 1 or successful[0]["analysis_id"] != item["analysis_id"]:
                _fail("completed item does not retain exactly one successful attempt")
            attempt = successful[0]
            analysis = analyses[item["analysis_id"]]
            if (
                attempt["result_sha256"] != item["result_sha256"]
                or attempt["evidence_sha256"] != item["evidence_sha256"]
            ):
                _fail("successful attempt digests do not match the item")
            if attempt["runtime"] != analysis["provenance"]:
                _fail("successful attempt runtime does not match analysis provenance")
        elif successful:
            _fail("non-completed item has a successful attempt")


def validate_snapshot(document: dict[str, Any]) -> None:
    """Validate a complete portable snapshot document without I/O."""
    if type(document) is not dict:
        _fail("snapshot must be a JSON object")
    _strict_tree(document)
    _exact(document, _TOP_FIELDS, "snapshot")
    if (
        document["profile"] != SNAPSHOT_PROFILE
        or document["schema_version"] != SNAPSHOT_SCHEMA_VERSION
    ):
        _fail("unsupported snapshot profile or schema version")
    mode = document["mode"]
    if mode not in _MODES:
        _fail("snapshot mode must be explicitly references or self_contained")
    _timestamp(document["captured_at"], "snapshot captured_at")
    batch_id, batch_info, manifest_inputs = _validate_batch(document["batch"])
    batch = batch_info["batch"]
    items = _validate_items(document["items"], batch_id, manifest_inputs)
    attempts, attempts_by_ordinal = _validate_attempts(document["attempts"], batch_id, items)
    runs = _validate_runs(document["runs"], batch_id, batch["revision"], items, attempts)
    _validate_controls(document["control_events"], batch_id, batch["revision"], set(items))
    _validate_control_coverage(
        document["control_events"],
        items,
        attempts_by_ordinal,
        runs,
        batch["revision"],
        batch["cancel_requested"],
    )
    operation_keys = [run["request_key_sha256"] for run in runs.values()]
    operation_keys.extend(event["idempotency_key_sha256"] for event in document["control_events"])
    if len(operation_keys) != len(set(operation_keys)):
        _fail("retained run and control operation keys must be unique")
    expected_revision = (
        1
        + len(runs)
        + sum(attempt["state"] in {"completed", "failed"} for attempt in attempts.values())
        + sum(
            event["revision_after"] - event["revision_before"]
            for event in document["control_events"]
        )
    )
    if batch["revision"] != expected_revision:
        _fail("batch revision does not account for retained runs, attempts and controls")
    if batch["control_event_count"] != len(document["control_events"]):
        _fail("batch control_event_count does not match the retained events")
    actual_counts = {state: 0 for state in _ITEM_STATES}
    for item in items.values():
        actual_counts[item["state"]] += 1
    if actual_counts != batch["counts"]:
        _fail("batch counts do not match retained item states")
    expected_status = (
        "cancelling"
        if batch["cancel_requested"] and actual_counts["running"]
        else "cancelled"
        if batch["cancel_requested"]
        else "completed"
        if actual_counts["completed"] == batch["item_count"]
        else "active"
    )
    if batch["status"] != expected_status:
        _fail("batch status does not describe its retained item states")
    if batch["cancel_requested"] and actual_counts["pending"]:
        _fail("cancelled batches cannot retain pending items")
    snapshot_sha = hashlib.sha256(_canonical_bytes(document)).hexdigest()
    datasets = _validate_datasets(
        document["datasets"], mode, batch_id, manifest_inputs, snapshot_sha
    )
    analyses = _validate_analyses(
        document["analyses"], mode, batch_id, batch_info["method"], items, datasets, snapshot_sha
    )
    _validate_success_bindings(items, attempts_by_ordinal, analyses)
    if document["availability"] != AVAILABILITY:
        _fail("snapshot availability declarations are unsupported")
    if batch["counts"]["running"] != 0:
        _fail("snapshot cannot contain running items")
    if batch["status"] == "completed" and batch["counts"]["completed"] != batch["item_count"]:
        _fail("completed batch does not contain all completed items")
    if len(_canonical_bytes(document)) > MAX_SNAPSHOT_BYTES:
        _fail("snapshot exceeds its byte limit")


def encode_snapshot(document: dict[str, Any]) -> bytes:
    """Validate and encode a snapshot using canonical strict UTF-8 JSON."""
    validate_snapshot(document)
    encoded = _canonical_bytes(document)
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        _fail("snapshot exceeds its byte limit")
    return encoded


def decode_snapshot(data: bytes) -> dict[str, Any]:
    """Decode canonical strict JSON and validate its complete cross-record closure."""
    document = _decode_json(data, "snapshot", MAX_SNAPSHOT_BYTES)
    validate_snapshot(document)
    return document


def snapshot_manifest(data: bytes) -> dict[str, Any]:
    """Return bounded identity metadata for one validated snapshot."""
    document = decode_snapshot(data)
    return {
        "profile": SNAPSHOT_PROFILE,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "mode": document["mode"],
        "snapshot_sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "batch_id": document["batch"]["batch_id"],
        "batch_revision": document["batch"]["revision"],
        "dataset_count": len(document["datasets"]),
        "analysis_count": len(document["analyses"]),
        "item_count": len(document["items"]),
    }
