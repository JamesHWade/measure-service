"""Coherent source capture and bounded portable history."""

import hashlib
import json
import sqlite3

import pytest
from snapshot_store_support import make_snapshot_store

from measure_service import batch_capture, batches, storage
from measure_service.batch_snapshot import decode_snapshot


@pytest.fixture
def declared(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    return make_snapshot_store()


def test_capture_retains_full_dataset_closure_and_historical_failure(declared):
    before = hashlib.sha256(storage.db_path().read_bytes()).hexdigest()
    doc = decode_snapshot(
        batch_capture.capture_snapshot(
            "owner", declared["batch_id"], declared["batch"]["revision"], "self_contained"
        )
    )
    assert doc["batch"]["counts"]["completed"] == 2
    assert len(doc["datasets"][0]["payload"]["traces"]) == 4
    assert doc["datasets"][0]["point_count"] == 20
    assert doc["datasets"][0]["source"]["content"].endswith("\r\n")
    assert [event["kind"] for event in doc["control_events"]] == [
        "cancel_requested",
        "retry_selected",
    ]
    failed = next(run for run in doc["runs"] if run["run_id"] == declared["failed_run_id"])
    assert failed["status"] == "failed"
    assert (
        failed["items"][0]["state"] == "failed" and failed["items"][0]["item_state"] == "completed"
    )
    assert failed["items"][0]["runtime"] == {"runtime_status": "unknown"}
    assert "execution_token" not in json.dumps(doc)
    assert hashlib.sha256(storage.db_path().read_bytes()).hexdigest() == before


def test_references_export_omits_assets_without_inventing_new_digests(declared):
    full = batch_capture.capture_document(
        "owner", declared["batch_id"], declared["batch"]["revision"], "self_contained"
    )
    refs = decode_snapshot(
        batch_capture.capture_snapshot(
            "owner", declared["batch_id"], declared["batch"]["revision"], "references"
        )
    )
    assert refs["datasets"][0]["payload"] is None
    assert refs["datasets"][0]["source"]["content"] is None
    assert refs["datasets"][0]["input_sha256"] == full["datasets"][0]["input_sha256"]
    assert (
        refs["datasets"][0]["source"]["source_sha256"]
        == full["datasets"][0]["source"]["source_sha256"]
    )
    assert all(a["result"] is None for a in refs["analyses"])
    assert [a["result_sha256"] for a in refs["analyses"]] == [
        a["result_sha256"] for a in full["analyses"]
    ]


@pytest.mark.parametrize("owner,revision", [("other", None), ("owner", 999), ("owner", True)])
def test_invalid_owner_or_revision_never_changes_state(declared, owner, revision):
    before = hashlib.sha256(storage.db_path().read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        batch_capture.capture_snapshot(
            owner, declared["batch_id"], revision or declared["batch"]["revision"], "self_contained"
        )
    assert hashlib.sha256(storage.db_path().read_bytes()).hexdigest() == before


def test_expired_running_claim_requires_explicit_recovery_before_capture(declared):
    identifier = declared["batch_id"]
    run, _ = batches.reserve_run(
        "owner", identifier, "fixture-pending-run", declared["batch"]["revision"], 1
    )
    with storage.connect() as con:
        con.execute(
            "UPDATE analysis_batch_attempts SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE run_id=?",
            (run["run_id"],),
        )
    current = batches.get_batch("owner", identifier)
    with pytest.raises(ValueError, match="recover running"):
        batch_capture.capture_snapshot("owner", identifier, current["revision"], "self_contained")
    recovered = batches.recover_batch("owner", identifier, "explicit-recover", current["revision"])
    doc = decode_snapshot(
        batch_capture.capture_snapshot("owner", identifier, recovered["revision"], "self_contained")
    )
    assert doc["items"][2]["state"] == "interrupted"


def test_wal_capture_stays_at_one_revision_while_cancellation_commits(declared, monkeypatch):
    with sqlite3.connect(storage.db_path()) as con:
        con.execute("PRAGMA journal_mode=WAL")
    original = batches._live_dataset_and_source
    committed = []

    def interleave(con, owner, item):
        if not committed:
            committed.append(
                batches.cancel_batch(
                    "owner",
                    declared["batch_id"],
                    "concurrent-cancel",
                    declared["batch"]["revision"],
                )
            )
        return original(con, owner, item)

    monkeypatch.setattr(batches, "_live_dataset_and_source", interleave)
    doc = decode_snapshot(
        batch_capture.capture_snapshot(
            "owner", declared["batch_id"], declared["batch"]["revision"], "self_contained"
        )
    )
    assert doc["batch"]["revision"] == declared["batch"]["revision"]
    assert doc["batch"]["counts"]["pending"] == 1 and len(doc["control_events"]) == 2
    assert committed[0]["revision"] == doc["batch"]["revision"] + 1
    assert committed[0]["counts"]["cancelled"] == 1


def test_oversized_attempt_compact_result_rejects_before_history_fetch(declared, monkeypatch):
    with storage.connect() as con:
        con.execute(
            "UPDATE analysis_batch_attempts SET compact_result=?",
            ("x" * (batches.MAX_COMPACT_RESULT_BYTES + 1),),
        )
    original = storage.connect
    statements = []

    def observed():
        con = original()
        con.set_trace_callback(statements.append)
        return con

    monkeypatch.setattr(storage, "connect", observed)
    with pytest.raises(ValueError, match="portable record limit"):
        batch_capture.capture_snapshot(
            "owner", declared["batch_id"], declared["batch"]["revision"], "self_contained"
        )
    assert not any(sql.startswith("SELECT id,batch_id,run_id") for sql in statements)


def test_unicode_request_key_at_existing_character_limit_is_exportable(declared):
    run, _ = batches.reserve_run(
        "owner", declared["batch_id"], "🧪" * 100, declared["batch"]["revision"], 1
    )
    batches.fail_item(
        "owner",
        declared["batch_id"],
        run["run_id"],
        2,
        run["items"][0]["execution_token"],
        "fixture",
    )
    current = batches.get_batch("owner", declared["batch_id"])
    doc = decode_snapshot(
        batch_capture.capture_snapshot(
            "owner", declared["batch_id"], current["revision"], "self_contained"
        )
    )
    assert len(doc["runs"]) == 4
