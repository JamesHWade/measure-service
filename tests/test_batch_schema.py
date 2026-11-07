"""Additive initialization and fail-closed component schema validation."""

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from measure_service.batch_schema import SCHEMA_NAME
from measure_service.storage import connect, db_path


@pytest.fixture(autouse=True)
def new_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))


def _digest():
    return hashlib.sha256(db_path().read_bytes()).hexdigest()


def test_existing_service_rows_survive_additive_batch_initialization():
    with sqlite3.connect(db_path()) as con:
        con.execute("""CREATE TABLE datasets (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, digest TEXT NOT NULL,
          payload TEXT NOT NULL, created_at TEXT NOT NULL)""")
        con.execute("INSERT INTO datasets VALUES ('retained','owner','digest','{}','timestamp')")
    with connect() as con:
        assert con.execute("SELECT * FROM datasets").fetchall() == [
            ("retained", "owner", "digest", "{}", "timestamp")
        ]
        assert con.execute("SELECT * FROM measure_batch_schema").fetchall() == [(SCHEMA_NAME, 1)]
        assert con.execute("SELECT COUNT(*) FROM analysis_batch_control_events").fetchone() == (0,)


def test_unknown_version_is_rejected_before_any_schema_mutation():
    with sqlite3.connect(db_path()) as con:
        con.execute(
            "CREATE TABLE measure_batch_schema (name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        con.execute("INSERT INTO measure_batch_schema VALUES (?, 2)", (SCHEMA_NAME,))
    before = _digest()
    with pytest.raises(ValueError, match="Unsupported.*schema version"):
        connect()
    assert _digest() == before


@pytest.mark.parametrize(
    "damage, message",
    [
        (
            "ALTER TABLE analysis_batches DROP COLUMN manifest_digest",
            "required columns are missing",
        ),
        ("DROP TABLE analysis_batch_control_events", "missing table"),
    ],
)
def test_incomplete_known_schema_is_not_repaired_or_given_a_new_content_digest(damage, message):
    with connect() as con:
        con.execute(damage)
    before = _digest()
    with pytest.raises(ValueError, match=message):
        connect()
    assert _digest() == before


def test_unmarked_partial_namespace_is_not_adopted():
    with sqlite3.connect(db_path()) as con:
        con.execute("CREATE TABLE analysis_batches (id TEXT PRIMARY KEY)")
    before = _digest()
    with pytest.raises(ValueError, match="Unmarked"):
        connect()
    assert _digest() == before


def test_two_initializers_commit_one_complete_component_schema():
    ready = threading.Barrier(2)

    def initialize():
        ready.wait(timeout=5)
        with connect() as con:
            return con.execute("SELECT * FROM measure_batch_schema").fetchall()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(initialize)
        second = pool.submit(initialize)
        assert first.result(timeout=10) == second.result(timeout=10) == [(SCHEMA_NAME, 1)]


def test_synthetic_runner_accepts_empty_schema_but_rejects_analytical_batch_data():
    from measure_service import batches, server
    from measure_service.batch_models import BatchSpec
    from measure_service.benchmarks import synthetic_campaigns as benchmark

    identity = "a" * 64
    with connect() as con:
        benchmark._insert_store_marker(con, identity)
        assert benchmark._check_owned_store(con, identity, allow_empty=False)
    trace = server.Trace(
        trace_id="uv",
        sample_id="sample",
        injection_id="inj",
        channel="UV",
        separation="LC",
        detector="UV",
        axis_unit="min",
        signal_unit="mAU",
        axis=[0, 1, 2],
        signal=[0, 1, 0],
    )
    dataset = server.register_dataset(
        benchmark.OWNER, server.Dataset(title="constructed", traces=[trace])
    )
    batches.create_batch(
        benchmark.OWNER,
        BatchSpec(
            title="analytical",
            method={},
            inputs=[
                {
                    "dataset_id": dataset["dataset_id"],
                    "trace_id": "uv",
                    "role": "unknown",
                }
            ],
        ),
        "separate-analytical-request",
    )
    before = _digest()
    with connect() as con, pytest.raises(ValueError, match="must not contain analytical batches"):
        benchmark._check_owned_store(con, identity, allow_empty=False)
    assert _digest() == before
