"""Additive, version-checked SQLite schema for analytical batches.

This module owns only the batch tables.  It intentionally does not import the
service storage module: ``storage.connect`` calls :func:`ensure_batch_schema`
after creating the base dataset and analysis tables.
"""

from __future__ import annotations

import sqlite3

SCHEMA_NAME = "measure-chromatogram-batch"
SCHEMA_VERSION = 1

_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "analysis_batch_control_events": (
        "id",
        "owner",
        "batch_id",
        "event_no",
        "payload",
        "digest",
        "created_at",
    ),
    "analysis_batches": (
        "id",
        "owner",
        "profile",
        "schema_version",
        "title",
        "method",
        "manifest",
        "manifest_digest",
        "input_digest",
        "point_count",
        "item_count",
        "revision",
        "status",
        "cancel_requested",
        "created_at",
        "updated_at",
    ),
    "analysis_batch_items": (
        "id",
        "owner",
        "batch_id",
        "ordinal",
        "dataset_id",
        "trace_id",
        "role",
        "input_sha256",
        "source_sha256",
        "payload",
        "state",
        "attempt_count",
        "analysis_id",
        "result_digest",
        "evidence_digest",
        "compact_result",
        "created_at",
        "updated_at",
    ),
    "analysis_batch_runs": (
        "id",
        "owner",
        "batch_id",
        "request_key",
        "request_digest",
        "expected_revision",
        "max_items",
        "selected_count",
        "created_at",
    ),
    "analysis_batch_run_items": (
        "run_id",
        "owner",
        "batch_id",
        "ordinal",
        "attempt_id",
        "execution_token",
        "created_at",
    ),
    "analysis_batch_attempts": (
        "id",
        "owner",
        "batch_id",
        "run_id",
        "ordinal",
        "attempt_no",
        "execution_token",
        "state",
        "lease_expires_at",
        "execution_context",
        "runtime",
        "error",
        "analysis_id",
        "result_digest",
        "evidence_digest",
        "compact_result",
        "started_at",
        "finished_at",
    ),
}


def validate_batch_schema(con: sqlite3.Connection) -> bool:
    """Check an existing component without changing it; return False for a new namespace."""
    objects = con.execute("SELECT type, name FROM sqlite_master").fetchall()
    reserved = set(_TABLE_COLUMNS) | {"measure_batch_schema"}
    if any(name in reserved and kind != "table" for kind, name in objects):
        raise ValueError("A reserved analytical batch table name is occupied by another object")
    names = {name for kind, name in objects if kind == "table"}
    if "measure_batch_schema" not in names:
        if any(name.startswith("analysis_batch") for _, name in objects):
            raise ValueError("Unmarked analytical batch tables cannot be interpreted")
        return False
    if tuple(row[1] for row in con.execute("PRAGMA table_info(measure_batch_schema)")) != (
        "name",
        "version",
    ):
        raise ValueError("Unsupported analytical batch schema metadata")
    row = con.execute(
        "SELECT version FROM measure_batch_schema WHERE name=?", (SCHEMA_NAME,)
    ).fetchone()
    if row is None or row[0] != SCHEMA_VERSION:
        raise ValueError(f"Unsupported {SCHEMA_NAME} schema version; expected {SCHEMA_VERSION}")
    for table, columns in _TABLE_COLUMNS.items():
        if table not in names:
            raise ValueError(f"Stored analytical batch schema is missing table {table}")
        present = tuple(row[1] for row in con.execute(f"PRAGMA table_info({table})"))
        if any(column not in present for column in columns):
            raise ValueError(f"{table} has an unsupported schema; required columns are missing")
    return True


