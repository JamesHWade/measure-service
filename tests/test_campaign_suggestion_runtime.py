"""Runtime integration checks for durable campaign-suggestion attempts."""

from __future__ import annotations

import json
import math
import threading
from concurrent.futures import Future, ThreadPoolExecutor

import pytest
from test_campaigns import _prepared, _record_snapshot

from measure_service import (
    campaign_protocol,
    campaigns,
    method_registry,
    optimizer_backend,
)
from measure_service.storage import canonical, registry_connection, sha256


@pytest.fixture(autouse=True)
def isolated_suggestion_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MEASURE_BO_PYTHON", raising=False)
    return tmp_path


def _campaign_after_initial_pipeline(key: str):
    prepared = _prepared(key=key, evaluation_budget=6)
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        prepared["protocol"]["protocol_id"],
        "two-analytes",
        17,
        "bo_mcp_gp_logei",
        f"{key}-campaign",
    )
    proposals = []
    for ordinal in range(prepared["spec"].initial_design_size):
        response = campaigns.suggest_campaign(
            owner,
            campaign["campaign_id"],
            campaign["revision"],
            f"{key}-initial-suggest-{ordinal}",
        )
        proposal = response["proposal"]
        assert proposal["ordinal"] == ordinal
        assert proposal["proposal_source"] == "common_initial_design"
        proposals.append(proposal)
        evidence = _record_snapshot(prepared, proposal, key=f"{key}-initial-{ordinal}")
        consumed = campaigns.consume_campaign_snapshot(
            owner,
            campaign["campaign_id"],
            proposal["proposal_id"],
            evidence[-1]["snapshot_id"],
            response["revision"],
            f"{key}-initial-consume-{ordinal}",
        )
        campaign = consumed["campaign"]
    assert campaign["consumed_evaluations"] == 3
    assert campaign["revision"] == 7
    with registry_connection() as con:
        training, members = campaigns._training_context(con, owner, campaign["campaign_id"])
    assert len(training) == len(members) == 3
    return prepared, campaign, proposals, training, members


def _valid_backend(spec, training, seed: int = 17):
    return {
        "backend_id": "bo-engine/BoTorchBackend",
        "backend_name": "bo-mcp",
        "generation_method": "bo",
        "model_type": "SingleTaskGP",
        "acquisition_method": "expected_improvement",
        "acquisition_implementation": "qLogExpectedImprovement",
        "optimization_strategy": "deterministic integration-test adapter",
        "source_revision": None,
        "source_tree_sha256": "a" * 64,
        "distribution_versions": {"botorch": "synthetic-test-version"},
        "protocol_sha256": campaign_protocol.campaign_protocol_sha256(spec),
        "seed": seed,
        "training_sha256": sha256(training),
        "training_count": len(training),
        "runtime": {"device": "cpu"},
        "settings": {"fixture": "provenance-valid"},
        "warnings": [],
        "suggestion_provenance": {"generation_method": "bo"},
    }


def _resource_counts(owner: str, campaign_id: str) -> dict[str, int]:
    with registry_connection() as con:
        return {
            "proposals": con.execute(
                "SELECT COUNT(*) FROM campaign_proposals WHERE owner=? AND campaign_id=?",
                (owner, campaign_id),
            ).fetchone()[0],
            "methods": con.execute(
                "SELECT COUNT(*) FROM executable_methods WHERE owner=?", (owner,)
            ).fetchone()[0],
            "physical_attempts": con.execute(
                "SELECT COUNT(*) FROM experiment_attempts WHERE owner=?", (owner,)
            ).fetchone()[0],
            "consumptions": con.execute(
                "SELECT COUNT(*) FROM campaign_consumptions WHERE owner=? AND campaign_id=?",
                (owner, campaign_id),
            ).fetchone()[0],
        }


