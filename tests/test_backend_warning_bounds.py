import json
from types import SimpleNamespace

import pytest

from measure_service import analysis_runtime
from measure_service.analysis_pages import (
    MAX_BACKEND_WARNINGS,
    MAX_WARNING_UTF8_BYTES,
    bound_backend_warnings,
)


def test_warning_bound_preserves_supported_legacy_shapes():
    assert bound_backend_warnings(["first", "second"]) == ["first", "second"]
    assert bound_backend_warnings("one warning") == ["one warning"]
    assert bound_backend_warnings(None) == []


def test_warning_count_uses_one_explicit_deterministic_omission_marker():
    warnings = [f"backend warning {index}" for index in range(MAX_BACKEND_WARNINGS + 5)]

    bounded = bound_backend_warnings(warnings)

    assert len(bounded) == MAX_BACKEND_WARNINGS
    assert bounded[:-1] == warnings[: MAX_BACKEND_WARNINGS - 1]
    assert bounded[-1] == "6 additional warning messages omitted"
    assert bound_backend_warnings(warnings) == bounded


def test_warning_string_truncation_is_utf8_safe_and_marker_fits_byte_budget():
    warning = "測定" * (MAX_WARNING_UTF8_BYTES + 100)

    bounded = bound_backend_warnings([warning])

    assert len(bounded) == 1
    assert bounded[0].endswith(" [warning text truncated]")
    assert len(bounded[0].encode("utf-8")) <= MAX_WARNING_UTF8_BYTES
    bounded[0].encode("utf-8")


@pytest.mark.parametrize("unsupported", [0, {}, ["valid", 2], ("warning",)])
def test_unsupported_warning_shapes_fail_closed(unsupported):
    with pytest.raises(ValueError, match="warnings must be"):
        bound_backend_warnings(unsupported)


def test_worker_stores_bounded_backend_warnings_without_changing_result(tmp_path, monkeypatch):
    worker = tmp_path / "worker.R"
    worker.write_text("fixed worker")
    scientific = {"peak_count": 2, "values": [0.5, 1.5]}
    warnings = ["μ" * (MAX_WARNING_UTF8_BYTES + 2)] * (MAX_BACKEND_WARNINGS + 2)
    monkeypatch.setattr(
        analysis_runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "result": scientific, "warnings": warnings}),
        ),
    )

    result = analysis_runtime.run_worker({"operation": "capabilities"}, worker_path=worker)

    assert {key: value for key, value in result.items() if key != "warnings"} == scientific
    assert len(result["warnings"]) == MAX_BACKEND_WARNINGS
    assert all(
        len(message.encode("utf-8")) <= MAX_WARNING_UTF8_BYTES for message in result["warnings"]
    )
    assert result["warnings"][-1] == "3 additional warning messages omitted"


def test_analysis_stores_a_warning_list_with_explicit_truncation(monkeypatch):
    trace = {
        "trace_id": "t1",
        "sample_id": "s1",
        "injection_id": "i1",
        "channel": "uv",
        "separation": "LC",
        "detector": "UV",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "axis": [0.0, 1.0, 2.0],
        "signal": [1.0, 2.0, 1.0],
    }
    warnings = ["backend" * (MAX_WARNING_UTF8_BYTES // 7 + 2)]
    monkeypatch.setattr(
        analysis_runtime,
        "run_worker",
        lambda payload: {"warnings": warnings, "peak_count": 0, "peaks": []},
    )

    result = analysis_runtime.process_chromatogram(trace, {})

    assert len(result["warnings"]) == 1
    assert result["warnings"][0].endswith(" [warning text truncated]")
    record = analysis_runtime.make_analysis_record(
        "dataset-1", "t1", "chromatogram", {}, result, "a" * 64, None
    )
    assert record["result"]["warnings"] == result["warnings"]


@pytest.mark.parametrize("already_bounded", [False, True])
def test_processing_warning_preserves_backend_omission_count(already_bounded):
    trace = {
        "trace_id": "t",
        "sample_id": "s",
        "injection_id": "i",
        "channel": "uv",
        "separation": "LC",
        "detector": "UV",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "axis": [0.0, 1.0, 3.0],
        "signal": [0.0, 1.0, 0.0],
    }
    backend = [f"backend {index}" for index in range(40)]
    messages = bound_backend_warnings(backend) if already_bounded else backend
    result = analysis_runtime.process_chromatogram(
        trace,
        {"smoothing": "gaussian"},
        worker=lambda payload: {"warnings": messages, "peak_count": 0, "peaks": []},
    )
    assert result["warnings"][:31] == backend[:31]
    assert result["warnings"][31] == "9 additional warning messages omitted"
    assert "spacing is irregular" in result["warnings"][32]
    assert len(result["warnings"]) == MAX_BACKEND_WARNINGS + 1
