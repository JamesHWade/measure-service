"""Regression boundaries identified during independent recovery review."""

import re
import sqlite3

import pytest

from measure_service import store_operations as operations


@pytest.mark.parametrize(
    ("table", "pattern", "replacement"),
    [
        ("datasets", r"owner TEXT NOT NULL", "owner TEXT"),
        ("analyses", r",\s*FOREIGN KEY\(dataset_id\) REFERENCES datasets\(id\)", ""),
        (
            "experiment_results",
            r",\s*FOREIGN KEY\(attempt_id, attempt_revision\) REFERENCES attempt_revisions\(attempt_id, revision\)",
            "",
        ),
        ("experiment_results", r"supersedes_id TEXT UNIQUE", "supersedes_id TEXT"),
        ("gaussian_observations", r"UNIQUE\(owner, attempt_id, attempt_revision\),", ""),
    ],
)
def test_missing_service_constraints_fail_closed(tmp_path, table, pattern, replacement):
    source = tmp_path / "source"
    operations.initialize_store(source)
    path = source / operations.DATABASE_NAME
    with sqlite3.connect(path) as con:
        original = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        changed, count = re.subn(pattern, replacement, original, count=1)
        assert count == 1
        indexes = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        ).fetchall()
        # Rebuild this empty table with one weakened constraint, retaining its
        # explicit indexes. The database remains valid SQLite; the service's
        # physical contract must independently reject the missing protection.
        con.execute(f'DROP TABLE "{table}"')
        con.execute(changed)
        for (sql,) in indexes:
            con.execute(sql)
    before = path.read_bytes()
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    with pytest.raises(operations.StoreOperationError):
        operations.inspect_store(source)
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["initialize", "inspect", "backup", "verify", "restore"])
def test_all_public_operations_translate_preflight_deadline(tmp_path, monkeypatch, operation):
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    operations.initialize_store(source)
    operations.backup_store(source, backup)
    calls = {
        "initialize": lambda: operations.initialize_store(tmp_path / "new-store"),
        "inspect": lambda: operations.inspect_store(source),
        "backup": lambda: operations.backup_store(source, tmp_path / "new-backup"),
        "verify": lambda: operations.verify_backup(backup),
        "restore": lambda: operations.restore_store(backup, tmp_path / "restored"),
    }
    monkeypatch.setattr(operations, "_OPERATION_TIMEOUT_SECONDS", 0)
    with pytest.raises(operations.StoreOperationError, match="time limit"):
        calls[operation]()


def test_unsupported_operator_host_fails_before_path_changes(tmp_path, monkeypatch):
    target = tmp_path / "unsupported-store"
    with monkeypatch.context() as patch:
        patch.setattr(operations.os, "name", "unsupported")
        with pytest.raises(operations.StoreOperationError, match="POSIX"):
            operations.initialize_store(target)
    assert not target.exists()