def _future_result_without_hanging(
    future: Future, release: threading.Event, executor: ThreadPoolExecutor
):
    """Release a blocked sidecar stub before waiting during test cleanup."""
    release.set()
    try:
        return future.result(timeout=10)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_duplicate_gp_candidate_is_retained_replayable_and_does_not_create_resources(
    monkeypatch,
):
    prepared, campaign, proposals, training, members = _campaign_after_initial_pipeline(
        "suggest-duplicate"
    )
    owner = prepared["owner"]
    campaign_id = campaign["campaign_id"]
    key = "duplicate-gp-key"
    duplicate = proposals[0]["candidate"]
    calls = []

    def suggest(spec, observed, seed):
        calls.append((spec, observed, seed))
        return {"candidate": dict(duplicate), "backend": _valid_backend(spec, observed, seed)}

    monkeypatch.setattr(optimizer_backend, "suggest_candidate", suggest)
    before = _resource_counts(owner, campaign_id)
    with pytest.raises(ValueError, match="Campaign candidate duplicates a prior proposal") as first:
        campaigns.suggest_campaign(owner, campaign_id, campaign["revision"], key)
    attempt_id = first.value.suggestion_attempt_id
    assert len(calls) == 1
    assert _resource_counts(owner, campaign_id) == before

    retained = campaigns.get_campaign_suggestion_attempt(owner, attempt_id)
    outcome = retained["outcome"]
    assert retained["state"] == "rejected"
    assert outcome["status"] == "rejected"
    assert outcome["phase"] == "proposal_validation"
    assert outcome["candidate"] == duplicate
    assert outcome["backend"]["generation_method"] == "bo"
    assert outcome["backend"]["training_sha256"] == sha256(training)
    assert isinstance(outcome["candidate_generation_elapsed_seconds"], float)
    assert math.isfinite(outcome["candidate_generation_elapsed_seconds"])
    assert outcome["candidate_generation_elapsed_seconds"] >= 0
    assert retained["context"]["training"]["records"] == training
    assert retained["context"]["training"]["snapshot_members"] == members
    assert retained["context"]["generation"]["request"]["training_data"] == training
    assert retained["context"]["generation"]["request"]["training_sha256"] == sha256(training)
    assert retained["context"]["generation"]["kind"] == "bo_mcp_gp_logei"

    with pytest.raises(ValueError) as retry:
        campaigns.suggest_campaign(owner, campaign_id, campaign["revision"], key)
    assert str(retry.value) == str(first.value)
    assert retry.value.suggestion_attempt_id == attempt_id
    assert len(calls) == 1
    assert _resource_counts(owner, campaign_id) == before

    declaration = campaign_protocol.method_declaration(
        prepared["spec"],
        {"strong_solvent_fraction": 0.46, "acquisition_duration_min": 7.2},
    )
    with pytest.raises(ValueError, match="reserved by a campaign suggestion attempt"):
        method_registry.register_method(owner, "Retry-key collision", declaration, None, key)

    next_candidate = {"strong_solvent_fraction": 0.46, "acquisition_duration_min": 7.2}

    def unique_suggestion(spec, observed, seed):
        calls.append((spec, observed, seed))
        return {"candidate": dict(next_candidate), "backend": _valid_backend(spec, observed, seed)}

    monkeypatch.setattr(optimizer_backend, "suggest_candidate", unique_suggestion)
    accepted = campaigns.suggest_campaign(
        owner, campaign_id, campaign["revision"], "duplicate-explicit-new-key"
    )
    assert accepted["proposal"]["candidate"] == next_candidate
    assert accepted["proposal"]["state"] == "pending"
    assert len(calls) == 2
    assert _resource_counts(owner, campaign_id) == {
        **before,
        "proposals": before["proposals"] + 1,
        "methods": before["methods"] + 1,
    }


def _reseal_attempt_evidence(
    owner: str, attempt_id: str, *, mutate_context=None, mutate_outcome=None
):
    """Recompute ledger digests after a deliberate semantic tamper in a test."""
    with registry_connection(write=True) as con:
        row = con.execute(
            "SELECT payload FROM campaign_suggestion_attempts WHERE id=? AND owner=?",
            (attempt_id, owner),
        ).fetchone()
        assert row is not None
        start = json.loads(row["payload"])
        if mutate_context is not None:
            mutate_context(start["context"])
        start["context_sha256"] = sha256(start["context"])
        start["start_sha256"] = sha256(
            {key: value for key, value in start.items() if key != "start_sha256"}
        )
        con.execute(
            "UPDATE campaign_suggestion_attempts SET payload=?, context_digest=?, record_digest=? WHERE id=?",
            (canonical(start), start["context_sha256"], sha256(start), attempt_id),
        )

        outcome_row = con.execute(
            "SELECT payload FROM campaign_suggestion_attempt_outcomes "
            "WHERE suggestion_attempt_id=? AND owner=?",
            (attempt_id, owner),
        ).fetchone()
        assert outcome_row is not None
        envelope = json.loads(outcome_row["payload"])
        envelope["start_sha256"] = start["start_sha256"]
        if mutate_outcome is not None:
            mutate_outcome(envelope["outcome"])
        envelope["outcome_sha256"] = sha256(
            {key: value for key, value in envelope.items() if key != "outcome_sha256"}
        )
        con.execute(
            "UPDATE campaign_suggestion_attempt_outcomes SET payload=?, output_digest=? "
            "WHERE suggestion_attempt_id=?",
            (canonical(envelope), envelope["outcome_sha256"], attempt_id),
        )


def _missing_sidecar_failure(prepared, campaign, key: str) -> str:
    with pytest.raises(optimizer_backend.OptimizerBackendError) as error:
        campaigns.suggest_campaign(
            prepared["owner"], campaign["campaign_id"], campaign["revision"], key
        )
    return error.value.suggestion_attempt_id


