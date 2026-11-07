"""Control outcomes survive retry and commit atomically with state and receipts."""

import json
import sqlite3

import pytest

from measure_service import analysis_runtime, batches, server
from measure_service.batch_models import BatchSpec
from measure_service.storage import canonical, connect


@pytest.fixture
def declared(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    trace = server.Trace(
        trace_id="uv",
        sample_id="sample",
        injection_id="inj",
        channel="uv",
        separation="LC",
        detector="UV",
        axis_unit="min",
        signal_unit="mAU",
        axis=[0, 1, 2],
        signal=[0, 1, 0],
    )
    dataset = server.register_dataset("owner", server.Dataset(title="constructed", traces=[trace]))
    spec = BatchSpec(
        title="control history",
        method={},
        inputs=[
            {
                "dataset_id": dataset["dataset_id"],
                "trace_id": "uv",
                "role": "unknown",
            }
        ],
    )
    return batches.create_batch("owner", spec, "create"), dataset, trace


def test_cancelled_item_history_survives_successful_retry_and_old_key_replay(declared):
    batch, dataset, trace = declared
    identifier = batch["batch_id"]
    cancelled = batches.cancel_batch("owner", identifier, "cancel", batch["revision"])
    pending = batches.retry_items("owner", identifier, "retry", cancelled["revision"], [0])
    run, _ = batches.reserve_run("owner", identifier, "run", pending["revision"])
    record = analysis_runtime.make_analysis_record(
        dataset["dataset_id"],
        "uv",
        "chromatogram",
        pending["method"],
        {
            "kind": "chromatogram",
            "peak_count": 0,
            "peaks": [],
            "warnings": [],
            "notes": [],
            "processed_trace": {"axis": trace.axis, "signal": trace.signal},
        },
        dataset["input_sha256"],
        None,
    )
    batches.complete_item(
        "owner", identifier, run["run_id"], 0, run["items"][0]["execution_token"], record
    )
    before = batches.read_control_events("owner", identifier)
    assert [event["kind"] for event in before["events"]] == ["cancel_requested", "retry_selected"]
    assert before["events"][0]["items"] == [
        {
            "ordinal": 0,
            "from_state": "pending",
            "to_state": "cancelled",
        }
    ]
    assert before["events"][1]["items"] == [
        {
            "ordinal": 0,
            "from_state": "cancelled",
            "to_state": "pending",
        }
    ]
    replay = batches.cancel_batch("owner", identifier, "cancel", batch["revision"])
    assert replay["counts"]["completed"] == 1
    assert batches.read_control_events("owner", identifier) == before
    assert replay["control_event_count"] == 2
    with pytest.raises(ValueError, match="not found or is not accessible"):
        batches.read_control_events("other", identifier)


def test_control_event_failure_rolls_back_state_revision_and_retry_receipt(declared, monkeypatch):
    batch, _, _ = declared
    original = batches._record_control_event

    def reject_insert(con, *args, **kwargs):
        # Inject only after supported-schema validation, inside the mutation.
        # Preexisting custom triggers now correctly reject before any writes.
        con.execute("""CREATE TRIGGER fail_control_event BEFORE INSERT ON analysis_batch_control_events
          BEGIN SELECT RAISE(ABORT, 'controlled event insertion failure'); END""")
        return original(con, *args, **kwargs)

    monkeypatch.setattr(batches, "_record_control_event", reject_insert)
    with pytest.raises(sqlite3.IntegrityError, match="controlled event insertion"):
        batches.cancel_batch("owner", batch["batch_id"], "cancel", batch["revision"])
    assert batches.get_batch("owner", batch["batch_id"]) == batch
    assert batches.read_control_events("owner", batch["batch_id"])["total"] == 0
    with connect() as con:
        assert (
            con.execute("SELECT 1 FROM sqlite_master WHERE name='fail_control_event'").fetchone()
            is None
        )
        assert (
            con.execute("SELECT 1 FROM registry_receipts WHERE request_key='cancel'").fetchone()
            is None
        )


def test_control_history_pages_are_stable_and_content_tampering_is_rejected(declared):
    batch, _, _ = declared
    for index in range(12):
        batch = batches.cancel_batch(
            "owner", batch["batch_id"], f"cancel-{index}", batch["revision"]
        )
        batch = batches.retry_items(
            "owner", batch["batch_id"], f"retry-{index}", batch["revision"], [0]
        )
    first = batches.read_control_events("owner", batch["batch_id"])
    assert first["count"] == first["next_offset"] == 20
    second = batches.read_control_events("owner", batch["batch_id"], first["next_offset"])
    assert second["count"] == 4 and second["next_offset"] is None
    assert [e["event_no"] for e in first["events"] + second["events"]] == list(range(1, 25))
    assert len(canonical(first).encode()) < 256 * 1024
    with connect() as con:
        row = con.execute(
            "SELECT id, payload FROM analysis_batch_control_events ORDER BY event_no LIMIT 1"
        ).fetchone()
        damaged = json.loads(row[1]) | {"kind": "changed"}
        con.execute(
            "UPDATE analysis_batch_control_events SET payload=? WHERE id=?",
            (canonical(damaged), row[0]),
        )
    with pytest.raises(ValueError, match="does not match its identity"):
        batches.read_control_events("owner", batch["batch_id"])
