"""Bounded two-account synthetic ownership and replay checks over MCP sessions."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from typing import Any

from mcp import ClientSession

from .bounded_json import preflight_json

PROFILE = "measure-service-two-account-pilot/1"
CHECKPOINT_MAX_BYTES = 2_000_000
SNAPSHOT_MAX_BYTES = 500_000
SOURCE_PAGE_SIZE = 1000
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_EXPECTED = "Dataset was not found or is not accessible"
_REGISTRY_EXPECTED = "Registry record was not found or is not accessible"
_LINK_DATASET_EXPECTED = "Measurement dataset was not found or is not accessible"
_LINK_ANALYSIS_EXPECTED = "Result analysis was not found or is not accessible"
_CALL_FAILURE = "Pilot check could not be completed"
_CHECKPOINT_FAILURE = "Invalid pilot checkpoint"
_ACCOUNT_KEYS = frozenset({"a", "b"})
_NUMERIC_CHECKS = frozenset(
    {
        "numerical_capabilities",
        "source_text_retained",
        "source_digest_retained",
        "analysis_provenance_bound",
        "chromatogram_one_peak",
        "chromatogram_center",
        "chromatogram_area",
        "sec_mn",
        "sec_mw",
        "sec_dispersity",
        "distribution_log10_mw_axis",
        "distribution_integral",
        "distribution_cumulative_endpoints",
    }
)
_NUMERIC_READ_TOOLS = frozenset(
    {
        "measurement_capabilities",
        "measurement_inspect_dataset",
        "measurement_read_source",
        "measurement_get_analysis",
        "measurement_read_analysis_data",
    }
)


class PilotCheckError(RuntimeError):
    """A sanitized failure to complete the synthetic pilot checks."""


def _reject_checkpoint(_message: str) -> ValueError:
    return ValueError(_CHECKPOINT_FAILURE)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _same(left: Any, right: Any) -> bool:
    try:
        return _canonical(left) == _canonical(right)
    except (TypeError, ValueError):
        return False


def _exact_keys(value: Any, keys: set[str] | frozenset[str]) -> bool:
    return type(value) is dict and set(value) == set(keys)


def _sha256(value: Any) -> bool:
    return type(value) is str and _HEX_64.fullmatch(value) is not None


def _record_id(value: Any) -> bool:
    if type(value) is not str or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _timestamp(value: Any) -> bool:
    return type(value) is str and 1 <= len(value) <= 100 and value.endswith("+00:00")


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _experiment_spec(run_id: str) -> dict[str, Any]:
    return {
        "title": f"Synthetic ownership pilot {run_id}",
        "source": "synthetic-pilot",
        "external_reference": None,
        "proposal_reference": None,
        "intended_conditions": [],
        "description": "Fixed synthetic ownership and exact-replay check.",
    }


def _experiment_key(run_id: str) -> str:
    return f"pilot-{run_id}-experiment"


def _attempt_key(run_id: str, account: str) -> str:
    return f"pilot-{run_id}-{account}-attempt"


def _results_key(run_id: str, account: str) -> str:
    return f"pilot-{run_id}-{account}-results"


def _dataset_title(run_id: str, account: str, numerical: bool) -> str:
    return (
        "Synthetic pilot SEC trace" if numerical else f"Synthetic ownership data {run_id}-{account}"
    )


def _trace_id(run_id: str, account: str, numerical: bool) -> str:
    return "pilot-trace-001" if numerical else f"pilot-{account}-{run_id[:8]}"


def _csv_dataset_arguments(run_id: str, account: str) -> dict[str, Any]:
    trace_id = _trace_id(run_id, account, False)
    metadata = {
        "trace_id": trace_id,
        "sample_id": f"pilot-sample-{account}-{run_id[:8]}",
        "injection_id": f"pilot-injection-{account}-{run_id[:8]}",
        "channel": "ri",
        "separation": "SEC",
        "detector": "RI",
        "axis_unit": "min",
        "signal_unit": "a.u.",
        "sample_type": "unknown",
    }
    return {
        "title": _dataset_title(run_id, account, False),
        "csv_text": "time,response\n1,0\n2,1\n3,0\n",
        "axis_column": "time",
        "signal_column": "response",
        "metadata": metadata,
        "delimiter": ",",
    }


def _numerical_source_text() -> str:
    rows = ["time,response\n"]
    for index in range(101):
        x = 1 + index * 0.02
        y = math.exp(-0.5 * ((x - 2) / 0.16) ** 2)
        rows.append(f"{x:.17g},{y:.17g}\n")
    return "".join(rows)


def _result_specs() -> list[dict[str, Any]]:
    return [
        {"metric": "synthetic_zero", "status": "observed", "unit": "1", "value": 0.0},
        {
            "metric": "synthetic_below_limit",
            "status": "below_limit",
            "unit": "mg/L",
            "upper_bound": 0.1,
            "explanation": "Synthetic below-limit pilot record.",
        },
    ]


def _expected_attempt_spec(account: dict[str, Any]) -> dict[str, Any]:
    trace = account["expected"]["dataset_inspection"]["traces"][0]
    metadata_keys = {
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
    return {
        "status": "completed",
        "evaluator_kind": "simulator",
        "evaluator_reference": "synthetic-pilot",
        "evaluator_revision": None,
        "training_snapshot_reference": None,
        "fidelity": None,
        "actual_conditions": [],
        "measurements": [
            {
                "dataset_id": account["dataset_id"],
                "trace_id": account["trace_id"],
                "input_sha256": account["expected"]["dataset_inspection"]["input_sha256"],
                "metadata": {key: value for key, value in trace.items() if key in metadata_keys},
            }
        ],
        "started_at": None,
        "ended_at": None,
        "external_reference": None,
        "instrument_reference": None,
        "method_reference": None,
        "replicate_group": None,
        "replicate_kind": None,
        "replicate_index": None,
        "explanation": "",
    }


def _check_numeric_evidence(value: Any, account: dict[str, Any]) -> bool:
    keys = {"profile", "checks", "dataset_id", "trace_id", "analysis_ids", "records", "report"}
    if not _exact_keys(value, keys) or value.get("profile") != "measure-pilot-numerics/1":
        return False
    if (
        value.get("dataset_id") != account["dataset_id"]
        or value.get("trace_id") != account["trace_id"]
    ):
        return False
    checks = value.get("checks")
    if not _exact_keys(checks, _NUMERIC_CHECKS) or any(
        type(item) is not bool or not item for item in checks.values()
    ):
        return False
    analysis_ids = value.get("analysis_ids")
    if not _exact_keys(analysis_ids, {"chromatogram", "sec"}):
        return False
    if (
        not all(_record_id(item) for item in analysis_ids.values())
        or len(set(analysis_ids.values())) != 2
    ):
        return False
    chromatogram_id = analysis_ids["chromatogram"]
    sec_id = analysis_ids["sec"]
    report = value.get("report")
    if not _exact_keys(report, {"scope", "chromatogram", "sec", "distribution"}):
        return False
    if report.get("scope") != (
        "Synthetic numerical smoke check of the installed measure and measure.sec adapters; "
        "no instrument, method, or deployment qualification."
    ):
        return False
    report_fields = {
        "chromatogram": {
            "observed_peak_center",
            "expected_peak_center",
            "observed_area",
            "analytic_area",
            "relative_area_error",
        },
        "sec": {
            "observed_mn_g_mol",
            "independent_mn_g_mol",
            "relative_mn_error",
            "observed_mw_g_mol",
            "independent_mw_g_mol",
            "relative_mw_error",
            "observed_dispersity",
            "independent_dispersity",
            "relative_dispersity_error",
        },
        "distribution": {"integral", "cumulative_start", "cumulative_end"},
    }
    for name, fields in report_fields.items():
        section = report.get(name)
        if not _exact_keys(section, fields) or any(
            not _finite_number(item) for item in section.values()
        ):
            return False

    source_text = _numerical_source_text()
    source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    source_page_count = math.ceil(len(source_text) / SOURCE_PAGE_SIZE)
    calls: list[tuple[str, dict[str, Any]]] = [
        ("measurement_capabilities", {}),
        ("measurement_inspect_dataset", {"dataset_id": account["dataset_id"]}),
    ]
    calls.extend(
        (
            "measurement_read_source",
            {"dataset_id": account["dataset_id"], "offset": offset, "limit": SOURCE_PAGE_SIZE},
        )
        for offset in range(0, len(source_text), SOURCE_PAGE_SIZE)
    )
    calls.extend(
        [
            (
                "measurement_read_analysis_data",
                {"analysis_id": chromatogram_id, "kind": "peaks", "offset": 0, "limit": 10},
            ),
            ("measurement_get_analysis", {"analysis_id": chromatogram_id}),
            (
                "measurement_read_analysis_data",
                {"analysis_id": sec_id, "kind": "distribution", "offset": 0, "limit": 1000},
            ),
            ("measurement_get_analysis", {"analysis_id": sec_id}),
        ]
    )
    records = value.get("records")
    if type(records) is not list or len(records) != len(calls):
        return False
    responses: list[dict[str, Any]] = []
    for entry, (expected_tool, expected_arguments) in zip(records, calls, strict=True):
        if (
            not _exact_keys(entry, {"tool", "arguments", "response"})
            or entry.get("tool") != expected_tool
            or not _same(entry.get("arguments"), expected_arguments)
            or type(entry.get("response")) is not dict
        ):
            return False
        responses.append(entry["response"])

    capabilities, inspection = responses[:2]
    if type(capabilities.get("techniques")) is not dict:
        return False
    if (
        capabilities["techniques"].get("generic_1d_trace") is not True
        or capabilities["techniques"].get("relative_sec") is not True
    ):
        return False
    backend = capabilities.get("backend")
    if type(backend) is not dict:
        return False
    packages = backend.get("packages")
    fingerprints = backend.get("installed_r_code_md5")
    if (
        type(packages) is not dict
        or any(
            type(packages.get(name)) is not str or not packages[name]
            for name in ("measure", "measure.sec", "recipes")
        )
        or type(backend.get("r_version")) is not str
        or not backend["r_version"]
        or type(fingerprints) is not dict
        or set(fingerprints) != {"measure", "measure.sec"}
        or any(
            type(item) is not str or re.fullmatch(r"[0-9a-f]{32}", item) is None
            for item in fingerprints.values()
        )
    ):
        return False
    if (
        inspection.get("dataset_id") != account["dataset_id"]
        or inspection.get("title") != "Synthetic pilot SEC trace"
        or not _sha256(inspection.get("input_sha256"))
        or type(inspection.get("traces")) is not list
        or len(inspection["traces"]) != 1
    ):
        return False
    trace = inspection["traces"][0]
    source_info = inspection.get("source")
    expected_mapping = {
        "delimiter": ",",
        "axis_column": "time",
        "signal_columns": [{"column": "response", "trace_id": account["trace_id"]}],
    }
    if (
        trace.get("trace_id") != account["trace_id"]
        or trace.get("sample_id") != "pilot-synthetic-sample"
        or trace.get("injection_id") != "pilot-synthetic-injection"
        or trace.get("channel") != "ri"
        or trace.get("separation") != "SEC"
        or trace.get("detector") != "RI"
        or trace.get("axis_unit") != "min"
        or trace.get("signal_unit") != "a.u."
        or trace.get("point_count") != 101
        or trace.get("axis_range") != [1, 3]
        or not _exact_keys(source_info, {"kind", "source_sha256", "mapping"})
        or source_info.get("kind") != "delimited_text"
        or source_info.get("source_sha256") != source_digest
        or source_info.get("mapping") != expected_mapping
    ):
        return False
    source_pages = responses[2 : 2 + source_page_count]
    if "".join(page.get("text", "") for page in source_pages) != source_text:
        return False
    if any(
        page.get("source_sha256") != source_digest
        or page.get("offset") != index * SOURCE_PAGE_SIZE
        or page.get("count") != len(source_text)
        or page.get("next_offset")
        != (
            (index + 1) * SOURCE_PAGE_SIZE
            if (index + 1) * SOURCE_PAGE_SIZE < len(source_text)
            else None
        )
        for index, page in enumerate(source_pages)
    ):
        return False

    peaks, chrom_record, distribution_page, sec_record = responses[2 + source_page_count :]
    peak_data = peaks.get("data")
    if (
        peaks.get("analysis_id") != chromatogram_id
        or peaks.get("kind") != "peaks"
        or peaks.get("count") != 1
        or peaks.get("next_offset") is not None
        or type(peak_data) is not list
        or len(peak_data) != 1
    ):
        return False
    peak = peak_data[0]
    center, area = peak.get("location"), peak.get("area")
    analytic_area = math.sqrt(2 * math.pi) * 0.16
    if (
        not _finite_number(center)
        or not math.isclose(center, 2.0, rel_tol=0.0, abs_tol=1e-8)
        or not _finite_number(area)
        or not math.isclose(area, analytic_area, rel_tol=0.001, abs_tol=0.0)
        or chrom_record.get("analysis_id") != chromatogram_id
        or chrom_record.get("dataset_id") != account["dataset_id"]
        or chrom_record.get("trace_id") != account["trace_id"]
        or chrom_record.get("kind") != "chromatogram"
        or type(chrom_record.get("warnings")) is not list
    ):
        return False
    sec_method = {
        "standards": [
            {"location": float(x), "molecular_weight_g_mol": 10 ** (6 - 0.4 * x + 0.03 * x * x)}
            for x in (1, 1.5, 2, 2.5, 3)
        ],
        "fit_type": "quadratic",
        "extrapolation": "none",
        "integration_range": None,
        "standard_description": "Synthetic quadratic reference standards",
        "baseline_corrected": True,
        "signal_proportional_to_weight_concentration": True,
    }
    chrom_method = {
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
    for record, analysis_id, kind, method in (
        (chrom_record, chromatogram_id, "chromatogram", chrom_method),
        (sec_record, sec_id, "sec", sec_method),
    ):
        provenance = record.get("provenance")
        if (
            record.get("analysis_id") != analysis_id
            or record.get("dataset_id") != account["dataset_id"]
            or record.get("trace_id") != account["trace_id"]
            or record.get("kind") != kind
            or not _same(record.get("method"), method)
            or type(provenance) is not dict
            or provenance.get("input_sha256") != inspection["input_sha256"]
            or provenance.get("source_sha256") != source_digest
            or provenance.get("method_sha256")
            != hashlib.sha256(_canonical(method).encode()).hexdigest()
            or type(provenance.get("worker_sha256")) is not str
            or not _sha256(provenance.get("worker_sha256"))
            or provenance.get("packages") != packages
            or provenance.get("installed_r_code_md5") != fingerprints
            or provenance.get("r_version") != backend["r_version"]
        ):
            return False
    chrom_summary = chrom_record.get("summary")
    sec_summary = sec_record.get("summary")
    if (
        type(chrom_summary) is not dict
        or chrom_summary.get("peak_count") != 1
        or type(sec_summary) is not dict
        or not all(
            _finite_number(sec_summary.get(field))
            for field in ("mn_g_mol", "mw_g_mol", "dispersity")
        )
    ):
        return False
    x_values = [1 + index * 0.02 for index in range(101)]
    y_values = [math.exp(-0.5 * ((x - 2) / 0.16) ** 2) for x in x_values]
    weights = [0.0] * len(x_values)
    for index, (left, right) in enumerate(zip(x_values, x_values[1:])):
        half_width = (right - left) / 2
        weights[index] += half_width
        weights[index + 1] += half_width
    mass_weights = [y * weight for y, weight in zip(y_values, weights, strict=True)]
    molecular_weights = [10 ** (6 - 0.4 * x + 0.03 * x * x) for x in x_values]
    total = sum(mass_weights)
    expected_mw = sum(w * mw for w, mw in zip(mass_weights, molecular_weights, strict=True)) / total
    expected_mn = total / sum(w / mw for w, mw in zip(mass_weights, molecular_weights, strict=True))
    expected_dispersity = expected_mw / expected_mn
    if not all(
        math.isclose(actual, expected, rel_tol=1e-8, abs_tol=0.0)
        for actual, expected in (
            (sec_summary["mn_g_mol"], expected_mn),
            (sec_summary["mw_g_mol"], expected_mw),
            (sec_summary["dispersity"], expected_dispersity),
        )
    ):
        return False
    dist = distribution_page.get("data")
    if (
        distribution_page.get("analysis_id") != sec_id
        or distribution_page.get("kind") != "distribution"
        or distribution_page.get("count") != 101
        or distribution_page.get("next_offset") is not None
        or not _exact_keys(dist, {"log10_mw", "differential", "cumulative"})
        or any(type(dist[field]) is not list or len(dist[field]) != 101 for field in dist)
        or any(not _finite_number(item) for values in dist.values() for item in values)
    ):
        return False
    log_axis = dist["log10_mw"]
    differential = dist["differential"]
    cumulative = dist["cumulative"]
    reference_axis = [6 - 0.4 * x + 0.03 * x * x for x in x_values]
    lower, upper = min(reference_axis), max(reference_axis)
    expected_axis = [lower + index * (upper - lower) / 100 for index in range(101)]
    if (
        any(
            not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-8)
            for actual, expected in zip(log_axis, expected_axis, strict=True)
        )
        or any(right <= left for left, right in zip(log_axis, log_axis[1:]))
        or any(item < 0 for item in differential)
        or any(right < left - 1e-7 for left, right in zip(cumulative, cumulative[1:]))
        or not math.isclose(cumulative[0], 0.0, rel_tol=0.0, abs_tol=1e-5)
        or not math.isclose(cumulative[-1], 1.0, rel_tol=0.0, abs_tol=1e-5)
    ):
        return False
    integral = sum(
        (left + right) / 2 * (next_x - x)
        for x, next_x, left, right in zip(
            log_axis[:-1], log_axis[1:], differential[:-1], differential[1:], strict=True
        )
    )
    if not math.isclose(integral, 1.0, rel_tol=1e-5, abs_tol=0.0):
        return False
    return all(
        math.isclose(actual, expected, rel_tol=1e-8, abs_tol=0.0)
        for actual, expected in (
            (report["chromatogram"]["observed_peak_center"], center),
            (report["chromatogram"]["expected_peak_center"], 2.0),
            (report["chromatogram"]["observed_area"], area),
            (report["chromatogram"]["analytic_area"], analytic_area),
            (report["sec"]["observed_mn_g_mol"], sec_summary["mn_g_mol"]),
            (report["sec"]["observed_mw_g_mol"], sec_summary["mw_g_mol"]),
            (report["sec"]["observed_dispersity"], sec_summary["dispersity"]),
            (report["distribution"]["integral"], integral),
            (report["distribution"]["cumulative_start"], cumulative[0]),
            (report["distribution"]["cumulative_end"], cumulative[-1]),
        )
    )


def _validate_snapshot(value: Any, keys: set[str], *, record_id: str | None = None) -> bool:
    if not _exact_keys(value, keys):
        return False
    if record_id is not None:
        id_keys = {"experiment_id", "dataset_id", "attempt_id", "result_id"}
        for key in id_keys & keys:
            if key in value and value[key] != record_id:
                return False
    for key in ("input_sha256", "source_sha256"):
        if key in value and value[key] is not None and not _sha256(value[key]):
            return False
    if "created_at" in value and not _timestamp(value["created_at"]):
        return False
    return True


def validate_checkpoint(checkpoint: Any) -> dict[str, Any]:
    """Validate a private pilot checkpoint before any MCP call is made."""
    try:
        if type(checkpoint) is not dict:
            raise _reject_checkpoint("type")
        preflight_json(
            checkpoint,
            "pilot checkpoint",
            max_bytes=CHECKPOINT_MAX_BYTES,
            fail=_reject_checkpoint,
        )
        if not _exact_keys(
            checkpoint,
            {"profile", "schema_version", "run_id", "numerical", "accounts", "numeric"},
        ):
            raise _reject_checkpoint("keys")
        if (
            checkpoint["profile"] != PROFILE
            or type(checkpoint["schema_version"]) is not int
            or checkpoint["schema_version"] != 1
            or type(checkpoint["run_id"]) is not str
            or _RUN_ID.fullmatch(checkpoint["run_id"]) is None
            or type(checkpoint["numerical"]) is not bool
            or not _exact_keys(checkpoint["accounts"], _ACCOUNT_KEYS)
        ):
            raise _reject_checkpoint("header")
        if (checkpoint["numeric"] is None) != (checkpoint["numerical"] is False):
            raise _reject_checkpoint("numeric profile")
        ids: list[str] = []
        run_id = checkpoint["run_id"]
        expected_keys = {
            "dataset_inspection",
            "source_pages",
            "experiment",
            "attempt",
            "results",
            "observed_result",
            "below_limit_result",
            "attempt_list",
            "result_list",
            "processed_trace",
            "chromatogram_evidence",
        }
        for account_name in ("a", "b"):
            account = checkpoint["accounts"][account_name]
            if not _exact_keys(
                account,
                {"experiment_id", "dataset_id", "trace_id", "attempt_id", "result_ids", "expected"},
            ):
                raise _reject_checkpoint("account")
            if not all(
                _record_id(account[name]) for name in ("experiment_id", "dataset_id", "attempt_id")
            ):
                raise _reject_checkpoint("account ids")
            if type(account["trace_id"]) is not str or not 1 <= len(account["trace_id"]) <= 100:
                raise _reject_checkpoint("trace id")
            if not _exact_keys(account["result_ids"], {"observed", "below_limit"}) or not all(
                _record_id(item) for item in account["result_ids"].values()
            ):
                raise _reject_checkpoint("result ids")
            if not _exact_keys(account["expected"], expected_keys):
                raise _reject_checkpoint("expected records")
            ids.extend(
                [account["experiment_id"], account["dataset_id"], account["attempt_id"]]
                + list(account["result_ids"].values())
            )
            expected = account["expected"]
            inspection = expected["dataset_inspection"]
            if (
                not _exact_keys(
                    inspection, {"dataset_id", "input_sha256", "title", "traces", "source"}
                )
                or inspection.get("dataset_id") != account["dataset_id"]
                or not _sha256(inspection.get("input_sha256"))
                or type(inspection.get("title")) is not str
                or not 1 <= len(inspection["title"]) <= 200
                or inspection.get("title")
                != _dataset_title(run_id, account_name, checkpoint["numerical"])
            ):
                raise _reject_checkpoint("dataset inspection")
            traces = inspection.get("traces")
            if type(traces) is not list or len(traces) != 1 or type(traces[0]) is not dict:
                raise _reject_checkpoint("dataset traces")
            trace = traces[0]
            expected_trace_id = _trace_id(run_id, account_name, checkpoint["numerical"])
            if checkpoint["numerical"]:
                expected_trace = {
                    "trace_id": expected_trace_id,
                    "sample_id": "pilot-synthetic-sample",
                    "injection_id": "pilot-synthetic-injection",
                    "channel": "ri",
                    "separation": "SEC",
                    "detector": "RI",
                    "axis_unit": "min",
                    "signal_unit": "a.u.",
                    "sample_type": "reference",
                    "batch_id": None,
                    "run_order": None,
                    "point_count": 101,
                    "axis_range": [1.0, 3.0],
                    "signal_range": [math.exp(-0.5 * (1 / 0.16) ** 2), 1.0],
                }
            else:
                metadata = _csv_dataset_arguments(run_id, account_name)["metadata"]
                expected_trace = {
                    **metadata,
                    "batch_id": None,
                    "run_order": None,
                    "point_count": 3,
                    "axis_range": [1.0, 3.0],
                    "signal_range": [0.0, 1.0],
                }
            if not _same(trace, expected_trace) or account["trace_id"] != expected_trace_id:
                raise _reject_checkpoint("dataset trace")
            source_info = inspection.get("source")
            if (
                not _exact_keys(source_info, {"kind", "source_sha256", "mapping"})
                or source_info.get("kind") != "delimited_text"
                or not _sha256(source_info.get("source_sha256"))
                or source_info.get("mapping")
                != {
                    "delimiter": ",",
                    "axis_column": "time",
                    "signal_columns": [{"column": "response", "trace_id": account["trace_id"]}],
                }
            ):
                raise _reject_checkpoint("dataset source")
            pages = expected["source_pages"]
            if type(pages) is not list or not 1 <= len(pages) <= 16:
                raise _reject_checkpoint("source pages")
            parts: list[str] = []
            for index, page in enumerate(pages):
                if not _exact_keys(
                    page,
                    {
                        "dataset_id",
                        "kind",
                        "source_sha256",
                        "mapping",
                        "offset",
                        "count",
                        "next_offset",
                        "text",
                    },
                ):
                    raise _reject_checkpoint("source page")
                offset, count, text = page["offset"], page["count"], page["text"]
                expected_offset = index * SOURCE_PAGE_SIZE
                if (
                    page.get("dataset_id") != account["dataset_id"]
                    or page.get("kind") != "delimited_text"
                    or page.get("source_sha256") != source_info["source_sha256"]
                    or page.get("mapping") != source_info["mapping"]
                    or type(offset) is not int
                    or offset != expected_offset
                    or type(count) is not int
                    or not 1 <= count <= 16_000
                    or type(text) is not str
                    or len(text) > SOURCE_PAGE_SIZE
                    or len(text) != min(SOURCE_PAGE_SIZE, count - offset)
                    or page.get("next_offset")
                    != (offset + SOURCE_PAGE_SIZE if offset + SOURCE_PAGE_SIZE < count else None)
                ):
                    raise _reject_checkpoint("source page values")
                parts.append(text)
            if sum(len(part) for part in parts) != pages[0]["count"]:
                raise _reject_checkpoint("source page completeness")
            source_text = "".join(parts)
            expected_source = (
                _numerical_source_text()
                if checkpoint["numerical"]
                else _csv_dataset_arguments(run_id, account_name)["csv_text"]
            )
            if (
                source_text != expected_source
                or hashlib.sha256(source_text.encode("utf-8")).hexdigest()
                != source_info["source_sha256"]
            ):
                raise _reject_checkpoint("source content")
            exp = expected["experiment"]
            if (
                not _validate_snapshot(
                    exp,
                    {
                        "schema_version",
                        "experiment_id",
                        "spec",
                        "executable_method",
                        "observation_contract",
                        "observation_producer",
                        "input_sha256",
                        "created_at",
                    },
                    record_id=account["experiment_id"],
                )
                or type(exp["schema_version"]) is not str
                or exp["spec"] != _experiment_spec(run_id)
                or not _sha256(exp["input_sha256"])
                or not _timestamp(exp["created_at"])
            ):
                raise _reject_checkpoint("experiment snapshot")
            attempt = expected["attempt"]
            if (
                not _exact_keys(
                    attempt,
                    {
                        "schema_version",
                        "attempt_id",
                        "experiment_id",
                        "revision",
                        "latest_revision",
                        "spec",
                        "executable_method",
                        "input_sha256",
                        "reason",
                        "created_at",
                    },
                )
                or attempt.get("attempt_id") != account["attempt_id"]
                or attempt.get("experiment_id") != account["experiment_id"]
                or attempt.get("schema_version") != "1.0"
                or attempt.get("revision") != 1
                or attempt.get("latest_revision") != 1
                or attempt.get("executable_method") is not None
                or attempt.get("reason") != "Initial record"
                or not _sha256(attempt.get("input_sha256"))
                or not _timestamp(attempt.get("created_at"))
                or type(attempt.get("spec")) is not dict
                or attempt["spec"].get("status") != "completed"
                or not _same(attempt["spec"], _expected_attempt_spec(account))
                or type(attempt["spec"].get("measurements")) is not list
                or len(attempt["spec"]["measurements"]) != 1
                or attempt["spec"]["measurements"][0].get("dataset_id") != account["dataset_id"]
                or attempt["spec"]["measurements"][0].get("trace_id") != account["trace_id"]
            ):
                raise _reject_checkpoint("attempt snapshot")
            results = expected["results"]
            if (
                not _exact_keys(
                    results, {"schema_version", "attempt_id", "attempt_revision", "results"}
                )
                or results.get("schema_version") != "1.0"
                or results.get("attempt_id") != account["attempt_id"]
                or results.get("attempt_revision") != 1
                or type(results.get("results")) is not list
                or len(results["results"]) != 2
            ):
                raise _reject_checkpoint("results snapshot")
            if [item.get("result_id") for item in results["results"]] != [
                account["result_ids"]["observed"],
                account["result_ids"]["below_limit"],
            ]:
                raise _reject_checkpoint("result identity")
            if any(
                not _exact_keys(
                    item,
                    {
                        "schema_version",
                        "result_id",
                        "attempt_id",
                        "attempt_revision",
                        "outcome",
                        "superseded_by_result_id",
                        "input_sha256",
                        "created_at",
                    },
                )
                or item.get("schema_version") != "1.0"
                or item.get("attempt_id") != account["attempt_id"]
                or item.get("attempt_revision") != 1
                or item.get("superseded_by_result_id") is not None
                or not _sha256(item.get("input_sha256"))
                or not _timestamp(item.get("created_at"))
                for item in results["results"]
            ):
                raise _reject_checkpoint("result record")
            for result_kind in ("observed", "below_limit"):
                result_id = account["result_ids"][result_kind]
                result_record = expected[f"{result_kind}_result"]
                if (
                    not _exact_keys(
                        result_record,
                        {
                            "schema_version",
                            "result_id",
                            "attempt_id",
                            "attempt_revision",
                            "outcome",
                            "superseded_by_result_id",
                            "input_sha256",
                            "created_at",
                        },
                    )
                    or result_record.get("result_id") != result_id
                    or result_record.get("attempt_id") != account["attempt_id"]
                    or result_record.get("attempt_revision") != 1
                    or result_record.get("superseded_by_result_id") is not None
                    or not _sha256(result_record.get("input_sha256"))
                    or not _timestamp(result_record.get("created_at"))
                    or type(result_record.get("outcome")) is not dict
                    or result_record["outcome"].get("status") != result_kind
                ):
                    raise _reject_checkpoint("result snapshot")
                outcome = result_record["outcome"]
                if result_kind == "observed":
                    request = _result_specs()[0]
                elif result_kind == "below_limit":
                    request = _result_specs()[1]
                normalized_outcome = {
                    "metric": request["metric"],
                    "analyte_reference": None,
                    "status": request["status"],
                    "unit": request["unit"],
                    "value": request.get("value"),
                    "lower_bound": request.get("lower_bound"),
                    "upper_bound": request.get("upper_bound"),
                    "analysis_id": None,
                    "summary_field": None,
                    "supersedes_result_id": None,
                    "explanation": request.get("explanation", ""),
                    "provenance": {
                        "value_origin": "caller_reported",
                        "unit_origin": "caller_declared",
                        "attempt_sha256": attempt["input_sha256"],
                        "evaluator": {
                            "evaluator_kind": "simulator",
                            "evaluator_reference": "synthetic-pilot",
                            "evaluator_revision": None,
                            "training_snapshot_reference": None,
                            "fidelity": None,
                        },
                    },
                }
                if not _same(outcome, normalized_outcome):
                    raise _reject_checkpoint("result outcome")
            if not _same(
                results["results"],
                [expected["observed_result"], expected["below_limit_result"]],
            ):
                raise _reject_checkpoint("result getter mismatch")
            attempt_list = expected["attempt_list"]
            if (
                not _exact_keys(attempt_list, {"items", "count", "offset", "next_offset"})
                or type(attempt_list.get("count")) is not int
                or attempt_list.get("count") != 1
                or attempt_list.get("offset") != 0
                or attempt_list.get("next_offset") is not None
                or type(attempt_list.get("items")) is not list
                or len(attempt_list["items"]) != 1
                or attempt_list["items"][0].get("attempt_id") != account["attempt_id"]
            ):
                raise _reject_checkpoint("attempt list")
            result_list = expected["result_list"]
            if (
                not _exact_keys(result_list, {"items", "count", "offset", "next_offset"})
                or type(result_list.get("count")) is not int
                or result_list.get("count") != 2
                or result_list.get("offset") != 0
                or result_list.get("next_offset") is not None
                or type(result_list.get("items")) is not list
                or len(result_list["items"]) != 2
                or {item.get("result_id") for item in result_list["items"]}
                != set(account["result_ids"].values())
            ):
                raise _reject_checkpoint("result list")
            evidence = expected["chromatogram_evidence"]
            if checkpoint["numerical"]:
                if (
                    not _exact_keys(
                        evidence,
                        {
                            "profile",
                            "analysis",
                            "trace",
                            "result",
                            "point_count",
                            "peak_count",
                            "availability",
                            "evidence_sha256",
                        },
                    )
                    or evidence.get("profile") != "measure-chromatogram-evidence/1"
                    or not _sha256(evidence.get("evidence_sha256"))
                    or type(evidence.get("point_count")) is not int
                    or evidence["point_count"] != 101
                    or type(evidence.get("peak_count")) is not int
                    or evidence["peak_count"] != 1
                    or evidence.get("analysis", {}).get("analysis_id")
                    != checkpoint["numeric"][account_name]["analysis_ids"]["chromatogram"]
                    or evidence.get("analysis", {}).get("dataset_id") != account["dataset_id"]
                    or evidence.get("analysis", {}).get("trace_id") != account["trace_id"]
                    or evidence.get("trace", {}).get("trace_id") != account["trace_id"]
                    or type(evidence.get("availability")) is not dict
                ):
                    raise _reject_checkpoint("chromatogram evidence")
                processed_trace = expected["processed_trace"]
                if (
                    not _exact_keys(
                        processed_trace,
                        {"analysis_id", "kind", "offset", "count", "next_offset", "data"},
                    )
                    or processed_trace.get("analysis_id")
                    != checkpoint["numeric"][account_name]["analysis_ids"]["chromatogram"]
                    or processed_trace.get("kind") != "processed_trace"
                    or processed_trace.get("offset") != 0
                    or processed_trace.get("count") != 101
                    or processed_trace.get("next_offset") is not None
                    or not _exact_keys(processed_trace.get("data"), {"axis", "signal"})
                    or any(
                        type(values) is not list
                        or len(values) != 101
                        or any(not _finite_number(value) for value in values)
                        for values in processed_trace["data"].values()
                    )
                ):
                    raise _reject_checkpoint("processed trace")
            elif evidence is not None:
                raise _reject_checkpoint("unexpected chromatogram evidence")
            elif expected["processed_trace"] is not None:
                raise _reject_checkpoint("unexpected processed trace")
        if len(set(ids)) != len(ids):
            raise _reject_checkpoint("duplicate ids")
        if checkpoint["numerical"]:
            numeric = checkpoint["numeric"]
            if not _exact_keys(numeric, _ACCOUNT_KEYS):
                raise _reject_checkpoint("numeric accounts")
            for account_name in ("a", "b"):
                account = checkpoint["accounts"][account_name]
                evidence = numeric[account_name]
                if not _check_numeric_evidence(evidence, account):
                    raise _reject_checkpoint("numeric evidence")
                analysis_ids = list(evidence["analysis_ids"].values())
                if set(analysis_ids) & set(ids):
                    raise _reject_checkpoint("numeric id collision")
                ids.extend(analysis_ids)
        if len(set(ids)) != len(ids):
            raise _reject_checkpoint("duplicate numeric ids")
        return checkpoint
    except ValueError as error:
        if str(error) == _CHECKPOINT_FAILURE:
            raise
        raise ValueError(_CHECKPOINT_FAILURE) from None
    except Exception:
        raise ValueError(_CHECKPOINT_FAILURE) from None


async def _call(client: ClientSession, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await client.call_tool(tool, arguments)
    except Exception:
        raise PilotCheckError(_CALL_FAILURE) from None
    if getattr(response, "isError", None) is not False:
        raise PilotCheckError(_CALL_FAILURE)
    structured = getattr(response, "structuredContent", None)
    if type(structured) is not dict:
        raise PilotCheckError(_CALL_FAILURE)
    try:
        preflight_json(
            structured,
            "pilot response",
            max_bytes=SNAPSHOT_MAX_BYTES,
            fail=lambda _message: ValueError(_CALL_FAILURE),
        )
    except Exception:
        raise PilotCheckError(_CALL_FAILURE) from None
    return structured


def _denial_matches(response: Any, expected: str) -> bool:
    if getattr(response, "isError", None) is not True:
        return False
    if getattr(response, "structuredContent", None) is not None:
        return False
    content = getattr(response, "content", None)
    if type(content) is not list or len(content) != 1:
        return False
    text = getattr(content[0], "text", None)
    return type(text) is str and text.strip().endswith(expected)


async def _deny(
    client: ClientSession,
    tool: str,
    arguments: dict[str, Any],
    expected: str,
) -> None:
    try:
        response = await client.call_tool(tool, arguments)
    except Exception:
        raise PilotCheckError(_CALL_FAILURE) from None
    if not _denial_matches(response, expected):
        raise PilotCheckError(_CALL_FAILURE)


async def _source_pages(client: ClientSession, dataset_id: str) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    offset = 0
    for _ in range(16):
        page = await _call(
            client,
            "measurement_read_source",
            {"dataset_id": dataset_id, "offset": offset, "limit": SOURCE_PAGE_SIZE},
        )
        pages.append(page)
        next_offset = page.get("next_offset")
        if next_offset is None:
            return pages
        if type(next_offset) is not int or next_offset != offset + SOURCE_PAGE_SIZE:
            raise PilotCheckError(_CALL_FAILURE)
        offset = next_offset
    raise PilotCheckError(_CALL_FAILURE)


async def _numeric_worker(client: ClientSession) -> dict[str, Any]:
    try:
        from .pilot_numerics import qualify_numerics

        evidence = await qualify_numerics(client)
    except Exception:
        raise PilotCheckError(_CALL_FAILURE) from None
    return evidence


async def _seed_account(
    client: ClientSession,
    *,
    account_name: str,
    run_id: str,
    experiment_id: str,
    experiment_created: dict[str, Any],
    numerical: bool,
    numeric_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    if numerical:
        if numeric_evidence is None:
            raise PilotCheckError(_CALL_FAILURE)
        dataset_id = numeric_evidence.get("dataset_id")
        trace_id = numeric_evidence.get("trace_id")
        if not _record_id(dataset_id) or type(trace_id) is not str or not trace_id:
            raise PilotCheckError(_CALL_FAILURE)
    else:
        registration = await _call(
            client,
            "measurement_register_csv",
            _csv_dataset_arguments(run_id, account_name),
        )
        dataset_id = registration.get("dataset_id")
        trace_id = _trace_id(run_id, account_name, False)
        if (
            not _record_id(dataset_id)
            or registration.get("source_sha256")
            != hashlib.sha256(
                _csv_dataset_arguments(run_id, account_name)["csv_text"].encode("utf-8")
            ).hexdigest()
            or registration.get("trace_count") != 1
        ):
            raise PilotCheckError(_CALL_FAILURE)

    dataset_inspection = await _call(
        client, "measurement_inspect_dataset", {"dataset_id": dataset_id}
    )
    source_pages = await _source_pages(client, dataset_id)
    if (
        dataset_inspection.get("dataset_id") != dataset_id
        or dataset_inspection.get("source") is None
        or not source_pages
        or any(page.get("dataset_id") != dataset_id for page in source_pages)
    ):
        raise PilotCheckError(_CALL_FAILURE)
    attempt_spec = {
        "status": "completed",
        "evaluator_kind": "simulator",
        "evaluator_reference": "synthetic-pilot",
        "measurements": [{"dataset_id": dataset_id, "trace_id": trace_id}],
    }
    attempt = await _call(
        client,
        "measurement_record_attempt",
        {
            "experiment_id": experiment_id,
            "spec": attempt_spec,
            "idempotency_key": _attempt_key(run_id, account_name),
        },
    )
    attempt_id = attempt.get("attempt_id")
    if not _record_id(attempt_id):
        raise PilotCheckError(_CALL_FAILURE)
    results = await _call(
        client,
        "measurement_record_results",
        {
            "attempt_id": attempt_id,
            "expected_attempt_revision": 1,
            "results": _result_specs(),
            "idempotency_key": _results_key(run_id, account_name),
        },
    )
    result_records = results.get("results")
    if (
        type(result_records) is not list
        or len(result_records) != 2
        or any(
            type(item) is not dict or not _record_id(item.get("result_id"))
            for item in result_records
        )
    ):
        raise PilotCheckError(_CALL_FAILURE)
    result_ids = {
        "observed": result_records[0]["result_id"],
        "below_limit": result_records[1]["result_id"],
    }
    experiment_record = await _call(
        client, "measurement_get_experiment", {"experiment_id": experiment_id}
    )
    attempt_record = await _call(client, "measurement_get_attempt", {"attempt_id": attempt_id})
    observed_result = await _call(
        client, "measurement_get_result", {"result_id": result_ids["observed"]}
    )
    below_limit_result = await _call(
        client, "measurement_get_result", {"result_id": result_ids["below_limit"]}
    )
    attempt_list = await _call(
        client,
        "measurement_list_attempts",
        {"experiment_id": experiment_id, "offset": 0, "limit": 20},
    )
    result_list = await _call(
        client,
        "measurement_list_results",
        {"attempt_id": attempt_id, "include_superseded": False, "offset": 0, "limit": 20},
    )
    chromatogram_evidence = None
    processed_trace = None
    if numerical and numeric_evidence is not None:
        processed_trace = await _call(
            client,
            "measurement_read_analysis_data",
            {
                "analysis_id": numeric_evidence["analysis_ids"]["chromatogram"],
                "kind": "processed_trace",
                "offset": 0,
                "limit": 1000,
            },
        )
        chromatogram_evidence = await _call(
            client,
            "measurement_get_chromatogram_evidence",
            {"analysis_id": numeric_evidence["analysis_ids"]["chromatogram"]},
        )
    if (
        not _same(experiment_record, experiment_created)
        or experiment_record.get("experiment_id") != experiment_id
        or not _same(attempt_record, attempt)
        or attempt_record.get("attempt_id") != attempt_id
        or observed_result.get("result_id") != result_ids["observed"]
        or below_limit_result.get("result_id") != result_ids["below_limit"]
        or not _same(results.get("results"), [observed_result, below_limit_result])
        or attempt_list.get("count") != 1
        or result_list.get("count") != 2
    ):
        raise PilotCheckError(_CALL_FAILURE)
    return {
        "experiment_id": experiment_id,
        "dataset_id": dataset_id,
        "trace_id": trace_id,
        "attempt_id": attempt_id,
        "result_ids": result_ids,
        "expected": {
            "dataset_inspection": dataset_inspection,
            "source_pages": source_pages,
            "experiment": experiment_record,
            "attempt": attempt_record,
            "results": results,
            "observed_result": observed_result,
            "below_limit_result": below_limit_result,
            "attempt_list": attempt_list,
            "result_list": result_list,
            "processed_trace": processed_trace,
            "chromatogram_evidence": chromatogram_evidence,
        },
    }


async def _check_own_records(client: ClientSession, account: dict[str, Any]) -> None:
    expected = account["expected"]
    calls: list[tuple[str, dict[str, Any], Any]] = [
        (
            "measurement_inspect_dataset",
            {"dataset_id": account["dataset_id"]},
            expected["dataset_inspection"],
        ),
        (
            "measurement_get_experiment",
            {"experiment_id": account["experiment_id"]},
            expected["experiment"],
        ),
        ("measurement_get_attempt", {"attempt_id": account["attempt_id"]}, expected["attempt"]),
        (
            "measurement_get_result",
            {"result_id": account["result_ids"]["observed"]},
            expected["observed_result"],
        ),
        (
            "measurement_get_result",
            {"result_id": account["result_ids"]["below_limit"]},
            expected["below_limit_result"],
        ),
        (
            "measurement_list_attempts",
            {"experiment_id": account["experiment_id"], "offset": 0, "limit": 20},
            expected["attempt_list"],
        ),
        (
            "measurement_list_results",
            {
                "attempt_id": account["attempt_id"],
                "include_superseded": False,
                "offset": 0,
                "limit": 20,
            },
            expected["result_list"],
        ),
    ]
    for tool, arguments, expected_response in calls:
        actual = await _call(client, tool, arguments)
        if not _same(actual, expected_response):
            raise PilotCheckError(_CALL_FAILURE)
    for page in expected["source_pages"]:
        actual = await _call(
            client,
            "measurement_read_source",
            {
                "dataset_id": account["dataset_id"],
                "offset": page["offset"],
                "limit": SOURCE_PAGE_SIZE,
            },
        )
        if not _same(actual, page):
            raise PilotCheckError(_CALL_FAILURE)
    if expected["processed_trace"] is not None:
        actual = await _call(
            client,
            "measurement_read_analysis_data",
            {
                "analysis_id": expected["processed_trace"]["analysis_id"],
                "kind": "processed_trace",
                "offset": 0,
                "limit": 1000,
            },
        )
        if not _same(actual, expected["processed_trace"]):
            raise PilotCheckError(_CALL_FAILURE)
    if expected["chromatogram_evidence"] is not None:
        actual = await _call(
            client,
            "measurement_get_chromatogram_evidence",
            {
                "analysis_id": account["expected"]["chromatogram_evidence"]["analysis"][
                    "analysis_id"
                ]
            },
        )
        if not _same(actual, expected["chromatogram_evidence"]):
            raise PilotCheckError(_CALL_FAILURE)


async def _check_foreign_getters(
    client_a: ClientSession,
    client_b: ClientSession,
    accounts: dict[str, dict[str, Any]],
    numeric: dict[str, dict[str, Any]] | None,
) -> None:
    for owner_name, client, foreign_name in (
        ("a", client_a, "b"),
        ("b", client_b, "a"),
    ):
        foreign = accounts[foreign_name]
        await _deny(
            client,
            "measurement_inspect_dataset",
            {"dataset_id": foreign["dataset_id"]},
            _SOURCE_EXPECTED,
        )
        await _deny(
            client,
            "measurement_read_source",
            {"dataset_id": foreign["dataset_id"], "offset": 0, "limit": SOURCE_PAGE_SIZE},
            _SOURCE_EXPECTED,
        )
        await _deny(
            client,
            "measurement_get_experiment",
            {"experiment_id": foreign["experiment_id"]},
            _REGISTRY_EXPECTED,
        )
        await _deny(
            client,
            "measurement_get_attempt",
            {"attempt_id": foreign["attempt_id"]},
            _REGISTRY_EXPECTED,
        )
        for result_id in foreign["result_ids"].values():
            await _deny(
                client, "measurement_get_result", {"result_id": result_id}, _REGISTRY_EXPECTED
            )
        if numeric is not None:
            foreign_analysis = numeric[foreign_name]["analysis_ids"]["chromatogram"]
            foreign_sec = numeric[foreign_name]["analysis_ids"]["sec"]
            await _deny(
                client,
                "measurement_get_analysis",
                {"analysis_id": foreign_analysis},
                "Analysis was not found or is not accessible",
            )
            await _deny(
                client,
                "measurement_get_analysis",
                {"analysis_id": foreign_sec},
                "Analysis was not found or is not accessible",
            )
            for kind in ("peaks", "processed_trace"):
                await _deny(
                    client,
                    "measurement_read_analysis_data",
                    {"analysis_id": foreign_analysis, "kind": kind, "offset": 0, "limit": 10},
                    "Analysis was not found or is not accessible",
                )
            await _deny(
                client,
                "measurement_read_analysis_data",
                {"analysis_id": foreign_sec, "kind": "distribution", "offset": 0, "limit": 10},
                "Analysis was not found or is not accessible",
            )
            await _deny(
                client,
                "measurement_get_chromatogram_evidence",
                {"analysis_id": foreign_analysis},
                "Analysis was not found or is not accessible",
            )


async def _check_foreign_links(
    client_a: ClientSession,
    client_b: ClientSession,
    accounts: dict[str, dict[str, Any]],
    numeric: dict[str, dict[str, Any]] | None,
    run_id: str,
) -> None:
    for owner_name, client, foreign_name in (("a", client_a, "b"), ("b", client_b, "a")):
        owned = accounts[owner_name]
        foreign = accounts[foreign_name]
        await _deny(
            client,
            "measurement_record_attempt",
            {
                "experiment_id": owned["experiment_id"],
                "spec": {
                    "status": "completed",
                    "evaluator_kind": "simulator",
                    "evaluator_reference": "synthetic-pilot-foreign-link-probe",
                    "measurements": [
                        {"dataset_id": foreign["dataset_id"], "trace_id": foreign["trace_id"]}
                    ],
                },
                "idempotency_key": f"pilot-{run_id}-{owner_name}-foreign-dataset",
            },
            _LINK_DATASET_EXPECTED,
        )
        if numeric is not None:
            analysis_id = numeric[foreign_name]["analysis_ids"]["chromatogram"]
            await _deny(
                client,
                "measurement_record_results",
                {
                    "attempt_id": owned["attempt_id"],
                    "expected_attempt_revision": 1,
                    "results": [
                        {
                            "metric": "synthetic_foreign_analysis_probe",
                            "status": "observed",
                            "unit": "1",
                            "analysis_id": analysis_id,
                            "summary_field": "peak_count",
                        }
                    ],
                    "idempotency_key": f"pilot-{run_id}-{owner_name}-foreign-analysis",
                },
                _LINK_ANALYSIS_EXPECTED,
            )


async def _replay_exact(
    client: ClientSession, run_id: str, account_name: str, account: dict[str, Any]
) -> None:
    experiment = await _call(
        client,
        "measurement_create_experiment",
        {"spec": _experiment_spec(run_id), "idempotency_key": _experiment_key(run_id)},
    )
    attempt = await _call(
        client,
        "measurement_record_attempt",
        {
            "experiment_id": account["experiment_id"],
            "spec": {
                "status": "completed",
                "evaluator_kind": "simulator",
                "evaluator_reference": "synthetic-pilot",
                "measurements": [
                    {"dataset_id": account["dataset_id"], "trace_id": account["trace_id"]}
                ],
            },
            "idempotency_key": _attempt_key(run_id, account_name),
        },
    )
    results = await _call(
        client,
        "measurement_record_results",
        {
            "attempt_id": account["attempt_id"],
            "expected_attempt_revision": 1,
            "results": _result_specs(),
            "idempotency_key": _results_key(run_id, account_name),
        },
    )
    expected = account["expected"]
    if (
        not _same(experiment, expected["experiment"])
        or not _same(attempt, expected["attempt"])
        or not _same(results, expected["results"])
    ):
        raise PilotCheckError(_CALL_FAILURE)


async def _recheck_numeric(client: ClientSession, evidence: dict[str, Any]) -> None:
    for entry in evidence["records"]:
        tool = entry["tool"]
        # validate_checkpoint restricts this to bounded, read-only getter arguments.
        actual = await _call(client, tool, entry["arguments"])
        if not _same(actual, entry["response"]):
            raise PilotCheckError(_CALL_FAILURE)


def _public_report(*, numerical: bool, analysis_count: int) -> dict[str, Any]:
    return {
        "profile": PROFILE,
        "passed": True,
        "checks": {
            "distinct_principals": True,
            "owned_records": True,
            "cross_owner_denied": True,
            "foreign_dataset_link_rejected": True,
            "foreign_analysis_link_rejected": numerical,
            "foreign_analysis_evidence_denied": numerical,
            "idempotent_replay": True,
            "no_duplicate_attempts_or_results": True,
            "numerical": numerical,
        },
        "counts": {
            "accounts": 2,
            "datasets": 2,
            "sources": 2,
            "experiments": 2,
            "attempts": 2,
            "results": 4,
            "analyses": analysis_count,
        },
        "operator_gate_required": True,
        "restart_claimed": False,
    }


async def verify_pilot(
    client_a: ClientSession,
    client_b: ClientSession,
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    """Recheck owned records and exact retry receipts using initialized MCP sessions."""
    checkpoint = validate_checkpoint(checkpoint)
    accounts = checkpoint["accounts"]
    numeric = checkpoint["numeric"]
    # This entire read/denial gate precedes every idempotent replay write.
    await _check_own_records(client_a, accounts["a"])
    await _check_own_records(client_b, accounts["b"])
    await _check_foreign_getters(client_a, client_b, accounts, numeric)
    await _check_foreign_links(client_a, client_b, accounts, numeric, checkpoint["run_id"])
    if numeric is not None:
        await _recheck_numeric(client_a, numeric["a"])
        await _recheck_numeric(client_b, numeric["b"])

    before_attempts: dict[str, dict[str, Any]] = {}
    before_results: dict[str, dict[str, Any]] = {}
    for account_name, client in (("a", client_a), ("b", client_b)):
        account = accounts[account_name]
        before_attempts[account_name] = await _call(
            client,
            "measurement_list_attempts",
            {"experiment_id": account["experiment_id"], "offset": 0, "limit": 20},
        )
        before_results[account_name] = await _call(
            client,
            "measurement_list_results",
            {
                "attempt_id": account["attempt_id"],
                "include_superseded": False,
                "offset": 0,
                "limit": 20,
            },
        )
        if not _same(
            before_attempts[account_name], account["expected"]["attempt_list"]
        ) or not _same(before_results[account_name], account["expected"]["result_list"]):
            raise PilotCheckError(_CALL_FAILURE)

    await _replay_exact(client_a, checkpoint["run_id"], "a", accounts["a"])
    await _replay_exact(client_b, checkpoint["run_id"], "b", accounts["b"])

    for account_name, client in (("a", client_a), ("b", client_b)):
        account = accounts[account_name]
        after_attempts = await _call(
            client,
            "measurement_list_attempts",
            {"experiment_id": account["experiment_id"], "offset": 0, "limit": 20},
        )
        after_results = await _call(
            client,
            "measurement_list_results",
            {
                "attempt_id": account["attempt_id"],
                "include_superseded": False,
                "offset": 0,
                "limit": 20,
            },
        )
        if not _same(after_attempts, before_attempts[account_name]) or not _same(
            after_results, before_results[account_name]
        ):
            raise PilotCheckError(_CALL_FAILURE)
    return _public_report(numerical=checkpoint["numerical"], analysis_count=4 if numeric else 0)


async def prepare_pilot(
    client_a: ClientSession,
    client_b: ClientSession,
    *,
    run_id: str,
    numerical: bool = False,
) -> dict[str, Any]:
    """Seed bounded synthetic records and return the private checkpoint for recheck."""
    if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None or type(numerical) is not bool:
        raise PilotCheckError(_CALL_FAILURE)
    spec = _experiment_spec(run_id)
    create_arguments = {"spec": spec, "idempotency_key": _experiment_key(run_id)}
    experiment_a = await _call(client_a, "measurement_create_experiment", create_arguments)
    experiment_b = await _call(client_b, "measurement_create_experiment", create_arguments)
    experiment_id_a = experiment_a.get("experiment_id")
    experiment_id_b = experiment_b.get("experiment_id")
    if (
        not _record_id(experiment_id_a)
        or not _record_id(experiment_id_b)
        or experiment_id_a == experiment_id_b
    ):
        raise PilotCheckError(_CALL_FAILURE)
    numeric: dict[str, dict[str, Any]] | None = None
    if numerical:
        numeric_a = await _numeric_worker(client_a)
        numeric_b = await _numeric_worker(client_b)
        numeric = {"a": numeric_a, "b": numeric_b}
    accounts = {
        "a": await _seed_account(
            client_a,
            account_name="a",
            run_id=run_id,
            experiment_id=experiment_id_a,
            experiment_created=experiment_a,
            numerical=numerical,
            numeric_evidence=None if numeric is None else numeric["a"],
        ),
        "b": await _seed_account(
            client_b,
            account_name="b",
            run_id=run_id,
            experiment_id=experiment_id_b,
            experiment_created=experiment_b,
            numerical=numerical,
            numeric_evidence=None if numeric is None else numeric["b"],
        ),
    }
    checkpoint = {
        "profile": PROFILE,
        "schema_version": 1,
        "run_id": run_id,
        "numerical": numerical,
        "accounts": accounts,
        "numeric": numeric,
    }
    checkpoint = validate_checkpoint(checkpoint)
    # Run the same read-before-replay verification once before returning a
    # checkpoint, so preparation proves its own exact retry receipts.
    await verify_pilot(client_a, client_b, checkpoint)
    return checkpoint


__all__ = ["PROFILE", "PilotCheckError", "prepare_pilot", "validate_checkpoint", "verify_pilot"]