def test_missing_sidecar_keeps_child_unknown_and_outer_elapsed_known(monkeypatch):
    prepared, campaign, _, training, _ = _campaign_after_initial_pipeline("missing-sidecar")
    owner = prepared["owner"]
    campaign_id = campaign["campaign_id"]
    monkeypatch.delenv("MEASURE_BO_PYTHON", raising=False)
    no_launch_calls = []

    def forbidden_process(*args, **kwargs):
        no_launch_calls.append((args, kwargs))
        raise AssertionError("a missing runtime must fail before launching a child process")

    monkeypatch.setattr(optimizer_backend.subprocess, "run", forbidden_process)
    before = _resource_counts(owner, campaign_id)
    key = "missing-sidecar-suggest"
    with pytest.raises(optimizer_backend.OptimizerBackendError) as first:
        campaigns.suggest_campaign(owner, campaign_id, campaign["revision"], key)
    attempt_id = first.value.suggestion_attempt_id
    assert no_launch_calls == []
    assert _resource_counts(owner, campaign_id) == before

    record = campaigns.get_campaign_suggestion_attempt(owner, attempt_id)
    outcome = record["outcome"]
    diagnostics = outcome["diagnostics"]
    assert record["state"] == "failed"
    assert outcome["candidate"] is None
    assert outcome["backend"] is None
    assert isinstance(outcome["candidate_generation_elapsed_seconds"], float)
    assert math.isfinite(outcome["candidate_generation_elapsed_seconds"])
    assert outcome["candidate_generation_elapsed_seconds"] >= 0
    assert diagnostics["phase"] == "runtime_resolution"
    assert diagnostics["category"] == "runtime_missing"
    assert diagnostics["subprocess_elapsed_seconds"] is None
    assert diagnostics["training_sha256"] == sha256(training)

    def forbidden_recompute(*args, **kwargs):
        raise AssertionError("a terminal retry must not recompute the missing backend")

    monkeypatch.setattr(optimizer_backend, "suggest_candidate", forbidden_recompute)
    with pytest.raises(optimizer_backend.OptimizerBackendError) as retry:
        campaigns.suggest_campaign(owner, campaign_id, campaign["revision"], key)
    assert str(retry.value) == str(first.value)
    assert retry.value.suggestion_attempt_id == attempt_id
    assert no_launch_calls == []
    assert _resource_counts(owner, campaign_id) == before


def test_active_claim_blocks_duplicate_dispatch_but_not_other_registry_work(monkeypatch):
    prepared, campaign, _, training, _ = _campaign_after_initial_pipeline("concurrent-claim")
    owner = prepared["owner"]
    campaign_id = campaign["campaign_id"]
    entered = threading.Event()
    release = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    def blocked_suggestion(spec, observed, seed):
        with calls_lock:
            calls.append((spec, observed, seed))
        entered.set()
        if not release.wait(timeout=8):
            raise TimeoutError("test did not release the blocked optimizer stub")
        candidate = {"strong_solvent_fraction": 0.44, "acquisition_duration_min": 7.4}
        return {"candidate": candidate, "backend": _valid_backend(spec, observed, seed)}

    monkeypatch.setattr(optimizer_backend, "suggest_candidate", blocked_suggestion)
    executor = ThreadPoolExecutor(max_workers=6)
    suggestion_future = executor.submit(
        campaigns.suggest_campaign,
        owner,
        campaign_id,
        campaign["revision"],
        "concurrent-claim-key",
    )
    try:
        assert entered.wait(timeout=5), "optimizer stub never reached its blocking point"
        active = campaigns.list_campaign_suggestion_attempts(owner, campaign_id)
        claimed_records = [item for item in active["records"] if item["state"] == "claimed"]
        assert len(claimed_records) == 1
        attempt_id = claimed_records[0]["suggestion_attempt_id"]
        same_key = executor.submit(
            campaigns.suggest_campaign,
            owner,
            campaign_id,
            campaign["revision"],
            "concurrent-claim-key",
        )
        different_key = executor.submit(
            campaigns.suggest_campaign,
            owner,
            campaign_id,
            campaign["revision"],
            "concurrent-different-key",
        )
        with pytest.raises(campaigns.SuggestionInProgressError) as same_error:
            same_key.result(timeout=3)
        with pytest.raises(campaigns.SuggestionInProgressError) as different_error:
            different_key.result(timeout=3)
        assert same_error.value.suggestion_attempt_id == attempt_id
        assert different_error.value.suggestion_attempt_id == attempt_id
        assert len(calls) == 1

        declaration = campaign_protocol.method_declaration(
            prepared["spec"],
            {"strong_solvent_fraction": 0.48, "acquisition_duration_min": 7.6},
        )
        unrelated_write = executor.submit(
            method_registry.register_method,
            owner,
            "Independent registry write during optimizer call",
            declaration,
            None,
            "concurrent-unrelated-method",
        )
        method = unrelated_write.result(timeout=3)
        assert method["method_id"]
        read_attempt = executor.submit(campaigns.get_campaign_suggestion_attempt, owner, attempt_id)
        assert read_attempt.result(timeout=3)["state"] == "claimed"
        list_page = executor.submit(campaigns.list_campaign_suggestion_attempts, owner, campaign_id)
        listed_ids = {
            item["suggestion_attempt_id"] for item in list_page.result(timeout=3)["records"]
        }
        assert attempt_id in listed_ids
        assert campaigns.get_campaign(owner, campaign_id)["state"] == "ready_to_suggest"
    finally:
        completed = None
        thread_error = None
        try:
            completed = _future_result_without_hanging(suggestion_future, release, executor)
        except BaseException as error:
            thread_error = error
    if thread_error is not None:
        raise thread_error
    assert completed["proposal"]["state"] == "pending"
    assert len(calls) == 1
    assert _resource_counts(owner, campaign_id)["consumptions"] == 3
    assert len(training) == 3


