"""Regression boundaries identified during independent recovery review."""

import os
import re
import sqlite3
from contextlib import contextmanager

import pytest

from measure_service import campaign_schema, experiments, storage
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


def test_default_rollback_journal_excludes_uncommitted_rows(tmp_path, monkeypatch):
    source = tmp_path / "source"
    operations.initialize_store(source)
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(source))
    committed = experiments.create_experiment(
        "owner", experiments.ExperimentSpec(title="Committed design", source="manual"), "committed"
    )
    path = source / operations.DATABASE_NAME
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO experiments VALUES ('uncommitted', 'owner', '{}', 'digest', 'now')"
        )
        journal = source / f"{operations.DATABASE_NAME}-journal"
        assert journal.is_file()
        source_bytes, journal_bytes = path.read_bytes(), journal.read_bytes()
        backup = tmp_path / "backup"
        receipt = operations.backup_store(source, backup)
        assert receipt["table_counts"]["experiments"] == 1
        with sqlite3.connect(backup / operations.DATABASE_NAME) as snapshot:
            assert snapshot.execute("SELECT id FROM experiments").fetchall() == [
                (committed["experiment_id"],)
            ]
        assert writer.execute("SELECT COUNT(*) FROM experiments").fetchone() == (2,)
        assert path.read_bytes() == source_bytes
        assert journal.read_bytes() == journal_bytes
        assert {p.name for p in backup.iterdir()} == {
            operations.DATABASE_NAME,
            operations.MANIFEST_NAME,
        }
        operations.verify_backup(backup)
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_rollback_journal_rejects_unsafe_entries(tmp_path, kind):
    source = tmp_path / "source"
    operations.initialize_store(source)
    journal = source / f"{operations.DATABASE_NAME}-journal"
    other = tmp_path / "preserve"
    other.write_bytes(b"operator-owned sentinel")
    if kind == "symlink":
        journal.symlink_to(other)
    elif kind == "hardlink":
        journal.hardlink_to(other)
    else:
        journal.mkdir()
    with pytest.raises(operations.StoreOperationError):
        operations.backup_store(source, tmp_path / "backup")
    assert other.read_bytes() == b"operator-owned sentinel"
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("vanishing", ["missing", "unlinked_stat"])
@pytest.mark.parametrize("main_database", [False, True])
def test_inventory_tolerates_only_optional_files_disappearing(
    tmp_path, monkeypatch, vanishing, main_database
):
    source = tmp_path / "source"
    operations.initialize_store(source)
    selected = operations.DATABASE_NAME
    if not main_database:
        selected += "-journal"
        (source / selected).write_bytes(b"")
    original_scandir = os.scandir
    observed = []

    class DisappearingEntry:
        def __init__(self, entry):
            self.entry = entry
            self.name = entry.name

        def stat(self, *, follow_symlinks):
            info = self.entry.stat(follow_symlinks=follow_symlinks)
            observed.append(self.name)
            if not main_database:
                (source / selected).unlink()
            if vanishing == "missing":
                raise FileNotFoundError
            fields = list(info)
            fields[3] = 0  # st_nlink can become zero during an unlink race.
            return os.stat_result(fields)

        def is_symlink(self):
            return self.entry.is_symlink()

    @contextmanager
    def inventory(path):
        with original_scandir(path) as entries:
            yield (
                DisappearingEntry(entry)
                if os.fspath(path) == os.fspath(source) and entry.name == selected
                else entry
                for entry in entries
            )

    monkeypatch.setattr(operations.os, "scandir", inventory)
    target = tmp_path / "backup"
    if main_database:
        before = (source / selected).read_bytes()
        with pytest.raises(operations.StoreOperationError):
            operations.backup_store(source, target)
        assert not target.exists()
        assert (source / selected).read_bytes() == before
    else:
        operations.backup_store(source, target)
        operations.verify_backup(target)
    assert observed == [selected]


@pytest.mark.parametrize("mutation", [None, "nullable_owner", "changed_pending_literal"])
def test_builtin_proposal_migration_preserves_compatible_schema(tmp_path, mutation):
    source = tmp_path / "source"
    operations.initialize_store(source)
    path = source / operations.DATABASE_NAME
    con = sqlite3.connect(path)
    try:
        con.execute("DROP TABLE campaign_proposals")
        con.execute(campaign_schema._LEGACY_DDL)
        con.execute(campaign_schema._PENDING_INDEX_SQL)
        con.commit()
    finally:
        con.close()
    # Exercise the same startup migration as a deployed older service store.
    storage._connect_at(path).close()
    con = sqlite3.connect(path)
    try:
        sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE name='campaign_proposals'"
        ).fetchone()[0]
        assert sql.startswith('CREATE TABLE "campaign_proposals"')
        if mutation == "nullable_owner":
            con.execute("PRAGMA writable_schema=ON")
            con.execute(
                "UPDATE sqlite_master SET sql=? WHERE name='campaign_proposals'",
                (sql.replace("owner TEXT NOT NULL", "owner TEXT"),),
            )
            version = con.execute("PRAGMA schema_version").fetchone()[0]
            con.execute(f"PRAGMA schema_version={version + 1}")
        elif mutation == "changed_pending_literal":
            con.execute("DROP INDEX campaign_one_pending")
            con.execute(campaign_schema._PENDING_INDEX_SQL.replace("'pending'", "'PENDING'"))
        con.commit()
    finally:
        con.close()
    before = path.read_bytes()
    if mutation is None:
        operations.inspect_store(source)
        backup = tmp_path / "backup"
        operations.backup_store(source, backup)
        operations.verify_backup(backup)
        restored = tmp_path / "restored"
        operations.restore_store(backup, restored)
        assert (restored / operations.DATABASE_NAME).read_bytes() == (
            backup / operations.DATABASE_NAME
        ).read_bytes()
    else:
        with pytest.raises(operations.StoreOperationError):
            operations.inspect_store(source)
        with pytest.raises(operations.StoreOperationError):
            operations.backup_store(source, tmp_path / "backup")
        assert not (tmp_path / "backup").exists()
    assert path.read_bytes() == before
