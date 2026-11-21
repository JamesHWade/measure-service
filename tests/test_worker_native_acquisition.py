"""Real-R contracts for the bounded native acquisition worker adapter."""

from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path

import pytest

from measure_service import analysis_runtime

CSV_MEMBER_NAMES = (
    "manifest.dcf",
    "samples.csv",
    "injections.csv",
    "channels.csv",
    "sources.csv",
    "curves.csv",
    "points.csv",
    "streams.csv",
    "scans.csv",
    "scan_points.csv",
    "arrays.csv",
    "array_axes.csv",
    "array_points.csv",
)
MAX_ACQUISITION_BYTES = 4_000_000


FIXTURE = Path(__file__).parent / "fixtures" / "native_acquisition_xic_v1"
WORKER = Path(analysis_runtime.ROOT / "worker.R")


def _fixture_files() -> dict[str, str]:
    files = {}
    for name in CSV_MEMBER_NAMES:
        with (FIXTURE / name).open("r", encoding="utf-8", newline="") as stream:
            files[name] = stream.read()
    return files


def _run_worker(payload: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    r_binary = os.environ.get("MEASURE_MCP_TEST_R")
    if not r_binary:
        pytest.skip("Set MEASURE_MCP_TEST_R to the qualified R executable")
    monkeypatch.setenv("MEASURE_MCP_R", r_binary)
    return analysis_runtime.run_worker(payload, worker_path=WORKER)


@pytest.mark.r_backend
def test_inspection_is_bounded_metadata_and_counts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_worker(
        {"operation": "inspect_acquisition", "files": _fixture_files()},
        monkeypatch,
    )

    assert result["profile"] == "measure-acquisition-inspection/1"
    summary = result["summary"]
    assert set(summary) == {
        "counts",
        "scan_status_counts",
        "streams",
        "samples",
        "injections",
        "channels",
        "sources",
    }
    assert summary["counts"] == {
        "samples": 1,
        "injections": 1,
        "channels": 1,
        "sources": 1,
        "curves": 0,
        "streams": 1,
        "scans": 5,
        "arrays": 0,
        "scan_points": 8,
        "array_points": 0,
    }
    assert summary["scan_status_counts"] == {"acquired": 4, "missing": 1}
    assert summary["samples"] == ["sample-xic"]
    assert summary["injections"] == [{"injection_id": "injection-xic", "sample_id": "sample-xic"}]
    assert summary["channels"] == [{"channel_id": "ms", "channel_name": "MS centroid"}]
    assert summary["sources"] == [
        {
            "source_id": "source-native",
            "source_name": "synthetic-native.csv",
            "source_sha256": "b" * 64,
        }
    ]
    assert summary["streams"] == [
        {
            "stream_id": "ms-xic",
            "injection_id": "injection-xic",
            "channel_id": "ms",
            "kind": "ms",
            "source_id": "source-native",
            "time_unit": "s",
            "mz_unit": "m/z",
            "value_name": "intensity",
            "value_unit": "counts",
        }
    ]
    # Raw scan points stay in the retained native files and never enter the
    # inspection response, which is safe to carry as one MCP summary.
    assert "scan_points" not in summary
    assert "array_points" not in summary
    assert "native_scan_rows" not in result
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) < 64 * 1024
    assert result["provenance"]["operation"] == "inspect_acquisition"
    assert result["provenance"]["backend"] == "measure"