def test_keyboard_interrupt_leaves_claim_until_owner_hash_reason_reconciliation(monkeypatch):
    prepared, campaign, _, _, _ = _campaign_after_initial_pipeline("interrupt-reconcile")
    owner = prepared["owner"]
    campaign_id = campaign["campaign_id"]
    key = "interrupt-suggestion-key"
    calls = []

    def interrupted(*args, **kwargs):
        calls.append((args, kwargs))
        raise KeyboardInterrupt("simulated worker interruption")

    monkeypatch.setattr(campaigns, "_bo_candidate", interrupted)
    with pytest.raises(KeyboardInterrupt, match="simulated worker interruption"):
        campaigns.suggest_campaign(owner, campaign_id, campaign["revision"], key)
    assert len(calls) == 1

    summaries = campaigns.list_campaign_suggestion_attempts(owner, campaign_id)
    claimed_records = [item for item in summaries["records"] if item["state"] == "claimed"]
    assert len(claimed_records) == 1
    attempt_id = claimed_records[0]["suggestion_attempt_id"]
    claimed = campaigns.get_campaign_suggestion_attempt(owner, attempt_id)
    start_digest = claimed["start_sha256"]
    assert claimed["state"] == "claimed"
    assert claimed["outcome"] is None

    with pytest.raises(ValueError, match="not found or is inaccessible"):
        campaigns.abandon_campaign_suggestion_attempt(
            "other-owner", attempt_id, start_digest, "restart review", "wrong-owner-reconcile"
        )
    with pytest.raises(ValueError, match="start digest does not match"):
        campaigns.abandon_campaign_suggestion_attempt(
            owner, attempt_id, "0" * 64, "restart review", "wrong-hash-reconcile"
        )
    assert campaigns.get_campaign_suggestion_attempt(owner, attempt_id)["state"] == "claimed"

    with pytest.raises(campaigns.SuggestionInProgressError) as same_key:
        campaigns.suggest_campaign(owner, campaign_id, campaign["revision"], key)
    with pytest.raises(campaigns.SuggestionInProgressError) as new_key:
        campaigns.suggest_campaign(
            owner, campaign_id, campaign["revision"], "interrupt-second-dispatch-blocked"
        )
    assert same_key.value.suggestion_attempt_id == attempt_id
    assert new_key.value.suggestion_attempt_id == attempt_id
    assert len(calls) == 1

    reason = "worker restart confirmed; original computation abandoned"
    abandoned = campaigns.abandon_campaign_suggestion_attempt(
        owner, attempt_id, start_digest, reason, "correct-reconcile-key"
    )
    assert abandoned["state"] == "abandoned_unknown"
    assert abandoned["outcome"]["reason"] == reason
    assert abandoned["outcome"]["failure"]["message"] == reason
    assert abandoned["outcome"]["candidate"] is None
    assert abandoned["outcome"]["backend"] is None
    assert abandoned["outcome"]["candidate_generation_elapsed_seconds"] is None
    assert campaigns.get_campaign_suggestion_attempt(owner, attempt_id) == abandoned
    assert len(calls) == 1


