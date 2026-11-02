"""Snapshot-scoped proposal verification during complete campaign replay."""

from __future__ import annotations

import inspect
import json
from collections import Counter

import pytest
from test_campaigns import _prepared, _record_snapshot

from measure_service import campaign_protocol, campaign_protocol_registry, campaigns
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
        prepared["owner"], f"History cache fixture {key}", spec, f"{key}-replication-protocol"
    )
    return spec, protocol


def _record_evaluation(prepared, campaign, ordinal: int, key: str):
    owner = prepared["owner"]
    proposal_response = campaigns.suggest_campaign(
        owner,
        campaign["campaign_id"],
        campaign["revision"],
        f"{key}-suggest",
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
    return consumed["campaign"], proposal


def test_complete_history_replay_shares_source_checks_but_rechecks_training(monkeypatch):
    evaluation_budget = 9
    prepared = _prepared(key="history-cache", evaluation_budget=evaluation_budget)
    spec, protocol = _registered_replication_protocol(prepared, "history-cache")
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        protocol["protocol_id"],
        "two-analytes",
        17,
        "random",
        "history-cache-campaign",
    )

    proposals = []
    original_random = campaign_protocol.random_candidate
    repeated_candidate = None

    def fixed_synthetic_repeat(candidate_spec, seed, ordinal):
        if ordinal >= spec.initial_design_size:
            assert repeated_candidate is not None
            return dict(repeated_candidate)
        return original_random(candidate_spec, seed, ordinal)

    monkeypatch.setattr(campaign_protocol, "random_candidate", fixed_synthetic_repeat)
    for ordinal in range(spec.evaluation_budget):
        campaign, proposal = _record_evaluation(
            prepared, campaign, ordinal, f"history-cache-evaluation-{ordinal}"
        )
        proposals.append(proposal)
        if ordinal == 0:
            repeated_candidate = proposal["candidate"]

    method_ids = [proposal["method"]["method_id"] for proposal in proposals]
    assert len(set(method_ids)) == evaluation_budget

    proposal_method_checks: Counter[str] = Counter()
    training_method_checks: Counter[str] = Counter()
    source_method_checks: Counter[str] = Counter()
    proposal_method_record_reads: Counter[str] = Counter()
    observation_source_checks: Counter[str] = Counter()

    original_owned_method = campaigns.method_registry._owned_method
    original_method_record = campaigns.method_registry._record
    original_verify_observation = campaigns._verify_campaign_observation

    def count_method_lookup(con, lookup_owner, method_id):
        frame = inspect.currentframe().f_back
        if frame is not None and frame.f_globals.get("__name__") == "measure_service.campaigns":
            caller = frame.f_code.co_name
            if caller == "_proposal_response":
                proposal_method_checks[method_id] += 1
            elif caller == "_training_context":
                training_method_checks[method_id] += 1
            elif caller == "_verify_campaign_observation":
                source_method_checks[method_id] += 1
        del frame
        return original_owned_method(con, lookup_owner, method_id)

    def count_method_record(row):
        frame = inspect.currentframe().f_back
        if (
            frame is not None
            and frame.f_globals.get("__name__") == "measure_service.campaigns"
            and frame.f_code.co_name == "_proposal_response"
        ):
            proposal_method_record_reads[row["id"]] += 1
        del frame
        return original_method_record(row)

    def count_observation_source(
        con, source_owner, task, proposal_row, observation_row, observation_response
    ):
        observation_source_checks[proposal_row["id"]] += 1
        return original_verify_observation(
            con, source_owner, task, proposal_row, observation_row, observation_response
        )

    monkeypatch.setattr(campaigns.method_registry, "_owned_method", count_method_lookup)
    monkeypatch.setattr(campaigns.method_registry, "_record", count_method_record)
    monkeypatch.setattr(campaigns, "_verify_campaign_observation", count_observation_source)

    replayed = campaigns.get_campaign(owner, campaign["campaign_id"])

    proposal_ids = {proposal["proposal_id"] for proposal in proposals}
    assert replayed["state"] == "budget_complete"
    assert replayed["consumed_evaluations"] == evaluation_budget
    assert set(observation_source_checks) == proposal_ids
    assert all(observation_source_checks[proposal_id] == 1 for proposal_id in proposal_ids)
    one_check_per_method = Counter({method_id: 1 for method_id in method_ids})
    assert proposal_method_checks == one_check_per_method
    assert proposal_method_record_reads == one_check_per_method
    assert training_method_checks == one_check_per_method
    assert source_method_checks == one_check_per_method

    # Change one proposal's declared prefix without changing its source hash.
    # A new read must still rebuild the ordered training prefix after its history
    # loop has cached the proposal's source-validated response.
    target = proposals[5]
    with registry_connection(write=True) as con:
        row = con.execute(
            "SELECT payload FROM campaign_proposals WHERE id=? AND owner=?",
            (target["proposal_id"], owner),
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["training"]["count"] += 1
        content = {key: value for key, value in payload.items() if key != "record_sha256"}
        payload["record_sha256"] = sha256(content)
        con.execute(
            "UPDATE campaign_proposals SET payload=? WHERE id=? AND owner=?",
            (canonical(payload), target["proposal_id"], owner),
        )

    with pytest.raises(
        ValueError, match="Stored proposal training inputs differ from consumed snapshots"
    ):
        campaigns.get_campaign(owner, campaign["campaign_id"])
