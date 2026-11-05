"""Portable, integrity-checked evidence for a stored chromatogram analysis.

This module is deliberately independent of storage connections, MCP, and the
analysis worker. It assembles the already stored result with its source trace;
it does not repeat or reinterpret scientific calculations.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from numbers import Real
from typing import Any, Literal

from .storage import canonical, sha256

PROFILE = "measure-chromatogram-evidence/1"
MAX_POINTS = 50_000
MAX_PEAKS = 25_000
MAX_PAGE = 1_000
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_PAGE_BYTES = 256 * 1024

_TRACE_METADATA_FIELDS = (
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
_ANALYSIS_FIELDS = (
    "analysis_id",
    "dataset_id",
    "trace_id",
    "kind",
    "method",
    "provenance",
    "created_at",
    "stored_result_sha256",
)
_PROVENANCE_HASH_FIELDS = (
    "input_sha256",
    "method_sha256",
    "worker_sha256",
)
_PEAK_REQUIRED_FIELDS = frozenset(
    {"peak_id", "location", "height", "left_base", "right_base", "area"}
)
_PEAK_OPTIONAL_FIELDS = frozenset({"prominence", "area_basis"})
_POINT_FIELDS = frozenset(
    {"sample_index", "axis", "processed_axis", "raw_signal", "processed_signal"}
)
_AVAILABILITY = {
    "compound_identity": "not_assigned",
    "calibration": "not_linked",
    "deconvolution": "not_performed",
    "physical_validation": "not_established",
}
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUNDLE_FIELDS = frozenset(
    {
        "profile",
        "analysis",
        "trace",
        "result",
        "point_count",
        "peak_count",
        "availability",
        "points",
        "peaks",
        "evidence_sha256",
    }
)
_MANIFEST_FIELDS = _BUNDLE_FIELDS - {"points", "peaks"}
_SNAPSHOT_VALIDATION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    """Immutable, validated byte snapshot used for repeated manifest/page reads."""

    _manifest_bytes: bytes = field(repr=False)
    _point_rows: tuple[bytes, ...] = field(repr=False)
    _peak_rows: tuple[bytes, ...] = field(repr=False)
    _analysis_id: str = field(repr=False)
    _evidence_sha256: str = field(repr=False)
    serialized_size_bytes: int
    _validation_token: object | None = field(default=None, init=False, repr=False, compare=False)


def capture_evidence(bundle: dict) -> EvidenceSnapshot:
    """Freeze one owned JSON copy, validate it, and index its rows as immutable bytes."""
    _require_mapping(bundle, "evidence bundle")
    frozen_bytes = _canonical_bytes(bundle)
    if len(frozen_bytes) > MAX_BUNDLE_BYTES:
        raise ValueError("evidence bundle exceeds the portable size limit")
    try:
        owned = json.loads(frozen_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("evidence bundle must be valid JSON data") from exc
    validate_evidence(owned)
    manifest = {key: value for key, value in owned.items() if key not in {"points", "peaks"}}
    snapshot = EvidenceSnapshot(
        _manifest_bytes=_canonical_bytes(manifest),
        _point_rows=tuple(_canonical_bytes(row) for row in owned["points"]),
        _peak_rows=tuple(_canonical_bytes(row) for row in owned["peaks"]),
        _analysis_id=owned["analysis"]["analysis_id"],
        _evidence_sha256=owned["evidence_sha256"],
        serialized_size_bytes=len(frozen_bytes),
    )
    object.__setattr__(snapshot, "_validation_token", _SNAPSHOT_VALIDATION_TOKEN)
    return snapshot


def build_evidence(record: dict, dataset: dict, digest: str) -> dict[str, Any]:
    """Build a bounded portable evidence document from stored source records.

    ``digest`` is the stored dataset identity. The builder verifies it against
    the supplied dataset and the analysis provenance before combining them.
    "stored_result_sha256" records the identity of the retained worker result;
    neither digest is an authentication signature.
    """
    _require_mapping(record, "analysis record")
    _require_mapping(dataset, "dataset")
    if not _is_sha256(digest):
        raise ValueError("dataset digest must be a lowercase SHA-256 value")
    if _sha256_or_error(dataset, "dataset") != digest:
        raise ValueError("dataset digest does not match the supplied dataset")

    required_record = {
        "analysis_id",
        "dataset_id",
        "trace_id",
        "kind",
        "method",
        "result",
        "provenance",
        "created_at",
    }
    if set(record) != required_record:
        raise ValueError("analysis record has an unexpected shape")
    if record["kind"] != "chromatogram":
        raise ValueError("portable evidence requires a chromatogram analysis")
    _require_mapping(record["method"], "analysis method")
    _require_mapping(record["provenance"], "analysis provenance")
    _require_mapping(record["result"], "analysis result")
    if record["provenance"].get("input_sha256") != digest:
        raise ValueError("analysis input digest does not match the dataset")
    if record["provenance"].get("method_sha256") != _sha256_or_error(
        record["method"], "analysis method"
    ):
        raise ValueError("analysis method digest does not match the stored method")

    trace = _find_trace(dataset, record["trace_id"])
    result = record["result"]
    if result.get("kind") != "chromatogram":
        raise ValueError("stored result is not a chromatogram result")
    raw_axis = trace.get("axis")
    raw_signal = trace.get("signal")
    processed = result.get("processed_trace")
    peaks = result.get("peaks")
    if not isinstance(processed, dict) or set(processed) != {"axis", "signal"}:
        raise ValueError("stored processed trace must contain axis and signal arrays")
    if not isinstance(peaks, list):
        raise ValueError("stored peaks must be a list")
    if _strict_integer(result.get("peak_count"), "stored peak_count", minimum=0) != len(peaks):
        raise ValueError("stored peak_count does not match the stored peak rows")

    points = _build_points(raw_axis, raw_signal, processed["axis"], processed["signal"])
    if len(points) > MAX_POINTS:
        raise ValueError("trace exceeds the portable point limit")
    normalized_peaks = _validate_peaks(peaks)
    if len(normalized_peaks) > MAX_PEAKS:
        raise ValueError("peaks exceed the portable peak limit")

    trace_metadata = {key: trace[key] for key in _TRACE_METADATA_FIELDS if key in trace}
    if set(trace_metadata) != set(_TRACE_METADATA_FIELDS):
        raise ValueError("source trace metadata is incomplete")
    _validate_trace_metadata(trace_metadata)

    analysis = {key: value for key, value in record.items() if key != "result"}
    analysis["stored_result_sha256"] = _sha256_or_error(result, "stored analysis result")
    evidence = {
        "profile": PROFILE,
        "analysis": analysis,
        "trace": trace_metadata,
        "result": {
            key: value for key, value in result.items() if key not in {"peaks", "processed_trace"}
        },
        "point_count": len(points),
        "peak_count": len(normalized_peaks),
        "availability": dict(_AVAILABILITY),
        "points": points,
        "peaks": normalized_peaks,
    }
    evidence["evidence_sha256"] = _evidence_digest(evidence)
    validate_evidence(evidence)
    return evidence


def manifest_for(snapshot: EvidenceSnapshot) -> dict[str, Any]:
    """Return fresh decoded metadata from a previously captured snapshot."""
    _require_snapshot(snapshot)
    return _decode_json(snapshot._manifest_bytes, "snapshot manifest")


def page_for(
    snapshot: EvidenceSnapshot,
    kind: Literal["trace", "peaks"],
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Decode one bounded row page without revalidating or rehashing the bundle."""
    _require_snapshot(snapshot)
    if not isinstance(kind, str) or kind not in {"trace", "peaks"}:
        raise ValueError("kind must be 'trace' or 'peaks'")
    _strict_integer(offset, "offset", minimum=0)
    _strict_integer(limit, "limit", minimum=1, maximum=MAX_PAGE)
    encoded_rows = snapshot._point_rows if kind == "trace" else snapshot._peak_rows
    count = len(encoded_rows)
    page_bytes = encoded_rows[offset : offset + limit]
    response = {
        "profile": PROFILE,
        "analysis_id": snapshot._analysis_id,
        "evidence_sha256": snapshot._evidence_sha256,
        "kind": kind,
        "offset": offset,
        "count": count,
        "next_offset": offset + limit if offset + limit < count else None,
        "data": [],
    }
    base_bytes = len(_canonical_bytes(response))
    if page_bytes:
        page_size = base_bytes - 2 + sum(len(row) for row in page_bytes) + len(page_bytes) - 1
    else:
        page_size = base_bytes
    if page_size > MAX_PAGE_BYTES:
        raise ValueError("page exceeds the portable page-size limit")
    response["data"] = [_decode_json(row, "snapshot row") for row in page_bytes]
    return response


