"""Fail-closed, transactional tests for the optional design-lineage schema."""

from __future__ import annotations

import sqlite3

import pytest

from measure_service import design_lineage_schema as schema

_ROOTS_DDL = """CREATE TABLE design_lineage_roots (
  root_experiment_id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  latest_revision INTEGER NOT NULL,
  latest_experiment_id TEXT NOT NULL UNIQUE,
  latest_entry_sha256 TEXT NOT NULL,
  FOREIGN KEY(root_experiment_id) REFERENCES experiments(id),
  FOREIGN KEY(latest_experiment_id) REFERENCES experiments(id)
)"""

_VERSIONS_DDL = """CREATE TABLE design_lineage_versions (
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
)"""


def _open_database(tmp_path):
    con = sqlite3.connect(tmp_path / "lineage.sqlite3")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _create_parent(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE experiments (id TEXT PRIMARY KEY)")


def _create_declared_schema(
    con: sqlite3.Connection,
    *,
    roots_ddl: str = _ROOTS_DDL,
    versions_ddl: str = _VERSIONS_DDL,
) -> None:
    con.execute(
        "CREATE TABLE design_lineage_schema(name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    con.execute(
        "INSERT INTO design_lineage_schema(name, version) VALUES (?, ?)",
        (schema.SCHEMA_NAME, schema.SCHEMA_VERSION),
    )
    con.execute(roots_ddl)
    con.execute(versions_ddl)
    con.execute(
        "CREATE INDEX design_lineage_versions_owner_root "
        "ON design_lineage_versions(owner, root_experiment_id, revision)"
    )


def _objects(con: sqlite3.Connection):
    return con.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()


def test_absent_reads_are_read_only_and_creation_rolls_back(tmp_path):
    con = _open_database(tmp_path)
    database = tmp_path / "lineage.sqlite3"
    before = database.read_bytes()

    assert schema.validate_design_lineage_schema(con) is False
    assert database.read_bytes() == before

    _create_parent(con)
    con.commit()
    con.execute("BEGIN")
    schema.ensure_design_lineage_schema(con)
    assert schema.validate_design_lineage_schema(con) is True
    con.rollback()

    assert schema.validate_design_lineage_schema(con) is False
    assert [name for _kind, name in _objects(con)] == ["experiments"]
    con.close()


def test_healthy_component_is_exact_and_idempotent(tmp_path):
    con = _open_database(tmp_path)
    _create_parent(con)
    con.execute("INSERT INTO experiments VALUES ('experiment-1')")
    con.commit()

    con.execute("BEGIN")
    schema.ensure_design_lineage_schema(con)
    con.commit()

    assert schema.validate_design_lineage_schema(con) is True
    schema.ensure_design_lineage_schema(con)
    assert con.execute("SELECT name, version FROM design_lineage_schema").fetchall() == [
        (schema.SCHEMA_NAME, schema.SCHEMA_VERSION)
    ]
    assert [name for _kind, name in _objects(con)] == [
        "design_lineage_versions_owner_root",
        "design_lineage_roots",
        "design_lineage_schema",
        "design_lineage_versions",
        "experiments",
    ]
    con.close()


def test_unmarked_partial_namespace_fails_without_mutation(tmp_path):
    con = _open_database(tmp_path)
    con.execute("CREATE TABLE design_lineage_versions (experiment_id TEXT)")
    con.commit()
    con.close()
    database = tmp_path / "lineage.sqlite3"
    before = database.read_bytes()

    con = _open_database(tmp_path)
    with pytest.raises(ValueError, match="Unmarked"):
        schema.validate_design_lineage_schema(con)
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name='experiments'").fetchone() is None
    con.close()
    assert database.read_bytes() == before


@pytest.mark.parametrize("tamper", ["wrong-version", "missing-marker", "extra-marker"])
def test_metadata_marker_tampering_is_read_only(tmp_path, tamper):
    con = _open_database(tmp_path)
    _create_parent(con)
    schema.ensure_design_lineage_schema(con)
    con.commit()
    if tamper == "wrong-version":
        con.execute(
            "UPDATE design_lineage_schema SET version=999 WHERE name=?", (schema.SCHEMA_NAME,)
        )
    elif tamper == "missing-marker":
        con.execute("DELETE FROM design_lineage_schema WHERE name=?", (schema.SCHEMA_NAME,))
    else:
        con.execute("INSERT INTO design_lineage_schema VALUES ('unexpected', 1)")
    con.commit()
    con.close()
    database = tmp_path / "lineage.sqlite3"
    before = database.read_bytes()

    con = _open_database(tmp_path)
    with pytest.raises(ValueError):
        schema.validate_design_lineage_schema(con)
    con.close()
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    ("roots_ddl", "versions_ddl", "reason"),
    [
        (_ROOTS_DDL.replace("owner TEXT", "owner BLOB"), _VERSIONS_DDL, "root-affinity"),
        (_ROOTS_DDL, _VERSIONS_DDL.replace("owner TEXT", "owner BLOB"), "affinity"),
        (
            _ROOTS_DDL,
            _VERSIONS_DDL.replace(
                "created_at TEXT NOT NULL", "created_at TEXT NOT NULL DEFAULT ''"
            ),
            "default",
        ),
        (
            _ROOTS_DDL,
            _VERSIONS_DDL.replace(
                "FOREIGN KEY(root_experiment_id) REFERENCES design_lineage_roots(root_experiment_id)",
                "FOREIGN KEY(root_experiment_id) REFERENCES other_experiments(id)",
            ),
            "foreign-key",
        ),
        (
            _ROOTS_DDL,
            _VERSIONS_DDL.replace(
                "revision INTEGER NOT NULL", "revision INTEGER NOT NULL, extra TEXT"
            ),
            "column",
        ),
    ],
    ids=[
        "altered-root-affinity",
        "altered-version-affinity",
        "altered-default",
        "altered-foreign-key",
        "extra-column",
    ],
)
def test_physical_changes_are_rejected_without_writes(tmp_path, roots_ddl, versions_ddl, reason):
    con = _open_database(tmp_path)
    _create_parent(con)
    if reason == "foreign-key":
        con.execute("CREATE TABLE other_experiments (id TEXT PRIMARY KEY)")
    _create_declared_schema(con, roots_ddl=roots_ddl, versions_ddl=versions_ddl)
    con.commit()
    con.close()
    database = tmp_path / "lineage.sqlite3"
    before = database.read_bytes()

    con = _open_database(tmp_path)
    with pytest.raises(ValueError, match="incompatible|Unsupported"):
        schema.validate_design_lineage_schema(con)
    con.close()
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "object_sql",
    [
        "CREATE TABLE design_lineage_future (id TEXT)",
        "CREATE VIEW design_lineage_future AS SELECT 1 AS id",
        "CREATE INDEX design_lineage_future ON experiments(id)",
        "CREATE TABLE DESIGN_LINEAGE_FUTURE (id TEXT)",
    ],
    ids=["table", "view", "index", "case-insensitive-prefix"],
)
def test_reserved_namespace_objects_are_not_adopted(tmp_path, object_sql):
    con = _open_database(tmp_path)
    _create_parent(con)
    con.execute(object_sql)
    con.commit()
    con.close()
    database = tmp_path / "lineage.sqlite3"
    before = database.read_bytes()

    con = _open_database(tmp_path)
    with pytest.raises(ValueError, match="Unmarked|Unsupported"):
        schema.validate_design_lineage_schema(con)
    con.close()
    assert database.read_bytes() == before


def test_component_trigger_and_extra_index_fail_closed(tmp_path):
    con = _open_database(tmp_path)
    _create_parent(con)
    schema.ensure_design_lineage_schema(con)
    con.commit()
    con.execute(
        "CREATE TRIGGER design_lineage_versions_audit AFTER INSERT ON design_lineage_versions "
        "BEGIN SELECT 1; END"
    )
    con.execute("CREATE INDEX design_lineage_versions_extra ON design_lineage_versions(owner)")
    con.commit()
    con.close()
    database = tmp_path / "lineage.sqlite3"
    before = database.read_bytes()

    con = _open_database(tmp_path)
    with pytest.raises(ValueError):
        schema.validate_design_lineage_schema(con)
    con.close()
    assert database.read_bytes() == before
