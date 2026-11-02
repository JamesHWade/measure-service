"""Storage contract for claimed campaign-suggestion attempts."""

from __future__ import annotations

import json

import pytest

from measure_service import campaign_suggestion_attempts as attempts
from measure_service.storage import canonical, registry_connection, sha256


@pytest.fixture
def campaign_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    owner = "chemist"
    campaign_id = "campaign-1"
    with registry_connection(write=True) as con:
        con.execute(
            "INSERT INTO campaign_protocols VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("protocol-1", owner, "Fixture", "{}", "input", "output", "2026-11-01T00:00:00Z"),
        )
        con.execute(
            "INSERT INTO campaign_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                campaign_id,
                owner,
                "protocol-1",
                "protocol-input",
                "task-1",
                17,
                "random",
                "{}",
                "campaign-input",
                1,
                "2026-11-01T00:00:00Z",
            ),
        )
    return owner, campaign_id


def _request(campaign_id="campaign-1", revision=1):
    return {
        "operation": "suggest_campaign",
        "campaign_id": campaign_id,
        "expected_revision": revision,
    }


def _context(campaign_id="campaign-1", revision=1, ordinal=0):
    return {
        "campaign": {
            "campaign_id": campaign_id,
            "ordinal": ordinal,
            "expected_revision": revision,
        },
        "training": [],
        "spec": {"profile": "fixture/1"},
    }


def _create_campaign(con, owner, campaign_id):
    protocol_id = f"protocol-{campaign_id}"
    con.execute(
        "INSERT INTO campaign_protocols VALUES (?, ?, ?, ?, ?, ?, ?)",
        (protocol_id, owner, "Fixture", "{}", "input", "output", "2026-11-01T00:00:00Z"),
    )
    con.execute(
        "INSERT INTO campaign_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            campaign_id,
            owner,
            protocol_id,
            "protocol-input",
            "task-1",
            17,
            "random",
            "{}",
            "campaign-input",
            1,
            "2026-11-01T00:00:00Z",
        ),
    )


def _claim(owner="chemist", campaign_id="campaign-1", key="suggest-1", revision=1, ordinal=0):
    request = _request(campaign_id, revision)
    with registry_connection(write=True) as con:
        return attempts.claim(con, owner, key, request, _context(campaign_id, revision, ordinal))


def _rejected_outcome(*, elapsed=None, message="candidate rejected"):
    return {
        "status": "rejected",
        "phase": "method_validation",
        "candidate": None,
        "backend": None,
        "candidate_generation_elapsed_seconds": elapsed,
        "failure": {"type": "candidate_rejected", "message": message},
        "proposal_id": None,
    }


