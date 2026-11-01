"""Safe SQLite migration for repeated campaign candidates.

The migration removes only the legacy uniqueness constraint on
``(campaign_id, candidate_digest)``. It preserves proposal identity, ordering,
all stored values, foreign keys, and user-defined schema objects. Its rebuild
follows SQLite's documented 12-step ALTER TABLE procedure:
https://www.sqlite.org/lang_altertable.html
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

_TABLE = "campaign_proposals"
_TEMP_TABLE = "campaign_proposals__migration"
_COLUMNS = (
    "id",
    "owner",
    "campaign_id",
    "ordinal",
    "state",
    "candidate_digest",
    "executable_method_id",
    "method_input_digest",
    "canonical_program_digest",
    "payload",
    "input_digest",
    "created_at",
)
_COLUMN_SIGNATURE = (
    ("id", "TEXT", 0, None, 1),
    ("owner", "TEXT", 1, None, 0),
    ("campaign_id", "TEXT", 1, None, 0),
    ("ordinal", "INTEGER", 1, None, 0),
    ("state", "TEXT", 1, None, 0),
    ("candidate_digest", "TEXT", 1, None, 0),
    ("executable_method_id", "TEXT", 1, None, 0),
    ("method_input_digest", "TEXT", 1, None, 0),
    ("canonical_program_digest", "TEXT", 1, None, 0),
    ("payload", "TEXT", 1, None, 0),
    ("input_digest", "TEXT", 1, None, 0),
    ("created_at", "TEXT", 1, None, 0),
)
_FOREIGN_KEYS = {
    ("campaign_id", "campaign_runs", "id", "NO ACTION", "NO ACTION", "NONE"),
    (
        "executable_method_id",
        "executable_methods",
        "id",
        "NO ACTION",
        "NO ACTION",
        "NONE",
    ),
}
_LEGACY_DDL = """
CREATE TABLE campaign_proposals (
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, campaign_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL, state TEXT NOT NULL, candidate_digest TEXT NOT NULL,
  executable_method_id TEXT NOT NULL, method_input_digest TEXT NOT NULL,
  canonical_program_digest TEXT NOT NULL, payload TEXT NOT NULL,
  input_digest TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(campaign_id, ordinal), UNIQUE(campaign_id, candidate_digest),
  FOREIGN KEY(campaign_id) REFERENCES campaign_runs(id),
  FOREIGN KEY(executable_method_id) REFERENCES executable_methods(id)
)
"""
_CURRENT_DDL = """
CREATE TABLE campaign_proposals (
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, campaign_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL, state TEXT NOT NULL, candidate_digest TEXT NOT NULL,
  executable_method_id TEXT NOT NULL, method_input_digest TEXT NOT NULL,
  canonical_program_digest TEXT NOT NULL, payload TEXT NOT NULL,
  input_digest TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(campaign_id, ordinal),
  FOREIGN KEY(campaign_id) REFERENCES campaign_runs(id),
  FOREIGN KEY(executable_method_id) REFERENCES executable_methods(id)
)
"""
_PENDING_INDEX_SQL = (
    "CREATE UNIQUE INDEX campaign_one_pending "
    "ON campaign_proposals(owner, campaign_id) WHERE state='pending'"
)
_CANDIDATE_INDEX_SQL = (
    "CREATE INDEX campaign_proposals_candidate "
    "ON campaign_proposals(campaign_id, candidate_digest, ordinal)"
)


def ensure_proposal_repetition_schema(con: sqlite3.Connection) -> None:
    """Migrate proposal uniqueness while owning one SQLite transaction.

    The helper rejects an active caller transaction because SQLite requires
    ``PRAGMA foreign_keys=OFF`` before the migration transaction begins. It is
    safe to call repeatedly and serializes concurrent callers with
    ``BEGIN IMMEDIATE``.
    """
    if con.in_transaction:
        raise ValueError("Proposal schema migration requires a connection outside a transaction")

    original_foreign_keys = int(con.execute("PRAGMA foreign_keys").fetchone()[0])
    # Schema recognition spans sqlite_master and several PRAGMAs. A read
    # snapshot prevents another startup from committing a rebuild between
    # these queries. Release it before changing this connection's FK setting.
    con.execute("BEGIN")
    try:
        _assert_supported_database_context(con)
        initial_state = _schema_state(con)
        ready = initial_state == "current" and _candidate_index_ready(con)
    finally:
        con.rollback()
    if ready:
        return

    if initial_state == "legacy" and original_foreign_keys:
        con.execute("PRAGMA foreign_keys=OFF")

    try:
        con.execute("BEGIN IMMEDIATE")
        _assert_supported_database_context(con)
        state = _schema_state(con)
        if state == "legacy":
            _rebuild_legacy_table(con)
        _ensure_candidate_index(con)
        _assert_no_foreign_key_violations(con)
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        current_foreign_keys = int(con.execute("PRAGMA foreign_keys").fetchone()[0])
        if current_foreign_keys != original_foreign_keys:
            con.execute(f"PRAGMA foreign_keys={original_foreign_keys}")


def _assert_supported_database_context(con: sqlite3.Connection) -> None:
    databases = [row[1] for row in con.execute("PRAGMA database_list")]
    if any(name not in {"main", "temp"} for name in databases):
        raise ValueError("Attached databases are unsupported for campaign proposal migration")
    temp_objects = con.execute(
        """SELECT type, name, tbl_name, sql FROM sqlite_temp_master
        WHERE name NOT LIKE 'sqlite_%'"""
    ).fetchall()
    for kind, name, table, sql in temp_objects:
        if (
            name in {_TABLE, _TEMP_TABLE}
            or table in {_TABLE, _TEMP_TABLE}
            or (isinstance(sql, str) and "campaign_proposals" in sql.casefold())
        ):
            raise ValueError(
                "Temporary schema objects involving campaign_proposals are unsupported"
            )


def _schema_state(con: sqlite3.Connection) -> str:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise ValueError("The campaign_proposals table is missing or unsupported")
    ddl = _normalize_sql(row[0])
    if ddl == _normalize_sql(_LEGACY_DDL):
        state = "legacy"
    elif ddl == _normalize_sql(_CURRENT_DDL):
        state = "current"
    else:
        raise ValueError("campaign_proposals has an unsupported schema; no migration was applied")

    info = con.execute("PRAGMA table_info(campaign_proposals)").fetchall()
    signature = tuple((r[1], str(r[2]).upper(), r[3], r[4], r[5]) for r in info)
    if signature != _COLUMN_SIGNATURE:
        raise ValueError("campaign_proposals columns differ from the supported schema")
    xinfo = con.execute("PRAGMA table_xinfo(campaign_proposals)").fetchall()
    if len(xinfo) != len(_COLUMN_SIGNATURE) or any(r[6] != 0 for r in xinfo):
        raise ValueError("campaign_proposals contains unsupported hidden or generated columns")
    foreign_keys = {
        (r[3], r[2], r[4], r[5], r[6], r[7])
        for r in con.execute("PRAGMA foreign_key_list(campaign_proposals)")
    }
    if foreign_keys != _FOREIGN_KEYS:
        raise ValueError("campaign_proposals foreign keys differ from the supported schema")

    indexes = _index_descriptions(con, _TABLE)
    primary = [item for item in indexes if item["origin"] == "pk"]
    unique_constraints = [item for item in indexes if item["origin"] == "u"]
    custom_candidate_uniques = [
        item
        for item in indexes
        if item["unique"]
        and item["origin"] == "c"
        and set(item["columns"]) == {"campaign_id", "candidate_digest"}
    ]
    if custom_candidate_uniques:
        raise ValueError("A custom unique candidate index is unsupported; no migration was applied")
    if len(primary) != 1 or primary[0]["columns"] != ("id",):
        raise ValueError("campaign_proposals must retain its id primary key")

    required_unique = {("campaign_id", "ordinal")}
    found_unique = {item["columns"] for item in unique_constraints}
    legacy_unique = {"campaign_id", "candidate_digest"}
    if not required_unique <= found_unique:
        raise ValueError("campaign_proposals must retain unique campaign ordinals")
    candidate_constraint_present = sum(
        set(item["columns"]) == legacy_unique for item in unique_constraints
    )
    if state == "legacy":
        if candidate_constraint_present != 1 or found_unique != {
            ("campaign_id", "ordinal"),
            ("campaign_id", "candidate_digest"),
        }:
            raise ValueError("campaign_proposals legacy uniqueness constraints are unsupported")
    elif candidate_constraint_present or found_unique != required_unique:
        raise ValueError("campaign_proposals has unsupported unique constraints")

    _validate_pending_index(indexes)
    _validate_candidate_index_if_present(indexes)
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TEMP_TABLE,)
    ).fetchone():
        raise ValueError("A leftover proposal migration table exists; no migration was applied")
    return state


def _index_descriptions(con: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    indexes: list[dict[str, Any]] = []
    for row in con.execute(f"PRAGMA index_list({_quote_identifier(table)})"):
        name = row[1]
        columns = tuple(
            info[2] for info in con.execute(f"PRAGMA index_info({_quote_identifier(name)})")
        )
        sql_row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        indexes.append(
            {
                "name": name,
                "unique": bool(row[2]),
                "origin": row[3],
                "partial": bool(row[4]),
                "columns": columns,
                "sql": None if sql_row is None else sql_row[0],
            }
        )
    return indexes


def _validate_pending_index(indexes: list[dict[str, Any]]) -> None:
    matches = [item for item in indexes if item["name"] == "campaign_one_pending"]
    if (
        len(matches) != 1
        or not matches[0]["unique"]
        or not matches[0]["partial"]
        or matches[0]["columns"] != ("owner", "campaign_id")
        or _normalize_sql(matches[0]["sql"] or "") != _normalize_sql(_PENDING_INDEX_SQL)
    ):
        raise ValueError("campaign_proposals pending-proposal index is missing or unsupported")


def _validate_candidate_index_if_present(indexes: list[dict[str, Any]]) -> None:
    matches = [item for item in indexes if item["name"] == "campaign_proposals_candidate"]
    if not matches:
        return
    if (
        len(matches) != 1
        or matches[0]["unique"]
        or matches[0]["partial"]
        or matches[0]["columns"] != ("campaign_id", "candidate_digest", "ordinal")
        or _normalize_sql(matches[0]["sql"] or "") != _normalize_sql(_CANDIDATE_INDEX_SQL)
    ):
        raise ValueError("campaign_proposals_candidate index exists with an unsupported definition")


def _rebuild_legacy_table(con: sqlite3.Connection) -> None:
    _assert_no_foreign_key_violations(con)
    preserved_objects = _table_indexes(con)
    preserved_views = _all_objects(con, "view")
    preserved_triggers = _all_objects(con, "trigger")
    foreign_key_shape = _all_foreign_key_shapes(con)
    before_count = con.execute("SELECT COUNT(*) FROM campaign_proposals").fetchone()[0]

    con.execute(
        """CREATE TABLE campaign_proposals__migration (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, campaign_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL, state TEXT NOT NULL, candidate_digest TEXT NOT NULL,
          executable_method_id TEXT NOT NULL, method_input_digest TEXT NOT NULL,
          canonical_program_digest TEXT NOT NULL, payload TEXT NOT NULL,
          input_digest TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(campaign_id, ordinal),
          FOREIGN KEY(campaign_id) REFERENCES campaign_runs(id),
          FOREIGN KEY(executable_method_id) REFERENCES executable_methods(id)
        )"""
    )
    columns = ", ".join(_quote_identifier(column) for column in _COLUMNS)
    con.execute(
        f"INSERT INTO {_quote_identifier(_TEMP_TABLE)} (rowid, {columns}) "
        f"SELECT rowid, {columns} FROM {_quote_identifier(_TABLE)} ORDER BY rowid"
    )
    copied_count = con.execute(f"SELECT COUNT(*) FROM {_quote_identifier(_TEMP_TABLE)}").fetchone()[
        0
    ]
    if before_count != copied_count or _has_row_difference(con, _TABLE, _TEMP_TABLE):
        raise RuntimeError("Campaign proposal rows changed while preparing the schema migration")

    # SQLite checks every view definition while renaming a table. Temporarily
    # remove and restore views and triggers in the same transaction so views
    # that refer to campaign_proposals remain valid across the brief table gap.
    for name, _, _ in preserved_triggers:
        con.execute(f"DROP TRIGGER {_quote_identifier(name)}")
    for name, _, _ in preserved_views:
        con.execute(f"DROP VIEW {_quote_identifier(name)}")
    con.execute(f"DROP TABLE {_quote_identifier(_TABLE)}")
    con.execute(
        f"ALTER TABLE {_quote_identifier(_TEMP_TABLE)} RENAME TO {_quote_identifier(_TABLE)}"
    )
    for item in preserved_objects:
        con.execute(item["sql"])
    for name, _, sql in preserved_views:
        con.execute(sql)
    for name, _, sql in preserved_triggers:
        con.execute(sql)

    if _table_indexes(con) != preserved_objects:
        raise RuntimeError("Campaign proposal indexes or triggers changed during migration")
    if _all_objects(con, "view") != preserved_views:
        raise RuntimeError("Database views changed during campaign proposal migration")
    if _all_objects(con, "trigger") != preserved_triggers:
        raise RuntimeError("Database triggers changed during campaign proposal migration")
    if _all_foreign_key_shapes(con) != foreign_key_shape:
        raise RuntimeError("Foreign-key declarations changed during proposal migration")
    if con.execute("SELECT COUNT(*) FROM campaign_proposals").fetchone()[0] != before_count:
        raise RuntimeError("Campaign proposal row count changed during schema migration")


def _has_row_difference(con: sqlite3.Connection, left: str, right: str) -> bool:
    projection = "rowid, " + ", ".join(_quote_identifier(column) for column in _COLUMNS)
    left_sql = f"SELECT {projection} FROM {_quote_identifier(left)}"
    right_sql = f"SELECT {projection} FROM {_quote_identifier(right)}"
    return (
        con.execute(f"{left_sql} EXCEPT {right_sql} LIMIT 1").fetchone() is not None
        or con.execute(f"{right_sql} EXCEPT {left_sql} LIMIT 1").fetchone() is not None
    )


def _table_indexes(con: sqlite3.Connection) -> list[dict[str, str]]:
    rows = con.execute(
        """SELECT type, name, tbl_name, sql FROM sqlite_master
        WHERE type='index' AND tbl_name=? AND sql IS NOT NULL
        ORDER BY type, name""",
        (_TABLE,),
    )
    return [{"type": row[0], "name": row[1], "tbl_name": row[2], "sql": row[3]} for row in rows]


def _all_objects(con: sqlite3.Connection, kind: str) -> list[tuple[str, str, str]]:
    if kind not in {"view", "trigger"}:
        raise ValueError("Unsupported schema object kind")
    return [
        tuple(row)
        for row in con.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type=? ORDER BY name",
            (kind,),
        )
    ]


def _all_foreign_key_shapes(con: sqlite3.Connection) -> list[tuple[Any, ...]]:
    tables = [
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    shapes = []
    for table in tables:
        for row in con.execute(f"PRAGMA foreign_key_list({_quote_identifier(table)})"):
            shapes.append((table, *tuple(row)))
    return shapes


def _ensure_candidate_index(con: sqlite3.Connection) -> None:
    row = con.execute(
        "SELECT type, tbl_name, sql FROM sqlite_master WHERE name='campaign_proposals_candidate'"
    ).fetchone()
    if row is None:
        con.execute(_CANDIDATE_INDEX_SQL)
        return
    indexes = _index_descriptions(con, _TABLE)
    _validate_candidate_index_if_present(indexes)
    if row[0] != "index" or row[1] != _TABLE:
        raise ValueError("campaign_proposals_candidate name is occupied by another schema object")


def _candidate_index_ready(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT type, tbl_name FROM sqlite_master WHERE name='campaign_proposals_candidate'"
    ).fetchone()
    if row is None:
        return False
    if row[0] != "index" or row[1] != _TABLE:
        raise ValueError("campaign_proposals_candidate name is occupied by another schema object")
    _validate_candidate_index_if_present(_index_descriptions(con, _TABLE))
    return True


def _assert_no_foreign_key_violations(con: sqlite3.Connection) -> None:
    violations = con.execute("PRAGMA foreign_key_check").fetchmany(10)
    if violations:
        raise ValueError("Cannot migrate campaign proposals while foreign-key violations exist")


def _normalize_sql(sql: str) -> str:
    # SQL keywords and identifiers are case insensitive; string literals are
    # not. In particular, state='PENDING' must never pass as state='pending'.
    parts = re.split(r"('(?:[^']|'')*')", sql)
    return "".join(
        part
        if index % 2
        else "".join(part.split())
        .replace('"', "")
        .replace("`", "")
        .replace("[", "")
        .replace("]", "")
        .casefold()
        for index, part in enumerate(parts)
    ).rstrip(";")


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
