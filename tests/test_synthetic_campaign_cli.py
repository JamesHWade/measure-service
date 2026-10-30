"""Installed comparison entrypoint preserves output and isolated stores."""

import subprocess
import sys


def test_installed_comparison_help_names_both_required_locations(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-m", "measure_service.benchmarks", "run-synthetic-campaigns", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--data-dir" in completed.stdout and "--output-dir" in completed.stdout


def test_installed_comparison_refuses_occupied_output_before_creating_a_store(tmp_path):
    output = tmp_path / "occupied"
    output.mkdir()
    marker = output / "original.txt"
    marker.write_text("keep this original artifact")
    data = tmp_path / "data"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "measure_service.benchmarks",
            "run-synthetic-campaigns",
            "--data-dir",
            str(data),
            "--output-dir",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "absent or empty" in completed.stderr
    assert not data.exists()
    assert marker.read_text() == "keep this original artifact"
