"""Real-R contracts for the bounded native acquisition worker adapter."""

from __future__ import annotations

import json
import os
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
