"""Bounded execution for durable chromatogram batch claims.

The state worker in :mod:`measure_service.batches` owns durable claims and
attempt history.  This module owns only the short-lived process boundary: it
reserves process-wide R permits, claims the selected inputs, runs the same
chromatogram seam used by preview, and hands a record back to the fenced
completion mutation.  There is deliberately no storage transaction around
the numerical call.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from . import analysis_runtime, batches

MAX_ITEMS = 2
MAX_FAILURE_BYTES = 1_000
_BUSY_MESSAGE = "R worker capacity is busy; retry after current analyses finish"


def _release_slots(slots: list[Any]) -> BaseException | None:
    """Release every permit and return the first release error, if any."""
    first_error: BaseException | None = None
    for slot in slots:
        try:
            slot.release()
        except BaseException as error:
            # A permit's release is an unwind operation.  Keep attempting the
            # remaining releases so one broken test double or runtime object
            # cannot strand all process capacity, but do not silently hide a
            # broken capacity implementation.
            if first_error is None:
                first_error = error
    return first_error


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value) if value is not None else ""
    if not text:
        text = "unknown worker error"
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore") or "worker error"


def _failure_payload(error: Exception) -> dict[str, Any]:
    """Build a small, structured failure without inferring an R runtime.

    A worker exception does not establish an R build or package version.  The
    state worker stores this payload in the immutable attempt history, so keep
    both individual text and the canonical JSON representation bounded.
    """
    payload = {
        "code": "chromatogram_worker_error",
        "exception_type": _bounded_text(type(error).__name__, 120),
        "message": _bounded_text(str(error), 700),
        "r_runtime_evidence": "unknown_until_success",
    }
    # The message limit above is conservative, but enforce the contract after
    # JSON escaping as well.  Truncate only the human text and preserve the
    # machine-readable fields.
    while (
        len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        > MAX_FAILURE_BYTES
    ):
        message = payload["message"]
        if not message:
            break
        payload["message"] = _bounded_text(message, max(1, len(message.encode("utf-8")) - 32))
    return payload


def _claim_field(claim: Mapping[str, Any], name: str) -> Any:
    try:
        return claim[name]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Batch claim is missing {name}") from exc


def _is_claim_fence_error(error: BaseException) -> bool:
    """Recognize only the state worker's typed claim-fencing rejection."""
    fence_type = getattr(batches, "ClaimFenceError", None)
    return isinstance(fence_type, type) and isinstance(error, fence_type)


def _record_failure(
    owner: str,
    batch_id: str,
    run_id: str,
    ordinal: int,
    token: str,
    error: Exception,
) -> None:
    """Persist an ordinary per-item error, leaving fenced claims untouched.

    ``fail_item`` is itself fenced by the claim token and lease.  If another
    actor recovered or completed the claim while this worker was unwinding,
    its rejection is expected: the final run read will expose the durable
    state and this executor must never retry or overwrite it.
    """
    try:
        batches.fail_item(
            owner,
            batch_id,
            run_id,
            ordinal,
            token,
            _failure_payload(error),
        )
    except Exception as failure_error:
        if not _is_claim_fence_error(failure_error):
            raise


def _execute_item(
    owner: str,
    batch_id: str,
    run_id: str,
    item: Mapping[str, Any],
    slot: Any,
) -> Any:
    """Claim, execute and complete one selected item on one reserved permit."""
    ordinal = _claim_field(item, "ordinal")
    token = _claim_field(item, "execution_token")
    try:
        claim = batches.claim_input(owner, batch_id, run_id, ordinal, token)
        trace = _claim_field(claim, "trace")
        method = _claim_field(claim, "method")
        dataset_id = _claim_field(claim, "dataset_id")
        trace_id = _claim_field(claim, "trace_id")
        input_sha256 = _claim_field(claim, "input_sha256")
        source_sha256 = claim.get("source_sha256")
        result = analysis_runtime.process_chromatogram(trace, method, worker=slot.run)
        # Preserve the private WorkerResult marker by handing the object from
        # process_chromatogram directly to make_analysis_record.  Serializing
        # or copying it here would lose execution-time worker provenance.
        record = analysis_runtime.make_analysis_record(
            dataset_id,
            trace_id,
            "chromatogram",
            method,
            result,
            input_sha256,
            source_sha256,
        )
    except BaseException as error:
        if isinstance(error, Exception):
            if _is_claim_fence_error(error):
                # Recovery, expiry, or another writer won the fence.  The
                # latest durable run read below is authoritative; do not try
                # to turn this claim into a new attempt.
                return None
            _record_failure(owner, batch_id, run_id, ordinal, token, error)
            # An ordinary item failure is retained in attempt history.  It is
            # not a run-level exception, so sibling items can complete and
            # the caller receives the mixed public run state.
            return None
        # BaseException (including process/thread interruption) intentionally
        # leaves the durable running claim for explicit recovery.
        raise

    # Completion errors are not worker failures.  In particular, do not call
    # fail_item after a stale/expired completion: the state worker's latest
    # durable status is authoritative and the outer read will expose it.
    try:
        return batches.complete_item(owner, batch_id, run_id, ordinal, token, record)
    except BaseException as error:
        if not isinstance(error, Exception):
            # BaseException (including process/thread interruption) leaves
            # the explicit running claim for recovery.
            raise
        if _is_claim_fence_error(error):
            # A recovered/expired claim may finish after another actor has
            # changed its state.  It must not overwrite that state.
            return None
        if isinstance(error, sqlite3.Error):
            # A persistence failure may have happened before the durable
            # state was committed.  Leave the claim for explicit recovery.
            raise
        # A known non-fence completion rejection (for example, invalid
        # evidence) is an ordinary item failure and must be retained rather
        # than leaving a permanently running claim.
        _record_failure(owner, batch_id, run_id, ordinal, token, error)
        return None