def assemble_evidence(manifest: dict, points: Sequence, peaks: Sequence) -> dict[str, Any]:
    """Assemble row lists or page responses and verify complete bundle identity.

    Page-response sequences must be ordered and contiguous. Rows inside each
    page must retain their source order.
    """
    _validate_manifest(manifest)
    complete = dict(manifest)
    complete["points"] = _assemble_rows(manifest, "trace", points)
    complete["peaks"] = _assemble_rows(manifest, "peaks", peaks)
    validate_evidence(complete)
    if _evidence_digest(complete) != manifest["evidence_sha256"]:
        raise ValueError("assembled evidence does not match the manifest digest")
    return complete


def validate_evidence(bundle: dict) -> None:
    """Validate a complete evidence document; raise ``ValueError`` if invalid."""
    _require_mapping(bundle, "evidence bundle")
    if set(bundle) != _BUNDLE_FIELDS:
        raise ValueError("evidence bundle has an unexpected shape")
    if bundle.get("profile") != PROFILE:
        raise ValueError("unsupported evidence profile")
    _validate_analysis(bundle["analysis"])
    _validate_trace_metadata(bundle["trace"])
    if bundle["analysis"]["trace_id"] != bundle["trace"]["trace_id"]:
        raise ValueError("analysis and trace identities do not match")
    _require_mapping(bundle["result"], "portable result metadata")
    if "peaks" in bundle["result"] or "processed_trace" in bundle["result"]:
        raise ValueError("portable result metadata must omit peaks and processed trace arrays")
    if bundle["result"].get("kind") != "chromatogram":
        raise ValueError("portable result metadata must identify a chromatogram")
    point_count = _strict_integer(
        bundle["point_count"], "point_count", minimum=3, maximum=MAX_POINTS
    )
    peak_count = _strict_integer(bundle["peak_count"], "peak_count", minimum=0, maximum=MAX_PEAKS)
    _validate_result_metadata(bundle["result"], peak_count)
    if bundle["availability"] != _AVAILABILITY:
        raise ValueError("availability declarations do not match this profile")

    points = bundle["points"]
    peaks = bundle["peaks"]
    if not isinstance(points, list) or len(points) != point_count:
        raise ValueError("point rows do not match point_count")
    if not isinstance(peaks, list) or len(peaks) != peak_count:
        raise ValueError("peak rows do not match peak_count")

    previous_axis: Real | None = None
    previous_processed_axis: Real | None = None
    for index, point in enumerate(points):
        _require_mapping(point, "point row")
        if set(point) != _POINT_FIELDS:
            raise ValueError("point row has an unexpected shape")
        if _strict_integer(point["sample_index"], "sample_index", minimum=0) != index:
            raise ValueError("sample_index values must be contiguous and ordered from zero")
        axis = _finite_number(point["axis"], "point axis")
        processed_axis = _finite_number(point["processed_axis"], "processed point axis")
        _finite_number(point["raw_signal"], "raw signal")
        _finite_number(point["processed_signal"], "processed signal")
        if previous_axis is not None and axis <= previous_axis:
            raise ValueError("raw point axes must be strictly increasing")
        if previous_processed_axis is not None and processed_axis <= previous_processed_axis:
            raise ValueError("processed point axes must be strictly increasing")
        previous_axis = axis
        previous_processed_axis = processed_axis

    normalized_peaks = _validate_peaks(peaks)
    axis_min, axis_max = points[0]["processed_axis"], points[-1]["processed_axis"]
    for peak in normalized_peaks:
        _validate_peak_bounds(peak, axis_min, axis_max)
    stored_result = dict(bundle["result"])
    stored_result["peaks"] = peaks
    stored_result["processed_trace"] = {
        "axis": [point["processed_axis"] for point in points],
        "signal": [point["processed_signal"] for point in points],
    }
    if (
        _sha256_or_error(stored_result, "reconstructed stored result")
        != bundle["analysis"]["stored_result_sha256"]
    ):
        raise ValueError("stored result digest does not match its portable rows")
    expected_digest = _evidence_digest(bundle)
    if not _is_sha256(bundle["evidence_sha256"]) or bundle["evidence_sha256"] != expected_digest:
        raise ValueError("evidence digest does not match the complete document")
    if len(_canonical_bytes(bundle)) > MAX_BUNDLE_BYTES:
        raise ValueError("evidence bundle exceeds the portable size limit")


