"""Regression coverage for the campaign-proposal schema migration."""

from __future__ import annotations

import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from measure_service import campaign_schema as schema
from measure_service import storage


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    con = storage.connect()
    con.close()
    return tmp_path / "measure.sqlite3"


def _connect():
    database = Path(os.environ["MEASURE_MCP_DATA_DIR"]) / "measure.sqlite3"
    con = sqlite3.connect(database, timeout=30)
    con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    return con


def _insert_parents(con):
    con.execute(
        "INSERT INTO campaign_protocols VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("protocol-1", "owner", "fixture", "{}", "protocol-input", "protocol-output", "created"),
    )
    con.execute(
        "INSERT INTO campaign_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "campaign-1",
            "owner",
            "protocol-1",
            "protocol-input",
            "task-1",
            7,
            "random",
            "{}",
            "campaign-input",
            1,
            "created",
        ),
    )
    con.execute(
        "INSERT INTO executable_methods VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("method-1", "owner", "fixture", "{}", "method-input", "program", "created"),
    )


def _insert_proposal(
    con,
    *,
    proposal_id,
    ordinal,
    candidate_digest,
    state="consumed",
    campaign_id="campaign-1",
):
    con.execute(
        """INSERT INTO campaign_proposals (
          id, owner, campaign_id, ordinal, state, candidate_digest,
          executable_method_id, method_input_digest, canonical_program_digest,
          payload, input_digest, created_at
        ) VALUES (?, 'owner', ?, ?, ?, ?, 'method-1', ?, ?, ?, ?, ?)""",
        (
            proposal_id,
            campaign_id,
            ordinal,
            state,
            candidate_digest,
            f"method-input-{proposal_id}",
            f"program-{proposal_id}",
            f'{{ "candidate": "{proposal_id}", "ordinal": {ordinal} }}',
            f"input-{proposal_id}",
            f"created-{proposal_id}",
        ),
    )


def _make_legacy_proposal_table(con):
    """Install the pre-migration service table definition in a test database."""
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("BEGIN IMMEDIATE")
    con.execute(
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
    columns = (
        "id, owner, campaign_id, ordinal, state, candidate_digest, "
        "executable_method_id, method_input_digest, canonical_program_digest, "
        "payload, input_digest, created_at"
    )
    con.execute(
        "INSERT INTO campaign_proposals__legacy (rowid, "
        + columns
        + f") SELECT rowid, {columns} FROM campaign_proposals"
    )
    con.execute("DROP TABLE campaign_proposals")
    con.execute("ALTER TABLE campaign_proposals__legacy RENAME TO campaign_proposals")
    con.execute(
        "CREATE UNIQUE INDEX campaign_one_pending "
        "ON campaign_proposals(owner, campaign_id) WHERE state='pending'"
    )
    con.commit()
    con.execute("PRAGMA foreign_keys=ON")


def _seed_legacy_database(database):
    con = _connect()
    _make_legacy_proposal_table(con)
    _insert_parents(con)
    _insert_proposal(
        con, proposal_id="proposal-1", ordinal=0, candidate_digest="digest-a", state="consumed"
    )
    _insert_proposal(
        con, proposal_id="proposal-2", ordinal=1, candidate_digest="digest-b", state="pending"
    )
    con.execute(
        "INSERT INTO registry_receipts VALUES (?, ?, ?, ?, ?)",
        ("owner", "existing-key", "existing-request-digest", '{"response":"kept"}', "receipt-time"),
    )
    con.execute("CREATE TABLE proposal_audit (proposal_id TEXT, event TEXT)")
    con.execute(
        "CREATE TRIGGER proposal_audit_update AFTER UPDATE ON campaign_proposals "
        "BEGIN INSERT INTO proposal_audit VALUES (NEW.id, 'updated'); END"
    )
    con.execute("CREATE INDEX custom_proposal_state ON campaign_proposals(state, ordinal)")
    con.execute("CREATE VIEW proposal_view AS SELECT id, candidate_digest FROM campaign_proposals")
    con.execute(
        "CREATE VIEW a_proposal_view_chain AS SELECT id, candidate_digest FROM proposal_view"
    )
    con.execute(
        "CREATE TRIGGER proposal_view_update INSTEAD OF UPDATE OF candidate_digest "
        "ON proposal_view BEGIN UPDATE campaign_proposals "
        "SET candidate_digest=NEW.candidate_digest WHERE id=OLD.id; END"
    )
    con.execute(
        "CREATE TABLE proposal_reference (proposal_id TEXT REFERENCES campaign_proposals(id))"
    )
    con.execute("INSERT INTO proposal_reference VALUES ('proposal-1')")
    before_rows = [
        tuple(row) for row in con.execute("SELECT rowid, * FROM campaign_proposals ORDER BY rowid")
    ]
    con.commit()
    con.close()
    return before_rows


def _candidate_uniques(con):
    rows = []
    for row in con.execute("PRAGMA index_list(campaign_proposals)"):
        if not row[2]:
            continue
        columns = tuple(item[2] for item in con.execute(f'PRAGMA index_info("{row[1]}")'))
        if set(columns) == {"campaign_id", "candidate_digest"}:
            rows.append((row[1], columns, row[3]))
    return rows


def _unrelated_main_schema(con):
    return [
        tuple(row)
        for row in con.execute(
            """SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
              AND NOT (type='table' AND name='campaign_proposals')
              AND name != 'campaign_proposals_candidate'
            ORDER BY type, name"""
        )
    ]


def test_fresh_storage_connection_has_repetition_schema(database):
    con = _connect()
    assert not _candidate_uniques(con)
    assert (
        con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='campaign_proposals_candidate'"
        ).fetchone()[0]
        == 1
    )
    assert con.execute("PRAGMA index_info('campaign_proposals_candidate')").fetchall()
    con.close()


