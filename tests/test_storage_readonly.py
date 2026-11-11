from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from measure_service import storage


def _initialize(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(root))
    connection = storage.connect()
    connection.close()
    return root / "measure.sqlite3"


def _schema(database: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
        return tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("make_directory", [False, True], ids=["missing-root", "empty-root"])
def test_read_does_not_create_a_missing_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_directory: bool
) -> None:
    root = tmp_path / "store"
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(root))
    if make_directory:
        root.mkdir()
    before = None if not root.exists() else sorted(root.iterdir())

    assert storage.db_path(create=False) == root / "measure.sqlite3"
    with pytest.raises(ValueError, match="Store is not initialized") as error:
        with storage.registry_connection():
            pass

    assert "measure-service-store initialize" in str(error.value)
    assert root.exists() is make_directory
    if make_directory:
        assert sorted(root.iterdir()) == before == []


def test_read_only_connection_rejects_sql_writes_and_preserves_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _initialize(tmp_path / "store", monkeypatch)
    before_digest = _digest(database)
    before_schema = _schema(database)

    with storage.registry_connection() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "INSERT INTO datasets VALUES ('read-only', 'owner', 'digest', '{}', 'now')"
            )
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE read_only_probe (value TEXT)")

    assert _digest(database) == before_digest
    assert _schema(database) == before_schema


def test_read_only_connection_observes_initialized_store_without_changing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _initialize(tmp_path / "store", monkeypatch)
    before_digest = _digest(database)
    before_schema = _schema(database)

    with storage.registry_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0

    assert _digest(database) == before_digest
    assert _schema(database) == before_schema


def test_read_only_connection_sees_committed_wal_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _initialize(tmp_path / "store", monkeypatch)
    writer = sqlite3.connect(database, timeout=30)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].casefold() == "wal"
        writer.execute(
            "INSERT INTO datasets VALUES (?, ?, ?, ?, ?)",
            ("wal-dataset", "owner", "digest", "{}", "now"),
        )
        writer.commit()
        assert Path(f"{database}-wal").exists()

        with storage.registry_connection() as reader:
            rows = reader.execute("SELECT id FROM datasets").fetchall()
            assert [row[0] for row in rows] == ["wal-dataset"]
    finally:
        writer.close()


def test_read_only_connection_rejects_incomplete_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _initialize(tmp_path / "store", monkeypatch)
    before_digest = _digest(database)
    marker = database.parent / "INCOMPLETE"
    marker.touch()

    with pytest.raises(ValueError, match="INCOMPLETE"):
        with storage.registry_connection():
            pass

    assert _digest(database) == before_digest
    assert marker.is_file()


def test_read_only_connection_rejects_symlinked_store_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_database = _initialize(tmp_path / "real-store", monkeypatch)
    linked_root = tmp_path / "linked-store"
    linked_root.symlink_to(real_database.parent, target_is_directory=True)
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(linked_root))
    with pytest.raises(ValueError, match="must not be a symlink"):
        with storage.registry_connection():
            pass

    linked_database_root = tmp_path / "database-link-store"
    linked_database_root.mkdir()
    (linked_database_root / "measure.sqlite3").symlink_to(real_database)
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(linked_database_root))
    with pytest.raises(ValueError, match="must not be a symlink"):
        with storage.registry_connection():
            pass


def test_read_does_not_migrate_legacy_campaign_proposal_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _initialize(tmp_path / "store", monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """CREATE TABLE campaign_proposals__legacy (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, campaign_id TEXT NOT NULL,
              ordinal INTEGER NOT NULL, state TEXT NOT NULL, candidate_digest TEXT NOT NULL,
              executable_method_id TEXT NOT NULL, method_input_digest TEXT NOT NULL,
              canonical_program_digest TEXT NOT NULL, payload TEXT NOT NULL,
              input_digest TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(campaign_id, ordinal), UNIQUE(campaign_id, candidate_digest),
              FOREIGN KEY(campaign_id) REFERENCES campaign_runs(id),
              FOREIGN KEY(executable_method_id) REFERENCES executable_methods(id)
            )"""
        )
        connection.execute("DROP TABLE campaign_proposals")
        connection.execute("ALTER TABLE campaign_proposals__legacy RENAME TO campaign_proposals")
        connection.execute(
            "CREATE UNIQUE INDEX campaign_one_pending "
            "ON campaign_proposals(owner, campaign_id) WHERE state='pending'"
        )

    before_digest = _digest(database)
    before_schema = _schema(database)
    with pytest.raises(ValueError, match="writable migration"):
        with storage.registry_connection():
            pass

    assert _digest(database) == before_digest
    assert _schema(database) == before_schema
