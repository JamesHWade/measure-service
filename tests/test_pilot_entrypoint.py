"""The built distribution exposes the pilot command without backend startup."""

import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path


def test_installed_pilot_entrypoint_outside_checkout(tmp_path):
    distribution = metadata.distribution("measure-service")
    entrypoint = next(
        entry for entry in distribution.entry_points if entry.name == "measure-service-pilot"
    )
    assert entrypoint.value == "measure_service.pilot_cli:main"
    suffix = ".exe" if os.name == "nt" else ""
    command = Path(sys.executable).parent / f"measure-service-pilot{suffix}"
    completed = subprocess.run(
        [str(command), "--help"],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=tmp_path,
        check=True,
    )
    assert "prepare" in completed.stdout and "verify" in completed.stdout
    assert not completed.stderr
    assert not list(tmp_path.iterdir()), "Pilot help must not create evidence or a service store"
