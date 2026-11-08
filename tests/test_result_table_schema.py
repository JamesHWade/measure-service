"""Fail-closed, transactional tests for the optional result-table schema."""

from __future__ import annotations

import sqlite3

import pytest

from measure_service import result_table_schema as schema

_IMPORTS_DDL = """CREATE TABLE result_table_imports (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  source_text TEXT NOT NULL,
  specification TEXT NOT NULL,
  metadata TEXT NOT NULL,
  created_at TEXT NOT NULL
)"""
_ROWS_DDL = """CREATE TABLE result_table_rows (
  import_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  result_id TEXT NOT NULL,
  result_sha256 TEXT NOT NULL,
  row_sha256 TEXT NOT NULL,
  PRIMARY KEY(import_id, ordinal),
  UNIQUE(result_id),
  FOREIGN KEY(import_id) REFERENCES result_table_imports(id),
  FOREIGN KEY(result_id) REFERENCES experiment_results(id)
)"""


def _open_database(tmp_path):
    return sqlite3.connect(tmp_path / "measure.sqlite3")


def _create_parent(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE experiment_results (id TEXT PRIMARY KEY)")


def _create_declared_schema(
    con: sqlite3.Connection,
    *,
    imports_ddl: str = _IMPORTS_DDL,
    rows_ddl: str = _ROWS_DDL,
) -> None:
    con.execute("CREATE TABLE result_table_schema(name TEXT PRIMARY KEY, version INTEGER NOT NULL)")
    con.execute(
        "INSERT INTO result_table_schema(name, version) VALUES (?, ?)",
        (schema.SCHEMA_NAME, schema.SCHEMA_VERSION),
    )
    con.execute(imports_ddl)
    con.execute(rows_ddl)
    con.execute(
        "CREATE INDEX result_table_imports_owner_order "
        "ON result_table_imports(owner, created_at, id)"
    )


def _objects(con: sqlite3.Connection):
    return con.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()


def test_absent_reads_are_read_only_and_creation_rolls_back(tmp_path):
    con = _open_database(tmp_path)
    before = (tmp_path / "measure.sqlite3").read_bytes()

    assert schema.validate_result_table_schema(con) is False
    assert (tmp_path / "measure.sqlite3").read_bytes() == before

    con.execute("CREATE TABLE experiment_results (id TEXT PRIMARY KEY)")
    con.execute("BEGIN")
    schema.ensure_result_table_schema(con)
    assert schema.validate_result_table_schema(con) is True
    con.rollback()

    assert schema.validate_result_table_schema(con) is False
    assert [name for _kind, name in _objects(con)] == ["experiment_results"]
    con.close()


def test_healthy_component_preserves_external_parent_and_is_idempotent(tmp_path):
    con = _open_database(tmp_path)
    _create_parent(con)
    con.execute("INSERT INTO experiment_results VALUES ('result-1')")
    con.commit()
    con.execute("BEGIN")
    schema.ensure_result_table_schema(con)
    con.commit()

    assert schema.validate_result_table_schema(con) is True
    schema.ensure_result_table_schema(con)
    assert con.execute("SELECT name, version FROM result_table_schema").fetchall() == [
        (schema.SCHEMA_NAME, schema.SCHEMA_VERSION)
    ]
    assert con.execute("SELECT * FROM experiment_results").fetchall() == [("result-1",)]
    assert [name for _kind, name in _objects(con)] == [
        "result_table_imports_owner_order",
        "experiment_results",
        "result_table_imports",
        "result_table_rows",
        "result_table_schema",
    ]
    con.close()


def test_unmarked_partial_namespace_fails_before_base_ddl_and_does_not_write(tmp_path):
    con = _open_database(tmp_path)
    con.execute("CREATE TABLE result_table_imports (id TEXT)")
    con.commit()
    con.close()
    database = tmp_path / "measure.sqlite3"
    before = database.read_bytes()

    con = sqlite3.connect(database)
    with pytest.raises(ValueError, match="Unmarked"):
        schema.validate_result_table_schema(con)
    assert (
        con.execute("SELECT 1 FROM sqlite_master WHERE name='experiment_results'").fetchone()
        is None
    )
    con.close()
    assert database.read_bytes() == before


@pytest.mark.parametrize("tamper", ["wrong-version", "missing-marker", "extra-marker"])
def test_metadata_marker_tampering_is_read_only(tmp_path, tamper):
    con = _open_database(tmp_path)
    _create_parent(con)
    schema.ensure_result_table_schema(con)
    con.commit()
    if tamper == "wrong-version":
        con.execute(
            "UPDATE result_table_schema SET version=999 WHERE name=?",
            (schema.SCHEMA_NAME,),
        )
    elif tamper == "missing-marker":
        con.execute("DELETE FROM result_table_schema WHERE name=?", (schema.SCHEMA_NAME,))
    else:
        con.execute("INSERT INTO result_table_schema VALUES ('unexpected', 1)")
    con.commit()
    con.close()
    database = tmp_path / "measure.sqlite3"
    before = database.read_bytes()

    con = sqlite3.connect(database)
    with pytest.raises(ValueError):
        schema.validate_result_table_schema(con)
    con.close()
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    ("imports_ddl", "rows_ddl"),
    [
        (
            _IMPORTS_DDL.replace("input_sha256 TEXT", "input_sha256 BLOB"),
            _ROWS_DDL,
        ),
        (
            _IMPORTS_DDL.replace("created_at TEXT NOT NULL", "created_at TEXT NOT NULL DEFAULT ''"),
            _ROWS_DDL,
        ),
        (
            _IMPORTS_DDL,
            _ROWS_DDL.replace(
                "FOREIGN KEY(result_id) REFERENCES experiment_results(id)",
                "FOREIGN KEY(result_id) REFERENCES another_results(id)",
            ),
        ),
    ],
    ids=["altered-affinity", "altered-default", "altered-foreign-key"],
)
def test_physical_schema_changes_are_rejected_without_writes(tmp_path, imports_ddl, rows_ddl):
    con = _open_database(tmp_path)
    _create_parent(con)
    _create_declared_schema(con, imports_ddl=imports_ddl, rows_ddl=rows_ddl)
    con.commit()
    con.close()
    database = tmp_path / "measure.sqlite3"
    before = database.read_bytes()

    con = sqlite3.connect(database)
    with pytest.raises(ValueError, match="incompatible|Unsupported"):
        schema.validate_result_table_schema(con)
    con.close()
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "object_sql",
    [
        "CREATE TABLE result_table_future (id TEXT)",
        "CREATE VIEW result_table_future AS SELECT 1 AS id",
        "CREATE INDEX result_table_future ON experiment_results(id)",
    ],
)
def test_reserved_namespace_objects_are_not_adopted(tmp_path, object_sql):
    con = _open_database(tmp_path)
    _create_parent(con)
    con.execute(object_sql)
    con.commit()
    con.close()
    database = tmp_path / "measure.sqlite3"
    before = database.read_bytes()

    con = sqlite3.connect(database)
    with pytest.raises(ValueError, match="Unmarked|Unsupported"):
        schema.validate_result_table_schema(con)
    con.close()
    assert database.read_bytes() == before


def test_trigger_on_component_table_and_extra_index_fail_closed(tmp_path):
    con = _open_database(tmp_path)
    _create_parent(con)
    schema.ensure_result_table_schema(con)
    con.commit()
    con.execute(
        "CREATE TRIGGER result_table_rows_audit AFTER INSERT ON result_table_rows "
        "BEGIN SELECT 1; END"
    )
    con.execute("CREATE INDEX result_table_rows_extra ON result_table_rows(result_id)")
    con.commit()
    con.close()
    database = tmp_path / "measure.sqlite3"
    before = database.read_bytes()

    con = sqlite3.connect(database)
    with pytest.raises(ValueError):
        schema.validate_result_table_schema(con)
    con.close()
    assert database.read_bytes() == before
