"""Public subprocess workflow; the installed suite also verifies distribution use."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).parents[1] / "examples" / "chromatogram_batch.py"


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["MEASURE_MCP_R"] = environment.get("MEASURE_MCP_TEST_R", "R")
    environment.pop("PYTHONPATH", None)
    return environment


def test_demo_rejects_existing_store_without_registering_inputs(tmp_path: Path) -> None:
    existing = tmp_path / "existing-store"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_bytes(b"owned data")
    output = tmp_path / "new-output"
    completed = subprocess.run(
        [sys.executable, str(EXAMPLE), "--output-dir", str(output), "--data-dir", str(existing)],
        cwd=tmp_path,
        env=dict(_child_environment(), MEASURE_MCP_R=str(tmp_path / "unavailable-R")),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode != 0
    assert "Use a new batch demonstration data directory" in completed.stderr
    assert not output.exists()
    assert [p.name for p in existing.iterdir()] == ["keep.txt"]
    assert marker.read_bytes() == b"owned data"


@pytest.mark.parametrize("layout", ["same", "output-in-store", "store-in-output"])
def test_demo_rejects_nested_directories_before_creating_either(
    tmp_path: Path, layout: str
) -> None:
    root = tmp_path / "new-parent"
    output = root if layout != "output-in-store" else root / "output"
    store = root if layout != "store-in-output" else root / "store"
    command = [sys.executable, str(EXAMPLE), "--output-dir", str(output), "--data-dir", str(store)]
    environment = dict(_child_environment(), MEASURE_MCP_R=str(tmp_path / "unavailable-R"))
    for _ in range(2):
        result = subprocess.run(
            command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20
        )
        assert result.returncode != 0
        assert "separate, non-nested" in result.stderr
        assert not root.exists(), "Preflight must not leave either path behind on rejection"


@pytest.mark.r_backend
def test_installed_batch_example_restarts_replays_and_renders(tmp_path: Path) -> None:
    """Run the real stdio/MCP workflow outside the checkout's import path."""

    output_dir = tmp_path / "batch-output"
    completed = subprocess.run(
        [sys.executable, str(EXAMPLE), "--output-dir", str(output_dir)],
        cwd=tmp_path,
        env=_child_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    receipt = json.loads(completed.stdout)

    assert receipt["profile"] == "measure-chromatogram-batch-example/1"
    assert receipt["counts"] == {
        "pending": 0,
        "running": 0,
        "completed": 4,
        "failed": 0,
        "interrupted": 0,
        "cancelled": 0,
    }
    assert len(receipt["analysis_ids"]) == 4
    assert len(set(receipt["analysis_ids"])) == 4
    first_ids = receipt["first_two_analysis_ids"]
    replay_ids = [
        item["analysis_id"] for item in receipt["run_records"]["replay_first_success_no_r"]["items"]
    ]
    assert replay_ids == first_ids
    assert receipt["run_records"]["replay_original_failure_no_r"]["status"] == "failed"
    assert receipt["run_records"]["replay_original_failure_no_r"]["items"][0]["state"] == ("failed")
    assert receipt["run_records"]["replay_original_failure_no_r"]["items"][0]["item_state"] == (
        "completed"
    )
    assert receipt["original_failure_unknowns"]["runtime"] == {"runtime_status": "unknown"}
    assert receipt["original_failure_unknowns"]["r_runtime_evidence"] == "unknown_until_success"
    assert receipt["internal_standard_references_scope"].startswith("The unknown trace label")
    assert receipt["cancellation_checkpoint"]["items"][3]["state"] == "cancelled"
    assert receipt["control_history"]["events"][0]["items"] == [
        {"ordinal": 3, "from_state": "pending", "to_state": "cancelled"}
    ]
    assert receipt["control_history"]["events"][1]["items"][-1]["from_state"] == "cancelled"
    assert receipt["attempt_pages"]["2"]["total"] == 2
    assert [attempt["state"] for attempt in receipt["attempt_pages"]["2"]["attempts"]] == [
        "failed",
        "completed",
    ]
    assert all(receipt["assertions"].values())
    tables = {}
    for name, expected in {
        "items.csv": 4,
        "attempts.csv": 5,
        "control-events.csv": 2,
        "control-items.csv": 3,
    }.items():
        with (output_dir / name).open(newline="") as stream:
            tables[name] = list(csv.DictReader(stream))
        assert len(tables[name]) == receipt["tables"][name]["row_count"] == expected
        assert (
            hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
            == receipt["tables"][name]["sha256"]
        )
    assert tables["items.csv"][1]["peak_count"] == "0"
    assert (
        len(
            [
                row
                for row in tables["attempts.csv"]
                if row["state"] == "failed" and row["r_version"] == ""
            ]
        )
        == 1
    )
    assert tables["control-items.csv"][0]["from_state"] == "pending"
    assert tables["control-items.csv"][0]["to_state"] == "cancelled"
    assert [transition["idempotency_key"] for transition in receipt["transitions"][:6]] == [
        "batch-example-create-v1",
        "batch-example-run-first-v1",
        "batch-example-run-failure-v1",
        "batch-example-cancel-v1",
        "batch-example-retry-2-3-v1",
        "batch-example-run-retry-v1",
    ]
    assert receipt["transitions"][4]["ordinals"] == [2, 3]
    assert receipt["transitions"][6]["replay"] is True

    assert [item["role"] for item in receipt["snapshots"]["initial"]["manifest"]["inputs"]] == [
        "reference",
        "blank",
        "qc",
        "unknown",
    ]
    assert [
        item["trace"]["sample_type"]
        for item in receipt["snapshots"]["initial"]["manifest"]["inputs"]
    ] == ["reference", "blank", "qc", "unknown"]

    assert (output_dir / "workflow_receipt.json").is_file()
    assert (output_dir / "summary.html").is_file()
    reports = receipt["reports"]
    assert [item["ordinal"] for item in reports] == [0, 1, 2, 3]
    assert [item["peak_count"] for item in reports] == [2, 0, 2, 2]
    for report in reports:
        assert report["result_metadata"]
        assert len(report["stored_result_sha256"]) == 64
        assert set(report["files"]) == {
            "report.html",
            "chromatogram.svg",
            "evidence.json",
            "trace.csv",
            "peaks.csv",
        }
        evidence_path = output_dir / report["files"]["evidence.json"]["path"]
        evidence = json.loads(evidence_path.read_text())
        assert evidence["analysis"]["analysis_id"] == report["analysis_id"]
        assert evidence["result"] == report["result_metadata"]
        assert evidence["analysis"]["stored_result_sha256"] == report["stored_result_sha256"]
        assert (
            hashlib.sha256(
                json.dumps(
                    report["result_metadata"],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            == report["result_metadata_sha256"]
        )
        assert set(evidence["points"][0]) == {
            "sample_index",
            "axis",
            "processed_axis",
            "raw_signal",
            "processed_signal",
        }
        for metadata in report["files"].values():
            path = output_dir / metadata["path"]
            assert path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]
            assert receipt["canonical_file_hashes"][metadata["path"]] == metadata["sha256"]
