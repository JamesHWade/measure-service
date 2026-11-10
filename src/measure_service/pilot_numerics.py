"""Transport-independent synthetic checks for installed numerical backends."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

PROFILE = "measure-pilot-numerics/1"

_READ_TOOLS = frozenset(
    {
        "measurement_capabilities",
        "measurement_inspect_dataset",
        "measurement_read_source",
        "measurement_get_analysis",
        "measurement_read_analysis_data",
    }
)
_WRITE_TOOLS = frozenset(
    {
        "measurement_register_csv",
        "measurement_analyze_chromatogram",
        "measurement_analyze_sec",
    }
)
_SOURCE_PAGE_SIZE = 1000
_MAX_SOURCE_PAGES = 8
_DISTRIBUTION_TOLERANCE = 1e-8
_GENERIC_FAILURE = "Pilot numerical qualification could not be completed"
_CALL_FAILURE = "Pilot numerical qualification call failed"
_CAPABILITY_FAILURE = "Required numerical capabilities are unavailable"
_SCOPE = (
    "Synthetic numerical smoke check of the installed measure and measure.sec adapters; "
    "no instrument, method, or deployment qualification."
)


class PilotCheckError(ValueError):
    """A fixed-message failure from the bounded pilot numerical check."""


def _fail(message: str = _GENERIC_FAILURE) -> None:
    raise PilotCheckError(message)


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail()
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        _fail()
    return value


def _integer(value: Any) -> int:
    if type(value) is not int or value < 0:
        _fail()
    return value


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail()
    result = float(value)
    if not math.isfinite(result):
        _fail()
    return result


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_md5(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return _sha256(encoded)


def _safe_read_response(tool: str, response: dict[str, Any]) -> dict[str, Any]:
    """Return the complete structured response from an allowlisted read tool."""
    del tool
    return response


async def _read(
    client: Any,
    tool: str,
    arguments: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    if tool not in _READ_TOOLS:
        _fail()
    try:
        result = await client.call_tool(tool, arguments)
    except Exception:
        raise PilotCheckError(_CALL_FAILURE) from None
    if getattr(result, "isError", None) is not False:
        _fail(_CALL_FAILURE)
    structured = getattr(result, "structuredContent", None)
    if not isinstance(structured, dict):
        _fail(_CALL_FAILURE)
    records.append(
        {
            "tool": tool,
            "arguments": dict(arguments),
            "response": _safe_read_response(tool, structured),
        }
    )
    return structured


async def _write(client: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool not in _WRITE_TOOLS:
        _fail()
    try:
        result = await client.call_tool(tool, arguments)
    except Exception:
        raise PilotCheckError(_CALL_FAILURE) from None
    if getattr(result, "isError", None) is not False:
        _fail(_CALL_FAILURE)
    structured = getattr(result, "structuredContent", None)
    if not isinstance(structured, dict):
        _fail(_CALL_FAILURE)
    return structured


def _source_text(xs: list[float], ys: list[float]) -> str:
    rows = ["time,response\n"]
    rows.extend(f"{x:.17g},{y:.17g}\n" for x, y in zip(xs, ys, strict=True))
    return "".join(rows)


def _expected_sec_moments(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Compute SEC mass moments independently with composite trapezoid weights."""
    quadrature_weights = [0.0] * len(xs)
    for index, (left, right) in enumerate(zip(xs, xs[1:])):
        half_width = (right - left) / 2
        quadrature_weights[index] += half_width
        quadrature_weights[index + 1] += half_width
    mass_weights = [signal * weight for signal, weight in zip(ys, quadrature_weights, strict=True)]
    molecular_weights = [10 ** (6 - 0.4 * x + 0.03 * x * x) for x in xs]
    total = sum(mass_weights)
    mw = (
        sum(weight * mass for weight, mass in zip(mass_weights, molecular_weights, strict=True))
        / total
    )
    mn = total / sum(
        weight / mass for weight, mass in zip(mass_weights, molecular_weights, strict=True)
    )
    return mn, mw


def _check_analysis_binding(
    record: dict[str, Any],
    *,
    analysis_id: str,
    dataset_id: str,
    trace_id: str,
    kind: str,
    input_sha256: str,
    source_sha256: str,
    backend: dict[str, Any],
    method: dict[str, Any],
) -> dict[str, Any]:
    if (
        record.get("analysis_id") != analysis_id
        or record.get("dataset_id") != dataset_id
        or record.get("trace_id") != trace_id
        or record.get("kind") != kind
        or record.get("method") != method
    ):
        _fail()
    provenance = _object(record.get("provenance"))
    if (
        provenance.get("input_sha256") != input_sha256
        or provenance.get("source_sha256") != source_sha256
        or provenance.get("method_sha256") != _canonical_sha256(method)
        or not _is_sha256(provenance.get("worker_sha256"))
        or provenance.get("packages") != backend.get("packages")
        or provenance.get("installed_r_code_md5") != backend.get("installed_r_code_md5")
        or provenance.get("r_version") != backend.get("r_version")
    ):
        _fail()
    warnings = record.get("warnings")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        _fail()
    return provenance


