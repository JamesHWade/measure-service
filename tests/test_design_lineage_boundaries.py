"""Regressions for retained bytes, SQL allocation bounds and read snapshots."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from measure_service import design_lineage as lineage
from measure_service import experiments
from measure_service.storage import db_path


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    return experiments.create_experiment(
        "chemist", experiments.ExperimentSpec(title="Original", source="manual"), "root"
    )


def _replace(root, key="next", title="Replacement"):
    return lineage.supersede_experiment(
        "chemist",
        root["experiment_id"],
        root["input_sha256"],
        experiments.ExperimentSpec(title=title, source="manual"),
        "New intended design",
        key,
    )


def _sql(statement, parameters=()):
    with sqlite3.connect(db_path()) as con:
        con.execute(statement, parameters)


@pytest.mark.parametrize("kind", ["whitespace", "duplicate-key"])
def test_new_receipts_require_exact_canonical_retained_json(root, kind):
    receipt = _replace(root)
    with sqlite3.connect(db_path()) as con:
        (raw,) = con.execute(
            "SELECT response FROM registry_receipts WHERE request_key='next'"
        ).fetchone()
    changed = " " + raw if kind == "whitespace" else raw[:-1] + ',"schema_version":"1.0"}'
    _sql("UPDATE registry_receipts SET response=? WHERE request_key='next'", (changed,))
    before = db_path().read_bytes()
    with pytest.raises(ValueError, match="receipt"):
        _replace(root)
    assert db_path().read_bytes() == before
    assert receipt["entry"]["revision"] == 2


def test_existing_receipts_keep_their_original_read_contract(root):
    with sqlite3.connect(db_path()) as con:
        (raw,) = con.execute(
            "SELECT response FROM registry_receipts WHERE request_key='root'"
        ).fetchone()
    _sql("UPDATE registry_receipts SET response=? WHERE request_key='root'", (" " + raw,))
    before = db_path().read_bytes()
    assert (
        experiments.create_experiment(
            "chemist", experiments.ExperimentSpec(title="Original", source="manual"), "root"
        )
        == root
    )
    assert db_path().read_bytes() == before


@pytest.mark.parametrize("kind", ["experiment", "version", "head-revision", "revision", "receipt"])
def test_corrupt_large_sql_values_are_rejected_before_python_materialization(
    root, monkeypatch, kind
):
    _replace(root)
    statements = {
        "experiment": (
            "UPDATE experiments SET payload=? WHERE id=?",
            ("x" * 100_000, root["experiment_id"]),
        ),
        "version": (
            "UPDATE design_lineage_versions SET payload=? WHERE revision=2",
            ("x" * 100_000,),
        ),
        "head-revision": ("UPDATE design_lineage_roots SET latest_revision=?", ("x" * 100_000,)),
        "revision": (
            "UPDATE design_lineage_versions SET revision=? WHERE revision=2",
            ("x" * 100_000,),
        ),
        "receipt": (
            "UPDATE registry_receipts SET response=? WHERE request_key='next'",
            ("x" * 100_000,),
        ),
    }
    _sql(*statements[kind])
    before = db_path().read_bytes()
    original_connect = sqlite3.connect

    def bounded_row(cursor, row):
        if any(isinstance(value, (str, bytes)) and len(value) > 64_000 for value in row):
            raise AssertionError("Oversized stored value reached Python before its SQL bound")
        return sqlite3.Row(cursor, row)

    class BoundedConnection(sqlite3.Connection):
        @property
        def row_factory(self):
            return sqlite3.Connection.row_factory.__get__(self)

        @row_factory.setter
        def row_factory(self, value):
            sqlite3.Connection.row_factory.__set__(
                self, bounded_row if value is sqlite3.Row else value
            )

    def connect(*args, **kwargs):
        return original_connect(*args, **{**kwargs, "factory": BoundedConnection})

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(ValueError):
        if kind == "receipt":
            _replace(root)
        else:
            lineage.get_experiment_design_lineage("chemist", root["experiment_id"])
    assert db_path().read_bytes() == before


def test_fresh_response_verification_failure_rolls_back_all_new_state(root, monkeypatch):
    before = db_path().read_bytes()
    original = lineage._load

    def reject_new_binding(con, owner, experiment_id):
        if experiment_id != root["experiment_id"]:
            raise ValueError("Injected response-binding verification failure")
        return original(con, owner, experiment_id)

    monkeypatch.setattr(lineage, "_load", reject_new_binding)
    with pytest.raises(ValueError, match="Injected"):
        _replace(root)
    assert db_path().read_bytes() == before
    with sqlite3.connect(db_path()) as con:
        assert con.execute("SELECT count(*) FROM experiments").fetchone()[0] == 1
        assert not con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='design_lineage_schema'"
        ).fetchone()
        assert not con.execute(
            "SELECT 1 FROM registry_receipts WHERE request_key='next'"
        ).fetchone()


def test_read_retains_one_snapshot_while_another_connection_advances_head(root, monkeypatch):
    first = _replace(root)
    child = experiments.get_experiment("chemist", first["entry"]["experiment_id"])
    _sql("PRAGMA journal_mode=WAL")
    selected, resume = threading.Event(), threading.Event()
    original = lineage._verified_experiment
    paused = False

    def intercept(con, owner, experiment_id):
        nonlocal paused
        result = original(con, owner, experiment_id)
        if threading.current_thread().name.startswith("lineage-reader") and not paused:
            paused = True
            selected.set()
            assert resume.wait(5)
        return result

    monkeypatch.setattr(lineage, "_verified_experiment", intercept)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="lineage-reader") as pool:
        future = pool.submit(
            lineage.get_experiment_design_lineage, "chemist", root["experiment_id"]
        )
        assert selected.wait(5)
        try:
            _replace(child, key="third", title="Third")
        finally:
            resume.set()
        captured = future.result(5)
    assert captured["count"] == captured["current_revision"] == 2
    assert captured["current_experiment_id"] == child["experiment_id"]
    assert lineage.get_experiment_design_lineage("chemist", root["experiment_id"])["count"] == 3
