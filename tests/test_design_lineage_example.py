"""Installed public-MCP coverage for the immutable design-lineage example."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

EXPECTED_OUTPUTS = {
    "designs.json",
    "lineage.json",
    "replacement-v1-v2.json",
    "replacement-v2-v3.json",
    "attempts.json",
    "results.json",
    "observations.json",
    "snapshot.json",
    "history.csv",
    "workflow_receipt.json",
    "index.html",
    "manifest.json",
}


def _script() -> Path:
    return Path(__file__).parents[1] / "examples" / "design_lineage.py"


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "MEASURE_MCP_DATA_DIR", "MEASURE_MCP_IDENTITY", "MEASURE_MCP_R"):
        environment.pop(name, None)
    return environment


def _run_cli(output: Path, data: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_script()), "--output-dir", str(output), "--data-dir", str(data)],
        cwd=output.parent,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_installed_public_workflow_preserves_design_history_and_artifact_hashes(tmp_path):
    output = tmp_path / "design-lineage-output"
    data = tmp_path / "design-lineage-data"
    completed = _run_cli(output, data)
    assert completed.returncode == 0, completed.stderr
    stdout_receipt = json.loads(completed.stdout)

    assert set(path.name for path in output.iterdir()) == EXPECTED_OUTPUTS
    assert not (output / "EXAMPLE_INCOMPLETE").exists()
    assert data.is_dir()
    receipt = _json(output / "workflow_receipt.json")
    assert stdout_receipt == receipt
    assert receipt["profile"] == "measure-experiment-design-example/1"
    assert receipt["counts"] == {
        "designs": 3,
        "attempts": 3,
        "attempt_revisions": 5,
        "results": 8,
        "assessments": 2,
        "snapshots": 1,
    }

    methods = receipt["methods"]
    assert [
        method["evaluation"]["declaration"]["program"]["acquisition_duration"] for method in methods
    ] == [
        7.0,
        8.0,
        9.0,
    ]
    assert [
        method["evaluation"]["declaration"]["program"]["state"]["flow"] for method in methods
    ] == [0.4, 0.45, 0.5]
    designs = receipt["designs"]
    assert len({design["experiment_id"] for design in designs}) == 3
    assert [design["spec"]["executable_method_id"] for design in designs] == [
        method["method_id"] for method in methods
    ]
    assert all(
        design["spec"]["observation_contract_id"] == receipt["contract"]["contract_id"]
        for design in designs
    )

    lineage = receipt["lineage"]
    assert [entry["revision"] for entry in lineage["entries"]] == [1, 2, 3]
    assert [entry["experiment_id"] for entry in lineage["entries"]] == [
        design["experiment_id"] for design in designs
    ]
    assert lineage["current_revision"] == 3
    assert lineage["current_experiment_id"] == designs[-1]["experiment_id"]
    assert _json(output / "designs.json")["records"] == designs
    assert _json(output / "lineage.json") == lineage
    assert _json(output / "replacement-v1-v2.json") == receipt["replacement_v1_v2"]
    assert _json(output / "replacement-v2-v3.json") == receipt["replacement_v2_v3"]

    attempts = receipt["attempts"]
    assert _json(output / "attempts.json") == attempts
    assert len(attempts["records"]) == 5
    assert len(attempts["latest"]) == 3
    assert attempts["total_revisions"] == 5
    assert (
        attempts["revision_views"]["old_revision_1_before"]["latest_revision"] == 1
        and attempts["revision_views"]["old_revision_1_after"]["latest_revision"] == 2
    )
    assert (
        attempts["revision_views"]["v2_revision_1_before"]["latest_revision"] == 1
        and attempts["revision_views"]["v2_revision_1_after"]["latest_revision"] == 2
    )
    v1_id, v2_id, v3_id = (design["experiment_id"] for design in designs)
    assert sum(attempt["experiment_id"] == v1_id for attempt in attempts["latest"]) == 2
    assert sum(attempt["experiment_id"] == v2_id for attempt in attempts["latest"]) == 1
    assert all(attempt["experiment_id"] != v3_id for attempt in attempts["latest"])
    old_latest = next(
        attempt for attempt in attempts["latest"] if attempt["experiment_id"] == v1_id
    )
    v2_latest = next(attempt for attempt in attempts["latest"] if attempt["experiment_id"] == v2_id)
    assert old_latest["revision"] == 2
    assert v2_latest["revision"] == 2
    assert v2_latest["spec"]["actual_conditions"][0]["value"] == 0.44
    assert v2_latest["executable_method"]["method_id"] == methods[1]["method_id"]
    old_result_attempt_id = next(
        attempt["attempt_id"]
        for attempt in attempts["records"]
        if attempt["experiment_id"] == v1_id and attempt["revision"] == 1
    )

    results = receipt["results"]["records"]
    assert _json(output / "results.json")["records"] == results
    assert len(results) == 8
    assert len({result["result_id"] for result in results}) == 8
    assert {result["outcome"]["status"] for result in results} == {"observed"}
    assert {result["outcome"]["unit"] for result in results} == {"min", "1"}
    assert {result["attempt_id"] for result in results} == {
        old_result_attempt_id,
        v2_latest["attempt_id"],
    }
    v2_result_ids = [
        result["result_id"] for result in results if result["attempt_id"] == v2_latest["attempt_id"]
    ]
    assert len(v2_result_ids) == 4
    assert (
        next(
            result["outcome"]["value"]
            for result in results
            if result["result_id"] in v2_result_ids
            and result["outcome"]["analyte_reference"] == "B"
            and result["outcome"]["metric"] == "resolution"
        )
        == 0
    )

    observations = receipt["observations"]
    assert _json(output / "observations.json") == observations
    assert _json(output / "snapshot.json")["snapshot"] == receipt["snapshot"]
    assert [
        observation["observation"]["assessment"]["analytical_acceptance"]
        for observation in observations["records"]
    ] == ["accepted", "not_accepted"]
    assert observations["old_after_later_work"] == observations["records"][0]
    assert observations["snapshot_after_later_work"] == receipt["snapshot"]
    assert receipt["verification"]["lineage_after_restart_exact"] is True
    assert receipt["verification"]["restart_replay_exact"] is True
    assert receipt["verification"]["stale_v1_rejected"] is True
    assert receipt["verification"]["experiment_count_after_stale_v1"] == 3
    assert receipt["verification"]["r_backend_used"] is False

    history = (output / "history.csv").read_text(encoding="utf-8")
    assert history.splitlines()[0].split(",") == [
        "record_type",
        "record_id",
        "parent_id",
        "revision",
        "status",
        "metric",
        "analyte",
        "value",
        "unit",
        "reason",
        "input_sha256",
        "digest",
    ]
    history_lines = history.splitlines()[1:]
    assert sum(line.startswith("design,") for line in history_lines) == 3
    assert sum(line.startswith("attempt,") for line in history_lines) == 5
    assert sum(line.startswith("result,") for line in history_lines) == 8

    manifest = _json(output / "manifest.json")
    assert {entry["path"] for entry in manifest["files"]} == EXPECTED_OUTPUTS - {"manifest.json"}
    for entry in manifest["files"]:
        content = (output / entry["path"]).read_bytes()
        assert entry["bytes"] == len(content)
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()

    document = (output / "index.html").read_text(encoding="utf-8")
    assert "<main>" in document
    assert 'role="region"' in document
    assert 'tabindex="0"' in document
    assert "not_accepted" in document
    assert "mL/min" in document
    assert "current pointer is discovery only" in document.lower()
    assert "<script" not in document.lower()
    assert "http://" not in document and "https://" not in document
    assert str(data) not in document


@pytest.mark.parametrize(
    "kind", ["existing-output", "existing-data", "nested", "dangling-output", "dangling-data"]
)
def test_preflight_rejects_conflicts_before_creating_any_target(tmp_path, kind):
    output = tmp_path / "output"
    data = tmp_path / "data"
    if kind == "existing-output":
        output.mkdir()
        (output / "sentinel.txt").write_text("keep", encoding="utf-8")
    elif kind == "existing-data":
        data.mkdir()
        (data / "sentinel.txt").write_text("keep", encoding="utf-8")
    elif kind == "nested":
        data = output / "data"
    elif kind == "dangling-output":
        output.symlink_to(tmp_path / "missing-output", target_is_directory=True)
    else:
        data.symlink_to(tmp_path / "missing-data", target_is_directory=True)

    completed = _run_cli(output, data)
    assert completed.returncode != 0
    if kind == "existing-output":
        assert (output / "sentinel.txt").read_text(encoding="utf-8") == "keep"
        assert not data.exists()
    elif kind == "existing-data":
        assert (data / "sentinel.txt").read_text(encoding="utf-8") == "keep"
        assert not output.exists()
    elif kind == "nested":
        assert not output.exists()
        assert not data.exists()
    elif kind == "dangling-output":
        assert output.is_symlink()
        assert not data.exists()
    else:
        assert data.is_symlink()
        assert not output.exists()
