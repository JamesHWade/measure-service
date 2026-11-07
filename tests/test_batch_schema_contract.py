"""Physical-schema compatibility checks for the analytical batch component."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from measure_service.storage import connect, db_path


@pytest.fixture(autouse=True)
def new_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))


def _digest() -> str:
    return hashlib.sha256(db_path().read_bytes()).hexdigest()


def _initialize() -> None:
    with connect():
        pass


def _replace_empty_table(
    table: str,
    create_sql: str,
    *,
    explicit_indexes: tuple[str, ...] = (),
    index_sql: tuple[str, ...] = (),
) -> None:
    """Replace an empty owned table with a controlled physical variant."""
    old = f"{table}_old"
    with sqlite3.connect(db_path()) as con:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("PRAGMA legacy_alter_table=ON")
        for index in explicit_indexes:
            con.execute(f'DROP INDEX "{index}"')
        con.execute(f'ALTER TABLE "{table}" RENAME TO "{old}"')
        con.execute(create_sql)
        con.execute(f'DROP TABLE "{old}"')
        for statement in index_sql:
            con.execute(statement)


def _assert_rejected_without_mutation(damage) -> None:
    _initialize()
    damage()
    before = _digest()
    with pytest.raises(ValueError, match="incompatible|unsupported|missing"):
        connect()
    assert _digest() == before


def test_unexpected_trigger_on_owned_table_is_rejected_before_base_ddl():
    def damage():
        with sqlite3.connect(db_path()) as con:
            con.execute(
                "CREATE TRIGGER custom_mutation AFTER INSERT ON analysis_batches BEGIN UPDATE analysis_batches SET revision=revision+1 WHERE id=NEW.id; END"
            )

    _assert_rejected_without_mutation(damage)


def test_implicit_foreign_key_target_is_rejected_as_an_incompatible_schema():
    def damage():
        with sqlite3.connect(db_path()) as con:
            sql = con.execute(
                "SELECT sql FROM sqlite_master WHERE name='analysis_batch_control_events'"
            ).fetchone()[0]
        _replace_empty_table(
            "analysis_batch_control_events",
            sql.replace("REFERENCES analysis_batches(id)", "REFERENCES analysis_batches"),
            explicit_indexes=("analysis_batch_events_owner_order",),
            index_sql=(
                "CREATE INDEX analysis_batch_events_owner_order ON analysis_batch_control_events(owner, batch_id, event_no)",
            ),
        )

    _assert_rejected_without_mutation(damage)


def test_wrong_batch_schema_version_affinity_is_rejected_before_base_ddl():
    def damage():
        _replace_empty_table(
            "analysis_batches",
            """CREATE TABLE analysis_batches (
              id TEXT PRIMARY KEY,
              owner TEXT NOT NULL,
              profile TEXT NOT NULL,
              schema_version INTEGER NOT NULL,
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
            explicit_indexes=("analysis_batches_owner_order",),
            index_sql=(
                "CREATE INDEX analysis_batches_owner_order "
                "ON analysis_batches(owner, created_at, id)",
            ),
        )

    _assert_rejected_without_mutation(damage)


@pytest.mark.parametrize(
    "create_sql",
    [
        """CREATE TABLE analysis_batch_control_events (
          owner TEXT NOT NULL,
          id TEXT PRIMARY KEY,
          batch_id TEXT NOT NULL,
          event_no INTEGER NOT NULL CHECK(event_no >= 1),
          payload TEXT NOT NULL,
          digest TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(batch_id, event_no),
          FOREIGN KEY(batch_id) REFERENCES analysis_batches(id)
        )""",
        """CREATE TABLE analysis_batch_control_events (
          id TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          batch_id TEXT NOT NULL,
          event_no INTEGER NOT NULL CHECK(event_no >= 1),
          payload TEXT NOT NULL,
          digest TEXT NOT NULL,
          created_at TEXT NOT NULL,
          extra TEXT,
          UNIQUE(batch_id, event_no),
          FOREIGN KEY(batch_id) REFERENCES analysis_batches(id)
        )""",
    ],
    ids=["reordered-columns", "extra-column"],
)
def test_control_event_column_layout_is_exact(create_sql):
    _assert_rejected_without_mutation(
        lambda: _replace_empty_table(
            "analysis_batch_control_events",
            create_sql,
            explicit_indexes=("analysis_batch_events_owner_order",),
            index_sql=(
                "CREATE INDEX analysis_batch_events_owner_order "
                "ON analysis_batch_control_events(owner, batch_id, event_no)",
            ),
        )
    )