def test_late_optimizer_return_is_fenced_after_reconciliation_and_new_attempt(monkeypatch):
    prepared, campaign, _, training, _ = _campaign_after_initial_pipeline("late-completion")
    owner = prepared["owner"]
    campaign_id = campaign["campaign_id"]
    entered = threading.Event()
    release = threading.Event()
    call_lock = threading.Lock()
    calls = []
    late_candidate = {"strong_solvent_fraction": 0.43, "acquisition_duration_min": 7.3}
    current_candidate = {"strong_solvent_fraction": 0.47, "acquisition_duration_min": 7.7}

    def late_then_current(spec, observed, seed):
        with call_lock:
            calls.append((spec, observed, seed))
            ordinal = len(calls)
        if ordinal == 1:
            entered.set()
            if not release.wait(timeout=8):
                raise TimeoutError("test did not release the late optimizer stub")
            candidate = late_candidate
        else:
            candidate = current_candidate
        return {"candidate": dict(candidate), "backend": _valid_backend(spec, observed, seed)}

    monkeypatch.setattr(optimizer_backend, "suggest_candidate", late_then_current)
    executor = ThreadPoolExecutor(max_workers=2)
    old_future = executor.submit(
        campaigns.suggest_campaign,
        owner,
        campaign_id,
        campaign["revision"],
        "late-original-key",
    )
    try:
        assert entered.wait(timeout=5), "first optimizer call did not reach its blocking point"
        page = campaigns.list_campaign_suggestion_attempts(owner, campaign_id)
        claimed_records = [item for item in page["records"] if item["state"] == "claimed"]
        assert len(claimed_records) == 1
        old_attempt_id = claimed_records[0]["suggestion_attempt_id"]
        claimed = campaigns.get_campaign_suggestion_attempt(owner, old_attempt_id)
        assert claimed["state"] == "claimed"
        abandoned = campaigns.abandon_campaign_suggestion_attempt(
            owner,
            old_attempt_id,
            claimed["start_sha256"],
            "operator confirmed old worker is unresolved",
            "late-abandon-key",
        )
        assert abandoned["state"] == "abandoned_unknown"
        with pytest.raises(RuntimeError, match="operator confirmed old worker is unresolved"):
            campaigns.suggest_campaign(
                owner, campaign_id, campaign["revision"], "late-original-key"
            )
        assert len(calls) == 1

        accepted = campaigns.suggest_campaign(
            owner, campaign_id, campaign["revision"], "late-new-attempt-key"
        )
        assert accepted["proposal"]["candidate"] == current_candidate
        assert accepted["proposal"]["state"] == "pending"
        assert len(calls) == 2
        accepted_revision = accepted["revision"]
        accepted_counts = _resource_counts(owner, campaign_id)
    finally:
        thread_error = None
        try:
            _future_result_without_hanging(old_future, release, executor)
        except RuntimeError as error:
            if "operator confirmed old worker is unresolved" not in str(error):
                thread_error = error
        except BaseException as error:
            thread_error = error
    if thread_error is not None:
        raise thread_error

    late_record = campaigns.get_campaign_suggestion_attempt(owner, old_attempt_id)
    late_result = late_record["late_result"]["result"]
    assert late_record["state"] == "abandoned_unknown"
    assert late_record["outcome"] == abandoned["outcome"]
    assert late_result["computation_status"] == "returned"
    assert late_result["candidate"] == late_candidate
    assert late_result["backend"]["training_sha256"] == sha256(training)
    assert isinstance(late_result["candidate_generation_elapsed_seconds"], float)
    assert math.isfinite(late_result["candidate_generation_elapsed_seconds"])
    assert late_result["candidate_generation_elapsed_seconds"] >= 0
    assert late_record["outcome"]["candidate"] is None
    assert late_record["outcome"]["candidate_generation_elapsed_seconds"] is None
    assert campaigns.get_campaign(owner, campaign_id)["revision"] == accepted_revision
    assert (
        campaigns.get_campaign(owner, campaign_id)["pending_proposal"]["candidate"]
        == current_candidate
    )
    assert _resource_counts(owner, campaign_id) == accepted_counts
    with registry_connection() as con:
        late_count = con.execute(
            "SELECT COUNT(*) FROM campaign_suggestion_attempt_late_results WHERE suggestion_attempt_id=?",
            (old_attempt_id,),
        ).fetchone()[0]
    assert late_count == 1
    assert len(calls) == 2