def test_storage_connect_migrates_populated_legacy_table(database):
    original_rows = _seed_legacy_database(database)
    con = storage.connect()
    assert not con.in_transaction
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not _candidate_uniques(con)
    assert [
        tuple(row) for row in con.execute("SELECT rowid, * FROM campaign_proposals ORDER BY rowid")
    ] == original_rows
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


@pytest.mark.parametrize("literal", ["PENDING", " pending "])
def test_pending_index_string_literal_semantics_are_not_normalized(database, literal):
    _seed_legacy_database(database)
    con = _connect()
    con.execute("DROP INDEX campaign_one_pending")
    con.execute(
        "CREATE UNIQUE INDEX campaign_one_pending ON campaign_proposals(owner,campaign_id) "
        f"WHERE state='{literal}'"
    )
    con.commit()
    before = con.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    with pytest.raises(ValueError, match="pending-proposal index"):
        schema.ensure_proposal_repetition_schema(con)
    assert not con.in_transaction
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert con.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before
    assert _candidate_uniques(con)
    con.close()


def test_shape_inspection_uses_one_read_snapshot_during_concurrent_migration(database):
    _seed_legacy_database(database)
    read_started = threading.Event()
    other_started = threading.Event()
    release_read = threading.Event()

    class PausedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.paused = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            if sql == "PRAGMA table_info(campaign_proposals)" and not self.paused:
                self.paused = True
                assert self.connection.in_transaction
                read_started.set()
                assert release_read.wait(10)
            return self.connection.execute(sql, *args)

    def observer():
        con = _connect()
        try:
            schema.ensure_proposal_repetition_schema(PausedConnection(con))
            assert not _candidate_uniques(con)
        finally:
            con.close()

    def competitor():
        con = _connect()
        other_started.set()
        try:
            schema.ensure_proposal_repetition_schema(con)
            assert not _candidate_uniques(con)
        finally:
            con.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(observer)
        try:
            assert read_started.wait(10)
            second = pool.submit(competitor)
            assert other_started.wait(10)
        finally:
            release_read.set()
        first.result(timeout=10)
        second.result(timeout=10)


def test_storage_connect_closes_connection_when_migration_is_refused(database, monkeypatch):
    captured = []

    def fail(connection):
        captured.append(connection)
        raise ValueError("unsupported schema")

    monkeypatch.setattr(storage, "ensure_proposal_repetition_schema", fail)
    with pytest.raises(ValueError, match="unsupported schema"):
        storage.connect()
    assert len(captured) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        captured[0].execute("SELECT 1")


