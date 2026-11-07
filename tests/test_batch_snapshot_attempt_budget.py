"""The portable retry ledger obeys the same attempt budget as the writer."""

import copy
import uuid

import pytest
from snapshot_store_support import make_snapshot_store

from measure_service import batch_capture, batches, storage
from measure_service.batch_snapshot import encode_snapshot


def test_rehashed_retry_after_eight_failures_is_not_a_valid_history(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    state = make_snapshot_store()
    identifier = state["batch_id"]
    for index in range(batches.MAX_ATTEMPTS):
        current = batches.get_batch("owner", identifier)
        run, _ = batches.reserve_run("owner", identifier, f"run-{index}", current["revision"], 1)
        batches.fail_item(
            "owner",
            identifier,
            run["run_id"],
            2,
            run["items"][0]["execution_token"],
            {"kind": "fixture_failure", "message": "No numerical execution"},
        )
        current = batches.get_batch("owner", identifier)
        if index < batches.MAX_ATTEMPTS - 1:
            batches.retry_items("owner", identifier, f"retry-{index}", current["revision"], [2])
    with pytest.raises(ValueError, match="maximum attempt count"):
        batches.retry_items("owner", identifier, "over-budget", current["revision"], [2])
    for mode in ("references", "self_contained"):
        document = batch_capture.capture_document("owner", identifier, current["revision"], mode)
        encode_snapshot(document)
        event = copy.deepcopy(document["control_events"][-1])
        timestamp = storage.now()
        document["captured_at"] = timestamp
        event.update(
            event_id=str(uuid.uuid4()),
            event_no=len(document["control_events"]) + 1,
            revision_before=current["revision"],
            revision_after=current["revision"] + 1,
            created_at=timestamp,
            idempotency_key_sha256="0" * 64,
        )
        event["event_sha256"] = storage.sha256(
            {key: value for key, value in event.items() if key != "event_sha256"}
        )
        document["control_events"].append(event)
        document["batch"].update(
            revision=current["revision"] + 1,
            control_event_count=len(document["control_events"]),
            updated_at=timestamp,
        )
        document["batch"]["counts"].update(failed=0, pending=1)
        document["items"][2]["state"] = "pending"
        for run in document["runs"]:
            for selection in run["items"]:
                if selection["ordinal"] == 2:
                    selection["item_state"] = "pending"
        with pytest.raises(ValueError, match="maximum retained attempt count"):
            encode_snapshot(document)
