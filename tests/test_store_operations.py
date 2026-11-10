from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from measure_service import campaign_suggestion_attempts, storage
from measure_service import store_operations as operations
from measure_service.store_operations import (
    DATABASE_NAME,
    INCOMPLETE_NAME,
    MANIFEST_NAME,
    StoreOperationError,
    backup_store,
    initialize_store,
    inspect_store,
    restore_store,
    verify_backup,
)


def _database(root: Path) -> Path:
    return root / DATABASE_NAME


def _manifest(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )


def test_initialize_and_inspect_are_private_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "store"
    initialized = initialize_store(root)
    database = _database(root)
    original = database.read_bytes()
    assert initialized["status"] == "initialized"
    assert root.stat().st_mode & 0o777 == 0o700
    assert database.stat().st_mode & 0o777 == 0o600
    assert not (root / INCOMPLETE_NAME).exists()

    inspected = inspect_store(root)
    assert inspected["status"] == "valid"
    assert inspected["table_counts"] == initialized["table_counts"]
    assert database.read_bytes() == original
    assert sorted(path.name for path in root.iterdir()) == [DATABASE_NAME]


def test_initialize_rejects_existing_empty_directory_without_changes(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    root.mkdir()
    with pytest.raises(StoreOperationError):
        initialize_store(root)
    assert list(root.iterdir()) == []


def test_inspection_rejects_incomplete_extra_and_nonservice_databases(tmp_path: Path) -> None:
    root = tmp_path / "store"
    initialize_store(root)
    marker = root / INCOMPLETE_NAME
    marker.write_text("", encoding="utf-8")
    with pytest.raises(StoreOperationError, match="incomplete"):
        inspect_store(root)
    marker.unlink()

    extra = root / "untracked.txt"
    extra.write_text("extra", encoding="utf-8")
    with pytest.raises(StoreOperationError):
        inspect_store(root)
    extra.unlink()

    database = _database(root)
    database.unlink()
    con = sqlite3.connect(database)
    con.execute("CREATE TABLE unrelated(value TEXT)")
    con.close()
    with pytest.raises(StoreOperationError):
        inspect_store(root)


def test_known_schema_constraints_and_durable_claim_index_are_required(tmp_path: Path) -> None:
    root = tmp_path / "store"
    initialize_store(root)
    con = sqlite3.connect(_database(root))
    campaign_suggestion_attempts.ensure_schema(con)
    con.execute("DROP INDEX campaign_suggestion_one_active")
    con.commit()
    con.close()
    with pytest.raises(StoreOperationError):
        inspect_store(root)


def test_sqlitex_extension_table_is_counted_and_preserved(tmp_path: Path) -> None:
    root = tmp_path / "store"
    initialize_store(root)
    con = sqlite3.connect(_database(root))
    con.execute("CREATE TABLE sqliteX_extension (value TEXT)")
    con.execute("INSERT INTO sqliteX_extension VALUES ('preserved')")
    con.commit()
    con.close()
    report = inspect_store(root)
    assert report["table_counts"]["sqliteX_extension"] == 1


def test_source_symlinks_and_path_collisions_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    initialize_store(source)
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(StoreOperationError):
        inspect_store(alias)
    with pytest.raises(StoreOperationError):
        backup_store(source, source / "nested-backup")
    assert not (source / "nested-backup").exists()


def test_new_root_collision_does_not_mark_or_change_existing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "colliding"
    real_mkdir = os.mkdir

    def race_mkdir(path: str | os.PathLike[str], mode: int = 0o777) -> None:
        real_mkdir(path, mode)
        raise FileExistsError(os.fspath(path))

    monkeypatch.setattr(operations.os, "mkdir", race_mkdir)
    with pytest.raises(StoreOperationError):
        operations._new_root(target, label="store")
    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert not (target / INCOMPLETE_NAME).exists()


def test_backup_captures_committed_wal_state_while_writer_is_open(tmp_path: Path) -> None:
    source = tmp_path / "source"
    initialize_store(source)
    writer = sqlite3.connect(_database(source))
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].casefold() == "wal"
        writer.execute(
            "INSERT INTO datasets(id, owner, digest, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            ("visible", "owner", "digest", "{}", "2026-01-01T00:00:00Z"),
        )
        writer.commit()
        writer.execute(
            "INSERT INTO datasets(id, owner, digest, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            ("uncommitted", "owner", "digest", "{}", "2026-01-01T00:00:01Z"),
        )
        backup = tmp_path / "backup"
        result = backup_store(source, backup)
        assert result["status"] == "created"
        con = sqlite3.connect(f"file:{_database(backup)}?mode=ro", uri=True)
        try:
            ids = [row[0] for row in con.execute("SELECT id FROM datasets ORDER BY id")]
        finally:
            con.close()
        assert ids == ["visible"]
        assert sorted(path.name for path in backup.iterdir()) == [MANIFEST_NAME, DATABASE_NAME]
        assert verify_backup(backup)["status"] == "verified"
    finally:
        writer.rollback()
        writer.close()


