"""Duplicate admission and replay for the explicit replication protocol."""

from __future__ import annotations

import json
import uuid

import pytest
from test_campaigns import _prepared, _record_snapshot

from measure_service import campaign_protocol, campaign_protocol_registry, campaigns
from measure_service.storage import canonical, now, registry_connection, sha256


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MEASURE_BO_PYTHON", raising=False)
    return tmp_path


def _register_profile(prepared, *, profile, key, duplicate_policy=None):
    data = prepared["spec"].model_dump(mode="json")
    data.update(
        {
            "profile": profile,
            "optimizer_target": "confirmed_acceptance_reward",
            "surrogate_noise_prior": "default",
        }
    )
    if duplicate_policy is not None:
        data["duplicate_policy"] = duplicate_policy
    spec = campaign_protocol.parse_campaign_protocol(data)
    protocol = campaign_protocol_registry.register_campaign_protocol(
        prepared["owner"], f"Replication admission fixture {key}", spec, f"{key}-a2-protocol"
    )
    return spec, protocol


def _campaign_with_initial_design(prepared, protocol, key, *, policy="random"):
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        protocol["protocol_id"],
        "two-analytes",
        17,
        policy,
        f"{key}-campaign",
    )
    proposals = []
    for ordinal in range(prepared["spec"].initial_design_size):
        suggested = campaigns.suggest_campaign(
            owner,
            campaign["campaign_id"],
            campaign["revision"],
            f"{key}-suggest-{ordinal}",
        )
        proposal = suggested["proposal"]
        evidence = _record_snapshot(prepared, proposal, key=f"{key}-evidence-{ordinal}")
        consumed = campaigns.consume_campaign_snapshot(
            owner,
            campaign["campaign_id"],
            proposal["proposal_id"],
            evidence[-1]["snapshot_id"],
            suggested["revision"],
            f"{key}-consume-{ordinal}",
        )
        proposals.append(proposal)
        campaign = consumed["campaign"]
    return campaign, proposals


def test_replicate_policy_records_repeat_and_retains_repeated_training(monkeypatch):
    prepared = _prepared(key="replicate-training", evaluation_budget=5)
    spec, protocol = _register_profile(
        prepared,
        profile=campaign_protocol.REPLICATION_CAMPAIGN_PROFILE,
        duplicate_policy="replicate",
        key="replicate-training",
    )
    campaign, initial = _campaign_with_initial_design(prepared, protocol, "replicate-training")
    original_random_candidate = campaign_protocol.random_candidate
    repeated_candidate = initial[0]["candidate"]

    def repeated_random_candidate(candidate_spec, seed, ordinal):
        if ordinal == spec.initial_design_size:
            return dict(repeated_candidate)
        return original_random_candidate(candidate_spec, seed, ordinal)

    monkeypatch.setattr(campaign_protocol, "random_candidate", repeated_random_candidate)
    owner = prepared["owner"]
    with registry_connection() as con:
        prior_experiment_id = con.execute(
            """SELECT o.experiment_id FROM campaign_consumptions AS c
            JOIN analytical_observations AS o ON o.id=c.observation_id
            WHERE c.campaign_id=? AND c.proposal_id=?""",
            (campaign["campaign_id"], initial[0]["proposal_id"]),
        ).fetchone()[0]
    suggested = campaigns.suggest_campaign(
        owner,
        campaign["campaign_id"],
        campaign["revision"],
        "replicate-training-suggest-repeat",
    )
    repeated = suggested["proposal"]
    assert repeated["candidate"] == repeated_candidate
    assert repeated["proposal_id"] != initial[0]["proposal_id"]
    assert repeated["method"]["method_id"] != initial[0]["method"]["method_id"]
    assert repeated["repetition"] == {
        "policy": "replicate",
        "is_repeat": True,
        "first_proposal_id": initial[0]["proposal_id"],
        "matching_prior_proposals": [{"proposal_id": initial[0]["proposal_id"], "ordinal": 0}],
    }

    attempts = campaigns.list_campaign_suggestion_attempts(owner, campaign["campaign_id"])[
        "records"
    ]
    repeated_attempt = next(
        campaigns.get_campaign_suggestion_attempt(owner, item["suggestion_attempt_id"])
        for item in attempts
        if item["ordinal"] == spec.initial_design_size
    )
    assert repeated_attempt["context"]["duplicate_policy"] == "replicate"

    evidence = _record_snapshot(prepared, repeated, key="replicate-training-repeat")
    assert evidence[0]["experiment_id"] != prior_experiment_id
    consumed = campaigns.consume_campaign_snapshot(
        owner,
        campaign["campaign_id"],
        repeated["proposal_id"],
        evidence[-1]["snapshot_id"],
        suggested["revision"],
        "replicate-training-consume-repeat",
    )
    assert consumed["consumption"]["proposal_id"] == repeated["proposal_id"]
    assert (
        campaigns.suggest_campaign(
            owner,
            campaign["campaign_id"],
            suggested["revision"] - 1,
            "replicate-training-suggest-repeat",
        )
        == suggested
    )

    with registry_connection() as con:
        training, members = campaigns._training_context(con, owner, campaign["campaign_id"])
    assert len(members) == 4
    assert len(training) == 4
    assert training[0]["candidate"] == training[-1]["candidate"]
    assert members[-1]["ordinal"] == 3
    assert campaigns.get_campaign(owner, campaign["campaign_id"])["consumed_evaluations"] == 4