def _insert_pending_proposal(con, *, owner, campaign_id, ordinal, proposal_id, candidate):
    method_id = f"method-{proposal_id}"
    con.execute(
        "INSERT INTO executable_methods VALUES (?, ?, ?, ?, ?, ?, ?)",
        (method_id, owner, "Fixture method", "{}", "method-input", "method-program", "now"),
    )
    con.execute(
        """INSERT INTO campaign_proposals (
        id, owner, campaign_id, ordinal, state, candidate_digest, executable_method_id,
        method_input_digest, canonical_program_digest, payload, input_digest, created_at
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
        (
            proposal_id,
            owner,
            campaign_id,
            ordinal,
            sha256(candidate),
            method_id,
            "method-input",
            "method-program",
            canonical({"candidate": candidate}),
            "proposal-input",
            "now",
        ),
    )


def test_claim_reserves_key_and_active_campaign_and_hides_execution_token(campaign_store):
    owner, campaign_id = campaign_store
    request = _request(campaign_id)
    with registry_connection(write=True) as con:
        created = attempts.claim(con, owner, "suggest-1", request, _context(campaign_id))
        record = created["record"]
        assert record["state"] == "claimed"
        assert record["campaign_id"] == campaign_id
        assert record["ordinal"] == 0
        assert created["execution_token"]
        assert created["execution_token"] not in canonical(record)
        found = attempts.lookup(con, owner, "suggest-1", request)
        assert found == {"kind": "attempt", **created}
        assert attempts.active(con, owner, campaign_id) == record
        with pytest.raises(ValueError, match="active suggestion attempt"):
            attempts.claim(con, owner, "suggest-2", request, _context(campaign_id))


def test_idempotency_key_is_owner_scoped_reserved_and_request_bound(campaign_store):
    owner, campaign_id = campaign_store
    request = _request(campaign_id)
    with registry_connection(write=True) as con:
        attempts.ensure_schema(con)
        _create_campaign(con, "other", "other-campaign")
        claim = attempts.claim(con, owner, "shared-key", request, _context(campaign_id))
        with pytest.raises(ValueError, match="different suggestion request"):
            attempts.lookup(con, owner, "shared-key", _request(campaign_id, revision=2))
        other = attempts.claim(
            con,
            "other",
            "shared-key",
            _request("other-campaign"),
            _context("other-campaign"),
        )
        assert other["record"]["suggestion_attempt_id"] != claim["record"]["suggestion_attempt_id"]


def test_legacy_success_receipt_is_returned_and_prevents_claim(campaign_store):
    owner, campaign_id = campaign_store
    request = _request(campaign_id)
    response = {"proposal": {"proposal_id": "old-proposal", "candidate": {"x": 0.4}}}
    with registry_connection(write=True) as con:
        con.execute(
            "INSERT INTO registry_receipts VALUES (?, ?, ?, ?, ?)",
            (owner, "legacy-key", sha256(request), canonical(response), "2026-11-01T00:00:00Z"),
        )
        assert attempts.lookup(con, owner, "legacy-key", request) == {
            "kind": "legacy",
            "response": response,
        }
        with pytest.raises(ValueError, match="legacy registry receipt"):
            attempts.claim(con, owner, "legacy-key", request, _context(campaign_id))
        with pytest.raises(ValueError, match="different registry request"):
            attempts.lookup(con, owner, "legacy-key", _request(campaign_id, revision=2))


@pytest.mark.parametrize(
    "request_data,context_data",
    [
        (_request(revision=True), _context()),
        (_request(), {**_context(), "campaign": {**_context()["campaign"], "ordinal": True}}),
        (
            _request(),
            {**_context(), "campaign": {**_context()["campaign"], "expected_revision": 2}},
        ),
        (_request(), {"campaign": _context()["campaign"], "bad": float("nan")}),
    ],
)
def test_claim_rejects_invalid_request_or_context_without_reserving_key(
    campaign_store, request_data, context_data
):
    owner, campaign_id = campaign_store
    with registry_connection(write=True) as con:
        with pytest.raises(ValueError):
            attempts.claim(con, owner, "invalid-claim", request_data, context_data)
        assert attempts.active(con, owner, campaign_id) is None
        assert attempts.lookup(con, owner, "invalid-claim", _request(campaign_id)) is None


def test_accepted_outcome_binds_proposal_and_retry_receipt_and_replays_exactly(campaign_store):
    owner, campaign_id = campaign_store
    candidate = {"strong_solvent_fraction": 0.4, "acquisition_duration_min": 5.0}
    request = _request(campaign_id)
    with registry_connection(write=True) as con:
        claim = attempts.claim(con, owner, "accepted-key", request, _context(campaign_id))
    record = claim["record"]
    outcome = {
        "status": "accepted",
        "phase": "proposal_committed",
        "candidate": candidate,
        "backend": None,
        "candidate_generation_elapsed_seconds": 0.0,
        "failure": None,
        "proposal_id": "proposal-1",
    }
    response = {
        "proposal": {"proposal_id": "proposal-1", "candidate": candidate},
        "campaign": {"campaign_id": campaign_id, "state": "proposal_pending"},
    }
    with registry_connection(write=True) as con:
        _insert_pending_proposal(
            con,
            owner=owner,
            campaign_id=campaign_id,
            ordinal=0,
            proposal_id="proposal-1",
            candidate=candidate,
        )
        accepted = attempts.finalize(
            con,
            owner,
            record["suggestion_attempt_id"],
            claim["execution_token"],
            outcome,
            response,
        )
        receipt = con.execute(
            "SELECT digest, response FROM registry_receipts WHERE owner=? AND request_key=?",
            (owner, "accepted-key"),
        ).fetchone()
        assert receipt["digest"] == sha256(request)
        assert receipt["response"] == canonical(response)
    assert accepted["state"] == "accepted"
    assert accepted["outcome"]["candidate_generation_elapsed_seconds"] == 0.0
    assert accepted["outcome"]["response_sha256"] == sha256(response)
    with registry_connection(write=True) as con:
        replayed = attempts.finalize(
            con,
            owner,
            record["suggestion_attempt_id"],
            claim["execution_token"],
            outcome,
            response,
        )
        found = attempts.lookup(con, owner, "accepted-key", request)
        assert found == {
            "kind": "attempt",
            "record": accepted,
            "execution_token": claim["execution_token"],
        }
        assert attempts.get(con, owner, record["suggestion_attempt_id"]) == accepted
    assert replayed == accepted
    with pytest.raises(ValueError, match="different terminal outcome"):
        with registry_connection(write=True) as con:
            attempts.finalize(
                con,
                owner,
                record["suggestion_attempt_id"],
                claim["execution_token"],
                {**outcome, "candidate_generation_elapsed_seconds": 1.0},
                response,
            )
    with registry_connection(write=True) as con:
        con.execute("UPDATE campaign_proposals SET state='consumed' WHERE id='proposal-1'")
        assert attempts.get(con, owner, record["suggestion_attempt_id"]) == accepted
        assert (
            attempts.finalize(
                con,
                owner,
                record["suggestion_attempt_id"],
                claim["execution_token"],
                outcome,
                response,
            )
            == accepted
        )


def test_terminal_error_replay_keeps_null_distinct_from_measured_zero(campaign_store):
    owner, campaign_id = campaign_store
    for ordinal, (key, elapsed) in enumerate(
        (("failed-unknown-time", None), ("failed-known-zero", 0.0)), start=1
    ):
        claim = _claim(key=key, revision=ordinal, ordinal=ordinal - 1)
        record = claim["record"]
        outcome = _rejected_outcome(elapsed=elapsed)
        with registry_connection(write=True) as con:
            terminal = attempts.finalize(
                con,
                owner,
                record["suggestion_attempt_id"],
                claim["execution_token"],
                outcome,
            )
            assert (
                terminal["outcome"]["candidate_generation_elapsed_seconds"]
                == outcome["candidate_generation_elapsed_seconds"]
            )
            assert terminal == attempts.finalize(
                con,
                owner,
                record["suggestion_attempt_id"],
                claim["execution_token"],
                outcome,
            )
            assert (
                con.execute(
                    "SELECT 1 FROM registry_receipts WHERE owner=? AND request_key=?",
                    (owner, key),
                ).fetchone()
                is None
            )


def test_reading_legacy_campaign_store_does_not_create_attempt_tables(campaign_store):
    owner, campaign_id = campaign_store
    with registry_connection(write=True) as con:
        assert attempts.lookup(con, owner, "legacy-key", _request(campaign_id)) is None
        assert attempts.active(con, owner, campaign_id) is None
        assert attempts.list_records(con, owner, campaign_id) == {
            "records": [],
            "next_after": None,
        }
        with pytest.raises(ValueError, match="not found or is inaccessible"):
            attempts.get(con, owner, "missing")
        table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_suggestion_attempts'"
        ).fetchone()
        assert table is None


def test_list_pages_are_bounded_and_cursors_cannot_cross_campaign_or_owner(campaign_store):
    owner, campaign_id = campaign_store
    with registry_connection(write=True) as con:
        _create_campaign(con, owner, "campaign-2")
        _create_campaign(con, "other", "other-campaign")
    terminal_records = []
    for ordinal in range(3):
        key = f"page-{ordinal}"
        claimed = _claim(key=key, revision=ordinal + 1, ordinal=ordinal)
        record = claimed["record"]
        with registry_connection(write=True) as con:
            terminal_records.append(
                attempts.finalize(
                    con,
                    owner,
                    record["suggestion_attempt_id"],
                    claimed["execution_token"],
                    _rejected_outcome(),
                )
            )
    other_campaign_claim = _claim(
        owner=owner,
        campaign_id="campaign-2",
        key="different-campaign",
    )
    foreign_claim = _claim(
        owner="other",
        campaign_id="other-campaign",
        key="foreign-campaign",
    )
    with registry_connection() as con:
        first_page = attempts.list_records(con, owner, campaign_id, limit=2)
        assert len(first_page["records"]) == 2
        assert first_page["next_after"] is not None
        second_page = attempts.list_records(
            con, owner, campaign_id, limit=2, after=first_page["next_after"]
        )
        assert len(second_page["records"]) == 1
        assert second_page["next_after"] is None
        assert [item["suggestion_attempt_id"] for item in first_page["records"]] == [
            item["suggestion_attempt_id"]
            for item in sorted(
                terminal_records,
                key=lambda item: (item["started_at"], item["suggestion_attempt_id"]),
            )[:2]
        ]
        with pytest.raises(ValueError, match="not accessible in this campaign"):
            attempts.list_records(
                con,
                owner,
                campaign_id,
                limit=2,
                after=other_campaign_claim["record"]["suggestion_attempt_id"],
            )
        with pytest.raises(ValueError, match="not accessible in this campaign"):
            attempts.list_records(
                con, "other", campaign_id, limit=2, after=first_page["next_after"]
            )
        with pytest.raises(ValueError, match="integer from 1 through 20"):
            attempts.list_records(con, owner, campaign_id, limit=21)
        with pytest.raises(ValueError, match="not found or is inaccessible"):
            attempts.get(con, owner, foreign_claim["record"]["suggestion_attempt_id"])


def test_transaction_rollback_restores_claimed_state(campaign_store):
    owner, campaign_id = campaign_store
    claimed = _claim(key="rollback-key")
    record = claimed["record"]
    with pytest.raises(RuntimeError, match="injected downstream failure"):
        with registry_connection(write=True) as con:
            attempts.finalize(
                con,
                owner,
                record["suggestion_attempt_id"],
                claimed["execution_token"],
                _rejected_outcome(elapsed=0.0),
            )
            raise RuntimeError("injected downstream failure")
    with registry_connection() as con:
        restored = attempts.get(con, owner, record["suggestion_attempt_id"])
        assert restored["state"] == "claimed"
        assert attempts.active(con, owner, campaign_id) == restored
        assert (
            con.execute(
                "SELECT 1 FROM registry_receipts WHERE owner=? AND request_key=?",
                (owner, "rollback-key"),
            ).fetchone()
            is None
        )


def test_claim_rollback_also_rolls_back_lazy_schema_creation(campaign_store):
    owner, campaign_id = campaign_store
    with pytest.raises(RuntimeError, match="abort claim transaction"):
        with registry_connection(write=True) as con:
            attempts.claim(
                con,
                owner,
                "claim-rollback",
                _request(campaign_id),
                _context(campaign_id),
            )
            raise RuntimeError("abort claim transaction")
    with registry_connection() as con:
        assert (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_suggestion_attempts'"
            ).fetchone()
            is None
        )
        assert attempts.lookup(con, owner, "claim-rollback", _request(campaign_id)) is None


def test_start_outcome_and_accepted_receipt_tampering_are_detected(campaign_store):
    owner, campaign_id = campaign_store
    with registry_connection(write=True) as con:
        _create_campaign(con, owner, "outcome-campaign")
        _create_campaign(con, owner, "receipt-campaign")
        _create_campaign(con, owner, "token-campaign")
    claimed = _claim(key="tamper-start")
    record = claimed["record"]
    with registry_connection(write=True) as con:
        row = con.execute(
            "SELECT payload FROM campaign_suggestion_attempts WHERE id=?",
            (record["suggestion_attempt_id"],),
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["context"]["training"].append({"tampered": True})
        con.execute(
            "UPDATE campaign_suggestion_attempts SET payload=? WHERE id=?",
            (canonical(payload), record["suggestion_attempt_id"]),
        )
        with pytest.raises(ValueError, match="start identity or digest"):
            attempts.get(con, owner, record["suggestion_attempt_id"])

    token_claim = _claim(campaign_id="token-campaign", key="tamper-token")
    with registry_connection(write=True) as con:
        con.execute(
            "UPDATE campaign_suggestion_attempts SET execution_token=? WHERE id=?",
            ("replacement-token", token_claim["record"]["suggestion_attempt_id"]),
        )
        with pytest.raises(ValueError, match="start identity or digest"):
            attempts.get(con, owner, token_claim["record"]["suggestion_attempt_id"])

    rejected = _claim(campaign_id="outcome-campaign", key="tamper-outcome")
    with registry_connection(write=True) as con:
        terminal = attempts.finalize(
            con,
            owner,
            rejected["record"]["suggestion_attempt_id"],
            rejected["execution_token"],
            _rejected_outcome(),
        )
        row = con.execute(
            "SELECT payload FROM campaign_suggestion_attempt_outcomes WHERE suggestion_attempt_id=?",
            (terminal["suggestion_attempt_id"],),
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["outcome"]["phase"] = "tampered"
        con.execute(
            "UPDATE campaign_suggestion_attempt_outcomes SET payload=? WHERE suggestion_attempt_id=?",
            (canonical(payload), terminal["suggestion_attempt_id"]),
        )
        with pytest.raises(ValueError, match="outcome identity or digest"):
            attempts.get(con, owner, terminal["suggestion_attempt_id"])

    accepted = _claim(campaign_id="receipt-campaign", key="tamper-receipt")
    candidate = {"x": 0.5}
    response = {"proposal": {"proposal_id": "proposal-receipt", "candidate": candidate}}
    outcome = _accepted_outcome(proposal_id="proposal-receipt", candidate=candidate)
    with registry_connection(write=True) as con:
        _insert_pending_proposal(
            con,
            owner=owner,
            campaign_id="receipt-campaign",
            ordinal=0,
            proposal_id="proposal-receipt",
            candidate=candidate,
        )
        terminal = attempts.finalize(
            con,
            owner,
            accepted["record"]["suggestion_attempt_id"],
            accepted["execution_token"],
            outcome,
            response,
        )
        con.execute(
            "UPDATE registry_receipts SET response=? WHERE owner=? AND request_key=?",
            (
                canonical({"proposal": {"proposal_id": "other", "candidate": candidate}}),
                owner,
                "tamper-receipt",
            ),
        )
        with pytest.raises(ValueError, match="does not match its outcome digest"):
            attempts.get(con, owner, terminal["suggestion_attempt_id"])


def test_abandon_fences_claim_and_accepts_one_immutable_late_result(campaign_store):
    owner, campaign_id = campaign_store
    request = _request(campaign_id)
    with registry_connection(write=True) as con:
        claim = attempts.claim(con, owner, "suggest-abandon", request, _context(campaign_id))
        attempt_id = claim["record"]["suggestion_attempt_id"]
        token = claim["execution_token"]
        abandoned = attempts.abandon(
            con, owner, attempt_id, claim["record"]["start_sha256"], "worker became unreachable"
        )
        assert abandoned["state"] == "abandoned_unknown"
        assert abandoned["outcome"]["candidate_generation_elapsed_seconds"] is None
        assert abandoned["late_result"] is None
        with pytest.raises(ValueError, match="original execution token"):
            attempts.record_late_result(
                con, owner, attempt_id, "wrong-token", {"candidate": {"x": 0.5}}
            )
        with pytest.raises(ValueError, match="active execution token"):
            attempts.finalize(con, owner, attempt_id, "wrong-token", _accepted_outcome(), None)
        with pytest.raises(ValueError, match="only as a late result"):
            attempts.finalize(con, owner, attempt_id, token, _accepted_outcome(), None)
        late = attempts.record_late_result(
            con, owner, attempt_id, token, {"candidate": {"x": 0.5}, "elapsed": 0.0}
        )
        assert late["state"] == "abandoned_unknown"
        assert late["late_result"]["result"]["elapsed"] == 0.0
        assert (
            attempts.record_late_result(
                con, owner, attempt_id, token, {"candidate": {"x": 0.5}, "elapsed": 0.0}
            )
            == late
        )
        with pytest.raises(ValueError, match="different late result"):
            attempts.record_late_result(
                con, owner, attempt_id, token, {"candidate": {"x": 0.7}, "elapsed": 0.0}
            )
        final = attempts.get(con, owner, attempt_id)
        assert final["state"] == "abandoned_unknown"
        assert final["outcome"] == abandoned["outcome"]
        assert final["outcome_sha256"] == abandoned["outcome_sha256"]
        assert final["late_result"] == late["late_result"]
        assert (
            con.execute(
                "SELECT 1 FROM registry_receipts WHERE owner=? AND request_key=?",
                (owner, "suggest-abandon"),
            ).fetchone()
            is None
        )
        late_row = con.execute(
            "SELECT payload FROM campaign_suggestion_attempt_late_results WHERE suggestion_attempt_id=?",
            (attempt_id,),
        ).fetchone()
        late_payload = json.loads(late_row["payload"])
        late_payload["result"]["elapsed"] = 1.0
        con.execute(
            "UPDATE campaign_suggestion_attempt_late_results SET payload=? WHERE suggestion_attempt_id=?",
            (canonical(late_payload), attempt_id),
        )
        with pytest.raises(ValueError, match="late suggestion evidence identity or digest"):
            attempts.get(con, owner, attempt_id)


def _accepted_outcome(proposal_id="proposal-1", elapsed=0.0, candidate=None):
    return {
        "status": "accepted",
        "phase": "complete",
        "candidate": {"x": 0.5} if candidate is None else candidate,
        "backend": None,
        "candidate_generation_elapsed_seconds": elapsed,
        "failure": None,
        "proposal_id": proposal_id,
    }