def test_backup_restore_preserve_exact_bytes_and_retry_receipt(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    initialize_store(source)
    request = {"operation": "record", "id": "stable"}
    response = {"id": "result-1", "state": "recorded"}
    con = sqlite3.connect(_database(source))
    con.execute(
        "INSERT INTO registry_receipts VALUES (?, ?, ?, ?, ?)",
        (
            "owner-one",
            "request-one",
            storage.sha256(request),
            storage.canonical(response),
            storage.now(),
        ),
    )
    con.commit()
    con.close()

    backup = tmp_path / "backup"
    backup_store(source, backup)
    restored = tmp_path / "restored"
    restore_store(backup, restored)
    assert _database(backup).read_bytes() == _database(restored).read_bytes()

    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(restored))
    actual = storage.registry_mutation(
        "owner-one",
        "request-one",
        request,
        lambda _connection: pytest.fail("restore did not preserve the original retry receipt"),
    )
    assert actual == response


def test_manifest_validation_rejects_hash_fields_duplicates_and_extra_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    initialize_store(source)
    backup = tmp_path / "backup"
    backup_store(source, backup)
    manifest_path = backup / MANIFEST_NAME
    original = _manifest(manifest_path)

    bad = dict(original)
    bad["database_sha256"] = "0" * 64
    _write_manifest(manifest_path, bad)
    with pytest.raises(StoreOperationError):
        verify_backup(backup)

    bad = dict(original)
    bad["database_bytes"] = True
    _write_manifest(manifest_path, bad)
    with pytest.raises(StoreOperationError):
        verify_backup(backup)

    bad = dict(original)
    bad["unexpected"] = "value"
    _write_manifest(manifest_path, bad)
    with pytest.raises(StoreOperationError):
        verify_backup(backup)

    duplicate = manifest_path.read_text(encoding="utf-8").replace(
        '"profile":"measure-store-backup/1"',
        '"profile":"measure-store-backup/1","profile":"measure-store-backup/1"',
    )
    manifest_path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(StoreOperationError):
        verify_backup(backup)

    _write_manifest(manifest_path, original)
    (backup / "unexpected.bin").write_bytes(b"x")
    with pytest.raises(StoreOperationError):
        verify_backup(backup)


def test_backup_rejects_symlinks_and_incomplete_targets(tmp_path: Path) -> None:
    source = tmp_path / "source"
    initialize_store(source)
    backup = tmp_path / "backup"
    backup_store(source, backup)
    target = tmp_path / "restore"
    with pytest.raises(StoreOperationError):
        restore_store(backup, backup / "nested")
    assert not (backup / "nested").exists()

    symlink_backup = tmp_path / "symlink-backup"
    symlink_backup.mkdir()
    (symlink_backup / MANIFEST_NAME).symlink_to(backup / MANIFEST_NAME)
    (symlink_backup / DATABASE_NAME).symlink_to(_database(backup))
    with pytest.raises(StoreOperationError):
        verify_backup(symlink_backup)

    incomplete_backup = tmp_path / "incomplete-backup"
    incomplete_backup.mkdir()
    (incomplete_backup / INCOMPLETE_NAME).write_text("", encoding="utf-8")
    with pytest.raises(StoreOperationError):
        verify_backup(incomplete_backup)
    assert not target.exists()


def test_failed_backup_keeps_incomplete_marker_and_readers_refuse_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    initialize_store(source)
    target = tmp_path / "backup"
    original = operations._readonly_connection
    source_connections = 0

    class TimedOutBackup:
        def __init__(self, connection: sqlite3.Connection):
            self.connection = connection

        def __getattr__(self, name: str):
            return getattr(self.connection, name)

        def backup(self, *args, **kwargs) -> None:
            del args, kwargs
            raise operations._DeadlineExpired

    def time_out_online_backup(path: Path, deadline: float):
        nonlocal source_connections
        connection = original(path, deadline)
        if path == _database(source):
            source_connections += 1
            if source_connections == 2:
                return TimedOutBackup(connection)
        return connection

    monkeypatch.setattr(operations, "_readonly_connection", time_out_online_backup)
    with pytest.raises(StoreOperationError, match="incomplete"):
        backup_store(source, target)
    assert (target / INCOMPLETE_NAME).exists()
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(target))
    with pytest.raises(ValueError, match="incomplete"):
        storage.connect()


def test_verify_rejects_corrupt_sqlite_even_with_updated_digest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    initialize_store(source)
    backup = tmp_path / "backup"
    backup_store(source, backup)
    database = _database(backup)
    data = bytearray(database.read_bytes())
    data[0] ^= 0xFF
    database.write_bytes(data)
    manifest = _manifest(backup / MANIFEST_NAME)
    manifest["database_sha256"] = hashlib.sha256(data).hexdigest()
    _write_manifest(backup / MANIFEST_NAME, manifest)
    with pytest.raises(StoreOperationError):
        verify_backup(backup)