def test_reject_policy_keeps_duplicate_candidate_as_failed_attempt(monkeypatch):
    prepared = _prepared(key="replicate-reject", evaluation_budget=5)
    spec, protocol = _register_profile(
        prepared,
        profile=campaign_protocol.REPLICATION_CAMPAIGN_PROFILE,
        duplicate_policy="reject",
        key="replicate-reject",
    )
    campaign, initial = _campaign_with_initial_design(prepared, protocol, "replicate-reject")
    repeated_candidate = initial[0]["candidate"]
    original_random_candidate = campaign_protocol.random_candidate
    monkeypatch.setattr(
        campaign_protocol,
        "random_candidate",
        lambda candidate_spec, seed, ordinal: (
            dict(repeated_candidate)
            if ordinal == spec.initial_design_size
            else original_random_candidate(candidate_spec, seed, ordinal)
        ),
    )
    owner = prepared["owner"]
    with registry_connection() as con:
        methods_before = con.execute(
            "SELECT COUNT(*) FROM executable_methods WHERE owner=?", (owner,)
        ).fetchone()[0]

    with pytest.raises(ValueError, match="Campaign candidate duplicates a prior proposal") as error:
        campaigns.suggest_campaign(
            owner,
            campaign["campaign_id"],
            campaign["revision"],
            "replicate-reject-duplicate",
        )
    attempt = campaigns.get_campaign_suggestion_attempt(owner, error.value.suggestion_attempt_id)
    assert attempt["state"] == "rejected"
    assert attempt["context"]["duplicate_policy"] == "reject"
    assert attempt["outcome"]["candidate"] == repeated_candidate
    with registry_connection() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM campaign_proposals WHERE campaign_id=?",
            (campaign["campaign_id"],),
        ).fetchone()[0]
        methods_after = con.execute(
            "SELECT COUNT(*) FROM executable_methods WHERE owner=?", (owner,)
        ).fetchone()[0]
    assert count == spec.initial_design_size
    assert methods_after == methods_before


def test_ablation_v2_request_binding_accepts_replication_v3_without_running_bo():
    prepared = _prepared(key="replication-request-binding", evaluation_budget=5)
    spec, protocol = _register_profile(
        prepared,
        profile=campaign_protocol.REPLICATION_CAMPAIGN_PROFILE,
        duplicate_policy="replicate",
        key="replication-request-binding",
    )
    campaign, _ = _campaign_with_initial_design(
        prepared,
        protocol,
        "replication-request-binding",
        policy="bo_mcp_gp_logei",
    )
    with registry_connection() as con:
        row = campaigns._campaign_row(con, prepared["owner"], campaign["campaign_id"])
        payload = campaigns._verify_campaign_payload(row)
        protocol_response, links = campaigns._task_for_campaign(
            con, prepared["owner"], row, payload
        )
        training, members = campaigns._training_context(
            con, prepared["owner"], campaign["campaign_id"], before_ordinal=3
        )
        context = campaigns._suggestion_context(row, protocol_response, spec, 3, training, members)

    request = context["generation"]["request"]
    assert context["generation"]["request_status"] == "prepared"
    assert request["spec"]["profile"] == campaign_protocol.REPLICATION_CAMPAIGN_PROFILE
    assert request["spec"]["duplicate_policy"] == "replicate"
    assert request["protocol_sha256"] == campaign_protocol.campaign_protocol_sha256(spec)
    campaigns._verify_suggestion_request_identity(
        {"context": context, "outcome": None, "late_result": None},
        links["spec"],
        training,
        17,
    )


