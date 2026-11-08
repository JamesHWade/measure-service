"""Optional SQLite schema for imported result tables.

The component is additive and fail-closed.  Ordinary reads do not create it,
and a database which contains a partial or physically altered result-table
namespace is rejected before a caller can run unrelated base-schema DDL.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

from .batch_schema import (
    _foreign_key_signature,
    _index_signature,
    _master_objects,
    _normalized_sql,
    _quoted_identifier,
    _sqlite_name,
    _table_xinfo,
)

SCHEMA_NAME = "measure-result-table-import"
SCHEMA_VERSION = 1

_NAMESPACE_PREFIX = "result_table"
_TABLES = (
    "result_table_schema",
    "result_table_imports",
    "result_table_rows",
)
_INDEXES = ("result_table_imports_owner_order",)

_DDL = (
    """CREATE TABLE result_table_schema (
      name TEXT PRIMARY KEY,
      version INTEGER NOT NULL
    )""",
    """CREATE TABLE result_table_imports (
      id TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      input_sha256 TEXT NOT NULL,
      source_text TEXT NOT NULL,
      specification TEXT NOT NULL,
      metadata TEXT NOT NULL,
      created_at TEXT NOT NULL
    )""",
    """CREATE TABLE result_table_rows (
      import_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      result_id TEXT NOT NULL,
      result_sha256 TEXT NOT NULL,
      row_sha256 TEXT NOT NULL,
      PRIMARY KEY(import_id, ordinal),
      UNIQUE(result_id),
      FOREIGN KEY(import_id) REFERENCES result_table_imports(id),
      FOREIGN KEY(result_id) REFERENCES experiment_results(id)
    )""",
    """CREATE INDEX result_table_imports_owner_order
      ON result_table_imports(owner, created_at, id)""",
)


def _component_object(name: str | None) -> bool:
    normalized = _sqlite_name(name)
    return normalized == _NAMESPACE_PREFIX or normalized.startswith(f"{_NAMESPACE_PREFIX}_")


def _create_component_schema(con: sqlite3.Connection) -> None:
    """Create the component with individual transactional statements."""
    con.execute(_DDL[0])
    con.execute(
        "INSERT INTO result_table_schema(name, version) VALUES (?, ?)",
        (SCHEMA_NAME, SCHEMA_VERSION),
    )
    for statement in _DDL[1:]:
        con.execute(statement)


@lru_cache(maxsize=1)
def _expected_signature() -> dict[str, object]:
    """Build the exact physical signature from the deployed component DDL."""
    reference = sqlite3.connect(":memory:")
    try:
        reference.execute("PRAGMA foreign_keys=ON")
        # The parent table is external to this optional component.  A minimal
        # reference parent is enough to make the FK signature deterministic.
        reference.execute("CREATE TABLE experiment_results (id TEXT PRIMARY KEY)")
        _create_component_schema(reference)
        objects = _master_objects(reference)
        tables = {
            table: (
                _table_xinfo(reference, objects[("table", _sqlite_name(table))][0]),
                _normalized_sql(objects[("table", _sqlite_name(table))][2]),
                _index_signature(reference, objects[("table", _sqlite_name(table))][0]),
                _foreign_key_signature(reference, objects[("table", _sqlite_name(table))][0]),
            )
            for table in _TABLES
        }
        indexes = {
            name: _normalized_sql(objects[("index", _sqlite_name(name))][2]) for name in _INDEXES
        }
        return {
            "tables": tables,
            "indexes": indexes,
            "table_names": frozenset(_sqlite_name(table) for table in _TABLES),
            "index_names": frozenset(_sqlite_name(index) for index in _INDEXES),
        }
    finally:
        reference.close()


def _incompatible(detail: str) -> ValueError:
    return ValueError(f"{SCHEMA_NAME} has an incompatible schema ({detail})")


def validate_result_table_schema(con: sqlite3.Connection) -> bool:
    """Validate the optional component without creating or mutating anything."""
    if not isinstance(con, sqlite3.Connection):
        raise TypeError("validate_result_table_schema requires a sqlite3.Connection")

    expected = _expected_signature()
    objects = _master_objects(con)
    expected_tables = expected["table_names"]
    expected_indexes = expected["index_names"]
    reserved = {*expected_tables, *expected_indexes}

    # A trigger on a component table is component state even when its own name
    # is unrelated.  It is not part of the frozen schema and must fail closed.
    if any(
        kind == "trigger" and table_name is not None and _sqlite_name(table_name) in expected_tables
        for (kind, _name), (_physical, table_name, _sql) in objects.items()
    ):
        raise _incompatible("unexpected trigger")

    for (kind, name), _object in objects.items():
        if name in reserved:
            expected_kind = "table" if name in expected_tables else "index"
            if kind != expected_kind:
                raise ValueError("A reserved result-table name is occupied by another object")

    namespace_present = any(_component_object(name) for _kind, name in objects)
    marker_name = _sqlite_name("result_table_schema")
    if marker_name not in {name for (kind, name) in objects if kind == "table"}:
        if namespace_present:
            raise ValueError("Unmarked result-table tables cannot be interpreted")
        return False

    unexpected = {
        name for (_kind, name) in objects if _component_object(name) and name not in reserved
    }
    if unexpected:
        raise ValueError(f"Unsupported result-table schema objects: {sorted(unexpected)}")

    marker_physical = objects[("table", marker_name)][0]
    expected_marker = expected["tables"]["result_table_schema"]
    actual_marker = (
        _table_xinfo(con, marker_physical),
        _normalized_sql(objects[("table", marker_name)][2]),
        _index_signature(con, marker_physical),
        _foreign_key_signature(con, marker_physical),
    )
    if actual_marker != expected_marker:
        raise _incompatible("metadata table")
    rows = con.execute(
        f"SELECT name, version FROM {_quoted_identifier(marker_physical)}"
    ).fetchall()
    if [tuple(row) for row in rows] != [(SCHEMA_NAME, SCHEMA_VERSION)]:
        raise ValueError(f"Unsupported {SCHEMA_NAME} schema version or metadata rows")

    for table in _TABLES:
        normalized = _sqlite_name(table)
        if normalized not in {name for (kind, name) in objects if kind == "table"}:
            raise ValueError(f"Stored result-table schema is missing table {table}")
        physical = objects[("table", normalized)][0]
        actual = (
            _table_xinfo(con, physical),
            _normalized_sql(objects[("table", normalized)][2]),
            _index_signature(con, physical),
            _foreign_key_signature(con, physical),
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
        raise _incompatible("index definitions")
    return True


def ensure_result_table_schema(con: sqlite3.Connection) -> None:
    """Create the optional component in the caller's current transaction."""
    if not isinstance(con, sqlite3.Connection):
        raise TypeError("ensure_result_table_schema requires a sqlite3.Connection")
    if validate_result_table_schema(con):
        return
    _create_component_schema(con)
    if not validate_result_table_schema(con):
        raise RuntimeError("Result-table schema initialization did not complete")


__all__ = [
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "ensure_result_table_schema",
    "validate_result_table_schema",
]