def test_resealed_claim_context_substitution_fails_campaign_source_replay(monkeypatch):
    prepared, campaign, _, _, _ = _campaign_after_initial_pipeline("resealed-context")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("leave an inspectable claim")

    monkeypatch.setattr(campaigns, "_bo_candidate", interrupted)
    with pytest.raises(KeyboardInterrupt, match="inspectable claim"):
        campaigns.suggest_campaign(
            prepared["owner"], campaign["campaign_id"], campaign["revision"], "context-claim-key"
        )
    records = campaigns.list_campaign_suggestion_attempts(
        prepared["owner"], campaign["campaign_id"]
    )["records"]
    claimed_records = [item for item in records if item["state"] == "claimed"]
    assert len(claimed_records) == 1
    record = claimed_records[0]
    attempt_id = record["suggestion_attempt_id"]

    with registry_connection(write=True) as con:
        row = con.execute(
            "SELECT payload FROM campaign_suggestion_attempts WHERE id=? AND owner=?",
            (attempt_id, prepared["owner"]),
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["context"]["campaign"]["task_id"] = "forged-task"
        payload["context_sha256"] = sha256(payload["context"])
        payload_without_start_digest = {
            key: value for key, value in payload.items() if key != "start_sha256"
        }
        payload["start_sha256"] = sha256(payload_without_start_digest)
        con.execute(
            "UPDATE campaign_suggestion_attempts SET payload=?, context_digest=?, record_digest=? WHERE id=?",
            (
                canonical(payload),
                payload["context_sha256"],
                sha256(payload),
                attempt_id,
            ),
        )

    with pytest.raises(
        ValueError, match="Stored suggestion context differs from the frozen campaign"
    ):
        campaigns.get_campaign_suggestion_attempt(prepared["owner"], attempt_id)
    with pytest.raises(
        ValueError, match="Stored suggestion context differs from the frozen campaign"
    ):
        campaigns.list_campaign_suggestion_attempts(prepared["owner"], campaign["campaign_id"])


@pytest.mark.parametrize(
    ("error_kind", "expected_message"),
    [
        ("empty", "RuntimeError during suggestion computation"),
        ("bad_str", "BadStringError during suggestion computation"),
    ],
)
def test_initial_design_error_without_safe_message_is_finalized_and_replayed(
    monkeypatch, error_kind, expected_message
):
    prepared = _prepared(key=f"failure-string-{error_kind}", evaluation_budget=6)
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        prepared["protocol"]["protocol_id"],
        "two-analytes",
        17,
        "random",
        f"failure-string-{error_kind}-campaign",
    )
    calls = []

    class BadStringError(Exception):
        def __str__(self):
            raise RuntimeError("unprintable failure")

    def fail_initial_candidate(*args, **kwargs):
        calls.append((args, kwargs))
        if error_kind == "empty":
            raise RuntimeError()
        raise BadStringError()

    monkeypatch.setattr(campaign_protocol, "initial_candidate", fail_initial_candidate)
    with pytest.raises(RuntimeError, match=expected_message) as first:
        campaigns.suggest_campaign(
            owner, campaign["campaign_id"], campaign["revision"], "unprintable-initial-error"
        )
    attempt_id = first.value.suggestion_attempt_id
    record = campaigns.get_campaign_suggestion_attempt(owner, attempt_id)
    assert record["state"] == "failed"
    assert record["outcome"]["failure"]["message"] == expected_message
    assert record["outcome"]["candidate"] is None
    assert len(calls) == 1

    with pytest.raises(RuntimeError, match=expected_message) as retry:
        campaigns.suggest_campaign(
            owner, campaign["campaign_id"], campaign["revision"], "unprintable-initial-error"
        )
    assert retry.value.suggestion_attempt_id == attempt_id
    assert str(retry.value) == str(first.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("diagnostic_source", "Stored failure diagnostics differ from the retained BO request"),
        ("missing_request", "Stored prepared BO request evidence is malformed"),
        ("non_dict_request", "Stored prepared BO request evidence is malformed"),
    ],
)
def test_resealed_prepared_request_is_cross_bound_to_failure_diagnostics(
    monkeypatch, mutation, expected_error
):
    prepared, campaign, _, _, _ = _campaign_after_initial_pipeline(f"request-crossbind-{mutation}")
    owner = prepared["owner"]
    attempt_id = _missing_sidecar_failure(prepared, campaign, f"request-crossbind-{mutation}-key")
    original = campaigns.get_campaign_suggestion_attempt(owner, attempt_id)
    request = original["context"]["generation"]["request"]
    assert original["context"]["generation"]["request_status"] == "prepared"
    assert original["outcome"]["diagnostics"]["request_sha256"] == sha256(request)

    def mutate_context(context):
        generation = context["generation"]
        if mutation == "diagnostic_source":
            generation["request"]["adapter_source_sha256"] = "0" * 64
        elif mutation == "missing_request":
            del generation["request"]
        else:
            generation["request"] = []

    _reseal_attempt_evidence(owner, attempt_id, mutate_context=mutate_context)
    with pytest.raises(ValueError, match=expected_error):
        campaigns.get_campaign_suggestion_attempt(owner, attempt_id)