@pytest.mark.parametrize("profile_kind", ["paired", "ablation", "replication-reject"])
def test_duplicate_legacy_or_reject_rows_are_refused_without_training_replay(
    profile_kind, monkeypatch
):
    prepared = _prepared(key=f"duplicate-read-{profile_kind}", evaluation_budget=4)
    if profile_kind == "paired":
        protocol = prepared["protocol"]
    else:
        profile = (
            campaign_protocol.ABLATION_CAMPAIGN_PROFILE
            if profile_kind == "ablation"
            else campaign_protocol.REPLICATION_CAMPAIGN_PROFILE
        )
        _, protocol = _register_profile(
            prepared,
            profile=profile,
            duplicate_policy="reject" if profile_kind == "replication-reject" else None,
            key=f"duplicate-read-{profile_kind}",
        )
    campaign, initial = _campaign_with_initial_design(
        prepared, protocol, f"duplicate-read-{profile_kind}"
    )
    if profile_kind == "replication-reject":
        assert all("repetition" in proposal for proposal in initial)
    else:
        assert all("repetition" not in proposal for proposal in initial)
    repeated_candidate = initial[0]["candidate"]
    original_random_candidate = campaign_protocol.random_candidate
    monkeypatch.setattr(
        campaign_protocol,
        "random_candidate",
        lambda candidate_spec, seed, ordinal: (
            dict(repeated_candidate)
            if ordinal == prepared["spec"].initial_design_size
            else original_random_candidate(candidate_spec, seed, ordinal)
        ),
    )

    with registry_connection(write=True) as con:
        row = campaigns._campaign_row(con, prepared["owner"], campaign["campaign_id"])
        campaign_payload = campaigns._verify_campaign_payload(row)
        protocol_response, links = campaigns._task_for_campaign(
            con, prepared["owner"], row, campaign_payload
        )
        # A distinct valid method isolates duplicate-policy replay from the
        # independent invariant forbidding method identity reuse.
        method = campaigns._insert_campaign_method(
            con,
            prepared["owner"],
            campaign["campaign_id"],
            prepared["spec"].initial_design_size,
            campaign_protocol.method_declaration(links["spec"], repeated_candidate),
            links["spec"].constraints,
        )
        request = {
            "operation": "suggest_campaign",
            "campaign_id": campaign["campaign_id"],
            "expected_revision": campaign["revision"],
        }
        training_sha256 = sha256([])
        payload = {
            "schema_version": campaigns.SCHEMA_VERSION,
            "request": request,
            "proposal_input": {
                "protocol_input_sha256": protocol_response["input_sha256"],
                "campaign_id": campaign["campaign_id"],
                "seed": 17,
                "policy": "random",
                "ordinal": prepared["spec"].initial_design_size,
                "candidate": repeated_candidate,
                "training_sha256": training_sha256,
                "backend": None,
            },
            "candidate": repeated_candidate,
            "policy": "random",
            "proposal_source": "random_policy",
            "training": {"count": 0, "sha256": training_sha256, "snapshot_members": []},
            "backend": None,
            "method": {
                "method_id": method["method_id"],
                "input_sha256": method["input_sha256"],
                "canonical_program_sha256": method["canonical_program_sha256"],
            },
        }
        if isinstance(links["spec"], campaign_protocol.ReplicationProtocolSpec):
            payload["repetition"] = {
                "policy": "reject",
                "is_repeat": True,
                "first_proposal_id": initial[0]["proposal_id"],
                "matching_prior_proposals": [
                    {"proposal_id": initial[0]["proposal_id"], "ordinal": 0}
                ],
            }
        payload["record_sha256"] = sha256(payload)
        proposal_id = str(uuid.uuid4())
        con.execute(
            """INSERT INTO campaign_proposals (
              id, owner, campaign_id, ordinal, state, candidate_digest,
              executable_method_id, method_input_digest, canonical_program_digest,
              payload, input_digest, created_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
            (
                proposal_id,
                prepared["owner"],
                campaign["campaign_id"],
                prepared["spec"].initial_design_size,
                sha256(repeated_candidate),
                method["method_id"],
                method["input_sha256"],
                method["canonical_program_sha256"],
                canonical(payload),
                sha256(request),
                now(),
            ),
        )
        duplicate_row = campaigns._proposal_row(con, prepared["owner"], proposal_id)

    with registry_connection() as con:
        with pytest.raises(ValueError, match="Campaign candidate duplicates a prior proposal"):
            campaigns._proposal_response(con, duplicate_row, verify_training=False)


def test_replication_proposal_rejects_resealed_reuse_of_an_earlier_method(monkeypatch):
    prepared = _prepared(key="reused-method", evaluation_budget=5)
    _, protocol = _register_profile(
        prepared,
        profile=campaign_protocol.REPLICATION_CAMPAIGN_PROFILE,
        duplicate_policy="replicate",
        key="reused-method",
    )
    campaign, initial = _campaign_with_initial_design(prepared, protocol, "reused-method")
    monkeypatch.setattr(
        campaign_protocol, "random_candidate", lambda *_: dict(initial[0]["candidate"])
    )
    suggested = campaigns.suggest_campaign(
        prepared["owner"], campaign["campaign_id"], campaign["revision"], "reused-method-repeat"
    )
    proposal_id = suggested["proposal"]["proposal_id"]
    earlier_method = initial[0]["method"]
    method = {
        key: earlier_method[key]
        for key in ("method_id", "input_sha256", "canonical_program_sha256")
    }
    with registry_connection(write=True) as con:
        row = campaigns._proposal_row(con, prepared["owner"], proposal_id)
        payload = json.loads(row["payload"])
        payload["method"] = method
        payload["record_sha256"] = sha256(
            {key: value for key, value in payload.items() if key != "record_sha256"}
        )
        con.execute(
            """UPDATE campaign_proposals SET executable_method_id=?, method_input_digest=?,
            canonical_program_digest=?, payload=? WHERE id=?""",
            (
                method["method_id"],
                method["input_sha256"],
                method["canonical_program_sha256"],
                canonical(payload),
                proposal_id,
            ),
        )
    with registry_connection() as con:
        row = campaigns._proposal_row(con, prepared["owner"], proposal_id)
        with pytest.raises(ValueError, match="distinct executable methods"):
            campaigns._proposal_response(con, row, verify_training=False)
    with pytest.raises(ValueError, match="distinct executable methods"):
        campaigns.get_campaign_proposal(prepared["owner"], proposal_id)


def test_replication_consumption_rejects_reused_experiment_on_write_and_read(monkeypatch):
    prepared = _prepared(key="replication-fresh-experiment", evaluation_budget=5)
    _, protocol = _register_profile(
        prepared,
        profile=campaign_protocol.REPLICATION_CAMPAIGN_PROFILE,
        duplicate_policy="replicate",
        key="replication-fresh-experiment",
    )
    campaign, initial = _campaign_with_initial_design(
        prepared, protocol, "replication-fresh-experiment"
    )
    # Use a real repeated candidate and retain the complete producer evidence for both rows.
    repeated_candidate = initial[0]["candidate"]
    original_random_candidate = campaign_protocol.random_candidate
    monkeypatch.setattr(
        campaign_protocol,
        "random_candidate",
        lambda spec, seed, ordinal: (
            dict(repeated_candidate)
            if ordinal == prepared["spec"].initial_design_size
            else original_random_candidate(spec, seed, ordinal)
        ),
    )
    owner = prepared["owner"]
    suggested = campaigns.suggest_campaign(
        owner,
        campaign["campaign_id"],
        campaign["revision"],
        "replication-fresh-experiment-suggest",
    )
    repeat = suggested["proposal"]
    evidence = _record_snapshot(prepared, repeat, key="replication-fresh-experiment-repeat")
    with registry_connection() as con:
        prior_experiment_id = con.execute(
            """SELECT o.experiment_id FROM campaign_consumptions AS c
            JOIN analytical_observations AS o ON o.id=c.observation_id
            WHERE c.campaign_id=? AND c.proposal_id=?""",
            (campaign["campaign_id"], initial[0]["proposal_id"]),
        ).fetchone()[0]
    with registry_connection(write=True) as con:
        con.execute(
            "UPDATE analytical_observations SET experiment_id=? WHERE id=?",
            (prior_experiment_id, evidence[-2]["observation_id"]),
        )
    with pytest.raises(ValueError, match="fresh experiment"):
        campaigns.consume_campaign_snapshot(
            owner,
            campaign["campaign_id"],
            repeat["proposal_id"],
            evidence[-1]["snapshot_id"],
            suggested["revision"],
            "replication-fresh-experiment-reused-write",
        )

    with registry_connection(write=True) as con:
        con.execute(
            "UPDATE analytical_observations SET experiment_id=? WHERE id=?",
            (evidence[0]["experiment_id"], evidence[-2]["observation_id"]),
        )
    consumed = campaigns.consume_campaign_snapshot(
        owner,
        campaign["campaign_id"],
        repeat["proposal_id"],
        evidence[-1]["snapshot_id"],
        suggested["revision"],
        "replication-fresh-experiment-valid-write",
    )
    with registry_connection(write=True) as con:
        con.execute(
            "UPDATE analytical_observations SET experiment_id=? WHERE id=?",
            (prior_experiment_id, evidence[-2]["observation_id"]),
        )
    with pytest.raises(ValueError, match="fresh experiment"):
        campaigns.get_campaign_consumption(owner, consumed["consumption"]["consumption_id"])
