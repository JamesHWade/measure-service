"""Additive, version-checked SQLite schema for analytical batches.

This module owns only the batch tables.  It intentionally does not import the
service storage module: ``storage.connect`` calls :func:`ensure_batch_schema`
after creating the base dataset and analysis tables.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

SCHEMA_NAME = "measure-chromatogram-batch"
SCHEMA_VERSION = 1
_SQLITE_IDENTIFIER_CASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

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

_SCHEMA_METADATA_DDL = (
    "CREATE TABLE measure_batch_schema (name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
)

# Keep the deployed DDL in one place.  The private reference database below
# executes these exact statements, so validation cannot silently drift from
# fresh initialization.
_BATCH_DDL_STATEMENTS = (
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


def _sqlite_name(value: str | None) -> str:
    """Normalize SQLite's ASCII-case-insensitive identifiers."""
    return "" if value is None else value.translate(_SQLITE_IDENTIFIER_CASE)


def _normalized_sql(value: str | None) -> str | None:
    if value is None:
        return None
    normalized: list[str] = []
    pending_space = False
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            normalized.append(character)
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    normalized.append(value[index + 1])
                    index += 1
                else:
                    quote = None
            elif quote == '"':
                normalized[-1] = character.translate(_SQLITE_IDENTIFIER_CASE)
            index += 1
            continue
        if character in {"'", '"'}:
            if pending_space and normalized:
                normalized.append(" ")
            pending_space = False
            quote = character
            normalized.append(character)
        elif character.isspace():
            pending_space = True
        else:
            if pending_space and normalized:
                normalized.append(" ")
            pending_space = False
            normalized.append(character.translate(_SQLITE_IDENTIFIER_CASE))
        index += 1
    return "".join(normalized).rstrip()


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _create_component_schema(con: sqlite3.Connection) -> None:
    """Create the component directly, without calling validation."""
    con.execute(_SCHEMA_METADATA_DDL)
    con.execute("INSERT INTO measure_batch_schema VALUES (?, ?)", (SCHEMA_NAME, SCHEMA_VERSION))
    for statement in _BATCH_DDL_STATEMENTS:
        con.execute(statement)


def _master_objects(
    con: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[str, str | None, str | None]]:
    return {
        (kind, _sqlite_name(name)): (name, table_name, sql)
        for kind, name, table_name, sql in con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
        )
    }


def _table_xinfo(con: sqlite3.Connection, physical_name: str) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            int(row[0]),
            _sqlite_name(row[1]),
            "" if row[2] is None else _sqlite_name(str(row[2]).strip()),
            int(row[3]),
            row[4],
            int(row[5]),
            int(row[6]),
        )
        for row in con.execute(f"PRAGMA table_xinfo({_quoted_identifier(physical_name)})")
    )


def _index_signature(
    con: sqlite3.Connection, physical_table: str
) -> tuple[tuple[object, ...], ...]:
    signatures = []
    for row in con.execute(f"PRAGMA index_list({_quoted_identifier(physical_table)})"):
        index_name = row[1]
        info = tuple(
            (int(item[0]), int(item[1]), None if item[2] is None else _sqlite_name(item[2]))
            for item in con.execute(f"PRAGMA index_info({_quoted_identifier(index_name)})")
        )
        xinfo = tuple(
            (
                int(item[0]),
                int(item[1]),
                None if item[2] is None else _sqlite_name(item[2]),
                int(item[3]),
                "" if item[4] is None else _sqlite_name(item[4]),
                int(item[5]),
            )
            for item in con.execute(f"PRAGMA index_xinfo({_quoted_identifier(index_name)})")
        )
        signatures.append(
            (
                _sqlite_name(index_name),
                int(row[2]),
                _sqlite_name(row[3]),
                int(row[4]),
                info,
                xinfo,
            )
        )
    return tuple(sorted(signatures, key=lambda item: item[0]))


def _foreign_key_signature(
    con: sqlite3.Connection, physical_table: str
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                int(row[0]),
                int(row[1]),
                _sqlite_name(row[2]),
                _sqlite_name(row[3]),
                None if row[4] is None else _sqlite_name(row[4]),
                _sqlite_name(row[5]),
                _sqlite_name(row[6]),
                _sqlite_name(row[7]),
            )
            for row in con.execute(f"PRAGMA foreign_key_list({_quoted_identifier(physical_table)})")
        )
    )


@lru_cache(maxsize=1)
def _expected_signature() -> dict[str, object]:
    """Build an isolated reference using the deployed component DDL only."""
    reference = sqlite3.connect(":memory:")
    try:
        reference.execute("PRAGMA foreign_keys=ON")
        # SQLite permits these child tables without parents, but declaring the
        # referenced service tables makes the reference independent of that
        # implementation detail and does not enter the target database.
        reference.execute("CREATE TABLE datasets (id TEXT PRIMARY KEY)")
        reference.execute("CREATE TABLE analyses (id TEXT PRIMARY KEY)")
        _create_component_schema(reference)
        objects = _master_objects(reference)
        tables = {}
        for table in ("measure_batch_schema", *_TABLE_COLUMNS):
            physical = objects[("table", table)][0]
            tables[table] = (
                _table_xinfo(reference, physical),
                _normalized_sql(objects[("table", table)][2]),
                _index_signature(reference, physical),
                _foreign_key_signature(reference, physical),
            )
        indexes = {}
        for (kind, name), (_physical, _table_name, sql) in objects.items():
            if kind == "index" and not name.startswith("sqlite_"):
                indexes[name] = _normalized_sql(sql)
        return {
            "tables": tables,
            "indexes": indexes,
            "table_names": frozenset(tables),
            "index_names": frozenset(indexes),
        }
    finally:
        reference.close()


