"""Accept real writer histories through recovery, repeated retries and no-op controls."""

import pytest
from snapshot_store_support import make_snapshot_store

from measure_service import analysis_runtime, batch_capture, batches, server, storage
from measure_service.batch_models import BatchSpec
from measure_service.batch_snapshot import decode_snapshot, encode_snapshot


@pytest.mark.parametrize("recover_first", [False, True])
def test_repeated_failure_retry_and_optional_recovery_remain_portable(
    tmp_path, monkeypatch, recover_first
):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    state = make_snapshot_store()
    identifier = state["batch_id"]

    def reserve(key):
        current = batches.get_batch("owner", identifier)
        return batches.reserve_run("owner", identifier, key, current["revision"], 1)[0]

    def retry(key):
        current = batches.get_batch("owner", identifier)
        return batches.retry_items("owner", identifier, key, current["revision"], [2])

    if recover_first:
        abandoned = reserve("abandoned")
        with storage.connect() as con:
            con.execute(
                "UPDATE analysis_batch_attempts SET lease_expires_at=? WHERE run_id=?",
                ("2000-01-01T00:00:00+00:00", abandoned["run_id"]),
            )
        current = batches.get_batch("owner", identifier)
        batches.recover_batch("owner", identifier, "recover", current["revision"])
        retry("retry-interrupted")

    for index in range(2):
        run = reserve(f"failure-{index}")
        batches.fail_item(
            "owner",
            identifier,
            run["run_id"],
            2,
            run["items"][0]["execution_token"],
            {"kind": "fixture_failure", "message": "No numerical execution"},
        )
        current = batches.get_batch("owner", identifier)
        # Recovery with no running claims is a retained no-op control.
        batches.recover_batch("owner", identifier, f"no-op-{index}", current["revision"])
        retry(f"retry-failed-{index}")

    current = batches.get_batch("owner", identifier)
    for mode in ("references", "self_contained"):
        document = decode_snapshot(
            batch_capture.capture_snapshot("owner", identifier, current["revision"], mode)
        )
        assert document["items"][2]["state"] == "pending"
        assert document["items"][2]["attempt_count"] == 2 + int(recover_first)
        assert [a["state"] for a in document["attempts"] if a["ordinal"] == 2] == (
            (["interrupted"] if recover_first else []) + ["failed", "failed"]
        )


@pytest.mark.parametrize("retry_after_cancel", [False, True])
def test_cancel_and_retry_between_attempts_preserve_the_previous_failure(
    tmp_path, monkeypatch, retry_after_cancel
):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    state = make_snapshot_store()
    identifier = state["batch_id"]
    run, _ = batches.reserve_run("owner", identifier, "fail", state["batch"]["revision"], 1)
    batches.fail_item(
        "owner",
        identifier,
        run["run_id"],
        2,
        run["items"][0]["execution_token"],
        {"kind": "fixture_failure", "message": "No numerical execution"},
    )
    current = batches.get_batch("owner", identifier)
    current = batches.retry_items(
        "owner", identifier, "retry-before-cancel", current["revision"], [2]
    )
    current = batches.cancel_batch("owner", identifier, "cancel-after-retry", current["revision"])
    if retry_after_cancel:
        current = batches.retry_items(
            "owner", identifier, "retry-cancelled", current["revision"], [2]
        )
    for mode in ("references", "self_contained"):
        document = decode_snapshot(
            batch_capture.capture_snapshot("owner", identifier, current["revision"], mode)
        )
        assert document["items"][2]["state"] == ("pending" if retry_after_cancel else "cancelled")
        history = [a for a in document["attempts"] if a["ordinal"] == 2]
        assert len(history) == 1 and history[0]["state"] == "failed"