def _run_items(
    owner: str,
    batch_id: str,
    run_id: str,
    items: list[Mapping[str, Any]],
    slots: list[Any],
) -> None:
    """Dispatch at most one item per permit, with no hidden work queue."""
    if len(items) != len(slots):
        raise ValueError("Durable run selected more items than available worker permits")
    executor = ThreadPoolExecutor(max_workers=len(items))
    futures: list[Future[Any]] = []
    try:
        # ``items`` are selected durably in ordinal order.  A one-to-one
        # mapping to permits ensures no item waits behind a 90-second worker
        # while the state lease is only 150 seconds.
        for item, slot in zip(items, slots, strict=True):
            futures.append(executor.submit(_execute_item, owner, batch_id, run_id, item, slot))
        # Observe every task, including failures, before returning.  A
        # BaseException is re-raised and leaves its claim explicitly running;
        # the caller's finally block still releases every process permit.
        for future in futures:
            future.result()
    finally:
        executor.shutdown(wait=True, cancel_futures=False)


def run_batch(
    owner: str,
    batch_id: str,
    key: str,
    expected_revision: int,
    max_items: int = MAX_ITEMS,
) -> dict[str, Any]:
    """Execute a bounded durable chromatogram run and return its public state.

    A retained request receipt is checked before taking process capacity.  A
    fresh request takes only immediately available global permits, reserves up
    to that capacity, and dispatches one numerical task per permit.  Replays
    and reservation race losers never execute computation.
    """
    # This read is intentionally first.  It is the replay fast path and must
    # remain free of capacity acquisition and numerical work.
    existing = batches.lookup_run_request(
        owner,
        batch_id,
        key,
        expected_revision,
        max_items=max_items,
    )
    if existing is not None:
        return existing

    # acquire_worker_slots is nonblocking and may return fewer than requested.
    # Never reserve durable claims when no process capacity is available.
    slots = analysis_runtime.acquire_worker_slots(max_items)
    if not slots:
        raise ValueError(_BUSY_MESSAGE)

    dispatch_slots = slots
    try:
        context = analysis_runtime.execution_context()
        run, created = batches.reserve_run(
            owner,
            batch_id,
            key,
            expected_revision,
            max_items=max_items,
            capacity=len(slots),
            execution_context=context,
        )
        if not created:
            # Another caller won the idempotency race.  It owns execution of
            # the durable run; this caller must release all permits and read
            # the sanitized current state without touching its claims.
            run_id = _claim_field(run, "run_id")
            release_error = _release_slots(dispatch_slots)
            dispatch_slots = []
            if release_error is not None:
                raise release_error
            return batches.get_run(owner, run_id)

        run_id = _claim_field(run, "run_id")
        items = _claim_field(run, "items")
        if not isinstance(items, list):
            raise ValueError("Reserved batch run has malformed selected items")
        if not items:
            raise ValueError("Reserved batch run has no selected items")
        if len(items) > len(slots):
            raise ValueError("Reserved batch run selected more items than available permits")
        # A batch may have fewer pending items than the permits acquired for
        # the request.  Return unused capacity before dispatching so unrelated
        # preview or batch work can use it immediately.
        unused_slots = slots[len(items) :]
        if unused_slots:
            release_error = _release_slots(unused_slots)
            if release_error is not None:
                raise release_error
            dispatch_slots = slots[: len(items)]
        _run_items(owner, batch_id, run_id, items, dispatch_slots)
        return batches.get_run(owner, run_id)
    finally:
        release_error = _release_slots(dispatch_slots)
        # Do not mask a worker/dispatch/persistence exception with a secondary
        # permit-release error.  When the operation otherwise succeeded,
        # surface the release failure rather than silently leaking capacity.
        if release_error is not None and sys.exc_info()[1] is None:
            raise release_error


__all__ = ["run_batch"]
