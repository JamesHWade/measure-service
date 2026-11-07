"""Public snapshot/restore example checks."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).parents[1] / "examples" / "batch_snapshot.py"
REPOSITORY = Path(__file__).parents[1].resolve()


def _load_example_file(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Load shipped example artifacts by filename. Service imports still resolve
# exclusively from the installed distribution when pytest runs outside the checkout.
_previous_helper = sys.modules.get("chromatogram_batch")
sys.modules["chromatogram_batch"] = _load_example_file(
    EXAMPLE.with_name("chromatogram_batch.py"), "snapshot_batch_support"
)
try:
    batch_snapshot = _load_example_file(EXAMPLE, "snapshot_example_under_test")
finally:
    if _previous_helper is None:
        del sys.modules["chromatogram_batch"]
    else:
        sys.modules["chromatogram_batch"] = _previous_helper
_compare_evidence = batch_snapshot._compare_evidence


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["MEASURE_MCP_R"] = environment.get("MEASURE_MCP_TEST_R", "R")
    retained = [
        entry
        for entry in environment.get("PYTHONPATH", "").split(os.pathsep)
        if entry and Path(entry).resolve() not in {REPOSITORY, REPOSITORY / "src"}
    ]
    if retained:
        environment["PYTHONPATH"] = os.pathsep.join(retained)
    else:
        environment.pop("PYTHONPATH", None)
    return environment


@pytest.mark.parametrize(
    "layout",
    ["same", "output-in-source", "source-in-output", "restore-in-output"],
)
def test_snapshot_example_rejects_equal_or_nested_targets_before_creation(
    tmp_path: Path, layout: str
) -> None:
    root = tmp_path / "new-root"
    if layout == "same":
        output = source = restore = root
    elif layout == "output-in-source":
        source = root
        output = root / "output"
        restore = tmp_path / "restore"
    elif layout == "source-in-output":
        output = root
        source = root / "source"
        restore = tmp_path / "restore"
    else:
        output = root
        source = tmp_path / "source"
        restore = root / "restore"
    completed = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE),
            "--output-dir",
            str(output),
            "--source-dir",
            str(source),
            "--restore-dir",
            str(restore),
        ],
        cwd=tmp_path,
        env=_child_environment(),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "separate, non-nested" in completed.stderr
    assert not root.exists()
    assert not (tmp_path / "source").exists()
    assert not (tmp_path / "restore").exists()


def test_snapshot_example_rejects_existing_target_without_clobbering(tmp_path: Path) -> None:
    output = tmp_path / "output"
    source = tmp_path / "source"
    restore = tmp_path / "restore"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("caller-owned", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE),
            "--output-dir",
            str(output),
            "--source-dir",
            str(source),
            "--restore-dir",
            str(restore),
        ],
        cwd=tmp_path,
        env=_child_environment(),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "must be new" in completed.stderr
    assert marker.read_text(encoding="utf-8") == "caller-owned"
    assert not source.exists()
    assert not restore.exists()


def test_snapshot_example_rejects_dangling_store_links_before_output_creation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    source = tmp_path / "source-link"
    restore = tmp_path / "restore-link"
    source.symlink_to(tmp_path / "missing-source")
    restore.symlink_to(tmp_path / "missing-restore")

    completed = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE),
            "--output-dir",
            str(output),
            "--source-dir",
            str(source),
            "--restore-dir",
            str(restore),
        ],
        cwd=tmp_path,
        env=_child_environment(),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "must be new" in completed.stderr
    assert not output.exists()
    assert source.is_symlink()
    assert restore.is_symlink()


def test_snapshot_example_reserves_source_after_preflight_before_starting_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    source = tmp_path / "source"
    restore = tmp_path / "restore"
    original = batch_snapshot._preflight_new_paths

    def race(paths: dict[str, Path]) -> dict[str, Path]:
        resolved = original(paths)
        paths["source"].mkdir()
        return resolved

    monkeypatch.setattr(batch_snapshot, "_preflight_new_paths", race)
    with pytest.raises(FileExistsError, match="created after preflight"):
        batch_snapshot.run(output, source_dir=str(source), restore_dir=str(restore))
    assert not output.exists()
    assert source.is_dir()
    assert list(source.iterdir()) == []
    assert not restore.exists()


def _comparison_fixture() -> dict:
    return {
        "trace": {"trace_id": "reference", "run_order": 1},
        "result": {
            "kind": "chromatogram",
            "peak_count": 1,
            "warnings": [],
            "notes": [],
            "summary": {"score": 1.0},
        },
        "analysis": {"analysis_id": "analysis-1", "method": {"baseline_lambda": 1_000_000.0}},
        "points": [
            {
                "sample_index": 0,
                "axis": 0.0,
                "processed_axis": 0.0,
                "raw_signal": 0.0,
                "processed_signal": 0.0,
            }
        ],
        "peaks": [
            {
                "peak_id": 1,
                "location": 1.0,
                "height": 2.0,
                "left_base": 0.0,
                "right_base": 2.0,
                "area": 1.5,
                "prominence": None,
                "area_basis": "local",
            }
        ],
    }


def test_snapshot_comparison_tolerates_nested_numeric_drift_but_not_categories() -> None:
    source = _comparison_fixture()
    fresh = copy.deepcopy(source)
    fresh["result"]["summary"]["score"] += 1e-11
    comparison = _compare_evidence(source, fresh)
    assert comparison["passed"] is True
    assert comparison["numeric_comparison_count"] > 0
    assert comparison["comparison_count"] == (
        comparison["numeric_comparison_count"] + comparison["categorical_comparison_count"]
    )

    fresh = copy.deepcopy(source)
    fresh["result"]["summary"]["score"] += 1e-3
    with pytest.raises(AssertionError, match="exceeded declared tolerance"):
        _compare_evidence(source, fresh)

    fresh = copy.deepcopy(source)
    fresh["peaks"][0]["peak_id"] = 1.0
    with pytest.raises(AssertionError, match="peak 0 ID"):
        _compare_evidence(source, fresh)


def test_runtime_build_changes_are_observed_while_trace_and_method_identity_stay_exact():
    source = _comparison_fixture()
    source["result"].update(packages={"measure": "old"}, installed_r_code_md5=None, r_version="old")
    source["analysis"]["provenance"] = {"packages": {"measure": "old"}, "worker_sha256": "0" * 64}
    fresh = copy.deepcopy(source)
    fresh["result"].update(
        packages={"measure": "new"}, installed_r_code_md5={"measure": "1" * 32}, r_version="new"
    )
    fresh["analysis"]["provenance"] = {"packages": {"measure": "new"}, "worker_sha256": "1" * 64}
    comparison = _compare_evidence(source, fresh)
    assert comparison["passed"] is True
    observations = comparison["execution_provenance"]
    assert observations["equality_required"] is False
    assert observations["source"] == source["analysis"]["provenance"]
    assert observations["fresh"] == fresh["analysis"]["provenance"]
    assert observations["source_result_fields"]["r_version"] == "old"
    assert observations["fresh_result_fields"]["r_version"] == "new"
    fresh["analysis"]["method"]["baseline_lambda"] += 1e-4
    with pytest.raises(AssertionError, match="processing method identity changed"):
        _compare_evidence(source, fresh)
    fresh = copy.deepcopy(source)
    source["trace"]["run_order"] = 10**12
    fresh["trace"]["run_order"] = 10**12 + 1
    with pytest.raises(AssertionError, match="trace metadata identity changed"):
        _compare_evidence(source, fresh)


@pytest.mark.r_backend
def test_installed_snapshot_example_archives_imports_and_reexecutes(tmp_path: Path) -> None:
    output = tmp_path / "output"
    source = tmp_path / "source"
    restore = tmp_path / "restore"
    completed = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE),
            "--output-dir",
            str(output),
            "--source-dir",
            str(source),
            "--restore-dir",
            str(restore),
        ],
        cwd=tmp_path,
        env=_child_environment(),
        capture_output=True,
        text=True,
        timeout=420,
    )
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    receipt = json.loads(completed.stdout)
    assert json.loads((output / "workflow_receipt.json").read_text(encoding="utf-8")) == receipt
    assert receipt["profile"] == "measure-chromatogram-batch-snapshot-example/1"

    source_info = receipt["source"]
    assert source_info["dataset"]["trace_count"] == 5
    assert source_info["dataset"]["point_count"] == 2505
    assert source_info["dataset"]["selected_point_count"] == 2004
    assert source_info["dataset"]["selected_trace_ids"] == [
        "reference",
        "blank",
        "qc",
        "unknown",
    ]
    assert source_info["dataset"]["unselected_trace_ids"] == ["unselected"]
    assert source_info["dataset"]["source_text_line_ending"] == "CRLF"
    assert (
        source_info["database_sha256_before_exports"]
        == source_info["database_sha256_after_exports"]
    )
    assert source_info["history"]["frozen"]["counts"] == {
        "pending": 2,
        "running": 0,
        "completed": 2,
        "failed": 0,
        "interrupted": 0,
        "cancelled": 0,
    }
    assert source_info["history"]["frozen"]["point_count"] == 2004
    assert sum(page["total"] for page in source_info["history"]["attempts"].values()) == 3
    assert source_info["history"]["controls"]["total"] == 2
    assert source_info["history"]["original_failure_unknowns"]["runtime"] == {
        "runtime_status": "unknown"
    }
    assert source_info["history"]["original_failure_unknowns"]["r_runtime_evidence"] == (
        "unknown_until_success"
    )

    for mode, archive_name in {
        "references": "source-references.zip",
        "self_contained": "source-self-contained.zip",
    }.items():
        snapshot = source_info["snapshots"][mode]
        archive = output / snapshot["archive"]["path"]
        assert archive.is_file()
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == snapshot["archive"]["sha256"]
        assert archive.name == archive_name
        with zipfile.ZipFile(archive) as opened:
            assert {info.filename for info in opened.infolist()} == {
                "manifest.json",
                "snapshot.json",
            }
            manifest = json.loads(opened.read("manifest.json"))
            assert manifest["mode"] == mode
            assert manifest["snapshot_sha256"] == snapshot["manifest"]["snapshot_sha256"]
            if mode == "self_contained":
                document = json.loads(opened.read("snapshot.json"))
                assert len(document["attempts"]) == 3
                assert len(document["control_events"]) == 2
                assert len(document["runs"]) == 2
                assert any(run["status"] == "failed" for run in document["runs"])

    restore_info = receipt["restore"]
    assert restore_info["reference_restore_rejected"] is True
    assert restore_info["reference_target_created"] is False
    assert len(restore_info["receipt"]["dataset_map"]) == 1
    assert len(restore_info["receipt"]["analysis_map"]) == 2
    assert all(
        source_id != local_id
        for source_id, local_id in restore_info["receipt"]["analysis_map"].items()
    )
    assert restore_info["receipt"]["no_execution_performed"] is True
    assert restore_info["public_receipt"] == restore_info["receipt"]
    assert restore_info["restored_history_before_fresh"] == {
        "attempts": 0,
        "controls": 0,
        "runs": 0,
        "no_execution_performed": True,
    }
    assert restore_info["restored_batch_before_fresh"]["counts"] == {
        "pending": 4,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "interrupted": 0,
        "cancelled": 0,
    }

    historical = receipt["historical_imports"]
    fresh = receipt["fresh_reexecution"]
    assert len(historical) == 2
    assert len(fresh) == 4
    historical_ids = [record["analysis_id"] for record in historical]
    fresh_ids = [record["analysis_id"] for record in fresh]
    assert len(set(historical_ids + fresh_ids)) == 6
    assert receipt["analysis_ids"]["restored_store_unique_total"] == 6
    assert receipt["analysis_ids"]["source_and_restore_unique_total"] == 8
    assert all(
        "import_origin" in record["analysis"] and "input_import_origin" in record["analysis"]
        for record in historical
    )
    assert all(
        "import_origin" not in record["analysis"] and "input_import_origin" in record["analysis"]
        for record in fresh
    )
    assert len(receipt["comparisons"]) == 2
    assert all(comparison["passed"] for comparison in receipt["comparisons"])
    assert all(comparison["point_count"] == 501 for comparison in receipt["comparisons"])
    for comparison in receipt["comparisons"]:
        assert comparison["comparison_count"] == (
            comparison["numeric_comparison_count"] + comparison["categorical_comparison_count"]
        )
    assert receipt["comparison_scope"] == {
        "original_successful_item_count": 2,
        "fresh_completed_item_count": 4,
        "compared_item_count": 2,
        "original_unresolved_source_items": [
            {"ordinal": 2, "role": "qc", "state": "pending"},
            {"ordinal": 3, "role": "unknown", "state": "pending"},
        ],
        "unresolved_items_are_not_reclassified": True,
    }

    report_paths = []
    for record in historical + fresh:
        report = record["report"]
        assert report["point_count"] == 501
        assert set(report["files"]) == {
            "report.html",
            "chromatogram.svg",
            "evidence.json",
            "trace.csv",
            "peaks.csv",
        }
        for metadata in report["files"].values():
            path = output / metadata["path"]
            assert path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]
        report_paths.append(report["files"]["report.html"]["path"])
    assert len(set(report_paths)) == 6

    table = output / receipt["tables"]["analysis-index.csv"]["path"]
    assert table.is_file()
    assert (
        hashlib.sha256(table.read_bytes()).hexdigest()
        == receipt["tables"]["analysis-index.csv"]["sha256"]
    )
    assert len(table.read_text(encoding="utf-8").splitlines()) == 7
    summary = output / receipt["summary"]["path"]
    assert summary.is_file()
    summary_text = summary.read_text(encoding="utf-8")
    assert 'role="region" aria-label="Snapshot analysis reports"' in summary_text
    assert "Imported historical analyses" in summary_text
    assert "Fresh re-executions" in summary_text
    assert "without optimization" in receipt["scope"]
    assert receipt["assertions"]["no_hidden_retry_or_optimization_or_physical_claim"] is True