def test_cancellation_flag_requires_a_retained_cancellation_even_without_pending_items(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    state = make_snapshot_store()
    spec = BatchSpec(
        title="One failed selection",
        method={},
        inputs=[{"dataset_id": state["dataset_id"], "trace_id": "qc", "role": "qc"}],
    )
    batch = batches.create_batch("owner", spec, "one-failed")
    identifier = batch["batch_id"]
    run, _ = batches.reserve_run("owner", identifier, "fail-once", batch["revision"], 1)
    batches.fail_item(
        "owner",
        identifier,
        run["run_id"],
        0,
        run["items"][0]["execution_token"],
        {"kind": "fixture_failure", "message": "No numerical execution"},
    )
    current = batches.get_batch("owner", identifier)
    for mode in ("references", "self_contained"):
        document = batch_capture.capture_document("owner", identifier, current["revision"], mode)
        encode_snapshot(document)
        assert document["control_events"] == []
        assert document["batch"]["counts"]["pending"] == 0
        document["batch"].update(cancel_requested=True, status="cancelled")
        with pytest.raises(ValueError):
            encode_snapshot(document)


def test_cancellation_between_two_live_claims_and_their_finishes_remains_portable(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    state = make_snapshot_store()
    spec = BatchSpec(
        title="Concurrent state fixture",
        method={},
        inputs=[
            {"dataset_id": state["dataset_id"], "trace_id": name, "role": name}
            for name in ("reference", "blank", "qc")
        ],
    )
    batch = batches.create_batch("owner", spec, "concurrent-create")
    identifier = batch["batch_id"]
    run, _ = batches.reserve_run("owner", identifier, "two-claims", batch["revision"], 2)
    current = batches.get_batch("owner", identifier)
    cancelled = batches.cancel_batch("owner", identifier, "pending-only", current["revision"])
    assert cancelled["counts"]["running"] == 2 and cancelled["counts"]["cancelled"] == 1
    for selected in reversed(run["items"]):
        batches.fail_item(
            "owner",
            identifier,
            run["run_id"],
            selected["ordinal"],
            selected["execution_token"],
            {"kind": "fixture_failure", "message": "No numerical execution"},
        )
    current = batches.get_batch("owner", identifier)
    current = batches.retry_items("owner", identifier, "retry-all", current["revision"], [0, 1, 2])
    for mode in ("references", "self_contained"):
        document = decode_snapshot(
            batch_capture.capture_snapshot("owner", identifier, current["revision"], mode)
        )
        assert document["batch"]["counts"]["pending"] == 3
        assert len(document["attempts"]) == 2
        assert all(attempt["state"] == "failed" for attempt in document["attempts"])


def test_cancelled_status_survives_successful_running_work_and_repeat_cancellation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    state = make_snapshot_store()
    previous = server.analysis_for("owner", state["analysis_ids"][0])
    spec = BatchSpec(
        title="Running success after cancellation",
        method={},
        inputs=[{"dataset_id": state["dataset_id"], "trace_id": "reference", "role": "reference"}],
    )
    batch = batches.create_batch("owner", spec, "cancel-success-create")
    identifier = batch["batch_id"]
    run, _ = batches.reserve_run("owner", identifier, "live-success", batch["revision"], 1)
    current = batches.get_batch("owner", identifier)
    batches.cancel_batch("owner", identifier, "cancel-live", current["revision"])
    record = analysis_runtime.make_analysis_record(
        state["dataset_id"],
        "reference",
        "chromatogram",
        spec.method.model_dump(mode="json"),
        previous["result"],
        previous["provenance"]["input_sha256"],
        previous["provenance"]["source_sha256"],
    )
    batches.complete_item(
        "owner", identifier, run["run_id"], 0, run["items"][0]["execution_token"], record
    )
    current = batches.get_batch("owner", identifier)
    assert current["status"] == "cancelled" and current["counts"]["completed"] == 1
    current = batches.cancel_batch("owner", identifier, "cancel-again", current["revision"])
    for mode in ("references", "self_contained"):
        document = decode_snapshot(
            batch_capture.capture_snapshot("owner", identifier, current["revision"], mode)
        )
        assert document["batch"]["status"] == "cancelled"
        assert document["items"][0]["state"] == "completed"
        assert len(document["control_events"]) == 2
