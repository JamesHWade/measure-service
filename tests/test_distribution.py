"""Check installed entrypoints, worker data and documented client workflows."""

import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

import measure_service

ROOT = Path(__file__).resolve().parents[1]


def test_service_distribution_contains_both_import_packages_and_worker():
    distribution = metadata.distribution("measure-service")
    files = {str(path) for path in distribution.files or ()}
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")

    if direct_url.get("dir_info", {}).get("editable"):
        pytest.skip("An editable install cannot verify built distribution contents")

    assert "measure_methods/__init__.py" in files
    assert "measure_methods/volume.py" in files
    assert "measure_service/worker.R" in files


def test_method_backend_imports_without_service_or_mcp(tmp_path):
    probe = """
import sys
from measure_methods import constant_solvent_use, program_solvent_use

assert constant_solvent_use(1, 1, {"A": 1}) == {"A": 1.0}
assert program_solvent_use([0, 1], [1, 1], [{"A": 1}, {"A": 1}]) == {"A": 1.0}
assert "measure_service" not in sys.modules
assert "mcp" not in sys.modules
"""
    subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, check=True)


def test_installed_cli_and_worker_outside_checkout(tmp_path):
    worker = Path(measure_service.__file__).with_name("worker.R")
    assert worker.is_file()
    assert 'input$operation, "capabilities"' in worker.read_text()
    suffix = ".exe" if os.name == "nt" else ""
    command = Path(sys.executable).parent / f"measure-service{suffix}"
    completed = subprocess.run(
        [str(command)],
        input="",
        capture_output=True,
        text=True,
        timeout=15,
        cwd=tmp_path,
        check=True,
    )
    assert not completed.stdout, "Stdio startup must not print non-protocol output"


def test_registry_example_outside_checkout(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "registry.py")],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result["observed_zero"] == 0
    assert result["below_limit"] == 0.05
    assert len(result["result_ids"]) == 2


@pytest.mark.r_backend
def test_analysis_example_outside_checkout(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "analyze_trace.py")],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=tmp_path,
        check=True,
        env=dict(os.environ, MEASURE_MCP_R=os.environ.get("MEASURE_MCP_TEST_R", "R")),
    )
    result = json.loads(completed.stdout)
    assert result["peak_count"] == 1
    assert len(result["input_sha256"]) == 64


def test_observation_example_outside_checkout_preserves_three_valued_evidence(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "observations.py")],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
        check=True,
    )
    evidence = json.loads(completed.stdout)
    assert evidence["analytical_acceptance"] == "accepted"
    assert evidence["feasible"] is True
    assert evidence["runtime_preference_satisfied"] is False
    assert evidence["corrected_acceptance"] == "not_accepted"
    assert evidence["ambiguous_acceptance"] == "unknown"
    assert evidence["historical_observation_unchanged"] is True
    assert evidence["origin"] == "simulator_latent"
    assert len(evidence["snapshot_sha256"]) == 64


def test_retention_example_outside_checkout_keeps_censoring_and_qualification_limits(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "retention.py")],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
        check=True,
    )
    evidence = json.loads(completed.stdout)
    assert evidence["profile"] == "ideal-lc-retention-zeta/1"
    assert evidence["eluted_retention_time_min"] == pytest.approx(2.2, abs=1e-12, rel=0)
    assert evidence["geometry_dead_time_min"] == pytest.approx(0.5, abs=1e-12, rel=0)
    assert evidence["post_column_transit_min"] == 0.2
    assert evidence["censored_status"] == "not_eluted"
    assert evidence["censored_retention_time_min"] is None
    assert evidence["right_censor_bound_min"] == 10
    assert evidence["qualification_scope"]["physical_retention_qualification_established"] is False
    assert evidence["qualification_scope"]["utility_score_calculated"] is False