def test_legacy_migration_preserves_rows_receipts_objects_and_required_constraints(database):
    original_rows = _seed_legacy_database(database)
    con = _connect()
    before_receipt = tuple(
        con.execute(
            "SELECT * FROM registry_receipts WHERE owner='owner' AND request_key='existing-key'"
        ).fetchone()
    )
    before_indexes = [
        tuple(row)
        for row in con.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='campaign_proposals' AND sql IS NOT NULL ORDER BY name"
        )
    ]
    before_triggers = [
        tuple(row)
        for row in con.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        )
    ]
    before_views = [
        tuple(row)
        for row in con.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='view' ORDER BY name"
        )
    ]
    before_unrelated_schema = _unrelated_main_schema(con)

    schema.ensure_proposal_repetition_schema(con)

    assert not con.in_transaction
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert [
        tuple(row) for row in con.execute("SELECT rowid, * FROM campaign_proposals ORDER BY rowid")
    ] == original_rows
    assert (
        tuple(
            con.execute(
                "SELECT * FROM registry_receipts WHERE owner='owner' AND request_key='existing-key'"
            ).fetchone()
        )
        == before_receipt
    )
    after_indexes = [
        tuple(row)
        for row in con.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='campaign_proposals' AND sql IS NOT NULL "
            "AND name != 'campaign_proposals_candidate' ORDER BY name"
        )
    ]
    assert after_indexes == before_indexes
    assert [
        tuple(row)
        for row in con.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        )
    ] == before_triggers
    assert [
        tuple(row)
        for row in con.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='view' ORDER BY name"
        )
    ] == before_views
    assert _unrelated_main_schema(con) == before_unrelated_schema
    assert not _candidate_uniques(con)
    assert (
        con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='campaign_proposals_candidate'"
        )
        .fetchone()[0]
        .casefold()
        .startswith("create index")
    )
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("SELECT * FROM a_proposal_view_chain ORDER BY id").fetchall()
    con.execute("UPDATE proposal_view SET candidate_digest='digest-after' WHERE id='proposal-1'")
    assert (
        con.execute(
            "SELECT candidate_digest FROM campaign_proposals WHERE id='proposal-1'"
        ).fetchone()[0]
        == "digest-after"
    )

    con.execute("UPDATE campaign_proposals SET state='revised' WHERE id='proposal-1'")
    assert [tuple(row) for row in con.execute("SELECT * FROM proposal_audit")] == [
        ("proposal-1", "updated"),
        ("proposal-1", "updated"),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE campaign_proposals SET ordinal=1 WHERE id='proposal-1'")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_proposal(
            con,
            proposal_id="proposal-pending-conflict",
            ordinal=2,
            candidate_digest="digest-c",
            state="pending",
        )
    _insert_proposal(
        con,
        proposal_id="proposal-repeat",
        ordinal=2,
        candidate_digest="digest-after",
        state="consumed",
    )
    assert (
        con.execute(
            "SELECT COUNT(*) FROM campaign_proposals WHERE campaign_id='campaign-1' AND candidate_digest='digest-after'"
        ).fetchone()[0]
        == 2
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_proposal(
            con,
            proposal_id="proposal-invalid-fk",
            ordinal=3,
            candidate_digest="digest-x",
            campaign_id="missing-campaign",
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM campaign_proposals WHERE id='proposal-1'")
    con.close()


def test_current_schema_is_idempotent_and_adds_missing_lookup_index(database):
    _seed_legacy_database(database)
    con = _connect()
    schema.ensure_proposal_repetition_schema(con)
    con.execute("DROP INDEX campaign_proposals_candidate")
    con.commit()
    schema.ensure_proposal_repetition_schema(con)
    schema.ensure_proposal_repetition_schema(con)
    assert not _candidate_uniques(con)
    assert (
        con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='campaign_proposals_candidate'"
        ).fetchone()[0]
        == 1
    )
    con.close()


def test_migration_rejects_callers_active_transaction_without_changing_it(database):
    con = _connect()
    con.execute("BEGIN")
    with pytest.raises(ValueError, match="outside a transaction"):
        schema.ensure_proposal_repetition_schema(con)
    assert con.in_transaction
    con.rollback()
    con.close()


def test_migration_preserves_disabled_foreign_key_setting(database):
    _seed_legacy_database(database)
    con = _connect()
    con.execute("PRAGMA foreign_keys=OFF")
    schema.ensure_proposal_repetition_schema(con)
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    assert not _candidate_uniques(con)
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_unsupported_custom_unique_candidate_index_is_refused_before_rebuild(database):
    con = _connect()
    _insert_parents(con)
    _insert_proposal(
        con, proposal_id="proposal-1", ordinal=0, candidate_digest="digest-a", state="consumed"
    )
    con.execute(
        "CREATE UNIQUE INDEX custom_candidate_guard "
        "ON campaign_proposals(campaign_id, candidate_digest)"
    )
    con.commit()
    before_sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='campaign_proposals'"
    ).fetchone()[0]
    before_candidate_sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='campaign_proposals_candidate'"
    ).fetchone()[0]
    with pytest.raises(ValueError, match="custom unique candidate index"):
        schema.ensure_proposal_repetition_schema(con)
    assert (
        con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='campaign_proposals'"
        ).fetchone()[0]
        == before_sql
    )
    assert con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='custom_candidate_guard'"
    ).fetchone()
    assert (
        con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='campaign_proposals_candidate'"
        ).fetchone()[0]
        == before_candidate_sql
    )
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    con.close()


