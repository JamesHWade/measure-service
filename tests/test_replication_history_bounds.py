"""Bounded prior-proposal validation for repeated synthetic campaign candidates."""

from __future__ import annotations

import json
from collections import Counter

import pytest
from test_campaigns import _prepared, _record_snapshot

from measure_service import (
    campaign_protocol,
    campaign_protocol_registry,
    campaign_suggestion_attempts,
    campaigns,
)
from measure_service.storage import canonical, registry_connection, sha256


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MEASURE_BO_PYTHON", raising=False)


def _registered_replication_protocol(prepared, key: str):
    spec_data = prepared["spec"].model_dump(mode="json")
    spec_data.update(
        {
            "profile": campaign_protocol.REPLICATION_CAMPAIGN_PROFILE,
            "optimizer_target": "confirmed_acceptance_reward",
            "surrogate_noise_prior": "default",
            "duplicate_policy": "replicate",
        }
    )
    spec = campaign_protocol.parse_campaign_protocol(spec_data)
    protocol = campaign_protocol_registry.register_campaign_protocol(
        prepared["owner"], f"Repeated-history fixture {key}", spec, f"{key}-a2-protocol"
    )
    return spec, protocol


def _record_evaluation(prepared, campaign, ordinal: int, key: str):
    owner = prepared["owner"]
    expected_revision = campaign["revision"]
    suggestion_key = f"{key}-suggest"
    proposal_response = campaigns.suggest_campaign(
        owner,
        campaign["campaign_id"],
        expected_revision,
        suggestion_key,
    )
    proposal = proposal_response["proposal"]
    evidence = _record_snapshot(prepared, proposal, key=key)
    consumed = campaigns.consume_campaign_snapshot(
        owner,
        campaign["campaign_id"],
        proposal["proposal_id"],
        evidence[-1]["snapshot_id"],
        proposal_response["revision"],
        f"{key}-consume",
    )
    assert proposal["ordinal"] == ordinal
    return (
        consumed["campaign"],
        proposal,
        {
            "suggestion_expected_revision": expected_revision,
            "suggestion_key": suggestion_key,
            "suggestion_response": proposal_response,
            "consumption_key": f"{key}-consume",
            "consumption_expected_revision": proposal_response["revision"],
            "consumption_response": consumed,
            "snapshot_id": evidence[-1]["snapshot_id"],
        },
    )


def test_long_repeat_lineage_is_validated_once_per_prior_source_and_rejects_bool_tamper(
    monkeypatch,
):
    repeat_count = 47
    prepared = _prepared(key="history-bounds", evaluation_budget=50)
    spec, protocol = _registered_replication_protocol(prepared, "history-bounds")
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        protocol["protocol_id"],
        "two-analytes",
        17,
        "random",
        "history-bounds-campaign",
    )

    proposals = []
    final_receipts = None
    for ordinal in range(spec.initial_design_size):
        campaign, proposal, final_receipts = _record_evaluation(
            prepared, campaign, ordinal, f"history-bounds-initial-{ordinal}"
        )
        proposals.append(proposal)

    repeated_candidate = proposals[0]["candidate"]
    random_candidate = campaign_protocol.random_candidate

    def fixed_synthetic_repeat(candidate_spec, seed, ordinal):
        if ordinal >= spec.initial_design_size:
            return dict(repeated_candidate)
        return random_candidate(candidate_spec, seed, ordinal)

    monkeypatch.setattr(campaign_protocol, "random_candidate", fixed_synthetic_repeat)
    for ordinal in range(spec.initial_design_size, spec.initial_design_size + repeat_count):
        campaign, proposal, final_receipts = _record_evaluation(
            prepared, campaign, ordinal, f"history-bounds-repeat-{ordinal}"
        )
        proposals.append(proposal)

    current_id = proposals[-1]["proposal_id"]
    current_ordinal = proposals[-1]["ordinal"]
    expected_lineage = [
        {"proposal_id": proposal["proposal_id"], "ordinal": proposal["ordinal"]}
        for proposal in proposals
        if proposal["ordinal"] < current_ordinal and proposal["candidate"] == repeated_candidate
    ]
    assert len(proposals) == 50
    assert campaign["state"] == "budget_complete"
    assert campaign["consumed_evaluations"] == 50
    assert len(canonical(campaign).encode("utf-8")) <= campaigns.MAX_RECORD_BYTES
    assert campaign["history"][-1]["repetition"] == {
        "policy": "replicate",
        "is_repeat": True,
        "first_proposal_id": expected_lineage[0]["proposal_id"],
        "matching_prior_proposal_count": repeat_count,
    }
    assert all(proposal["backend"] is None for proposal in proposals)
    public_proposal = campaigns.get_campaign_proposal(owner, current_id)
    assert public_proposal["repetition"]["matching_prior_proposals"] == expected_lineage

    suggestion_size = len(canonical(final_receipts["suggestion_response"]).encode("utf-8"))
    assert (
        campaigns.MAX_RECORD_BYTES
        < suggestion_size
        <= (campaign_suggestion_attempts.MAX_SUCCESS_RESPONSE_BYTES)
    )
    replayed_suggestion = campaigns.suggest_campaign(
        owner,
        campaign["campaign_id"],
        final_receipts["suggestion_expected_revision"],
        final_receipts["suggestion_key"],
    )
    assert canonical(replayed_suggestion) == canonical(final_receipts["suggestion_response"])
    replayed_consumption = campaigns.consume_campaign_snapshot(
        owner,
        campaign["campaign_id"],
        current_id,
        final_receipts["snapshot_id"],
        final_receipts["consumption_expected_revision"],
        final_receipts["consumption_key"],
    )
    assert canonical(replayed_consumption) == canonical(final_receipts["consumption_response"])

    expected_ids = {item["proposal_id"] for item in expected_lineage} | {current_id}
    calls: Counter[str] = Counter()
    original_response = campaigns._proposal_response

    def count_source_checks(con, row, **kwargs):
        calls[row["id"]] += 1
        return original_response(con, row, **kwargs)

    monkeypatch.setattr(campaigns, "_proposal_response", count_source_checks)
    with registry_connection() as con:
        current_row = campaigns._proposal_row(con, owner, current_id)
        checked = campaigns._proposal_response(con, current_row, verify_training=False)

    assert checked["repetition"]["matching_prior_proposals"] == expected_lineage
    assert checked["repetition"]["first_proposal_id"] == expected_lineage[0]["proposal_id"]
    assert len(expected_lineage) == repeat_count
    assert set(calls) == expected_ids
    assert all(calls[proposal_id] == 1 for proposal_id in expected_ids)
    assert max(calls.values()) == 1

    with registry_connection(write=True) as con:
        row = campaigns._proposal_row(con, owner, current_id)
        payload = json.loads(row["payload"])
        payload["repetition"]["matching_prior_proposals"][0]["ordinal"] = False
        without_record_digest = {
            key: value for key, value in payload.items() if key != "record_sha256"
        }
        payload["record_sha256"] = sha256(without_record_digest)
        con.execute(
            "UPDATE campaign_proposals SET payload=? WHERE id=? AND owner=?",
            (canonical(payload), current_id, owner),
        )

    with registry_connection() as con:
        tampered_row = campaigns._proposal_row(con, owner, current_id)
        with pytest.raises(ValueError, match="repetition evidence differs from prior proposals"):
            campaigns._proposal_response(con, tampered_row, verify_training=False)