_ATTEMPT_DDL = """CREATE TABLE analysis_batch_attempts (
  id {id_definition},
  owner TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  attempt_no INTEGER NOT NULL {attempt_check},
  execution_token TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ({state_values})),
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
  {unique_attempt}
  UNIQUE(batch_id, ordinal, execution_token),
  {run_fk}
  FOREIGN KEY(batch_id, ordinal) REFERENCES analysis_batch_items(batch_id, ordinal),
  FOREIGN KEY(analysis_id) REFERENCES analyses(id)
)"""


@pytest.mark.parametrize(
    "variant",
    [
        {
            "id_definition": "TEXT NOT NULL",
            "attempt_check": "CHECK(attempt_no BETWEEN 1 AND 8)",
            "state_values": "'running', 'completed', 'failed', 'interrupted', 'cancelled'",
            "unique_attempt": "UNIQUE(batch_id, ordinal, attempt_no),",
            "run_fk": "FOREIGN KEY(run_id) REFERENCES analysis_batch_runs(id),",
        },
        {
            "id_definition": "TEXT PRIMARY KEY",
            "attempt_check": "CHECK(attempt_no BETWEEN 1 AND 8)",
            "state_values": "'running', 'completed', 'failed', 'interrupted', 'cancelled'",
            "unique_attempt": "",
            "run_fk": "FOREIGN KEY(run_id) REFERENCES analysis_batch_runs(id),",
        },
        {
            "id_definition": "TEXT PRIMARY KEY",
            "attempt_check": "CHECK(attempt_no BETWEEN 1 AND 8)",
            "state_values": "'running', 'completed', 'failed', 'interrupted', 'cancelled'",
            "unique_attempt": "UNIQUE(batch_id, ordinal, attempt_no),",
            "run_fk": "",
        },
        {
            "id_definition": "TEXT PRIMARY KEY",
            "attempt_check": "",
            "state_values": "'running', 'completed', 'failed', 'interrupted', 'cancelled'",
            "unique_attempt": "UNIQUE(batch_id, ordinal, attempt_no),",
            "run_fk": "FOREIGN KEY(run_id) REFERENCES analysis_batch_runs(id),",
        },
        {
            "id_definition": "TEXT PRIMARY KEY",
            "attempt_check": "CHECK(attempt_no BETWEEN 1 AND 8)",
            "state_values": "'RUNNING', 'completed', 'failed', 'interrupted', 'cancelled'",
            "unique_attempt": "UNIQUE(batch_id, ordinal, attempt_no),",
            "run_fk": "FOREIGN KEY(run_id) REFERENCES analysis_batch_runs(id),",
        },
    ],
    ids=[
        "dropped-primary-key",
        "dropped-attempt-unique",
        "dropped-run-foreign-key",
        "dropped-check",
        "changed-check-literal",
    ],
)
def test_attempt_constraints_are_part_of_the_accepted_schema(variant):
    _assert_rejected_without_mutation(
        lambda: _replace_empty_table(
            "analysis_batch_attempts",
            _ATTEMPT_DDL.format(**variant),
            explicit_indexes=(
                "analysis_batch_attempts_owner_batch_order",
                "analysis_batch_attempts_owner_run_order",
            ),
            index_sql=(
                "CREATE INDEX analysis_batch_attempts_owner_batch_order "
                "ON analysis_batch_attempts(owner, batch_id, ordinal, attempt_no)",
                "CREATE INDEX analysis_batch_attempts_owner_run_order "
                "ON analysis_batch_attempts(owner, run_id, ordinal)",
            ),
        )
    )