def ensure_batch_schema(con: sqlite3.Connection) -> None:
    """Create or validate the durable batch tables on an existing connection.

    The helper is safe to call repeatedly.  It never starts or commits a
    transaction, so the caller controls migration atomicity and connection
    lifetime.  A known schema version with missing columns is rejected rather
    than silently trying to reinterpret an incompatible database.
    """

    if validate_batch_schema(con):
        return
    con.execute(
        "CREATE TABLE measure_batch_schema (name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    con.execute("INSERT INTO measure_batch_schema VALUES (?, ?)", (SCHEMA_NAME, SCHEMA_VERSION))

    statements = (
        """CREATE TABLE IF NOT EXISTS analysis_batches (
          id TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          profile TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          title TEXT NOT NULL,
          method TEXT NOT NULL,
          manifest TEXT NOT NULL,
          manifest_digest TEXT NOT NULL,
          input_digest TEXT NOT NULL,
          point_count INTEGER NOT NULL CHECK(point_count >= 0),
          item_count INTEGER NOT NULL CHECK(item_count >= 1),
          revision INTEGER NOT NULL CHECK(revision >= 1),
          status TEXT NOT NULL CHECK(status IN ('active', 'cancelling', 'cancelled', 'completed')),
          cancel_requested INTEGER NOT NULL CHECK(cancel_requested IN (0, 1)),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS analysis_batches_owner_order
          ON analysis_batches(owner, created_at, id)""",
        """CREATE TABLE IF NOT EXISTS analysis_batch_items (
          id TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          batch_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
          dataset_id TEXT NOT NULL,
          trace_id TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('unknown', 'blank', 'standard', 'qc', 'reference')),
          input_sha256 TEXT NOT NULL,
          source_sha256 TEXT,
          payload TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('pending', 'running', 'completed', 'failed', 'interrupted', 'cancelled')),
          attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
          analysis_id TEXT,
          result_digest TEXT,
          evidence_digest TEXT,
          compact_result TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(batch_id, ordinal),
          UNIQUE(batch_id, dataset_id, trace_id),
          FOREIGN KEY(batch_id) REFERENCES analysis_batches(id),
          FOREIGN KEY(dataset_id) REFERENCES datasets(id),
          FOREIGN KEY(analysis_id) REFERENCES analyses(id)
        )""",
        """CREATE INDEX IF NOT EXISTS analysis_batch_items_owner_batch_order
          ON analysis_batch_items(owner, batch_id, ordinal)""",
        """CREATE INDEX IF NOT EXISTS analysis_batch_items_owner_state
          ON analysis_batch_items(owner, batch_id, state, ordinal)""",
        """CREATE TABLE IF NOT EXISTS analysis_batch_runs (
          id TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          batch_id TEXT NOT NULL,
          request_key TEXT NOT NULL,
          request_digest TEXT NOT NULL,
          expected_revision INTEGER NOT NULL CHECK(expected_revision >= 1),
          max_items INTEGER NOT NULL CHECK(max_items BETWEEN 1 AND 2),
          selected_count INTEGER NOT NULL CHECK(selected_count BETWEEN 0 AND 2),
          created_at TEXT NOT NULL,
          UNIQUE(owner, batch_id, request_key),
          FOREIGN KEY(batch_id) REFERENCES analysis_batches(id)
        )""",
        """CREATE INDEX IF NOT EXISTS analysis_batch_runs_owner_batch_order
          ON analysis_batch_runs(owner, batch_id, created_at, id)""",
        """CREATE TABLE IF NOT EXISTS analysis_batch_run_items (
          run_id TEXT NOT NULL,
          owner TEXT NOT NULL,
          batch_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
          attempt_id TEXT NOT NULL,
          execution_token TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(run_id, ordinal),
          UNIQUE(run_id, attempt_id),
          FOREIGN KEY(run_id) REFERENCES analysis_batch_runs(id),
          FOREIGN KEY(batch_id, ordinal) REFERENCES analysis_batch_items(batch_id, ordinal)
        )""",
        """CREATE TABLE IF NOT EXISTS analysis_batch_attempts (
          id TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          batch_id TEXT NOT NULL,
          run_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
          attempt_no INTEGER NOT NULL CHECK(attempt_no BETWEEN 1 AND 8),
          execution_token TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('running', 'completed', 'failed', 'interrupted', 'cancelled')),
          lease_expires_at TEXT NOT NULL,
          execution_context TEXT NOT NULL,
          runtime TEXT NOT NULL,
          error TEXT,
          analysis_id TEXT,
          result_digest TEXT,
          evidence_digest TEXT,
          compact_result TEXT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          UNIQUE(batch_id, ordinal, attempt_no),
          UNIQUE(batch_id, ordinal, execution_token),
          FOREIGN KEY(run_id) REFERENCES analysis_batch_runs(id),
          FOREIGN KEY(batch_id, ordinal) REFERENCES analysis_batch_items(batch_id, ordinal),
          FOREIGN KEY(analysis_id) REFERENCES analyses(id)
        )""",
        """CREATE INDEX IF NOT EXISTS analysis_batch_attempts_owner_batch_order
          ON analysis_batch_attempts(owner, batch_id, ordinal, attempt_no)""",
        """CREATE INDEX IF NOT EXISTS analysis_batch_attempts_owner_run_order
          ON analysis_batch_attempts(owner, run_id, ordinal)""",
    )
    statements += (
        """CREATE TABLE IF NOT EXISTS analysis_batch_control_events (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, batch_id TEXT NOT NULL,
          event_no INTEGER NOT NULL CHECK(event_no >= 1),
          payload TEXT NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(batch_id, event_no),
          FOREIGN KEY(batch_id) REFERENCES analysis_batches(id)
        )""",
        """CREATE INDEX IF NOT EXISTS analysis_batch_events_owner_order
          ON analysis_batch_control_events(owner, batch_id, event_no)""",
    )
    for statement in statements:
        con.execute(statement)

    validate_batch_schema(con)
