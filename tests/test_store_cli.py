from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from measure_service.store_operations import DATABASE_NAME, MANIFEST_NAME


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("MEASURE_MCP_DATA_DIR", None)
    return subprocess.run(
        [sys.executable, "-m", "measure_service.store_cli", *args],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_runs_all_store_operations_outside_current_directory(tmp_path: Path) -> None:
    store = tmp_path / "fresh-store"
    backup = tmp_path / "sealed-backup"
    restored = tmp_path / "restored-store"

    initialized = _run(tmp_path, "initialize", "--data-dir", str(store))
    assert initialized.returncode == 0
    assert json.loads(initialized.stdout)["status"] == "initialized"
    assert initialized.stderr == ""

    inspected = _run(tmp_path, "inspect", "--data-dir", str(store))
    assert inspected.returncode == 0
    assert json.loads(inspected.stdout)["status"] == "valid"

    created = _run(
        tmp_path,
        "backup",
        "--data-dir",
        str(store),
        "--output-dir",
        str(backup),
    )
    assert created.returncode == 0
    assert json.loads(created.stdout)["status"] == "created"
    assert sorted(path.name for path in backup.iterdir()) == [MANIFEST_NAME, DATABASE_NAME]

    verified = _run(tmp_path, "verify", "--backup-dir", str(backup))
    assert verified.returncode == 0
    assert json.loads(verified.stdout)["status"] == "verified"

    restored_result = _run(
        tmp_path,
        "restore",
        "--backup-dir",
        str(backup),
        "--data-dir",
        str(restored),
    )
    assert restored_result.returncode == 0
    assert json.loads(restored_result.stdout)["status"] == "restored"
    assert (backup / DATABASE_NAME).read_bytes() == (restored / DATABASE_NAME).read_bytes()


def test_cli_errors_are_nonzero_and_do_not_echo_operator_paths(tmp_path: Path) -> None:
    private_path = tmp_path / "private-store-name"
    failed = _run(tmp_path, "inspect", "--data-dir", str(private_path))
    assert failed.returncode != 0
    assert failed.stdout == ""
    assert "error:" in failed.stderr
    assert str(private_path) not in failed.stderr

    help_result = _run(tmp_path, "--help")
    assert help_result.returncode == 0
    assert "measure-service-store" in help_result.stdout
    assert help_result.stderr == ""