def _incompatible(table: str, detail: str = "physical definition") -> ValueError:
    return ValueError(f"{table} has an incompatible analytical batch schema ({detail})")


def validate_batch_schema(con: sqlite3.Connection) -> bool:
    """Check an existing component without changing it; return False for new namespace.

    Validation is deliberately performed against a private reference database.
    It checks the ordered physical column declarations, constraints, indexes,
    foreign keys and table/index SQL before the caller may run any base DDL.
    """
    expected = _expected_signature()
    objects = _master_objects(con)
    expected_tables = expected["table_names"]
    expected_indexes = expected["index_names"]
    reserved = {"measure_batch_schema", *expected_tables, *expected_indexes}

    if any(
        kind == "trigger" and table_name is not None and _sqlite_name(table_name) in expected_tables
        for (kind, _name), (_physical, table_name, _sql) in objects.items()
    ):
        raise _incompatible("analytical batch", "unexpected trigger")

    for (kind, name), _object in objects.items():
        if name in reserved:
            expected_kind = "table" if name in expected_tables else "index"
            if kind != expected_kind:
                raise ValueError(
                    "A reserved analytical batch table name is occupied by another object"
                )

    names = {name for (kind, name) in objects if kind == "table"}
    if "measure_batch_schema" not in names:
        if any(
            name.startswith("analysis_batch") or name == "measure_batch_schema"
            for _kind, name in objects
        ):
            raise ValueError("Unmarked analytical batch tables cannot be interpreted")
        return False

    # A marker commits the namespace to this exact component.  Reject a
    # future/unknown object in that namespace instead of silently ignoring it.
    unexpected = {
        name
        for kind, name in objects
        if (name.startswith("analysis_batch") or name == "measure_batch_schema")
        and name not in reserved
    }
    if unexpected:
        raise ValueError(f"Unsupported analytical batch schema objects: {sorted(unexpected)}")

    metadata_name = objects[("table", "measure_batch_schema")][0]
    metadata_columns = _table_xinfo(con, metadata_name)
    expected_metadata_columns = expected["tables"]["measure_batch_schema"][0]
    if metadata_columns != expected_metadata_columns:
        raise ValueError("Unsupported analytical batch schema metadata")
    row = con.execute(
        f"SELECT version FROM {_quoted_identifier(metadata_name)} WHERE name=?", (SCHEMA_NAME,)
    ).fetchone()
    if row is None or row[0] != SCHEMA_VERSION:
        raise ValueError(f"Unsupported {SCHEMA_NAME} schema version; expected {SCHEMA_VERSION}")

    for table, required_columns in _TABLE_COLUMNS.items():
        if table not in names:
            raise ValueError(f"Stored analytical batch schema is missing table {table}")
        physical_name = objects[("table", table)][0]
        present_columns = {
            _sqlite_name(row[1])
            for row in con.execute(f"PRAGMA table_xinfo({_quoted_identifier(physical_name)})")
        }
        if any(column not in present_columns for column in required_columns):
            raise ValueError(f"{table} has an unsupported schema; required columns are missing")

    for table in ("measure_batch_schema", *_TABLE_COLUMNS):
        physical_name = objects[("table", table)][0]
        actual = (
            _table_xinfo(con, physical_name),
            _normalized_sql(objects[("table", table)][2]),
            _index_signature(con, physical_name),
            _foreign_key_signature(con, physical_name),
        )
        if actual != expected["tables"][table]:
            raise _incompatible(table)

    actual_indexes = {
        name: _normalized_sql(sql)
        for (kind, name), (_physical, table_name, sql) in objects.items()
        if kind == "index"
        and not name.startswith("sqlite_")
        and table_name is not None
        and _sqlite_name(table_name) in expected_tables
    }
    if actual_indexes != expected["indexes"]:
        raise _incompatible("analytical batch", "index definitions")
    return True


def ensure_batch_schema(con: sqlite3.Connection) -> None:
    """Create or validate the durable batch tables on an existing connection.

    The helper is safe to call repeatedly.  It never starts or commits a
    transaction, so the caller controls migration atomicity and connection
    lifetime.  A known schema version with missing or incompatible physical
    objects is rejected rather than silently repairing the database.
    """
    if validate_batch_schema(con):
        return
    _create_component_schema(con)
    if not validate_batch_schema(con):
        raise RuntimeError("Analytical batch schema initialization did not complete")