def _validate_manifest(manifest: dict) -> None:
    _require_mapping(manifest, "evidence manifest")
    if set(manifest) != _MANIFEST_FIELDS:
        raise ValueError("evidence manifest has an unexpected shape")
    if manifest.get("profile") != PROFILE:
        raise ValueError("unsupported evidence profile")
    _validate_analysis(manifest["analysis"])
    _validate_trace_metadata(manifest["trace"])
    _require_mapping(manifest["result"], "portable result metadata")
    if "peaks" in manifest["result"] or "processed_trace" in manifest["result"]:
        raise ValueError("portable result metadata must omit peaks and processed trace arrays")
    if manifest["result"].get("kind") != "chromatogram":
        raise ValueError("portable result metadata must identify a chromatogram")
    _strict_integer(manifest["point_count"], "point_count", minimum=3, maximum=MAX_POINTS)
    peak_count = _strict_integer(manifest["peak_count"], "peak_count", minimum=0, maximum=MAX_PEAKS)
    _validate_result_metadata(manifest["result"], peak_count)
    if manifest["analysis"]["trace_id"] != manifest["trace"]["trace_id"]:
        raise ValueError("analysis and trace identities do not match")
    if manifest["availability"] != _AVAILABILITY:
        raise ValueError("availability declarations do not match this profile")
    if not _is_sha256(manifest["evidence_sha256"]):
        raise ValueError("manifest evidence digest must be a lowercase SHA-256 value")
    if len(_canonical_bytes(manifest)) > MAX_BUNDLE_BYTES:
        raise ValueError("manifest exceeds the portable bundle-size limit")