def _read_page_identity(page: dict[str, Any], *, analysis_id: str, kind: str, count: int) -> None:
    if (
        page.get("analysis_id") != analysis_id
        or page.get("kind") != kind
        or _integer(page.get("offset")) != 0
        or _integer(page.get("count")) != count
        or page.get("next_offset") is not None
    ):
        _fail()


async def _qualify_numerics(client: Any) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    capabilities = await _read(client, "measurement_capabilities", {}, records)
    techniques = _object(capabilities.get("techniques"))
    if techniques.get("generic_1d_trace") is not True or techniques.get("relative_sec") is not True:
        _fail(_CAPABILITY_FAILURE)

    backend = _object(capabilities.get("backend"))
    packages = _object(backend.get("packages"))
    fingerprints = _object(backend.get("installed_r_code_md5"))
    if (
        not all(
            isinstance(packages.get(name), str) and packages[name]
            for name in ("measure", "measure.sec", "recipes")
        )
        or not isinstance(backend.get("r_version"), str)
        or not backend["r_version"]
        or not _is_md5(fingerprints.get("measure"))
        or not _is_md5(fingerprints.get("measure.sec"))
    ):
        _fail(_CAPABILITY_FAILURE)

    trace_id = "pilot-trace-001"
    metadata = {
        "trace_id": trace_id,
        "sample_id": "pilot-synthetic-sample",
        "injection_id": "pilot-synthetic-injection",
        "channel": "ri",
        "separation": "SEC",
        "detector": "RI",
        "axis_unit": "min",
        "signal_unit": "a.u.",
        "sample_type": "reference",
    }
    xs = [1 + index * 0.02 for index in range(101)]
    ys = [math.exp(-0.5 * ((x - 2) / 0.16) ** 2) for x in xs]
    csv_text = _source_text(xs, ys)
    source_sha256 = _sha256(csv_text)
    registration = await _write(
        client,
        "measurement_register_csv",
        {
            "title": "Synthetic pilot SEC trace",
            "csv_text": csv_text,
            "axis_column": "time",
            "signal_column": "response",
            "metadata": metadata,
            "delimiter": ",",
        },
    )
    dataset_id = _text(registration.get("dataset_id"))
    input_sha256 = registration.get("input_sha256")
    if (
        not _is_sha256(input_sha256)
        or registration.get("source_sha256") != source_sha256
        or _integer(registration.get("point_count")) != 101
    ):
        _fail()

    inspected = await _read(
        client, "measurement_inspect_dataset", {"dataset_id": dataset_id}, records
    )
    if (
        inspected.get("dataset_id") != dataset_id
        or inspected.get("input_sha256") != input_sha256
        or inspected.get("title") != "Synthetic pilot SEC trace"
    ):
        _fail()
    traces = inspected.get("traces")
    if not isinstance(traces, list) or len(traces) != 1:
        _fail()
    trace = _object(traces[0])
    if any(trace.get(name) != value for name, value in metadata.items()):
        _fail()
    axis_range = trace.get("axis_range")
    if (
        _integer(trace.get("point_count")) != 101
        or not isinstance(axis_range, list)
        or len(axis_range) != 2
        or [_finite_number(value) for value in axis_range] != [1.0, 3.0]
    ):
        _fail()
    source_info = _object(inspected.get("source"))
    expected_mapping = {
        "delimiter": ",",
        "axis_column": "time",
        "signal_columns": [{"column": "response", "trace_id": trace_id}],
    }
    if (
        source_info.get("source_sha256") != source_sha256
        or source_info.get("mapping") != expected_mapping
        or source_info.get("kind") != "delimited_text"
    ):
        _fail()

    retained_parts: list[str] = []
    offset = 0
    for _ in range(_MAX_SOURCE_PAGES):
        page = await _read(
            client,
            "measurement_read_source",
            {"dataset_id": dataset_id, "offset": offset, "limit": _SOURCE_PAGE_SIZE},
            records,
        )
        count = _integer(page.get("count"))
        part = page.get("text")
        if (
            page.get("dataset_id") != dataset_id
            or page.get("source_sha256") != source_sha256
            or count != len(csv_text)
            or _integer(page.get("offset")) != offset
            or not isinstance(part, str)
            or len(part) != min(_SOURCE_PAGE_SIZE, count - offset)
        ):
            _fail()
        retained_parts.append(part)
        next_offset = offset + _SOURCE_PAGE_SIZE
        expected_next = next_offset if next_offset < count else None
        if page.get("next_offset") != expected_next:
            _fail()
        if expected_next is None:
            break
        offset = next_offset
    else:
        _fail()
    retained_source = "".join(retained_parts)
    if retained_source != csv_text or _sha256(retained_source) != source_sha256:
        _fail()

    chromatogram_receipt = await _write(
        client,
        "measurement_analyze_chromatogram",
        {"dataset_id": dataset_id, "trace_id": trace_id, "method": {}},
    )
    chromatogram_id = _text(chromatogram_receipt.get("analysis_id"))
    if (
        chromatogram_receipt.get("dataset_id") != dataset_id
        or chromatogram_receipt.get("trace_id") != trace_id
        or chromatogram_receipt.get("kind") != "chromatogram"
    ):
        _fail()

    peak_page = await _read(
        client,
        "measurement_read_analysis_data",
        {"analysis_id": chromatogram_id, "kind": "peaks", "offset": 0, "limit": 10},
        records,
    )
    _read_page_identity(peak_page, analysis_id=chromatogram_id, kind="peaks", count=1)
    peaks = peak_page.get("data")
    if not isinstance(peaks, list) or len(peaks) != 1:
        _fail()
    peak = _object(peaks[0])
    peak_center = _finite_number(peak.get("location"))
    peak_area = _finite_number(peak.get("area"))
    analytic_area = math.sqrt(2 * math.pi) * 0.16
    if not math.isclose(peak_center, 2.0, rel_tol=0.0, abs_tol=1e-8):
        _fail()
    if not math.isclose(peak_area, analytic_area, rel_tol=0.001, abs_tol=0.0):
        _fail()

    chromatogram = await _read(
        client,
        "measurement_get_analysis",
        {"analysis_id": chromatogram_id},
        records,
    )
    chromatogram_method = {
        "baseline": "none",
        "baseline_lambda": 1e6,
        "baseline_p": 0.01,
        "smoothing": "none",
        "smoothing_sigma": 1.0,
        "min_height": 0.0,
        "min_prominence": 0.0,
        "min_distance": 0.0,
        "integration_baseline": "local",
    }
    _check_analysis_binding(
        chromatogram,
        analysis_id=chromatogram_id,
        dataset_id=dataset_id,
        trace_id=trace_id,
        kind="chromatogram",
        input_sha256=input_sha256,
        source_sha256=source_sha256,
        backend=backend,
        method=chromatogram_method,
    )
    chrom_summary = _object(chromatogram.get("summary"))
    if _integer(chrom_summary.get("peak_count")) != 1:
        _fail()

    standards = [
        {
            "location": float(x),
            "molecular_weight_g_mol": 10 ** (6 - 0.4 * x + 0.03 * x * x),
        }
        for x in (1, 1.5, 2, 2.5, 3)
    ]
    sec_method = {
        "standards": standards,
        "fit_type": "quadratic",
        "standard_description": "Synthetic quadratic reference standards",
        "baseline_corrected": True,
        "signal_proportional_to_weight_concentration": True,
    }
    sec_receipt = await _write(
        client,
        "measurement_analyze_sec",
        {"dataset_id": dataset_id, "trace_id": trace_id, "method": sec_method},
    )
    sec_id = _text(sec_receipt.get("analysis_id"))
    if (
        sec_receipt.get("dataset_id") != dataset_id
        or sec_receipt.get("trace_id") != trace_id
        or sec_receipt.get("kind") != "sec"
        or sec_id == chromatogram_id
    ):
        _fail()

    sec_page = await _read(
        client,
        "measurement_read_analysis_data",
        {"analysis_id": sec_id, "kind": "distribution", "offset": 0, "limit": 1000},
        records,
    )
    _read_page_identity(sec_page, analysis_id=sec_id, kind="distribution", count=101)
    distribution = _object(sec_page.get("data"))
    log10_mw = distribution.get("log10_mw")
    differential = distribution.get("differential")
    cumulative = distribution.get("cumulative")
    if not all(
        isinstance(values, list) and len(values) == 101
        for values in (log10_mw, differential, cumulative)
    ):
        _fail()
    log10_mw = [_finite_number(value) for value in log10_mw]
    differential = [_finite_number(value) for value in differential]
    cumulative = [_finite_number(value) for value in cumulative]
    if any(right <= left for left, right in zip(log10_mw, log10_mw[1:])):
        _fail()
    reference_log10_mw = [6 - 0.4 * x + 0.03 * x * x for x in xs]
    minimum_log10_mw = min(reference_log10_mw)
    maximum_log10_mw = max(reference_log10_mw)
    expected_log10_axis = [
        minimum_log10_mw + index * (maximum_log10_mw - minimum_log10_mw) / 100
        for index in range(101)
    ]
    if any(
        not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-8)
        for actual, expected in zip(log10_mw, expected_log10_axis, strict=True)
    ):
        _fail()
    if any(value < 0 for value in differential):
        _fail()
    if any(
        value < -_DISTRIBUTION_TOLERANCE or value > 1 + _DISTRIBUTION_TOLERANCE
        for value in cumulative
    ) or any(
        next_value < value - _DISTRIBUTION_TOLERANCE
        for value, next_value in zip(cumulative, cumulative[1:])
    ):
        _fail()
    integral = sum(
        (left + right) / 2 * (next_x - x)
        for x, next_x, left, right in zip(log10_mw, log10_mw[1:], differential, differential[1:])
    )
    if not math.isfinite(integral) or not math.isclose(integral, 1.0, rel_tol=1e-5, abs_tol=0.0):
        _fail()
    cumulative_start, cumulative_end = cumulative[0], cumulative[-1]
    if not math.isclose(cumulative_start, 0.0, rel_tol=0.0, abs_tol=1e-5) or not math.isclose(
        cumulative_end, 1.0, rel_tol=0.0, abs_tol=1e-5
    ):
        _fail()

    sec = await _read(client, "measurement_get_analysis", {"analysis_id": sec_id}, records)
    normalized_sec_method = sec_method | {
        "extrapolation": "none",
        "integration_range": None,
    }
    _check_analysis_binding(
        sec,
        analysis_id=sec_id,
        dataset_id=dataset_id,
        trace_id=trace_id,
        kind="sec",
        input_sha256=input_sha256,
        source_sha256=source_sha256,
        backend=backend,
        method=normalized_sec_method,
    )
    sec_summary = _object(sec.get("summary"))
    observed_mn = _finite_number(sec_summary.get("mn_g_mol"))
    observed_mw = _finite_number(sec_summary.get("mw_g_mol"))
    observed_dispersity = _finite_number(sec_summary.get("dispersity"))
    expected_mn, expected_mw = _expected_sec_moments(xs, ys)
    expected_dispersity = expected_mw / expected_mn
    if not math.isclose(observed_mn, expected_mn, rel_tol=1e-8, abs_tol=0.0):
        _fail()
    if not math.isclose(observed_mw, expected_mw, rel_tol=1e-8, abs_tol=0.0):
        _fail()
    if not math.isclose(observed_dispersity, expected_dispersity, rel_tol=1e-8, abs_tol=0.0):
        _fail()

    return {
        "profile": PROFILE,
        "checks": {
            "numerical_capabilities": True,
            "source_text_retained": True,
            "source_digest_retained": True,
            "analysis_provenance_bound": True,
            "distribution_log10_mw_axis": True,
            "chromatogram_one_peak": True,
            "chromatogram_center": True,
            "chromatogram_area": True,
            "sec_mn": True,
            "sec_mw": True,
            "sec_dispersity": True,
            "distribution_integral": True,
            "distribution_cumulative_endpoints": True,
        },
        "dataset_id": dataset_id,
        "trace_id": trace_id,
        "analysis_ids": {"chromatogram": chromatogram_id, "sec": sec_id},
        "records": records,
        "report": {
            "scope": _SCOPE,
            "chromatogram": {
                "observed_peak_center": peak_center,
                "expected_peak_center": 2.0,
                "observed_area": peak_area,
                "analytic_area": analytic_area,
                "relative_area_error": abs(peak_area - analytic_area) / analytic_area,
            },
            "sec": {
                "observed_mn_g_mol": observed_mn,
                "independent_mn_g_mol": expected_mn,
                "relative_mn_error": abs(observed_mn - expected_mn) / expected_mn,
                "observed_mw_g_mol": observed_mw,
                "independent_mw_g_mol": expected_mw,
                "relative_mw_error": abs(observed_mw - expected_mw) / expected_mw,
                "observed_dispersity": observed_dispersity,
                "independent_dispersity": expected_dispersity,
                "relative_dispersity_error": abs(observed_dispersity - expected_dispersity)
                / expected_dispersity,
            },
            "distribution": {
                "integral": integral,
                "cumulative_start": cumulative_start,
                "cumulative_end": cumulative_end,
            },
        },
    }


async def qualify_numerics(client: Any) -> dict[str, Any]:
    """Import one deterministic synthetic CSV and qualify scalar/SEC numerics via MCP.

    ``client`` must be an initialized MCP ``ClientSession`` or a compatible async
    client exposing ``call_tool``. No server code or transport implementation is
    imported here. Write operations are limited to the synthetic CSV and its two
    analyses; every returned call record is from the fixed read-only allowlist.
    """
    try:
        return await _qualify_numerics(client)
    except PilotCheckError:
        raise
    except Exception:
        raise PilotCheckError(_GENERIC_FAILURE) from None
