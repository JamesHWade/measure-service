"""Exercise the documented method workflow using the installed service."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_method_example_outside_checkout(tmp_path):
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, str(root / "examples" / "methods.py")],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result["intended_method_id"] != result["reported_method_id"]
    assert result["acquisition_duration_min"] == 2
    assert result["analytical_solvent_use_ml"] == pytest.approx({"A": 2.2, "B": 1.8})
    assert len(result["input_sha256"]) == 64