def _validate_analysis(analysis: dict) -> None:
    _require_mapping(analysis, "analysis metadata")
    if set(analysis) != set(_ANALYSIS_FIELDS):
        raise ValueError("analysis metadata has an unexpected shape")
    for key in ("analysis_id", "dataset_id", "trace_id", "created_at"):
        if not isinstance(analysis[key], str) or not analysis[key].strip():
            raise ValueError(f"analysis {key} must be a nonempty string")
    if analysis["kind"] != "chromatogram":
        raise ValueError("analysis metadata must identify a chromatogram")
    _require_mapping(analysis["method"], "analysis method")
    _require_mapping(analysis["provenance"], "analysis provenance")
    provenance = analysis["provenance"]
    for key in _PROVENANCE_HASH_FIELDS:
        if not _is_sha256(provenance.get(key)):
            raise ValueError(f"analysis provenance {key} must be a lowercase SHA-256 value")
    source_digest = provenance.get("source_sha256")
    if source_digest is not None and not _is_sha256(source_digest):
        raise ValueError("analysis source_sha256 must be null or a lowercase SHA-256 value")
    if provenance["method_sha256"] != _sha256_or_error(analysis["method"], "analysis method"):
        raise ValueError("analysis method digest does not match the portable method")
    if not _is_sha256(analysis["stored_result_sha256"]):
        raise ValueError("stored_result_sha256 must be a lowercase SHA-256 value")


def _validate_result_metadata(result: dict, peak_count: int) -> None:
    if _strict_integer(result.get("peak_count"), "result peak_count", minimum=0) != peak_count:
        raise ValueError("result peak_count does not match the evidence peak_count")
    warnings = result.get("warnings")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise ValueError("result warnings must be a list of strings")
    notes = result.get("notes")
    if isinstance(notes, str):
        return
    if not isinstance(notes, list) or any(not isinstance(item, str) for item in notes):
        raise ValueError("result notes must be a string or list of strings")


