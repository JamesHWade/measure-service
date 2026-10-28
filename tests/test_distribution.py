"""Check installed entrypoints, worker data and documented client workflows."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import measure_service

ROOT = Path(__file__).resolve().parents[1]


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
