"""Installed-runtime checks for the synthetic store recovery example."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

EXAMPLE = Path(__file__).parents[1] / "examples" / "store_recovery.py"
REPOSITORY = Path(__file__).parents[1].resolve()
EXAMPLE_PYTHON = os.environ.get("MEASURE_EXAMPLE_PYTHON", sys.executable)
EXPECTED_OUTPUTS = {
    "source",
    "backup",
    "restored",
    "records.json",
    "workflow_receipt.json",
}


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "MEASURE_BO_PYTHON",
        "MEASURE_MCP_DATA_DIR",
        "MEASURE_MCP_IDENTITY",
        "MEASURE_MCP_R",
    ):
        environment.pop(name, None)
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


def _run_cli(output: Path, *, args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    assert Path(EXAMPLE_PYTHON).is_absolute() and Path(EXAMPLE_PYTHON).is_file()
    command = [EXAMPLE_PYTHON, str(EXAMPLE)]
    if args is None:
        command.extend(["--output-dir", str(output)])
    else:
        command.extend(args)
    return subprocess.run(
        command,
        cwd=output.parent,
        env=_child_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_installed_public_workflow_recovers_and_replays_the_complete_store(tmp_path: Path) -> None:
    output = tmp_path / "store-recovery-output"
    completed = _run_cli(output)
    assert completed.returncode == 0, completed.stderr
    stdout_receipt = json.loads(completed.stdout)
    assert set(path.name for path in output.iterdir()) == EXPECTED_OUTPUTS
    assert not (output / "EXAMPLE_INCOMPLETE").exists()
    if os.name == "posix":
        assert stat.S_IMODE(output.stat().st_mode) == 0o700

    receipt = _json(output / "workflow_receipt.json")
    records = _json(output / "records.json")
    assert stdout_receipt == receipt
    assert receipt["profile"] == "measure-store-recovery-example/1"
    assert receipt["store_profile"] == "measure-store/1"
    assert receipt["backup_profile"] == "measure-store-backup/1"
    assert set(receipt["operator_receipts"]) == {
        "initialize_source",
        "inspect_source_before_restored_write",
        "backup",
        "verify_backup",
        "restore",
        "inspect_source_after_restored_write",
    }
    assert (
        receipt["source_table_counts_before_restored_write"]
        == receipt["source_table_counts_after_restored_write"]
    )
    assert receipt["source_table_counts_before_restored_write"]["experiments"] == 2

    verification = receipt["verification"]
    assert verification == {
        "exact_initial_restore_before_mcp_connect": True,
        "original_receipt_replay_exact": True,
        "historical_retrieval_continuity_exact": True,
        "source_count_unchanged_by_restored_work": True,
        "source_unmodified_by_restored_work": True,
        "backup_unchanged": True,
        "second_restore_rejected_without_target_change": True,
        "r_backend_used": False,
        "optimizer_used": False,
        "instrument_controlled": False,
        "hosted_auth_qualified": False,
    }
    assert records["profile"] == receipt["profile"]
    original_calls = records["original_calls"]
    restored_calls = records["restored_replay_calls"]
    assert len(original_calls) == len(restored_calls) == 10
    assert [call["tool"] for call in original_calls[:4]] == [
        "measurement_create_experiment",
        "measurement_record_attempt",
        "measurement_record_results",
        "measurement_supersede_experiment",
    ]
    assert [_canonical(call["arguments"]) for call in original_calls] == [
        _canonical(call["arguments"]) for call in restored_calls
    ]
    assert [_canonical(call["response"]) for call in original_calls] == [
        _canonical(call["response"]) for call in restored_calls
    ]

    ids = receipt["synthetic_records"]
    assert len(ids["result_ids"]) == 2
    assert ids["initial_experiment_id"] != ids["replacement_experiment_id"]
    assert ids["restored_only_experiment_id"] not in {
        ids["initial_experiment_id"],
        ids["replacement_experiment_id"],
    }
    original_design = original_calls[0]["response"]
    attempt = original_calls[1]["response"]
    results = original_calls[2]["response"]["results"]
    supersession = original_calls[3]["response"]
    assert attempt["experiment_id"] == original_design["experiment_id"]
    assert supersession["entry"]["experiment_id"] == ids["replacement_experiment_id"]
    assert [result["outcome"]["status"] for result in results] == [
        "observed",
        "below_limit",
    ]
    assert results[0]["outcome"]["value"] == 0
    assert results[1]["outcome"]["upper_bound"] == 0.05

    getter_calls = original_calls[4:]
    assert sum(call["tool"] == "measurement_get_result" for call in getter_calls) == 2
    assert (
        sum(call["tool"] == "measurement_get_experiment_design_lineage" for call in getter_calls)
        == 1
    )
    lineage = getter_calls[-1]["response"]
    assert [entry["experiment_id"] for entry in lineage["entries"]] == [
        ids["initial_experiment_id"],
        ids["replacement_experiment_id"],
    ]
    assert lineage["current_experiment_id"] == ids["replacement_experiment_id"]

    manifest = _json(output / "backup" / "manifest.json")
    assert manifest["profile"] == "measure-store-backup/1"
    assert manifest["database"] == "measure.sqlite3"
    assert manifest["database_bytes"] == (output / "backup" / manifest["database"]).stat().st_size
    assert (output / "source" / "measure.sqlite3").is_file()
    assert (output / "restored" / "measure.sqlite3").is_file()

    all_artifacts = completed.stdout + (output / "records.json").read_text(encoding="utf-8")
    all_artifacts += (output / "workflow_receipt.json").read_text(encoding="utf-8")
    assert str(output) not in all_artifacts
    assert "PYTHONPATH" not in all_artifacts


@pytest.mark.parametrize("kind", ["existing", "symlink", "dangling-symlink"])
def test_preflight_rejects_existing_or_symlink_output_without_overwriting(
    tmp_path: Path, kind: str
) -> None:
    output = tmp_path / "store-recovery-output"
    target = tmp_path / "target"
    if kind == "existing":
        output.mkdir()
        (output / "sentinel.txt").write_text("preserve", encoding="utf-8")
    else:
        if kind == "symlink":
            target.mkdir()
        output.symlink_to(target, target_is_directory=True)

    completed = _run_cli(output)
    assert completed.returncode != 0
    assert str(output) not in completed.stdout + completed.stderr
    if kind == "existing":
        assert (output / "sentinel.txt").read_text(encoding="utf-8") == "preserve"
        assert not (output / "EXAMPLE_INCOMPLETE").exists()
    else:
        assert output.is_symlink()
        if kind == "dangling-symlink":
            assert not target.exists()


def test_runtime_failure_keeps_incomplete_marker_and_hides_local_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = importlib.util.spec_from_file_location("store_recovery_example_under_test", EXAMPLE)
    assert spec is not None and spec.loader is not None
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)

    def fail_initialization(data_dir: str) -> None:
        raise RuntimeError(f"synthetic failure at {data_dir}")

    monkeypatch.setattr(example, "initialize_store", fail_initialization)
    output = tmp_path / "failed-recovery-output"
    assert example.main(["--output-dir", str(output)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(output) not in captured.err
    assert json.loads(captured.err)["error"] == "RuntimeError"
    assert (output / "EXAMPLE_INCOMPLETE").is_file()
    assert set(path.name for path in output.iterdir()) == {"EXAMPLE_INCOMPLETE"}


def test_cli_requires_an_explicit_output_directory(tmp_path: Path) -> None:
    completed = _run_cli(tmp_path / "unused", args=[])
    assert completed.returncode != 0
    assert not (tmp_path / "unused").exists()


def test_inventory_rejects_added_empty_directory(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("store_recovery_inventory_under_test", EXAMPLE)
    assert spec is not None and spec.loader is not None
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    root = tmp_path / "flat-backup"
    root.mkdir()
    (root / "database").write_bytes(b"synthetic")
    original = example._inventory(root)
    assert set(original) == {"database"}
    (root / "unexpected-empty-directory").mkdir()
    with pytest.raises(RuntimeError, match="unexpected filesystem entry"):
        example._inventory(root)
