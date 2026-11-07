"""Reject contradictory histories even when individual record digests are refreshed."""

import copy

import pytest
from snapshot_store_support import make_snapshot_store

from measure_service import batch_capture, storage
from measure_service.batch_snapshot import encode_snapshot


@pytest.fixture
def documents(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    state = make_snapshot_store()
    result = {
        mode: batch_capture.capture_document(
            "owner", state["batch_id"], state["batch"]["revision"], mode
        )
        for mode in ("references", "self_contained")
    }
    for document in result.values():
        encode_snapshot(document)
    return result


def _refresh_control(event):
    event["event_sha256"] = storage.sha256(
        {key: value for key, value in event.items() if key != "event_sha256"}
    )


def test_rehashed_mapping_must_replay_against_complete_retained_source(documents):
    document = copy.deepcopy(documents["self_contained"])
    document["datasets"][0]["source"]["mapping"]["axis_column"] = "absent"
    for item in document["batch"]["manifest"]["inputs"]:
        item["source"]["mapping"]["axis_column"] = "absent"
    document["batch"]["manifest_sha256"] = storage.sha256(document["batch"]["manifest"])
    with pytest.raises(ValueError):
        encode_snapshot(document)


@pytest.mark.parametrize("mode", ["references", "self_contained"])
@pytest.mark.parametrize(
    "corruption",
    [
        "all-controls",
        "cancellation",
        "empty-retry",
        "early-retry",
        "late-retry",
        "final-revision",
        "cancel-after-retry",
        "request-digest",
        "missing-finish",
        "missing-error",
        "completed-error",
        "failed-runtime",
        "compact-analysis",
        "compact-runtime",
    ],
)
def test_rehashed_history_must_reconcile_to_current_state(documents, mode, corruption):
    document = copy.deepcopy(documents[mode])
    controls = document["control_events"]
    if corruption == "all-controls":
        controls.clear()
        document["batch"]["control_event_count"] = 0
    elif corruption == "cancellation":
        controls.pop(0)
        controls[0]["event_no"] = 1
        _refresh_control(controls[0])
        document["batch"]["control_event_count"] = 1
    elif corruption in {"empty-retry", "early-retry", "late-retry"}:
        event = controls[1]
        if corruption == "empty-retry":
            event["items"] = []
        else:
            before = 1 if corruption == "early-retry" else 8
            event.update(revision_before=before, revision_after=before + 1)
        _refresh_control(event)
    elif corruption == "final-revision":
        document["batch"]["revision"] += 1
    elif corruption == "cancel-after-retry":
        document["batch"].update(cancel_requested=True, status="cancelled")
    elif corruption == "request-digest":
        document["runs"][0]["request_sha256"] = "0" * 64
    elif corruption.startswith("compact-"):
        result = document["items"][0]["result"]
        if corruption == "compact-analysis":
            result["analysis_id"] = "different-analysis"
        else:
            result["provenance"]["worker_sha256"] = "0" * 64
        for run in document["runs"]:
            for selection in run["items"]:
                if selection["ordinal"] == 0:
                    selection["result"] = copy.deepcopy(result)
    else:
        state = "completed" if corruption == "completed-error" else "failed"
        attempt = next(a for a in document["attempts"] if a["state"] == state)
        if corruption == "missing-finish":
            attempt["finished_at"] = None
        elif corruption == "missing-error":
            attempt["error"] = None
        elif corruption == "completed-error":
            attempt["error"] = {"kind": "contradiction", "message": "Not a successful terminal"}
        else:
            attempt["runtime"] = {"r_version": "fabricated"}
        for run in document["runs"]:
            for selection in run["items"]:
                if selection["attempt_id"] == attempt["attempt_id"]:
                    for key in ("finished_at", "error", "runtime"):
                        selection[key] = copy.deepcopy(attempt[key])
    with pytest.raises(ValueError):
        encode_snapshot(document)


def test_reference_closure_cannot_be_smaller_than_selected_traces(documents):
    document = copy.deepcopy(documents["references"])
    document["datasets"][0].update(trace_count=1, point_count=3)
    with pytest.raises(ValueError):
        encode_snapshot(document)
