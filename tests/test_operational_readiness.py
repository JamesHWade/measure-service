"""Readiness observes storage without initializing it or invoking R."""

import os
import sqlite3

import pytest

from measure_service.operational_readiness import storage_ready
from measure_service.storage import _connect_at


@pytest.fixture
def configured_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "connect")
    monkeypatch.setenv("MEASURE_MCP_TRUST_CONNECT_HEADERS", "1")
    root = tmp_path / "store"
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(root))
    return root


def test_readiness_does_not_create_missing_store(configured_store):
    assert storage_ready() is False
    assert not configured_store.exists()


def test_readiness_accepts_initialized_store_without_sql_writes(configured_store):
    configured_store.mkdir()
    path = configured_store / "measure.sqlite3"
    _connect_at(path).close()
    before = path.read_bytes()
    assert storage_ready() is True
    assert path.read_bytes() == before


@pytest.mark.parametrize("marker", ["file", "dangling_symlink"])
def test_readiness_rejects_incomplete_store(configured_store, marker):
    configured_store.mkdir()
    _connect_at(configured_store / "measure.sqlite3").close()
    path = configured_store / "INCOMPLETE"
    if marker == "file":
        path.touch()
    else:
        path.symlink_to(configured_store / "absent")
    assert storage_ready() is False


def test_readiness_rejects_uninitialized_and_corrupt_database(configured_store):
    configured_store.mkdir()
    path = configured_store / "measure.sqlite3"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE unrelated (id INTEGER)")
    before = path.read_bytes()
    assert storage_ready() is False
    assert path.read_bytes() == before
    path.write_bytes(b"not a sqlite database")
    assert storage_ready() is False


@pytest.mark.parametrize(("mode", "trust"), [("local", "1"), ("unknown", "1"), ("connect", "0")])
def test_readiness_rejects_invalid_http_configuration(configured_store, monkeypatch, mode, trust):
    configured_store.mkdir()
    _connect_at(configured_store / "measure.sqlite3").close()
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", mode)
    monkeypatch.setenv("MEASURE_MCP_TRUST_CONNECT_HEADERS", trust)
    assert storage_ready() is False


def test_readiness_rejects_database_symlink(configured_store, tmp_path):
    configured_store.mkdir()
    real = tmp_path / "elsewhere.sqlite3"
    _connect_at(real).close()
    (configured_store / "measure.sqlite3").symlink_to(real)
    assert storage_ready() is False


def test_readiness_rejects_missing_required_column(configured_store):
    configured_store.mkdir()
    path = configured_store / "measure.sqlite3"
    _connect_at(path).close()
    with sqlite3.connect(path) as con:
        con.execute("ALTER TABLE datasets RENAME COLUMN digest TO unrelated")
    assert storage_ready() is False


def test_readiness_rejects_core_view(configured_store):
    configured_store.mkdir()
    path = configured_store / "measure.sqlite3"
    _connect_at(path).close()
    with sqlite3.connect(path) as con:
        con.execute("ALTER TABLE registry_receipts RENAME TO retained_receipts")
        con.execute("CREATE VIEW registry_receipts AS SELECT * FROM retained_receipts")
    assert storage_ready() is False


def test_readiness_reads_live_wal_without_ddl(configured_store):
    configured_store.mkdir()
    path = configured_store / "measure.sqlite3"
    con = _connect_at(path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("INSERT INTO experiments VALUES ('x','owner','{}','digest','now')")
        con.commit()
        assert os.path.exists(str(path) + "-wal")
        assert storage_ready() is True
        assert con.execute("SELECT COUNT(*) FROM experiments").fetchone() == (1,)
    finally:
        con.close()