def test_temporary_schema_dependency_is_refused_before_mutation(database):
    _seed_legacy_database(database)
    con = _connect()
    con.execute("CREATE TEMP VIEW temporary_proposals AS SELECT id FROM campaign_proposals")
    con.commit()
    before_sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='campaign_proposals'"
    ).fetchone()[0]
    with pytest.raises(ValueError, match="Temporary schema objects"):
        schema.ensure_proposal_repetition_schema(con)
    assert (
        con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='campaign_proposals'"
        ).fetchone()[0]
        == before_sql
    )
    assert (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='campaign_proposals_candidate'"
        ).fetchone()
        is None
    )
    con.close()


def test_attached_database_is_refused_before_mutation(database, tmp_path):
    _seed_legacy_database(database)
    con = _connect()
    attached_path = tmp_path / "attached.sqlite3"
    con.execute("ATTACH DATABASE ? AS extra", (str(attached_path),))
    con.commit()
    before_sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='campaign_proposals'"
    ).fetchone()[0]
    with pytest.raises(ValueError, match="Attached databases are unsupported"):
        schema.ensure_proposal_repetition_schema(con)
    assert (
        con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='campaign_proposals'"
        ).fetchone()[0]
        == before_sql
    )
    con.close()


def test_failure_after_rebuild_rolls_back_schema_rows_and_foreign_key_setting(
    database, monkeypatch
):
    original_rows = _seed_legacy_database(database)
    original_rebuild = schema._rebuild_legacy_table

    def fail_after_rebuild(con):
        original_rebuild(con)
        raise RuntimeError("injected post-rebuild failure")

    monkeypatch.setattr(schema, "_rebuild_legacy_table", fail_after_rebuild)
    con = _connect()
    with pytest.raises(RuntimeError, match="injected post-rebuild failure"):
        schema.ensure_proposal_repetition_schema(con)
    assert not con.in_transaction
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert [
        tuple(row) for row in con.execute("SELECT rowid, * FROM campaign_proposals ORDER BY rowid")
    ] == original_rows
    assert _candidate_uniques(con)
    assert (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_proposals__migration'"
        ).fetchone()
        is None
    )
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_concurrent_calls_serialize_and_complete_one_migration(database):
    _seed_legacy_database(database)
    db_file = str(database)

    def migrate_once(_):
        con = sqlite3.connect(db_file, timeout=30)
        con.execute("PRAGMA foreign_keys=ON")
        try:
            schema.ensure_proposal_repetition_schema(con)
            return con.execute("SELECT COUNT(*) FROM campaign_proposals").fetchone()[0]
        finally:
            con.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        counts = list(pool.map(migrate_once, range(6)))
    assert counts == [2] * 6
    con = _connect()
    assert not _candidate_uniques(con)
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()