def test_resealed_returned_backend_source_identity_must_match_prepared_request(monkeypatch):
    prepared, campaign, proposals, training, _ = _campaign_after_initial_pipeline(
        "backend-request-crossbind"
    )
    owner = prepared["owner"]
    duplicate = proposals[0]["candidate"]
    calls = []

    def suggest(spec, observed, seed):
        calls.append((spec, observed, seed))
        backend = _valid_backend(spec, observed, seed)
        request = optimizer_backend.suggestion_request_evidence(spec, observed, seed)
        for key in (
            "adapter_source_sha256",
            "campaign_protocol_source_sha256",
            "design_kernel_source_sha256",
        ):
            backend[key] = request[key]
        return {"candidate": dict(duplicate), "backend": backend}

    monkeypatch.setattr(optimizer_backend, "suggest_candidate", suggest)
    with pytest.raises(ValueError, match="Campaign candidate duplicates a prior proposal") as error:
        campaigns.suggest_campaign(
            owner, campaign["campaign_id"], campaign["revision"], "backend-request-crossbind-key"
        )
    attempt_id = error.value.suggestion_attempt_id
    record = campaigns.get_campaign_suggestion_attempt(owner, attempt_id)
    assert record["outcome"]["backend"]["training_sha256"] == sha256(training)

    def mutate_outcome(outcome):
        outcome["backend"]["adapter_source_sha256"] = "0" * 64

    _reseal_attempt_evidence(owner, attempt_id, mutate_outcome=mutate_outcome)
    with pytest.raises(
        ValueError, match="Returned backend identity differs from its retained request"
    ):
        campaigns.get_campaign_suggestion_attempt(owner, attempt_id)
    assert len(calls) == 1


def test_near_limit_duplicate_backend_is_terminally_retained_by_digest(monkeypatch):
    prepared, campaign, proposals, training, _ = _campaign_after_initial_pipeline(
        "bounded-terminal-evidence"
    )
    owner = prepared["owner"]
    campaign_id = campaign["campaign_id"]
    duplicate = proposals[0]["candidate"]
    calls = []
    returned = []
    target_response_bytes = 63_850

    def suggest(spec, observed, seed):
        calls.append((spec, observed, seed))
        backend = _valid_backend(spec, observed, seed)
        backend["runtime"]["elapsed_seconds"] = 7.0
        request = optimizer_backend.suggestion_request_evidence(spec, observed, seed)
        for key in (
            "adapter_source_sha256",
            "campaign_protocol_source_sha256",
            "design_kernel_source_sha256",
        ):
            backend[key] = request[key]
        backend["padding"] = ""
        result = {"candidate": dict(duplicate), "backend": backend}
        remaining = target_response_bytes - len(canonical(result).encode("utf-8"))
        assert remaining > 0
        backend["padding"] = "x" * remaining
        assert len(canonical(result).encode("utf-8")) == target_response_bytes
        returned.append(backend)
        return result

    monkeypatch.setattr(optimizer_backend, "suggest_candidate", suggest)
    before = _resource_counts(owner, campaign_id)
    with pytest.raises(ValueError, match="Campaign candidate duplicates a prior proposal") as first:
        campaigns.suggest_campaign(
            owner, campaign_id, campaign["revision"], "bounded-terminal-evidence-key"
        )
    attempt_id = first.value.suggestion_attempt_id
    assert len(calls) == 1
    returned_size = len(canonical({"candidate": duplicate, "backend": returned[0]}).encode("utf-8"))
    assert 63_800 <= returned_size < 64_000
    assert _resource_counts(owner, campaign_id) == before

    record = campaigns.get_campaign_suggestion_attempt(owner, attempt_id)
    outcome = record["outcome"]
    assert record["state"] == "rejected"
    assert outcome["status"] == "rejected"
    assert outcome["candidate"] == duplicate
    assert outcome["backend"] is None
    assert outcome["retention"]["reason"] == "full_returned_evidence_not_retained"
    retained_backend = outcome["retention"]["backend"]
    assert retained_backend["sha256"] == sha256(returned[0])
    assert retained_backend["size_bytes"] == len(canonical(returned[0]).encode("utf-8"))
    assert retained_backend["request_identity"]["training_sha256"] == sha256(training)
    assert (
        retained_backend["request_identity"]["adapter_source_sha256"]
        == record["context"]["generation"]["request"]["adapter_source_sha256"]
    )
    assert isinstance(outcome["candidate_generation_elapsed_seconds"], float)
    assert math.isfinite(outcome["candidate_generation_elapsed_seconds"])
    assert outcome["candidate_generation_elapsed_seconds"] >= 0

    with pytest.raises(ValueError) as retry:
        campaigns.suggest_campaign(
            owner, campaign_id, campaign["revision"], "bounded-terminal-evidence-key"
        )
    assert retry.value.suggestion_attempt_id == attempt_id
    assert str(retry.value) == str(first.value)
    assert len(calls) == 1
    assert _resource_counts(owner, campaign_id) == before