@pytest.mark.r_backend
def test_capabilities_report_native_acquisition_export(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_worker({"operation": "capabilities"}, monkeypatch)
    assert result["native_acquisition_support"] is True


@pytest.mark.r_backend
def test_native_files_are_validated_before_worker_temp_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _fixture_files()
    files["points.csv"] = "x" * (MAX_ACQUISITION_BYTES + 1)
    with pytest.raises(ValueError, match="native acquisition bundle member|cumulative byte"):
        _run_worker(
            {"operation": "inspect_acquisition", "files": files},
            monkeypatch,
        )


def _direct_r_points(r_binary: str) -> list[dict]:
    script = r"""
suppressPackageStartupMessages(library(measure))
suppressPackageStartupMessages(library(jsonlite))
acquisition <- read_measure_acquisition(Sys.getenv("BUNDLE"))
trace <- measure_ms_trace(
  acquisition,
  stream_id = "ms-xic",
  polarity = "positive",
  ms_level = 1L,
  mz_range = c(100, 101)
)
points <- lapply(seq_len(nrow(trace)), function(i) {
  list(
    scan_id = trace$scan_id[[i]],
    scan_index = trace$scan_index[[i]],
    source_scan_id = trace$source_scan_id[[i]],
    retention_time = trace$retention_time[[i]],
    value = trace$value[[i]],
    status = trace$status[[i]],
    selected_points = trace$selected_points[[i]],
    saturation_unknown = trace$saturation_unknown[[i]]
  )
})
cat(toJSON(points, auto_unbox = TRUE, null = "null", na = "null", digits = 16))
"""
    environment = os.environ.copy()
    environment.pop("R_HOME", None)
    environment["BUNDLE"] = str(FIXTURE)
    completed = subprocess.run(
        [r_binary, "--vanilla", "--slave", "-e", script],
        env=environment,
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(completed.stdout)


@pytest.mark.r_backend
def test_centroid_xic_points_and_lineage_match_direct_measure_r(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r_binary = os.environ.get("MEASURE_MCP_TEST_R")
    if not r_binary:
        pytest.skip("Set MEASURE_MCP_TEST_R to the qualified R executable")
    result = _run_worker(
        {
            "operation": "extract_ms_trace",
            "files": _fixture_files(),
            "stream_id": "ms-xic",
            "polarity": "positive",
            "ms_level": 1,
            "mz_min": 100,
            "mz_max": 101,
        },
        monkeypatch,
    )

    assert set(result) == {
        "points",
        "source_rows",
        "processing",
        "packages",
        "installed_r_code_md5",
        "r_version",
        "warnings",
    }
    expected = _direct_r_points(r_binary)
    assert len(result["points"]) == len(expected) == 5
    for observed, reference in zip(result["points"], expected, strict=True):
        assert observed.keys() == reference.keys()
        for field in (
            "scan_id",
            "scan_index",
            "source_scan_id",
            "status",
            "selected_points",
            "saturation_unknown",
        ):
            assert observed[field] == reference[field]
        if reference["value"] is None:
            assert observed["value"] is None
        else:
            assert math.isclose(observed["value"], reference["value"], rel_tol=0, abs_tol=1e-12)
        assert math.isclose(
            observed["retention_time"], reference["retention_time"], rel_tol=0, abs_tol=1e-12
        )

    assert result["source_rows"] == [
        {
            "scan_id": "scan-1",
            "point_index": 2,
            "source_row": 11,
            "mz": 100,
            "intensity": 2,
            "saturated": None,
        },
        {
            "scan_id": "scan-1",
            "point_index": 3,
            "source_row": 12,
            "mz": 101,
            "intensity": 3,
            "saturated": False,
        },
        {
            "scan_id": "scan-saturated",
            "point_index": 1,
            "source_row": 20,
            "mz": 100,
            "intensity": 4,
            "saturated": True,
        },
        {
            "scan_id": "scan-saturated",
            "point_index": 2,
            "source_row": 21,
            "mz": 101,
            "intensity": 6,
            "saturated": False,
        },
        {
            "scan_id": "scan-5",
            "point_index": 1,
            "source_row": 30,
            "mz": 100,
            "intensity": 7,
            "saturated": False,
        },
    ]
    assert result["processing"]["operation"] == "xic"
    assert result["processing"]["stream_id"] == "ms-xic"
    assert result["processing"]["injection_id"] == "injection-xic"
    assert result["processing"]["channel_id"] == "ms"
    assert result["processing"]["source_id"] == "source-native"
    assert result["processing"]["mz_range"] == [100, 101]
    assert result["processing"]["mz_unit"] == "m/z"
    assert result["processing"]["time_unit"] == "s"
    assert result["processing"]["value_unit"] == "counts"


@pytest.mark.r_backend
def test_xic_bounds_are_rejected_before_native_bundle_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="mz_min and mz_max"):
        _run_worker(
            {
                "operation": "extract_ms_trace",
                "files": _fixture_files(),
                "stream_id": "ms-xic",
                "polarity": "positive",
                "ms_level": 1,
                "mz_min": 101,
                "mz_max": 100,
            },
            monkeypatch,
        )