def _validate_trace_metadata(trace: dict) -> None:
    _require_mapping(trace, "trace metadata")
    if set(trace) != set(_TRACE_METADATA_FIELDS):
        raise ValueError("trace metadata has an unexpected shape")
    for key in (
        "trace_id",
        "sample_id",
        "injection_id",
        "channel",
        "detector",
        "axis_unit",
        "signal_unit",
    ):
        if not isinstance(trace[key], str) or not trace[key].strip():
            raise ValueError(f"trace {key} must be a nonempty string")
    if trace["separation"] not in {"SEC", "LC", "GC", "CE", "other"}:
        raise ValueError("trace separation is not supported")
    if trace["sample_type"] not in {"unknown", "blank", "standard", "qc", "reference"}:
        raise ValueError("trace sample_type is not supported")
    if trace["batch_id"] is not None and not isinstance(trace["batch_id"], str):
        raise ValueError("trace batch_id must be a string or null")
    if trace["run_order"] is not None:
        _strict_integer(trace["run_order"], "trace run_order", minimum=0)


def _find_trace(dataset: dict, trace_id: Any) -> dict:
    traces = dataset.get("traces")
    if not isinstance(traces, list):
        raise ValueError("dataset traces must be a list")
    matches = [
        trace for trace in traces if isinstance(trace, dict) and trace.get("trace_id") == trace_id
    ]
    if len(matches) != 1:
        raise ValueError("analysis trace identity does not select exactly one dataset trace")
    trace = matches[0]
    if set(trace) != set(_TRACE_METADATA_FIELDS) | {"axis", "signal"}:
        raise ValueError("source trace has an unexpected shape")
    if "axis" not in trace or "signal" not in trace:
        raise ValueError("source trace must contain axis and signal arrays")
    return trace


def _build_points(
    raw_axis: Any, raw_signal: Any, processed_axis: Any, processed_signal: Any
) -> list[dict]:
    arrays = (raw_axis, raw_signal, processed_axis, processed_signal)
    if any(not isinstance(values, list) for values in arrays):
        raise ValueError("source and processed trace arrays must be lists")
    if len(raw_axis) < 3 or len(raw_axis) > MAX_POINTS:
        raise ValueError("source trace length is outside the supported point range")
    if not (len(raw_axis) == len(raw_signal) == len(processed_axis) == len(processed_signal)):
        raise ValueError("source and processed trace arrays have different lengths")
    points = []
    previous: Real | None = None
    previous_processed: Real | None = None
    for index, (axis, raw, processed_x, processed) in enumerate(
        zip(raw_axis, raw_signal, processed_axis, processed_signal, strict=True)
    ):
        axis = _finite_number(axis, "source axis")
        _finite_number(raw, "raw signal")
        processed_x = _finite_number(processed_x, "processed axis")
        _finite_number(processed, "processed signal")
        if previous is not None and axis <= previous:
            raise ValueError("source trace axes must be strictly increasing")
        if previous_processed is not None and processed_x <= previous_processed:
            raise ValueError("processed trace axes must be strictly increasing")
        previous = axis
        previous_processed = processed_x
        points.append(
            {
                "sample_index": index,
                "axis": axis,
                "processed_axis": processed_x,
                "raw_signal": raw,
                "processed_signal": processed,
            }
        )
    return points


def _validate_peaks(peaks: Any) -> list[dict]:
    if not isinstance(peaks, list):
        raise ValueError("peak rows must be a list")
    if len(peaks) > MAX_PEAKS:
        raise ValueError("peaks exceed the portable peak limit")
    seen: set[int] = set()
    normalized = []
    for peak in peaks:
        _require_mapping(peak, "peak row")
        peak_fields = set(peak)
        if not _PEAK_REQUIRED_FIELDS.issubset(peak_fields) or peak_fields - (
            _PEAK_REQUIRED_FIELDS | _PEAK_OPTIONAL_FIELDS
        ):
            raise ValueError("peak row has an unexpected shape")
        peak_id = _strict_integer(peak["peak_id"], "peak_id", minimum=1)
        if peak_id in seen:
            raise ValueError("peak_id values must be unique")
        seen.add(peak_id)
        normalized_peak = {"peak_id": peak_id}
        for key in ("location", "height", "left_base", "right_base", "prominence", "area"):
            if key not in peak:
                continue
            value = peak[key]
            normalized_peak[key] = None if value is None else _finite_number(value, f"peak {key}")
        if "area_basis" in peak:
            area_basis = peak["area_basis"]
            if not isinstance(area_basis, str) or not area_basis.strip():
                raise ValueError("peak area_basis must be a nonempty string")
            normalized_peak["area_basis"] = area_basis
        normalized.append(normalized_peak)
    return normalized


