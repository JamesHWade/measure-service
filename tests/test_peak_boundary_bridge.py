"""R worker peak-row contract and view-only historical overlap diagnostics."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from measure_service import analysis_evidence, analysis_runtime, evidence_report, server
from measure_service.evidence_client import collect_chromatogram_evidence
from measure_service.storage import sha256

_REQUIRED_PEAK_FIELDS = {
    "peak_id",
    "location",
    "height",
    "left_base",
    "right_base",
    "area",
}
_OPTIONAL_PEAK_FIELDS = {"area_basis", "prominence"}
_OVERLAP_WARNING = (
    "Peak integration ranges overlap in their interiors; stored peak areas are unchanged and "
    "shared signal may contribute to more than one area."
)
_ZERO_THRESHOLD_WARNING = (
    "Peak detection with all thresholds set to zero can retain noise; inspect the signal range "
    "and sampling before choosing thresholds."
)


def _gaussian_inputs(*, min_height: float = 0.05) -> tuple[dict, dict]:
    axis = [index / 100 for index in range(601)]
    centers = (1.85, 2.5, 3.15)
    signal = [
        0.02
        + sum(
            height * math.exp(-0.5 * ((coordinate - center) / 0.24) ** 2)
            for height, center in zip((2.0, 1.5, 1.8), centers, strict=True)
        )
        for coordinate in axis
    ]
    trace = {
        "trace_id": "uv",
        "sample_id": "synthetic-sample",
        "injection_id": "synthetic-injection",
        "channel": "UV 254 nm",
        "separation": "LC",
        "detector": "UV",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "sample_type": "unknown",
        "batch_id": None,
        "run_order": 1,
        "axis": axis,
        "signal": signal,
    }
    method = {
        "baseline": "none",
        "smoothing": "none",
        "min_height": min_height,
        "min_prominence": 0.02,
        "min_distance": 0,
        "integration_baseline": "local",
    }
    return trace, method


def _run_worker(
    worker_path: Path,
    trace: dict,
    method: dict,
) -> dict:
    result = analysis_runtime.process_chromatogram(
        trace,
        method,
        worker=lambda payload: analysis_runtime.run_worker(payload, worker_path=worker_path),
    )
    return result


def _single_peak_inputs() -> tuple[dict, dict]:
    trace, method = _gaussian_inputs()
    trace["signal"] = [
        0.02 + 1.5 * math.exp(-0.5 * ((coordinate - 3.0) / 0.24) ** 2)
        for coordinate in trace["axis"]
    ]
    method.update(min_height=0, min_prominence=0, min_distance=0)
    return trace, method


def _interior_overlap(peaks: list[dict]) -> bool:
    ranges = [
        (peak["left_base"], peak["right_base"])
        for peak in peaks
        if isinstance(peak.get("left_base"), (int, float))
        and isinstance(peak.get("right_base"), (int, float))
        and math.isfinite(peak["left_base"])
        and math.isfinite(peak["right_base"])
    ]
    return any(
        max(left_a, left_b) < min(right_a, right_b)
        for index, (left_a, right_a) in enumerate(ranges)
        for left_b, right_b in ranges[index + 1 :]
    )


def _evidence_record(trace: dict, method: dict, result: dict) -> tuple[dict, dict, str]:
    dataset = {"title": "Synthetic peak-boundary specimen", "traces": [trace]}
    digest = sha256(dataset)
    record = analysis_runtime.make_analysis_record(
        "dataset-1",
        trace["trace_id"],
        "chromatogram",
        method,
        result,
        digest,
        None,
    )
    return record, dataset, digest


class _SnapshotClient:
    def __init__(self, bundle: dict) -> None:
        self.snapshot = analysis_evidence.capture_evidence(bundle)

    async def call_tool(self, name: str, arguments: dict) -> SimpleNamespace:
        if name == "measurement_get_chromatogram_evidence":
            response = analysis_evidence.manifest_for(self.snapshot)
        else:
            response = analysis_evidence.page_for(
                self.snapshot,
                arguments["kind"],
                arguments["offset"],
                arguments["limit"],
            )
        return SimpleNamespace(isError=False, structuredContent=response, content=[])


@pytest.mark.r_backend
def test_real_r_peak_rows_warn_on_overlap_and_survive_future_backend_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r_binary = os.environ.get("MEASURE_MCP_TEST_R")
    if not r_binary:
        pytest.skip("Set MEASURE_MCP_TEST_R to the qualified R executable")
    monkeypatch.setenv("MEASURE_MCP_R", r_binary)

    worker_path = Path(analysis_runtime.ROOT / "worker.R")
    trace, method = _gaussian_inputs()
    result = _run_worker(worker_path, trace, method)
    assert result["peak_count"] == len(result["peaks"]) == 3
    assert all(
        _REQUIRED_PEAK_FIELDS.issubset(peak)
        and set(peak) <= _REQUIRED_PEAK_FIELDS | _OPTIONAL_PEAK_FIELDS
        for peak in result["peaks"]
    )
    source = worker_path.read_text()
    needle = "    peaks <- project_peak_rows(out$.peaks[[1]])"
    assert source.count(needle) == 1
    overlaps = _interior_overlap(result["peaks"])
    assert (_OVERLAP_WARNING in result["warnings"]) is overlaps

    # Exercise the R-side strict-interior check with finite ranges that only
    # meet at an apex; shared boundary points do not count as overlap.
    touching_replacement = """    peaks <- project_peak_rows(out$.peaks[[1]])
    if (is.data.frame(peaks) && nrow(peaks) >= 3L) {
      apex <- as_num(peaks$location)
      peaks$left_base <- c(0, apex[1:2])
      peaks$right_base <- c(apex[1:2], max(as_num(input$trace$axis)))
    }"""
    touching_worker = tmp_path / "worker-touching-ranges.R"
    touching_worker.write_text(source.replace(needle, touching_replacement))
    touching_result = _run_worker(touching_worker, trace, method)
    assert touching_result["peak_count"] == 3
    assert not _interior_overlap(touching_result["peaks"])
    assert _OVERLAP_WARNING not in touching_result["warnings"]

    # A point interval nested inside a wider interval has no shared interior.
    point_worker = tmp_path / "worker-point-ranges.R"
    point_worker.write_text(
        source.replace(
            needle,
            """    peaks <- project_peak_rows(out$.peaks[[1]])
    stopifnot(!peak_ranges_overlap(data.frame(
      left_base = c(0, 2), right_base = c(4, 2)
    )))
    stopifnot(!peak_ranges_overlap(data.frame(
      left_base = c(0, 2), right_base = c(2, 4)
    )))
    stopifnot(peak_ranges_overlap(data.frame(
      left_base = c(0, 2), right_base = c(4, 3)
    )))""",
        )
    )
    _run_worker(point_worker, trace, method)

    # Injected columns represent future backend metadata. The service contract
    # retains only its published peak fields, and complete evidence still pages.
    replacement = """    backend_peak_rows <- as.data.frame(out$.peaks[[1]])
    backend_peak_rows$detected_left_base <- backend_peak_rows$left_base
    backend_peak_rows$future_backend_value <- rep("discard me", nrow(backend_peak_rows))
    peaks <- project_peak_rows(backend_peak_rows)"""
    future_worker = tmp_path / "worker-with-future-columns.R"
    future_worker.write_text(source.replace(needle, replacement))

    future_result = _run_worker(future_worker, trace, method)
    assert future_result["peak_count"] == 3
    assert all(
        _REQUIRED_PEAK_FIELDS.issubset(peak)
        and set(peak) <= _REQUIRED_PEAK_FIELDS | _OPTIONAL_PEAK_FIELDS
        for peak in future_result["peaks"]
    )
    assert all(
        "detected_left_base" not in peak and "future_backend_value" not in peak
        for peak in future_result["peaks"]
    )

    record, dataset, digest = _evidence_record(trace, method, future_result)
    assert (
        record["provenance"]["worker_sha256"]
        == hashlib.sha256(future_worker.read_bytes()).hexdigest()
    )
    bundle = analysis_evidence.build_evidence(record, dataset, digest)
    analysis_evidence.validate_evidence(bundle)
    collected = asyncio.run(
        collect_chromatogram_evidence(_SnapshotClient(bundle), record["analysis_id"], page_size=2)
    )
    assert collected == bundle


@pytest.mark.r_backend
def test_real_r_empty_peak_table_is_valid_and_missing_nonempty_fields_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r_binary = os.environ.get("MEASURE_MCP_TEST_R")
    if not r_binary:
        pytest.skip("Set MEASURE_MCP_TEST_R to the qualified R executable")
    monkeypatch.setenv("MEASURE_MCP_R", r_binary)

    worker_path = Path(analysis_runtime.ROOT / "worker.R")
    trace, method = _gaussian_inputs(min_height=100)
    empty = _run_worker(worker_path, trace, method)
    assert empty["peak_count"] == 0
    assert empty["peaks"] == []
    record, dataset, digest = _evidence_record(trace, method, empty)
    bundle = analysis_evidence.build_evidence(record, dataset, digest)
    assert bundle["peaks"] == []
    analysis_evidence.validate_evidence(bundle)

    trace, method = _gaussian_inputs()
    source = worker_path.read_text()
    needle = "    peaks <- project_peak_rows(out$.peaks[[1]])"
    replacement = """    malformed_peak_rows <- as.data.frame(out$.peaks[[1]])
    malformed_peak_rows$area <- NULL
    peaks <- project_peak_rows(malformed_peak_rows)"""
    assert source.count(needle) == 1
    malformed_worker = tmp_path / "worker-missing-required-peak-field.R"
    malformed_worker.write_text(source.replace(needle, replacement))
    with pytest.raises(ValueError, match="malformed nonempty peak table"):
        _run_worker(malformed_worker, trace, method)


@pytest.mark.r_backend
@pytest.mark.parametrize(
    ("thresholds", "warns"),
    [
        ((0.0, 0.0, 0.0), True),
        ((0.2, 0.0, 0.0), False),
        ((0.0, 0.2, 0.0), False),
        ((0.0, 0.0, 0.2), False),
        ((0.2, 0.2, 0.0), False),
        ((0.2, 0.0, 0.2), False),
        ((0.0, 0.2, 0.2), False),
        ((0.2, 0.2, 0.2), False),
    ],
)
def test_zero_threshold_caution_only_fires_when_all_three_thresholds_are_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    thresholds: tuple[float, float, float],
    warns: bool,
) -> None:
    r_binary = os.environ.get("MEASURE_MCP_TEST_R")
    if not r_binary:
        pytest.skip("Set MEASURE_MCP_TEST_R to the qualified R executable")
    monkeypatch.setenv("MEASURE_MCP_R", r_binary)

    trace, method = _single_peak_inputs()
    method.update(
        min_height=thresholds[0],
        min_prominence=thresholds[1],
        min_distance=thresholds[2],
    )
    worker_path = Path(analysis_runtime.ROOT / "worker.R")
    result = _run_worker(worker_path, trace, method)
    assert result["peak_count"] == 1
    assert (result["warnings"] == [_ZERO_THRESHOLD_WARNING]) is warns
    if warns:
        assert len(result["warnings"]) == 1

        # Removing the caution from a temporary worker preserves every other
        # result field, including boundaries and the integrated area.
        source = worker_path.read_text()
        warning_block = f'''    if (
      isTRUE(
        method$min_height == 0 &&
          method$min_prominence == 0 &&
          method$min_distance == 0
      )
    ) {{
      warning(
        "{_ZERO_THRESHOLD_WARNING}"
      )
    }}
'''
        assert source.count(warning_block) == 1
        no_caution_worker = tmp_path / "worker-without-zero-threshold-caution.R"
        no_caution_worker.write_text(source.replace(warning_block, ""))
        reference = _run_worker(no_caution_worker, trace, method)
        assert result["warnings"] == [_ZERO_THRESHOLD_WARNING]
        assert reference["warnings"] == []
        assert {key: value for key, value in result.items() if key != "warnings"} == {
            key: value for key, value in reference.items() if key != "warnings"
        }


def _stored_overlap_analysis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path / "store"))
    trace = {
        "trace_id": "uv",
        "sample_id": "historical-sample",
        "injection_id": "historical-injection",
        "channel": "UV 254 nm",
        "separation": "LC",
        "detector": "UV",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "axis": [0.0, 1.0, 2.0, 3.0, 4.0],
        "signal": [0.0, 2.0, 4.0, 2.0, 0.0],
    }
    registered = server.register_dataset(
        "owner", server.Dataset.model_validate({"title": "Historical overlap", "traces": [trace]})
    )
    dataset, digest = server.dataset_for("owner", registered["dataset_id"])
    method = server.ChromatogramMethod().model_dump(mode="json")
    result = {
        "kind": "chromatogram",
        "peak_count": 2,
        "peaks": [
            {
                "peak_id": 1,
                "location": 1.0,
                "height": 2.0,
                "left_base": 0.0,
                "right_base": 2.5,
                "area": 4.25,
            },
            {
                "peak_id": 2,
                "location": 3.0,
                "height": 2.0,
                "left_base": 2.0,
                "right_base": 4.0,
                "area": 5.5,
            },
        ],
        "processed_trace": {"axis": trace["axis"], "signal": trace["signal"]},
        "warnings": [],
        "notes": ["Historical values are retained as stored."],
    }
    analysis = server.save_analysis(
        "owner", registered["dataset_id"], "uv", "chromatogram", method, result, digest
    )
    return analysis["analysis_id"]


def _raw_stored_result(analysis_id: str) -> str:
    with server.registry_connection() as connection:
        row = connection.execute(
            "SELECT result FROM analyses WHERE id=?", (analysis_id,)
        ).fetchone()
    assert row is not None
    return row[0]


def test_historical_capture_and_report_keep_stored_values_and_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis_id = _stored_overlap_analysis(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "identity", lambda context: context)
    raw_before = _raw_stored_result(analysis_id)

    manifest = server.measurement_get_chromatogram_evidence(analysis_id, "owner")
    trace_page = server.measurement_read_chromatogram_evidence(
        analysis_id, "trace", "owner", manifest["evidence_sha256"], limit=10
    )
    peak_page = server.measurement_read_chromatogram_evidence(
        analysis_id, "peaks", "owner", manifest["evidence_sha256"], limit=10
    )
    bundle = analysis_evidence.assemble_evidence(manifest, [trace_page], [peak_page])
    original_digest = bundle["evidence_sha256"]
    assert bundle["result"]["warnings"] == []
    assert [peak["area"] for peak in bundle["peaks"]] == [4.25, 5.5]

    artifacts = evidence_report.render_report(bundle)
    report = artifacts["report.html"].decode("utf-8")
    assert "Derived view diagnostic." in report
    assert "Stored boundaries and peak areas are unchanged" in report
    assert "does not recalculate areas or deconvolve peaks" in report
    assert json.loads(artifacts["evidence.json"]) == bundle
    assert bundle["evidence_sha256"] == original_digest
    csv_rows = list(csv.DictReader(io.StringIO(artifacts["peaks.csv"].decode("utf-8"))))
    assert [float(row["area"]) for row in csv_rows] == [4.25, 5.5]
    assert _raw_stored_result(analysis_id) == raw_before
    assert [
        peak["area"] for peak in server.analysis_for("owner", analysis_id)["result"]["peaks"]
    ] == [
        4.25,
        5.5,
    ]


def test_touching_peak_boundaries_have_no_derived_overlap_diagnostic() -> None:
    trace, method = _gaussian_inputs()
    peaks = [
        {
            "peak_id": 1,
            "location": 1.0,
            "height": 2.0,
            "left_base": 0.0,
            "right_base": 2.0,
            "area": 1.5,
        },
        {
            "peak_id": 2,
            "location": 3.0,
            "height": 2.0,
            "left_base": 2.0,
            "right_base": 4.0,
            "area": 1.5,
        },
    ]
    result = {
        "kind": "chromatogram",
        "peak_count": 2,
        "peaks": peaks,
        "processed_trace": {"axis": trace["axis"], "signal": trace["signal"]},
        "warnings": [],
        "notes": [],
    }
    record, dataset, digest = _evidence_record(trace, method, result)
    bundle = analysis_evidence.build_evidence(record, dataset, digest)

    artifacts = evidence_report.render_report(bundle)
    assert "Derived view diagnostic." not in artifacts["report.html"].decode("utf-8")
    assert json.loads(artifacts["evidence.json"]) == bundle


def test_published_historical_evidence_json_reconstructs_exactly() -> None:
    archived = (
        Path(__file__).parents[1]
        / "docs"
        / "reference-data"
        / "chromatogram-batch-example"
        / "items"
        / "000"
        / "evidence.json"
    )
    original_bytes = archived.read_bytes()
    bundle = json.loads(original_bytes)
    receipt_path = archived.parents[2] / "workflow_receipt.json"
    receipt = json.loads(receipt_path.read_text())
    expected_sha = receipt["canonical_file_hashes"]["items/000/evidence.json"]
    assert hashlib.sha256(original_bytes).hexdigest() == expected_sha

    snapshot = analysis_evidence.capture_evidence(bundle)
    manifest = analysis_evidence.manifest_for(snapshot)
    reconstructed = analysis_evidence.assemble_evidence(manifest, bundle["points"], bundle["peaks"])
    reserialized = (
        json.dumps(reconstructed, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    assert reconstructed == bundle
    assert reserialized == original_bytes
    assert reconstructed["evidence_sha256"] == bundle["evidence_sha256"]
    assert "Derived view diagnostic." not in evidence_report.render_report(bundle)[
        "report.html"
    ].decode("utf-8")


def test_chromatogram_client_schema_has_no_executable_source_field() -> None:
    tools = asyncio.run(server.mcp.registered_tools())
    analyze = next(tool for tool in tools if tool.name == "measurement_analyze_chromatogram")
    properties = set(analyze.inputSchema["properties"])
    assert not properties.intersection({"code", "expression", "script", "r_code"})


@pytest.mark.parametrize(
    ("bounds", "overlaps"),
    [
        ([(0.0, 4.0), (2.0, 2.0)], False),
        ([(0.0, 2.0), (2.0, 4.0)], False),
        ([(2.0, 2.0), (2.0, 2.0)], False),
        ([(0.0, 4.0), (2.0, 3.0)], True),
    ],
)
def test_html_overlap_requires_positive_width(bounds, overlaps):
    peaks = [{"left_base": left, "right_base": right} for left, right in bounds]
    assert evidence_report._has_interior_peak_overlap(peaks) is overlaps
