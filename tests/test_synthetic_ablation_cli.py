"""The installed fixed-study command preserves caller artifacts and stores."""

import subprocess
import sys


def test_ablation_command_requires_separate_data_and_output_locations(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "measure_service.benchmarks", "run-synthetic-ablations", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--data-dir" in result.stdout
    assert "--output-dir" in result.stdout


def test_ablation_command_rejects_occupied_output_before_creating_store(tmp_path):
    output = tmp_path / "existing-report"
    output.mkdir()
    original = output / "original.txt"
    original.write_text("Keep the original report.")
    data = tmp_path / "new-store"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "measure_service.benchmarks",
            "run-synthetic-ablations",
            "--data-dir",
            str(data),
            "--output-dir",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "absent or empty" in result.stderr
    assert not data.exists()
    assert original.read_text() == "Keep the original report."