def _validate_peak_bounds(peak: dict, axis_min: Real, axis_max: Real) -> None:
    boundaries = ("left_base", "location", "right_base")
    for key in boundaries:
        value = peak[key]
        if value is not None and not axis_min <= value <= axis_max:
            raise ValueError(f"peak {key} must lie within the trace axis range")
    left, location, right = (peak[key] for key in boundaries)
    if left is not None and right is not None and left > right:
        raise ValueError("peak left_base must not exceed right_base")
    if left is not None and location is not None and left > location:
        raise ValueError("peak left_base must not exceed location")
    if location is not None and right is not None and location > right:
        raise ValueError("peak location must not exceed right_base")


def _assemble_rows(manifest: dict, kind: Literal["trace", "peaks"], source: Sequence) -> list:
    if not isinstance(source, Sequence) or isinstance(source, (str, bytes, bytearray)):
        raise ValueError(f"{kind} input must be a row or page sequence")
    expected = manifest["point_count"] if kind == "trace" else manifest["peak_count"]
    if not source and expected == 0:
        return []
    if source and all(isinstance(item, dict) and "data" in item for item in source):
        pages = list(source)
        rows: list = []
        next_offset = 0
        for page_index, page in enumerate(pages):
            if page_index and next_offset >= expected:
                raise ValueError(f"{kind} pages continue after the terminal page")
            if set(page) != {
                "profile",
                "analysis_id",
                "evidence_sha256",
                "kind",
                "offset",
                "count",
                "next_offset",
                "data",
            }:
                raise ValueError(f"{kind} page has an unexpected shape")
            page_offset = _strict_integer(page["offset"], "page offset", minimum=0)
            page_count = _strict_integer(page["count"], "page count", minimum=0)
            page_next = page["next_offset"]
            if page_next is not None:
                page_next = _strict_integer(page_next, "page next_offset", minimum=0)
            if (
                page["profile"] != PROFILE
                or page["analysis_id"] != manifest["analysis"]["analysis_id"]
                or page["evidence_sha256"] != manifest["evidence_sha256"]
                or page["kind"] != kind
                or page_count != expected
                or page_offset != next_offset
            ):
                raise ValueError(f"{kind} page does not match manifest identity or ordering")
            if len(_canonical_bytes(page)) > MAX_PAGE_BYTES:
                raise ValueError(f"{kind} page exceeds the portable page-size limit")
            page_data = page["data"]
            if not isinstance(page_data, list) or len(page_data) > MAX_PAGE:
                raise ValueError(f"{kind} page data is invalid")
            if page_offset < expected and not page_data:
                raise ValueError(f"{kind} page is empty before the declared end")
            if page_offset + len(page_data) > expected:
                raise ValueError(f"{kind} page extends beyond its declared count")
            expected_next = page_offset + len(page_data)
            if page_next != (expected_next if expected_next < expected else None):
                raise ValueError(f"{kind} page next_offset is inconsistent")
            rows.extend(page_data)
            next_offset = expected_next
        if len(rows) != expected or next_offset != expected:
            raise ValueError(f"{kind} pages do not cover every declared row")
        return rows
    if len(source) != expected:
        raise ValueError(f"{kind} row count does not match the manifest")
    return list(source)


def _evidence_digest(bundle: dict) -> str:
    return _sha256_or_error(
        {key: value for key, value in bundle.items() if key != "evidence_sha256"},
        "evidence document",
    )


def _sha256_or_error(value: Any, label: str) -> str:
    try:
        return sha256(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be JSON-safe finite data") from exc


def _canonical_bytes(value: Any) -> bytes:
    try:
        return canonical(value).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("evidence must contain JSON-safe finite data") from exc


def _decode_json(encoded: bytes, label: str) -> Any:
    try:
        return json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid canonical JSON") from exc


def _require_snapshot(value: Any) -> None:
    if (
        not isinstance(value, EvidenceSnapshot)
        or value._validation_token is not _SNAPSHOT_VALIDATION_TOKEN
    ):
        raise ValueError("a validated EvidenceSnapshot is required; call capture_evidence(bundle)")


def _require_mapping(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")


def _strict_integer(value: Any, label: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        raise ValueError(f"{label} must be {bound}")
    return value


def _finite_number(value: Any, label: str) -> Real:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a finite number")
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite:
        raise ValueError(f"{label} must be a finite number")
    return value


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _HEX_SHA256.fullmatch(value) is not None