def test_adapter_child_elapsed_is_retained_separately_from_host_elapsed(monkeypatch):
    prepared, campaign, proposals, _, _ = _campaign_after_initial_pipeline("separate-child-runtime")
    owner = prepared["owner"]
    duplicate = proposals[0]["candidate"]
    calls = []

    def suggest(spec, observed, seed):
        calls.append((spec, observed, seed))
        backend = _valid_backend(spec, observed, seed)
        backend["runtime"]["elapsed_seconds"] = 7.0
        return {"candidate": dict(duplicate), "backend": backend}

    monkeypatch.setattr(optimizer_backend, "suggest_candidate", suggest)
    with pytest.raises(ValueError, match="Campaign candidate duplicates a prior proposal") as error:
        campaigns.suggest_campaign(
            owner, campaign["campaign_id"], campaign["revision"], "separate-child-runtime-key"
        )
    record = campaigns.get_campaign_suggestion_attempt(owner, error.value.suggestion_attempt_id)
    outcome = record["outcome"]
    host_elapsed = outcome["backend"]["runtime"]["elapsed_seconds"]
    assert outcome["adapter_subprocess_elapsed_seconds"] == 7.0
    assert isinstance(host_elapsed, float) and math.isfinite(host_elapsed) and host_elapsed >= 0
    assert host_elapsed != outcome["adapter_subprocess_elapsed_seconds"]
    assert len(calls) == 1


def test_returned_source_contradiction_is_rejected_before_proposal_side_effects(monkeypatch):
    prepared, campaign, proposals, training, _ = _campaign_after_initial_pipeline(
        "atomic-source-admission"
    )
    owner = prepared["owner"]
    campaign_id = campaign["campaign_id"]
    candidate = {"strong_solvent_fraction": 0.47, "acquisition_duration_min": 6.7}
    assert candidate not in [proposal["candidate"] for proposal in proposals]
    calls = []

    def contradictory_source(spec, observed, seed):
        calls.append((spec, observed, seed))
        backend = _valid_backend(spec, observed, seed)
        request = optimizer_backend.suggestion_request_evidence(spec, observed, seed)
        for key in (
            "adapter_source_sha256",
            "campaign_protocol_source_sha256",
            "design_kernel_source_sha256",
        ):
            backend[key] = request[key]
        backend["adapter_source_sha256"] = "0" * 64
        return {"candidate": dict(candidate), "backend": backend}

    monkeypatch.setattr(optimizer_backend, "suggest_candidate", contradictory_source)
    before_counts = _resource_counts(owner, campaign_id)
    before_revision = campaigns.get_campaign(owner, campaign_id)["revision"]
    request_key = "atomic-source-admission-key"
    with pytest.raises(
        ValueError, match="Returned backend identity differs from its retained request"
    ) as first:
        campaigns.suggest_campaign(owner, campaign_id, campaign["revision"], request_key)
    attempt_id = first.value.suggestion_attempt_id

    record = campaigns.get_campaign_suggestion_attempt(owner, attempt_id)
    outcome = record["outcome"]
    assert record["state"] == "rejected"
    assert outcome["status"] == "rejected"
    assert outcome["phase"] == "returned_evidence_validation"
    assert outcome["candidate"] == candidate
    assert outcome["backend"] is None
    assert outcome["diagnostics"] is None
    assert outcome["adapter_subprocess_elapsed_seconds"] is None
    assert isinstance(outcome["candidate_generation_elapsed_seconds"], float)
    assert math.isfinite(outcome["candidate_generation_elapsed_seconds"])
    assert outcome["candidate_generation_elapsed_seconds"] >= 0
    untrusted = outcome["untrusted_returned_evidence"]
    assert untrusted["backend"]["adapter_source_sha256"] == "0" * 64
    assert untrusted["diagnostics"] is None
    assert untrusted["adapter_subprocess_elapsed_seconds"] is None
    assert untrusted["original_failure"] is None

    assert _resource_counts(owner, campaign_id) == before_counts
    current_campaign = campaigns.get_campaign(owner, campaign_id)
    assert current_campaign["revision"] == before_revision
    assert current_campaign["consumed_evaluations"] == 3
    assert current_campaign["pending_proposal"] is None
    with registry_connection() as con:
        receipt = con.execute(
            "SELECT 1 FROM registry_receipts WHERE owner=? AND request_key=?",
            (owner, request_key),
        ).fetchone()
    assert receipt is None

    summaries = campaigns.list_campaign_suggestion_attempts(owner, campaign_id)["records"]
    assert any(
        item["suggestion_attempt_id"] == attempt_id and item["state"] == "rejected"
        for item in summaries
    )
    with pytest.raises(
        ValueError, match="Returned backend identity differs from its retained request"
    ) as retry:
        campaigns.suggest_campaign(owner, campaign_id, campaign["revision"], request_key)
    assert retry.value.suggestion_attempt_id == attempt_id
    assert str(retry.value) == str(first.value)
    assert len(calls) == 1
    assert _resource_counts(owner, campaign_id) == before_counts
    assert campaigns.get_campaign(owner, campaign_id)["revision"] == before_revision
