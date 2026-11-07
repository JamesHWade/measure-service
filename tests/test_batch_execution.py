"""Focused contracts for the bounded durable batch executor."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from measure_service import analysis_runtime as runtime
from measure_service import batch_execution as execution

_REAL_ACQUIRE_SLOTS = runtime.acquire_worker_slots


def _item(ordinal: int) -> dict:
    return {
        "ordinal": ordinal,
        "attempt_id": f"attempt-{ordinal}",
        "execution_token": f"token-{ordinal}",
    }


def _claim(ordinal: int) -> dict:
    return {
        "trace": {"trace_id": f"trace-{ordinal}"},
        "method": {"baseline": "none"},
        "dataset_id": f"dataset-{ordinal}",
        "trace_id": f"trace-{ordinal}",
        "input_sha256": f"input-{ordinal}",
        "source_sha256": None,
        "execution_context": {"r_runtime_evidence": "unknown_until_success"},
    }


class FakeSlot:
    def __init__(self, name: str):
        self.name = name
        self.calls: list[dict] = []
        self.release_count = 0

    def run(self, payload: dict) -> dict:
        self.calls.append(payload)
        return {"warnings": [], "peak_count": 0}

    def release(self) -> None:
        self.release_count += 1


def _install_fakes(
    monkeypatch, *, item_count: int = 1, slot_count: int | None = None, created: bool = True
):
    slots = [FakeSlot(f"slot-{i}") for i in range(item_count if slot_count is None else slot_count)]
    items = [_item(i) for i in range(item_count)]
    calls: dict[str, list] = {
        "lookup": [],
        "reserve": [],
        "claims": [],
        "completed": [],
        "failed": [],
        "processed": [],
    }
    public = {
        "run_id": "run-1",
        "batch_id": "batch-1",
        "status": "completed",
        "items": [],
    }

    def lookup(*args, **kwargs):
        calls["lookup"].append((args, kwargs))
        return None

    def reserve(*args, **kwargs):
        calls["reserve"].append((args, kwargs))
        return {"run_id": "run-1", "batch_id": "batch-1", "items": items}, created

    def claim_input(owner, batch_id, run_id, ordinal, token):
        calls["claims"].append((owner, batch_id, run_id, ordinal, token))
        return _claim(ordinal)

    def complete_item(owner, batch_id, run_id, ordinal, token, record):
        calls["completed"].append((ordinal, token, record))
        return {"ordinal": ordinal, "state": "completed"}

    def fail_item(owner, batch_id, run_id, ordinal, token, error):
        calls["failed"].append((ordinal, token, error))
        return {"ordinal": ordinal, "state": "failed"}

    def get_run(owner, run_id):
        calls.setdefault("get_run", []).append((owner, run_id))
        return public

    monkeypatch.setattr(execution.batches, "lookup_run_request", lookup)
    monkeypatch.setattr(execution.batches, "reserve_run", reserve)
    monkeypatch.setattr(execution.batches, "claim_input", claim_input, raising=False)
    monkeypatch.setattr(execution.batches, "complete_item", complete_item)
    monkeypatch.setattr(execution.batches, "fail_item", fail_item)
    monkeypatch.setattr(execution.batches, "get_run", get_run)
    monkeypatch.setattr(execution.analysis_runtime, "acquire_worker_slots", lambda maximum: slots)
    monkeypatch.setattr(
        execution.analysis_runtime,
        "execution_context",
        lambda: {"runtime_status": "unknown"},
    )

    def process(trace, method, *, worker):
        calls["processed"].append((trace, method, worker))
        worker({"operation": "chromatogram"})
        return {"warnings": [], "peak_count": 0}

    monkeypatch.setattr(execution.analysis_runtime, "process_chromatogram", process)
    monkeypatch.setattr(
        execution.analysis_runtime,
        "make_analysis_record",
        lambda dataset_id, trace_id, kind, method, result, input_sha256, source_sha256: {
            "analysis_id": f"analysis-{trace_id}",
            "dataset_id": dataset_id,
            "trace_id": trace_id,
            "kind": kind,
            "method": method,
            "result": result,
            "provenance": {
                "input_sha256": input_sha256,
                "source_sha256": source_sha256,
            },
            "created_at": "now",
        },
    )
    return slots, items, calls, public


def test_replay_returns_before_acquiring_capacity_or_executing(monkeypatch):
    retained = {"run_id": "run-1", "status": "running", "items": []}
    calls = []

    monkeypatch.setattr(
        execution.batches,
        "lookup_run_request",
        lambda *args, **kwargs: calls.append((args, kwargs)) or retained,
    )
    monkeypatch.setattr(
        execution.analysis_runtime,
        "acquire_worker_slots",
        lambda maximum: pytest.fail("replay must not acquire process capacity"),
    )
    monkeypatch.setattr(
        execution.analysis_runtime,
        "process_chromatogram",
        lambda *args, **kwargs: pytest.fail("replay must not execute numerical work"),
    )

    assert execution.run_batch("owner", "batch-1", "same-key", 4) is retained
    assert calls[0][1]["max_items"] == 2


def test_reservation_race_loser_releases_all_permits_and_does_not_execute(monkeypatch):
    slots, _items, calls, public = _install_fakes(monkeypatch, item_count=2, created=False)
    monkeypatch.setattr(
        execution.analysis_runtime,
        "process_chromatogram",
        lambda *args, **kwargs: pytest.fail("race loser must not execute"),
    )

    assert execution.run_batch("owner", "batch-1", "race", 1) is public
    assert calls["reserve"][0][1]["capacity"] == 2
    assert calls["get_run"] == [("owner", "run-1")]
    assert [slot.release_count for slot in slots] == [1, 1]
    assert calls["claims"] == []


def test_partial_capacity_is_passed_to_reservation_and_replay_keeps_same_request(monkeypatch):
    slots, _items, calls, public = _install_fakes(monkeypatch, item_count=1, slot_count=2)
    # The caller asks for two items, but only one global permit is available.
    monkeypatch.setattr(
        execution.analysis_runtime, "acquire_worker_slots", lambda maximum: slots[:1]
    )

    first = execution.run_batch("owner", "batch-1", "partial", 7, max_items=2)
    assert first is public
    assert calls["reserve"][0][1]["max_items"] == 2
    assert calls["reserve"][0][1]["capacity"] == 1
    assert len(calls["completed"]) == 1
    assert [slot.release_count for slot in slots] == [1, 0]

    retained = {"run_id": "run-1", "status": "completed", "items": []}
    monkeypatch.setattr(execution.batches, "lookup_run_request", lambda *args, **kwargs: retained)
    monkeypatch.setattr(
        execution.analysis_runtime,
        "acquire_worker_slots",
        lambda maximum: pytest.fail("same request replay must not acquire capacity"),
    )
    assert execution.run_batch("owner", "batch-1", "partial", 7, max_items=2) is retained


def test_one_selected_item_releases_the_unused_reserved_permit_before_dispatch(monkeypatch):
    slots, _items, calls, public = _install_fakes(monkeypatch, item_count=1, slot_count=2)

    assert execution.run_batch("owner", "batch-1", "one-of-two", 1, max_items=2) is public
    assert calls["reserve"][0][1]["capacity"] == 2
    assert [slot.release_count for slot in slots] == [1, 1]


def test_two_selected_items_use_distinct_immediately_dispatched_permits(monkeypatch):
    slots, _items, calls, _public = _install_fakes(monkeypatch, item_count=2)
    entered = threading.Barrier(3)
    release = threading.Event()

    def process(trace, method, *, worker):
        calls["processed"].append((trace, method, worker))
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        worker({"operation": "chromatogram"})
        return {"warnings": [], "peak_count": 0}

    monkeypatch.setattr(execution.analysis_runtime, "process_chromatogram", process)
    # Run in a background thread so both tasks must enter before either can
    # finish; a queued third task would deadlock the barrier.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execution.run_batch, "owner", "batch-1", "parallel", 1)
        entered.wait(timeout=5)
        assert len(calls["processed"]) == 2
        assert len({id(worker) for _trace, _method, worker in calls["processed"]}) == 2
        release.set()
        assert future.result(timeout=5) is not None
    assert [slot.release_count for slot in slots] == [1, 1]


def test_batch_uses_only_remaining_capacity_while_preview_holds_one_slot(monkeypatch):
    preview_slot = _REAL_ACQUIRE_SLOTS(1)
    assert len(preview_slot) == 1
    try:
        _fake_slots, _items, calls, public = _install_fakes(monkeypatch, item_count=1)
        monkeypatch.setattr(execution.analysis_runtime, "acquire_worker_slots", _REAL_ACQUIRE_SLOTS)
        monkeypatch.setattr(
            runtime,
            "_execute_worker",
            lambda payload, **kwargs: {"warnings": [], "peak_count": 0},
        )
        assert execution.run_batch("owner", "batch-1", "preview-mixed", 1, max_items=2) is public
        assert calls["reserve"][0][1]["max_items"] == 2
        assert calls["reserve"][0][1]["capacity"] == 1
    finally:
        preview_slot[0].release()


def test_worker_failure_is_bounded_and_sibling_success_is_retained(monkeypatch):
    slots, _items, calls, public = _install_fakes(monkeypatch, item_count=2)

    def process(trace, method, *, worker):
        ordinal = int(trace["trace_id"].rsplit("-", 1)[1])
        calls["processed"].append((trace, method, worker))
        if ordinal == 1:
            raise RuntimeError("x" * 20_000)
        worker({"operation": "chromatogram"})
        return {"warnings": [], "peak_count": 0}

    monkeypatch.setattr(execution.analysis_runtime, "process_chromatogram", process)
    assert execution.run_batch("owner", "batch-1", "mixed", 1) is public
    assert [ordinal for ordinal, _token, _record in calls["completed"]] == [0]
    assert [ordinal for ordinal, _token, _error in calls["failed"]] == [1]
    error = calls["failed"][0][2]
    assert error["code"] == "chromatogram_worker_error"
    assert error["r_runtime_evidence"] == "unknown_until_success"
    assert len(json.dumps(error, ensure_ascii=False).encode("utf-8")) <= 1_000
    assert [slot.release_count for slot in slots] == [1, 1]


def test_source_corruption_is_recorded_without_running_the_worker(monkeypatch):
    slots, _items, calls, public = _install_fakes(monkeypatch, item_count=1)
    monkeypatch.setattr(
        execution.batches,
        "claim_input",
        lambda *args: (_ for _ in ()).throw(ValueError("Frozen source digest no longer matches")),
    )
    monkeypatch.setattr(
        execution.analysis_runtime,
        "process_chromatogram",
        lambda *args, **kwargs: pytest.fail("corrupt source must be rejected before execution"),
    )

    assert execution.run_batch("owner", "batch-1", "corrupt", 1) is public
    assert calls["failed"][0][2]["message"] == "Frozen source digest no longer matches"
    assert calls["completed"] == []
    assert [slot.release_count for slot in slots] == [1]


def test_stale_completion_is_not_retried_or_converted_to_failure(monkeypatch):
    slots, _items, calls, public = _install_fakes(monkeypatch, item_count=1)

    class ClaimFenceError(ValueError):
        pass

    monkeypatch.setattr(execution.batches, "ClaimFenceError", ClaimFenceError, raising=False)
    monkeypatch.setattr(
        execution.batches,
        "complete_item",
        lambda *args: (_ for _ in ()).throw(ClaimFenceError("Batch claim became stale or expired")),
    )

    assert execution.run_batch("owner", "batch-1", "stale", 1) is public
    assert calls["failed"] == []
    assert [slot.release_count for slot in slots] == [1]


def test_stale_word_in_ordinary_worker_error_is_still_recorded_as_failure(monkeypatch):
    slots, _items, calls, public = _install_fakes(monkeypatch, item_count=1)
    monkeypatch.setattr(
        execution.analysis_runtime,
        "process_chromatogram",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("stale baseline warning")),
    )

    assert execution.run_batch("owner", "batch-1", "ordinary-stale", 1) is public
    assert calls["failed"][0][2]["message"] == "stale baseline warning"
    assert [slot.release_count for slot in slots] == [1]


def test_nonfence_completion_rejection_is_retained_as_failure(monkeypatch):
    slots, _items, calls, public = _install_fakes(monkeypatch, item_count=1)
    monkeypatch.setattr(
        execution.batches,
        "complete_item",
        lambda *args: (_ for _ in ()).throw(ValueError("invalid result evidence")),
    )

    assert execution.run_batch("owner", "batch-1", "bad-completion", 1) is public
    assert calls["failed"][0][2]["message"] == "invalid result evidence"
    assert [slot.release_count for slot in slots] == [1]


def test_worker_baseexception_leaves_claim_for_recovery_and_releases_permit(monkeypatch):
    slots, _items, calls, _public = _install_fakes(monkeypatch, item_count=1)
    monkeypatch.setattr(
        execution.analysis_runtime,
        "process_chromatogram",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        execution.run_batch("owner", "batch-1", "interrupt", 1)
    assert calls["failed"] == []
    assert calls.get("completed", []) == []
    assert [slot.release_count for slot in slots] == [1]


def test_dispatch_exception_releases_every_permit_without_hidden_retry(monkeypatch):
    slots, _items, calls, _public = _install_fakes(monkeypatch, item_count=2)

    class BrokenExecutor:
        def __init__(self, max_workers):
            assert max_workers == 2
            self.shutdown_called = False

        def submit(self, *args, **kwargs):
            raise RuntimeError("dispatch failed")

        def shutdown(self, **kwargs):
            self.shutdown_called = True

    monkeypatch.setattr(execution, "ThreadPoolExecutor", BrokenExecutor)
    with pytest.raises(RuntimeError, match="dispatch failed"):
        execution.run_batch("owner", "batch-1", "dispatch", 1)
    assert calls["claims"] == []
    assert calls["failed"] == []
    assert [slot.release_count for slot in slots] == [1, 1]


def test_worker_result_is_passed_directly_to_record_builder(monkeypatch):
    slots, _items, calls, public = _install_fakes(monkeypatch, item_count=1)
    worker_result = runtime._WorkerResult({"warnings": [], "peak_count": 0}, "executed-sha")
    observed = []

    monkeypatch.setattr(
        execution.analysis_runtime,
        "process_chromatogram",
        lambda trace, method, *, worker: worker_result,
    )

    def make_record(dataset_id, trace_id, kind, method, result, input_sha256, source_sha256):
        observed.append(result)
        return {
            "analysis_id": "analysis-1",
            "dataset_id": dataset_id,
            "trace_id": trace_id,
            "kind": kind,
            "method": method,
            "result": result,
            "provenance": {"input_sha256": input_sha256, "source_sha256": source_sha256},
            "created_at": "now",
        }

    monkeypatch.setattr(execution.analysis_runtime, "make_analysis_record", make_record)
    assert execution.run_batch("owner", "batch-1", "provenance", 1) is public
    assert observed == [worker_result]
    assert observed[0].worker_sha256 == "executed-sha"
    assert calls["completed"][0][2]["result"] is worker_result
    assert [slot.release_count for slot in slots] == [1]
