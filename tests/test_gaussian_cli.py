"""Installed offline qualification entrypoint and non-overwrite behavior."""

import hashlib
import json
import subprocess
import sys


def test_gaussian_qualification_cli_uses_packaged_defaults_outside_checkout(tmp_path):
    output = tmp_path / "qualified"
    command = [
        sys.executable,
        "-m",
        "measure_service.benchmarks",
        "qualify-gaussian-producer",
        "--output-dir",
        str(output),
    ]
    completed = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True)
    assert str(output / "report.md") in completed.stdout
    report = json.loads((output / "report.json").read_text())
    assert report["qualified"] is True
    numerical = report["numerical_comparison"]
    assert numerical["passed_case_count"] == numerical["case_count"] == 43
    assert numerical["numeric_match_count"] == numerical["numeric_comparison_count"] == 2057
    assert (
        numerical["null_semantics_match_count"] == numerical["null_semantics_check_count"] == 2489
    )
    assert numerical["absolute_tolerance"] == "0"
    assert report["range_and_input_failure_checks"]["passed_check_count"] == 10
    assert report["scope"]["physical_peak_widths_qualified"] is False
    assert report["scope"]["fresh_optimizer_run"] is False
    assert report["scope"]["fresh_bo_readiness"] is False
    seal_bytes = (output / "complete.json").read_bytes()
    for name, evidence in json.loads(seal_bytes)["files"].items():
        content = (output / name).read_bytes()
        assert len(content) == evidence["size_bytes"]
        assert hashlib.sha256(content).hexdigest() == evidence["sha256"]
    second = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert second.returncode != 0
    assert (output / "complete.json").read_bytes() == seal_bytes
