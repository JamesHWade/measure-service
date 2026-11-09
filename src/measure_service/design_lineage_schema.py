"""Optional SQLite schema for immutable experiment-design lineage.

The component is additive and fail-closed.  Validation only observes the
target connection; initialization uses individual statements in the caller's
transaction and never commits or executes a script.
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

SCHEMA_NAME = "measure-experiment-design-lineage"
SCHEMA_VERSION = 1

_NAMESPACE_PREFIX = "design_lineage"
_TABLES = (
    "design_lineage_schema",
    "design_lineage_roots",
    "design_lineage_versions",
)
_INDEXES = ("design_lineage_versions_owner_root",)

_DDL = (
    """CREATE TABLE design_lineage_schema (
      name TEXT PRIMARY KEY,
      version INTEGER NOT NULL
    )""",
    """CREATE TABLE design_lineage_roots (
      root_experiment_id TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      latest_revision INTEGER NOT NULL,
      latest_experiment_id TEXT NOT NULL UNIQUE,
      latest_entry_sha256 TEXT NOT NULL,
      FOREIGN KEY(root_experiment_id) REFERENCES experiments(id),
      FOREIGN KEY(latest_experiment_id) REFERENCES experiments(id)
    )""",
    """CREATE TABLE design_lineage_versions (
      experiment_id TEXT PRIMARY KEY,
      root_experiment_id TEXT NOT NULL,
      owner TEXT NOT NULL,
      revision INTEGER NOT NULL,
      previous_experiment_id TEXT,
      payload TEXT NOT NULL,
      digest TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(root_experiment_id, revision),
      UNIQUE(previous_experiment_id),
      FOREIGN KEY(experiment_id) REFERENCES experiments(id),
      FOREIGN KEY(root_experiment_id) REFERENCES design_lineage_roots(root_experiment_id),
      FOREIGN KEY(previous_experiment_id) REFERENCES design_lineage_versions(experiment_id)
    )""",
    """CREATE INDEX design_lineage_versions_owner_root
      ON design_lineage_versions(owner, root_experiment_id, revision)""",
)


def _component_object(name: str | None) -> bool:
    normalized = _sqlite_name(name)
    return normalized == _NAMESPACE_PREFIX or normalized.startswith(f"{_NAMESPACE_PREFIX}_")


def _create_component_schema(con: sqlite3.Connection) -> None:
    """Create the component with individual statements in the current transaction."""
    con.execute(_DDL[0])
    con.execute(
        "INSERT INTO design_lineage_schema(name, version) VALUES (?, ?)",
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
        # The experiment table is owned by the base registry.  A minimal
        # reference parent makes the external FK signatures deterministic.
        reference.execute("CREATE TABLE experiments (id TEXT PRIMARY KEY)")
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


def validate_design_lineage_schema(con: sqlite3.Connection) -> bool:
    """Validate the optional component without creating or mutating anything."""
    if not isinstance(con, sqlite3.Connection):
        raise TypeError("validate_design_lineage_schema requires a sqlite3.Connection")

    expected = _expected_signature()
    objects = _master_objects(con)
    expected_tables = expected["table_names"]
    expected_indexes = expected["index_names"]
    reserved = {*expected_tables, *expected_indexes}

    # A trigger attached to a component table is component state even when its
    # own name is unrelated.  It is not part of the frozen schema.
    if any(
        kind == "trigger" and table_name is not None and _sqlite_name(table_name) in expected_tables
        for (kind, _name), (_physical, table_name, _sql) in objects.items()
    ):
        raise _incompatible("unexpected trigger")

    for (kind, name), _object in objects.items():
        if name in reserved:
            expected_kind = "table" if name in expected_tables else "index"
            if kind != expected_kind:
                raise ValueError("A reserved design-lineage name is occupied by another object")

    marker_name = _sqlite_name("design_lineage_schema")
    table_names = {name for kind, name in objects if kind == "table"}
    namespace_present = any(_component_object(name) for _kind, name in objects)
    if marker_name not in table_names:
        if namespace_present:
            raise ValueError("Unmarked design-lineage tables cannot be interpreted")
        return False

    unexpected = {
        name for (_kind, name) in objects if _component_object(name) and name not in reserved
    }
    if unexpected:
        raise ValueError(f"Unsupported design-lineage schema objects: {sorted(unexpected)}")

    marker_physical = objects[("table", marker_name)][0]
    expected_marker = expected["tables"]["design_lineage_schema"]
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
        if normalized not in table_names:
            raise ValueError(f"Stored design-lineage schema is missing table {table}")
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


def ensure_design_lineage_schema(con: sqlite3.Connection) -> None:
    """Create or validate the component in the caller's current transaction."""
    if not isinstance(con, sqlite3.Connection):
        raise TypeError("ensure_design_lineage_schema requires a sqlite3.Connection")
    if validate_design_lineage_schema(con):
        return
    _create_component_schema(con)
    if not validate_design_lineage_schema(con):
        raise RuntimeError("Design-lineage schema initialization did not complete")


__all__ = [
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "ensure_design_lineage_schema",
    "validate_design_lineage_schema",
]
