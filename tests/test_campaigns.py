"""Durable matched-campaign transitions and exact synthetic evidence use."""

from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from measure_methods.gaussian_observations import RESOLUTION_DEFINITION
from measure_methods.moses_objective import moses_observation_score
from measure_service import (
    campaign_protocol,
    campaign_protocol_registry,
    campaigns,
    experiments,
    gaussian_producer,
    method_registry,
    observations,
    retention,
)
from measure_service.storage import canonical, registry_connection


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    return tmp_path


def _prepared(
    *,
    owner="chemist",
    key="fixture",
    evaluation_budget=5,
    analyte_count=2,
    retention_ln_k0=None,
    retention_ln_k0_step=0.05,
    width_min=0.02,
):
    analyte_ids = (
        ["A", "B"]
        if analyte_count == 2
        else [f"Analyte-{index:02d}" for index in range(analyte_count)]
    )
    model = retention.register_retention_model(
        owner,
        "Two-analyte synthetic reference",
        {
            "gradient_coordinate": "column_inlet_at_analyte_entry",
            "analytes": [
                {
                    "analyte_id": analyte_id,
                    "model": "linear",
                    "ln_k0": (
                        math.log(2.0 + index * 0.05)
                        if retention_ln_k0 is None
                        else retention_ln_k0 + index * retention_ln_k0_step
                    ),
                    "S1": 0.0,
                }
                for index, analyte_id in enumerate(analyte_ids)
            ],
            "column_length_m": 0.1,
            "effective_area_m2": 1e-6,
            "post_column_volume_ml": 0.0,
            "strong_solvent": "B",
            "parameter_origin": "synthetic",
            "parameter_reference": "Independently specified synthetic coefficients",
        },
        f"{key}-model",
    )
    producer = gaussian_producer.register_gaussian_producer(
        owner,
        "Two-analyte synthetic centroid producer",
        model["model_id"],
        {
            "widths": [
                {"analyte_id": analyte_id, "fwhm_min": width_min} for analyte_id in analyte_ids
            ],
            "width_origin": "synthetic_assumption",
            "width_reference": "Independently specified synthetic widths",
        },
        f"{key}-producer",
    )
    contract = observations.register_observation_contract(
        owner,
        "Two-analyte observation contract",
        {
            "required_analytes": analyte_ids,
            "resolution_definition": RESOLUTION_DEFINITION,
            "max_acquisition_duration_min": 10.0,
            "runtime_preference_min": 5.0,
        },
        f"{key}-contract",
    )
    spec = campaign_protocol.CampaignProtocolSpec.model_validate(
        {
            "domain": {
                "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
                "acquisition_duration_min": {"lower": 2.0, "upper": 10.0},
            },
            "flow_ml_min": 0.5,
            "constraints": {
                "max_acquisition_duration_min": 10.0,
                "max_flow_ml_min": 0.5,
                "solvent_bounds": [
                    {"solvent_id": "A", "min_fraction": 0.1, "max_fraction": 0.9},
                    {"solvent_id": "B", "min_fraction": 0.1, "max_fraction": 0.9},
                ],
            },
            "tasks": [
                {
                    "task_id": "two-analytes",
                    "producer_id": producer["producer_id"],
                    "observation_contract_id": contract["contract_id"],
                }
            ],
            "seeds": [17],
            "evaluation_budget": evaluation_budget,
            "initial_design_size": 3,
            "evaluation_cost": 1.0,
            "evaluation_cost_unit": "synthetic_evaluation",
        }
    )
    protocol = campaign_protocol_registry.register_campaign_protocol(
        owner, "Matched two-analyte synthetic protocol", spec, f"{key}-protocol"
    )
    return {
        "owner": owner,
        "model": model,
        "producer": producer,
        "contract": contract,
        "spec": spec,
        "protocol": protocol,
    }


def _registered_ablation_protocol(prepared, *, target, noise_prior, key):
    spec_data = prepared["spec"].model_dump(mode="json")
    spec_data.update(
        {
            "profile": "synthetic-isocratic-ablation/1",
            "optimizer_target": target,
            "surrogate_noise_prior": noise_prior,
        }
    )
    spec = campaign_protocol.parse_campaign_protocol(spec_data)
    protocol = campaign_protocol_registry.register_campaign_protocol(
        prepared["owner"],
        f"Synthetic ablation protocol {key}",
        spec,
        f"{key}-ablation-protocol",
    )
    return spec, protocol


def _record_snapshot(prepared, proposal, *, key, evidence_change=None):
    owner = prepared["owner"]
    method_id = proposal["method"]["method_id"]
    experiment = experiments.create_experiment(
        owner,
        experiments.ExperimentSpec(
            title=f"Synthetic campaign evaluation {key}",
            source="synthetic campaign fixture",
            executable_method_id=method_id,
            observation_contract_id=prepared["contract"]["contract_id"],
            observation_producer_id=prepared["producer"]["producer_id"],
        ),
        f"{key}-experiment",
    )
    attempt = experiments.record_attempt(
        owner,
        experiment["experiment_id"],
        experiments.AttemptSpec(
            status="completed",
            evaluator_kind="simulator",
            fidelity=1.0,
            executable_method_id=method_id,
        ),
        f"{key}-attempt",
    )
    prediction = retention.predict_retention(
        owner,
        prepared["model"]["model_id"],
        method_id,
        f"{key}-prediction",
    )
    run = gaussian_producer.record_gaussian_observation(
        owner,
        prepared["producer"]["producer_id"],
        prediction["prediction_id"],
        attempt["attempt_id"],
        attempt["revision"],
        f"{key}-latent-run",
    )
    evidence_spec = copy.deepcopy(run["evidence_spec"])
    if evidence_change == "empty":
        evidence_spec["analytes"] = []
    elif evidence_change == "partial":
        evidence_spec["analytes"] = evidence_spec["analytes"][:1]
    elif evidence_change == "ambiguous":
        evidence_spec["analytes"][0]["assignment"] = "ambiguous"
    observation = observations.evaluate_observation(
        owner,
        prepared["contract"]["contract_id"],
        attempt["attempt_id"],
        attempt["revision"],
        evidence_spec,
        f"{key}-assessment",
    )
    snapshot = observations.create_observation_snapshot(
        owner,
        prepared["contract"]["contract_id"],
        [observation["observation_id"]],
        f"{key}-snapshot",
    )
    return experiment, attempt, prediction, run, observation, snapshot


def _consume_one(prepared, campaign, *, key):
    proposal_response = campaigns.suggest_campaign(
        prepared["owner"], campaign["campaign_id"], campaign["revision"], f"{key}-suggest"
    )
    proposal = proposal_response["proposal"]
    evidence = _record_snapshot(prepared, proposal, key=key)
    consumed = campaigns.consume_campaign_snapshot(
        prepared["owner"],
        campaign["campaign_id"],
        proposal["proposal_id"],
        evidence[-1]["snapshot_id"],
        proposal_response["revision"],
        f"{key}-consume",
    )
    return proposal_response, evidence, consumed


def _reseal_consumption_result(consumption_id, update):
    with registry_connection(write=True) as con:
        row = con.execute(
            "SELECT payload FROM campaign_consumptions WHERE id=?", (consumption_id,)
        ).fetchone()
        payload = json.loads(row["payload"])
        update(payload["result"])
        content = {key: value for key, value in payload.items() if key != "output_sha256"}
        payload["output_sha256"] = campaigns.sha256(content)
        con.execute(
            "UPDATE campaign_consumptions SET payload=?, output_digest=? WHERE id=?",
            (canonical(payload), payload["output_sha256"], consumption_id),
        )


def test_create_suggest_consume_suggest_preserves_exact_sources_and_receipts():
    prepared = _prepared()
    owner = prepared["owner"]
    random_campaign = campaigns.create_campaign(
        owner, prepared["protocol"]["protocol_id"], "two-analytes", 17, "random", "random-campaign"
    )
    bo_campaign = campaigns.create_campaign(
        owner,
        prepared["protocol"]["protocol_id"],
        "two-analytes",
        17,
        "bo_mcp_gp_logei",
        "bo-campaign",
    )
    assert random_campaign["state"] == "ready_to_suggest"
    assert random_campaign["revision"] == 1

    random_proposal_response = campaigns.suggest_campaign(
        owner, random_campaign["campaign_id"], 1, "random-suggest-0"
    )
    bo_proposal_response = campaigns.suggest_campaign(
        owner, bo_campaign["campaign_id"], 1, "bo-suggest-0"
    )
    random_proposal = random_proposal_response["proposal"]
    bo_proposal = bo_proposal_response["proposal"]
    assert random_proposal["proposal_source"] == "common_initial_design"
    assert bo_proposal["proposal_source"] == "common_initial_design"
    assert random_proposal["candidate"] == bo_proposal["candidate"]
    assert random_proposal["method"]["canonical_program"]["program_kind"] == "isocratic"
    assert random_proposal_response["revision"] == 2

    evidence = _record_snapshot(prepared, random_proposal, key="first-evaluation")
    attempt = evidence[1]
    observation_before = observations.get_observation(owner, evidence[4]["observation_id"])
    snapshot_before = observations.get_observation_snapshot(owner, evidence[-1]["snapshot_id"])

    # Correct one current result after the observation/snapshot was frozen.
    # Campaign utility remains bound to the immutable Gaussian run artifact.
    source_resolution = next(
        item
        for item in evidence[3]["generated_results"]
        if item["metric"] == "resolution" and item["analyte_reference"] == "A"
    )
    correction = experiments.record_results(
        owner,
        attempt["attempt_id"],
        attempt["revision"],
        [
            experiments.ResultSpec(
                metric="resolution",
                analyte_reference="A",
                status="observed",
                unit="1",
                value=0.05,
                explanation="A synthetic later correction used to test snapshot immutability.",
                supersedes_result_id=source_resolution["result_id"],
            )
        ],
        "result-correction-after-snapshot",
    )["results"][0]
    assert (
        experiments.get_result(owner, source_resolution["result_id"])["superseded_by_result_id"]
        == correction["result_id"]
    )

    # A later attempt revision must not rewrite the exact revision frozen in
    # the snapshot and stored Gaussian producer run.
    experiments.revise_attempt(
        owner,
        attempt["attempt_id"],
        attempt["revision"],
        experiments.AttemptSpec(
            status="completed",
            evaluator_kind="simulator",
            fidelity=1.0,
            executable_method_id=random_proposal["method"]["method_id"],
            actual_conditions=[
                experiments.Condition(name="revision_note", value="later", unit=None)
            ],
        ),
        "A later immutable attempt revision for snapshot replay",
        "attempt-revision-after-snapshot",
    )

    consumed_response = campaigns.consume_campaign_snapshot(
        owner,
        random_campaign["campaign_id"],
        random_proposal["proposal_id"],
        evidence[-1]["snapshot_id"],
        random_proposal_response["revision"],
        "random-consume-0",
    )
    consumption = consumed_response["consumption"]
    result = consumption["result"]
    assert consumed_response["revision"] == 3
    assert consumed_response["campaign"]["consumed_evaluations"] == 1
    assert consumed_response["campaign"]["state"] == "ready_to_suggest"
    assert (
        result["analytical_acceptance"]
        == observation_before["observation"]["assessment"]["analytical_acceptance"]
    )
    assert (
        result["runtime_preference_satisfied"]
        == observation_before["observation"]["assessment"]["runtime_preference_satisfied"]
    )
    assert result["utility"]["status"] == "scored"
    assert "optimizer_target" not in result
    assert (
        result["utility"]["score"]
        == moses_observation_score(
            profile=prepared["contract"]["utility_definition"]["profile"],
            num_compounds=2,
            peak_resolutions=[row["resolution"] for row in evidence[3]["observation"]["analytes"]],
            run_time_min=random_proposal["candidate"]["acquisition_duration_min"],
            max_run_time_min=10.0,
            objective="NumPeaks-CritRes",
            count_threshold=0.5,
            resolution_threshold=1.5,
            run_time_threshold_min=5.0,
        )["score"]
    )
    assert result["evaluation_cost"] == 1.0
    assert result["evaluation_cost_unit"] == "synthetic_evaluation"
    assert result["raw_metrics"]["analyte_ids"] == ["A", "B"]
    assert consumption["attempt_revision"] == attempt["revision"]
    assert consumption["snapshot_sha256"] == snapshot_before["sha256"]
    assert (
        observations.get_observation_snapshot(owner, evidence[-1]["snapshot_id"]) == snapshot_before
    )
    assert observations.get_observation(owner, evidence[4]["observation_id"]) == observation_before
    assert not any("success" in key for key in result)

    # The retry receipt continues to return the original proposal after the
    # campaign advances, while a fresh key at the stale revision is rejected.
    assert (
        campaigns.suggest_campaign(owner, random_campaign["campaign_id"], 1, "random-suggest-0")
        == random_proposal_response
    )
    with pytest.raises(ValueError, match="revision conflict"):
        campaigns.suggest_campaign(owner, random_campaign["campaign_id"], 2, "stale-suggest")

    second_proposal_response = campaigns.suggest_campaign(
        owner, random_campaign["campaign_id"], 3, "random-suggest-1"
    )
    assert second_proposal_response["proposal"]["ordinal"] == 1
    assert second_proposal_response["proposal"]["proposal_source"] == "common_initial_design"
    assert second_proposal_response["proposal"]["candidate"] == campaign_protocol.initial_candidate(
        prepared["spec"], 17, 1
    )
    assert campaigns.get_campaign(owner, random_campaign["campaign_id"])["revision"] == 4
    with pytest.raises(ValueError, match="not found or is not accessible"):
        campaigns.get_campaign("another-owner", random_campaign["campaign_id"])
    with pytest.raises(ValueError, match="not found or is not accessible"):
        campaigns.get_campaign_proposal("another-owner", random_proposal["proposal_id"])

    # A separate process can re-open the durable campaign from the same local
    # store without reconstructing optimizer state.
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; from measure_service.campaigns import get_campaign; "
            "print(json.dumps(get_campaign(sys.argv[1],sys.argv[2]), sort_keys=True))",
            owner,
            random_campaign["campaign_id"],
        ],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    restarted = json.loads(process.stdout)
    assert restarted["revision"] == 4
    assert restarted["history"][0]["consumption_id"] == consumption["consumption_id"]


def test_snapshot_for_another_method_is_rejected_without_consuming_or_advancing():
    prepared = _prepared(key="mismatch")
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        prepared["protocol"]["protocol_id"],
        "two-analytes",
        17,
        "random",
        "mismatch-campaign",
    )
    suggestion = campaigns.suggest_campaign(owner, campaign["campaign_id"], 1, "mismatch-suggest")
    proposal = suggestion["proposal"]
    other_declaration = campaign_protocol.method_declaration(
        prepared["spec"],
        {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 7.0},
    )
    other_method = method_registry.register_method(
        owner,
        "Unproposed synthetic method",
        other_declaration,
        prepared["spec"].constraints,
        "unproposed-method",
    )
    evidence = _record_snapshot(
        prepared,
        {"method": {"method_id": other_method["method_id"]}},
        key="unproposed-evaluation",
    )
    with pytest.raises(ValueError, match="source identities do not match|differs from the exact"):
        campaigns.consume_campaign_snapshot(
            owner,
            campaign["campaign_id"],
            proposal["proposal_id"],
            evidence[-1]["snapshot_id"],
            suggestion["revision"],
            "mismatch-consume",
        )
    retained = campaigns.get_campaign(owner, campaign["campaign_id"])
    assert retained["revision"] == suggestion["revision"]
    assert retained["consumed_evaluations"] == 0
    assert retained["pending_proposal"]["proposal_id"] == proposal["proposal_id"]
    with registry_connection() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM campaign_consumptions WHERE campaign_id=?",
                (campaign["campaign_id"],),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("evidence_change", ["empty", "partial", "ambiguous"])
def test_campaign_rejects_snapshot_that_omits_or_reclassifies_producer_evidence(
    evidence_change,
):
    prepared = _prepared(key=f"evidence-{evidence_change}")
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        prepared["protocol"]["protocol_id"],
        "two-analytes",
        17,
        "random",
        f"evidence-{evidence_change}-campaign",
    )
    suggestion = campaigns.suggest_campaign(
        owner, campaign["campaign_id"], 1, f"evidence-{evidence_change}-suggest"
    )
    evidence = _record_snapshot(
        prepared,
        suggestion["proposal"],
        key=f"evidence-{evidence_change}-evaluation",
        evidence_change=evidence_change,
    )
    with pytest.raises(ValueError, match="explicitly link|complete confirmed"):
        campaigns.consume_campaign_snapshot(
            owner,
            campaign["campaign_id"],
            suggestion["proposal"]["proposal_id"],
            evidence[-1]["snapshot_id"],
            suggestion["revision"],
            f"evidence-{evidence_change}-consume",
        )
    retained = campaigns.get_campaign(owner, campaign["campaign_id"])
    assert retained["revision"] == suggestion["revision"]
    assert retained["consumed_evaluations"] == 0
    assert retained["pending_proposal"]["proposal_id"] == suggestion["proposal"]["proposal_id"]
    with registry_connection() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM campaign_consumptions WHERE campaign_id=?",
                (campaign["campaign_id"],),
            ).fetchone()[0]
            == 0
        )


def test_simultaneous_suggestions_use_revision_cas_and_leave_one_pending():
    prepared = _prepared(key="concurrent")
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        prepared["protocol"]["protocol_id"],
        "two-analytes",
        17,
        "random",
        "concurrent-campaign",
    )

    def suggest(key):
        try:
            return campaigns.suggest_campaign(owner, campaign["campaign_id"], 1, key)
        except ValueError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(suggest, ("concurrent-a", "concurrent-b")))
    successes = [item for item in outcomes if isinstance(item, dict)]
    failures = [item for item in outcomes if isinstance(item, ValueError)]
    assert len(successes) == len(failures) == 1
    assert "revision conflict" in str(failures[0]).lower()
    retained = campaigns.get_campaign(owner, campaign["campaign_id"])
    assert retained["revision"] == 2
    assert retained["pending_proposal"]["proposal_id"] == successes[0]["proposal"]["proposal_id"]
    with registry_connection() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM campaign_proposals WHERE campaign_id=?",
                (campaign["campaign_id"],),
            ).fetchone()[0]
            == 1
        )


def test_same_key_concurrent_consumption_commits_once_and_new_key_cannot_reconsume():
    prepared = _prepared(key="consume-concurrent")
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        prepared["protocol"]["protocol_id"],
        "two-analytes",
        17,
        "random",
        "consume-concurrent-campaign",
    )
    suggestion = campaigns.suggest_campaign(
        owner, campaign["campaign_id"], 1, "consume-concurrent-suggest"
    )
    proposal = suggestion["proposal"]
    evidence = _record_snapshot(prepared, proposal, key="consume-concurrent-evaluation")
    request = (
        owner,
        campaign["campaign_id"],
        proposal["proposal_id"],
        evidence[-1]["snapshot_id"],
        suggestion["revision"],
        "consume-concurrent-once",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda _: campaigns.consume_campaign_snapshot(*request), range(2))
        )
    assert responses[0] == responses[1]
    consumed = responses[0]
    assert consumed["revision"] == 3
    assert consumed["campaign"]["consumed_evaluations"] == 1
    consumption = consumed["consumption"]
    assert campaigns.get_campaign_consumption(owner, consumption["consumption_id"]) == consumption
    with pytest.raises(ValueError, match="not found or is not accessible"):
        campaigns.get_campaign_consumption("another-owner", consumption["consumption_id"])
    with pytest.raises(ValueError, match="pending proposal"):
        campaigns.consume_campaign_snapshot(
            owner,
            campaign["campaign_id"],
            proposal["proposal_id"],
            evidence[-1]["snapshot_id"],
            consumed["revision"],
            "consume-concurrent-different-key",
        )
    with pytest.raises(ValueError, match="not found or is not accessible"):
        campaigns.consume_campaign_snapshot(
            "another-owner",
            campaign["campaign_id"],
            proposal["proposal_id"],
            evidence[-1]["snapshot_id"],
            suggestion["revision"],
            "consume-foreign-owner",
        )
    assert campaigns.get_campaign(owner, campaign["campaign_id"])["revision"] == 3
    with registry_connection() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM campaign_consumptions WHERE campaign_id=?",
                (campaign["campaign_id"],),
            ).fetchone()[0]
            == 1
        )


def test_failed_state_transitions_roll_back_records_methods_revision_and_receipt(monkeypatch):
    prepared = _prepared(key="rollback")
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        prepared["protocol"]["protocol_id"],
        "two-analytes",
        17,
        "random",
        "rollback-campaign",
    )
    original_response = campaigns._campaign_response

    def fail_response(*args, **kwargs):
        raise RuntimeError("injected response serialization failure")

    monkeypatch.setattr(campaigns, "_campaign_response", fail_response)
    with pytest.raises(RuntimeError, match="injected response serialization failure"):
        campaigns.suggest_campaign(owner, campaign["campaign_id"], 1, "rollback-suggest")
    monkeypatch.setattr(campaigns, "_campaign_response", original_response)
    assert campaigns.get_campaign(owner, campaign["campaign_id"])["revision"] == 1
    with registry_connection() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM campaign_proposals WHERE campaign_id=?",
                (campaign["campaign_id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM executable_methods WHERE owner=? AND title LIKE ?",
                (owner, f"Campaign {campaign['campaign_id'][:8]} evaluation%"),
            ).fetchone()[0]
            == 0
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM registry_receipts WHERE owner=? AND request_key=?",
                (owner, "rollback-suggest"),
            ).fetchone()[0]
            == 0
        )

    suggestion = campaigns.suggest_campaign(owner, campaign["campaign_id"], 1, "rollback-suggest")
    evidence = _record_snapshot(prepared, suggestion["proposal"], key="rollback-evaluation")
    monkeypatch.setattr(campaigns, "_campaign_response", fail_response)
    with pytest.raises(RuntimeError, match="injected response serialization failure"):
        campaigns.consume_campaign_snapshot(
            owner,
            campaign["campaign_id"],
            suggestion["proposal"]["proposal_id"],
            evidence[-1]["snapshot_id"],
            suggestion["revision"],
            "rollback-consume",
        )
    monkeypatch.setattr(campaigns, "_campaign_response", original_response)
    assert campaigns.get_campaign(owner, campaign["campaign_id"])["revision"] == 2
    with registry_connection() as con:
        proposal_state = con.execute(
            "SELECT state FROM campaign_proposals WHERE id=?",
            (suggestion["proposal"]["proposal_id"],),
        ).fetchone()[0]
        consumption_count = con.execute(
            "SELECT COUNT(*) FROM campaign_consumptions WHERE campaign_id=?",
            (campaign["campaign_id"],),
        ).fetchone()[0]
        receipt_count = con.execute(
            "SELECT COUNT(*) FROM registry_receipts WHERE owner=? AND request_key=?",
            (owner, "rollback-consume"),
        ).fetchone()[0]
    assert (proposal_state, consumption_count, receipt_count) == ("pending", 0, 0)
    retried = campaigns.consume_campaign_snapshot(
        owner,
        campaign["campaign_id"],
        suggestion["proposal"]["proposal_id"],
        evidence[-1]["snapshot_id"],
        suggestion["revision"],
        "rollback-consume",
    )
    assert retried["revision"] == 3
    assert retried["campaign"]["consumed_evaluations"] == 1


def test_full_declared_budget_is_consumed_before_campaign_stops():
    prepared = _prepared(key="full-budget", evaluation_budget=50)
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        prepared["protocol"]["protocol_id"],
        "two-analytes",
        17,
        "random",
        "full-budget-campaign",
    )
    for ordinal in range(prepared["spec"].evaluation_budget):
        suggestion = campaigns.suggest_campaign(
            owner,
            campaign["campaign_id"],
            campaign["revision"],
            f"full-budget-suggest-{ordinal}",
        )
        assert suggestion["proposal"]["ordinal"] == ordinal
        evidence = _record_snapshot(
            prepared, suggestion["proposal"], key=f"full-budget-evaluation-{ordinal}"
        )
        consumed = campaigns.consume_campaign_snapshot(
            owner,
            campaign["campaign_id"],
            suggestion["proposal"]["proposal_id"],
            evidence[-1]["snapshot_id"],
            suggestion["revision"],
            f"full-budget-consume-{ordinal}",
        )
        campaign = consumed["campaign"]
    assert campaign["state"] == "budget_complete"
    assert campaign["consumed_evaluations"] == prepared["spec"].evaluation_budget == 50
    assert sum(item["evaluation_cost"] for item in campaign["history"]) == 50.0
    assert len({item["candidate"]["strong_solvent_fraction"] for item in campaign["history"]}) == 50
    revision = campaign["revision"]
    with pytest.raises(ValueError, match="full declared evaluation budget"):
        campaigns.suggest_campaign(
            owner, campaign["campaign_id"], revision, "full-budget-exhausted"
        )
    exhausted = campaigns.get_campaign(owner, campaign["campaign_id"])
    assert exhausted["revision"] == revision
    assert exhausted["consumed_evaluations"] == 50


def test_32_analyte_cohort_can_be_suggested_and_consumed_within_record_bounds():
    prepared = _prepared(key="cohort-32", analyte_count=32, evaluation_budget=50)
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        prepared["protocol"]["protocol_id"],
        "two-analytes",
        17,
        "random",
        "cohort-32-campaign",
    )
    suggestion = campaigns.suggest_campaign(
        owner, campaign["campaign_id"], campaign["revision"], "cohort-32-suggest"
    )
    assert len(suggestion["proposal"]["candidate"]) == 2
    evidence = _record_snapshot(prepared, suggestion["proposal"], key="cohort-32-evaluation")
    run = evidence[3]
    assert len(run["observation"]["analytes"]) == 32
    assert len(run["generated_results"]) == 64
    assert len(evidence[4]["observation"]["evidence"]["analytes"]) == 32
    consumed = campaigns.consume_campaign_snapshot(
        owner,
        campaign["campaign_id"],
        suggestion["proposal"]["proposal_id"],
        evidence[-1]["snapshot_id"],
        suggestion["revision"],
        "cohort-32-consume",
    )
    assert consumed["campaign"]["consumed_evaluations"] == 1
    assert len(consumed["consumption"]["result"]["raw_metrics"]["analyte_ids"]) == 32
    assert len(canonical(suggestion).encode("utf-8")) < campaigns.MAX_RECORD_BYTES
    assert len(canonical(consumed).encode("utf-8")) < campaigns.MAX_RECORD_BYTES


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("task", {"task_id": "tampered-task"}),
        ("protocol_output_sha256", "0" * 64),
        ("implementation_provenance", {"tampered": True}),
    ],
)
def test_campaign_immutable_payload_fields_are_digest_checked(field, replacement):
    prepared = _prepared(key=f"tamper-{field}")
    campaign = campaigns.create_campaign(
        prepared["owner"],
        prepared["protocol"]["protocol_id"],
        "two-analytes",
        17,
        "random",
        f"tamper-{field}-campaign",
    )
    with registry_connection(write=True) as con:
        row = con.execute(
            "SELECT payload FROM campaign_runs WHERE id=?", (campaign["campaign_id"],)
        ).fetchone()
        payload = json.loads(row["payload"])
        payload[field] = replacement
        con.execute(
            "UPDATE campaign_runs SET payload=? WHERE id=?",
            (canonical(payload), campaign["campaign_id"]),
        )
    with pytest.raises(ValueError, match="content digest"):
        campaigns.get_campaign(prepared["owner"], campaign["campaign_id"])


def test_backend_failure_is_not_replaced_with_random_candidate(monkeypatch):
    prepared = _prepared(key="bo-failure")
    spec = prepared["spec"]
    training = [
        {
            "candidate": {"strong_solvent_fraction": 0.3, "acquisition_duration_min": 3.0},
            "score": 2.0,
        }
    ]

    def broken_backend(name):
        assert name == "measure_service.optimizer_backend"

        def fail(*args):
            raise RuntimeError("synthetic adapter failure")

        return SimpleNamespace(suggest_candidate=fail)

    monkeypatch.setattr(campaigns.importlib, "import_module", broken_backend)
    with pytest.raises(RuntimeError, match="synthetic adapter failure"):
        campaigns._bo_candidate(spec, training, 17)


def test_backend_candidate_validation_binds_training_and_rejects_out_of_domain_values():
    prepared = _prepared(key="bo-validation")
    spec = prepared["spec"]
    training = [
        {
            "candidate": {"strong_solvent_fraction": 0.3, "acquisition_duration_min": 3.0},
            "score": 2.0,
        }
    ]
    provenance = {
        "backend_id": "bo-engine/BoTorchBackend",
        "backend_name": "BoTorchBackend",
        "generation_method": "bo",
        "model_type": "SingleTaskGP",
        "acquisition_method": "expected_improvement",
        "acquisition_implementation": "qLogExpectedImprovement",
        "optimization_strategy": "synthetic two-dimensional optimization fixture",
        "source_revision": None,
        "source_tree_sha256": "a" * 64,
        "distribution_versions": {"botorch": "synthetic-test-version"},
        "protocol_sha256": campaign_protocol.campaign_protocol_sha256(spec),
        "seed": 17,
        "training_sha256": campaigns.sha256(training),
        "training_count": len(training),
        "runtime": {"device": "cpu"},
        "settings": {},
        "warnings": [],
        "suggestion_provenance": {"model_type": "SingleTaskGP"},
    }
    candidate, retained = campaigns._validate_backend_result(
        {
            "candidate": {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 5.0},
            "backend": provenance,
        },
        spec=spec,
        seed=17,
        training=training,
    )
    assert candidate == {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 5.0}
    assert retained["training_sha256"] == campaigns.sha256(training)
    with pytest.raises(ValueError, match="outside its finite declared bounds"):
        campaigns._validate_backend_result(
            {
                "candidate": {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 11.0},
                "backend": provenance,
            },
            spec=spec,
            seed=17,
            training=training,
        )
    with pytest.raises(ValueError, match="training digest"):
        campaigns._validate_backend_result(
            {
                "candidate": {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 5.0},
                "backend": {**provenance, "training_sha256": "0" * 64},
            },
            spec=spec,
            seed=17,
            training=training,
        )


def test_ablation_reward_keeps_unknown_assessment_and_replays_zero_training_in_order():
    prepared = _prepared(
        key="ablation-unknown",
        evaluation_budget=5,
        retention_ln_k0=20.0,
    )
    ablation_spec, protocol = _registered_ablation_protocol(
        prepared,
        target="confirmed_acceptance_reward",
        noise_prior="low_noise",
        key="ablation-unknown",
    )
    prepared.update(spec=ablation_spec, protocol=protocol)
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        protocol["protocol_id"],
        "two-analytes",
        17,
        "bo_mcp_gp_logei",
        "ablation-unknown-campaign",
    )
    proposals = []
    for ordinal in range(ablation_spec.initial_design_size):
        suggestion = campaigns.suggest_campaign(
            owner,
            campaign["campaign_id"],
            campaign["revision"],
            f"ablation-unknown-suggest-{ordinal}",
        )
        proposal = suggestion["proposal"]
        proposals.append(proposal)
        evidence = _record_snapshot(
            prepared, proposal, key=f"ablation-unknown-evaluation-{ordinal}"
        )
        assert all(
            item["retention_status"] == "not_eluted"
            for item in evidence[3]["observation"]["analytes"]
        )
        consumed = campaigns.consume_campaign_snapshot(
            owner,
            campaign["campaign_id"],
            proposal["proposal_id"],
            evidence[-1]["snapshot_id"],
            suggestion["revision"],
            f"ablation-unknown-consume-{ordinal}",
        )
        result = consumed["consumption"]["result"]
        assert result["analytical_acceptance"] == "unknown"
        assert result["utility"]["status"] == "scored"
        assert result["optimizer_target"] == {
            "profile": "confirmed-acceptance-lower-bound/1",
            "name": "confirmed_acceptance_reward",
            "direction": "maximize",
            "status": "scored",
            "value": 0.0,
        }
        history_row = consumed["campaign"]["history"][ordinal]
        assert history_row["analytical_acceptance"] == "unknown"
        assert history_row["score"] == result["utility"]["score"]
        assert history_row["optimizer_target"] == result["optimizer_target"]
        campaign = consumed["campaign"]

    with registry_connection() as con:
        training, source_members = campaigns._training_context(con, owner, campaign["campaign_id"])
    assert [item["candidate"] for item in training] == [
        proposal["candidate"] for proposal in proposals
    ]
    assert [item["score"] for item in training] == [0.0, 0.0, 0.0]
    assert [item["ordinal"] for item in source_members] == [0, 1, 2]
    assert all(item["optimizer_training_included"] for item in source_members)
    assert [member["consumption_id"] for member in source_members] == [
        row["consumption_id"] for row in campaign["history"]
    ]
    assert campaign_protocol.optimizer_configuration(ablation_spec)["noise_prior_params"] == [
        1.1,
        100.0,
    ]


def test_ablation_accepted_reward_is_separate_from_utility_and_target_tampering_fails():
    prepared = _prepared(
        key="ablation-accepted",
        evaluation_budget=4,
        retention_ln_k0=math.log(2.0),
        retention_ln_k0_step=0.5,
        width_min=0.01,
    )
    spec, protocol = _registered_ablation_protocol(
        prepared,
        target="confirmed_acceptance_reward",
        noise_prior="default",
        key="ablation-accepted",
    )
    prepared.update(spec=spec, protocol=protocol)
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        protocol["protocol_id"],
        "two-analytes",
        17,
        "random",
        "ablation-accepted-campaign",
    )
    suggestion = campaigns.suggest_campaign(
        owner, campaign["campaign_id"], 1, "ablation-accepted-suggest"
    )
    evidence = _record_snapshot(
        prepared, suggestion["proposal"], key="ablation-accepted-evaluation"
    )
    consumed = campaigns.consume_campaign_snapshot(
        owner,
        campaign["campaign_id"],
        suggestion["proposal"]["proposal_id"],
        evidence[-1]["snapshot_id"],
        suggestion["revision"],
        "ablation-accepted-consume",
    )
    result = consumed["consumption"]["result"]
    assert result["analytical_acceptance"] == "accepted"
    assert result["optimizer_target"] == {
        "profile": "confirmed-acceptance-lower-bound/1",
        "name": "confirmed_acceptance_reward",
        "direction": "maximize",
        "status": "scored",
        "value": 1.0,
    }
    assert result["utility"]["profile"] == "moses-botier-1.0.0-binary64/1"
    history = consumed["campaign"]["history"][0]
    assert history["score"] == result["utility"]["score"]
    assert history["optimizer_target"]["value"] == 1.0
    target_kernel = consumed["consumption"]["implementation_provenance"]["campaign"][
        "optimizer_target_kernel"
    ]
    assert target_kernel["module"] == "measure_methods.observations"
    assert len(target_kernel["source_sha256"]) == 64
    assert (
        campaigns.get_campaign_consumption(owner, consumed["consumption"]["consumption_id"])
        == consumed["consumption"]
    )
    assert (
        campaigns.consume_campaign_snapshot(
            owner,
            campaign["campaign_id"],
            suggestion["proposal"]["proposal_id"],
            evidence[-1]["snapshot_id"],
            suggestion["revision"],
            "ablation-accepted-consume",
        )
        == consumed
    )

    _reseal_consumption_result(
        consumed["consumption"]["consumption_id"],
        lambda result: result["optimizer_target"].update(value=0.0),
    )
    with pytest.raises(ValueError, match="optimizer target differs"):
        campaigns.get_campaign(owner, campaign["campaign_id"])


@pytest.mark.parametrize(
    ("target", "prepared_options"),
    [
        ("confirmed_acceptance_reward", {"retention_ln_k0": 20.0}),
        ("historical_utility", {}),
    ],
)
def test_ablation_consumption_replay_rejects_resealed_target_and_source_outcome(
    target, prepared_options
):
    key = f"ablation-resealed-{target}"
    prepared = _prepared(key=key, **prepared_options)
    spec, protocol = _registered_ablation_protocol(
        prepared,
        target=target,
        noise_prior="default",
        key=key,
    )
    prepared.update(spec=spec, protocol=protocol)
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner, protocol["protocol_id"], "two-analytes", 17, "random", f"{key}-campaign"
    )
    _, _, consumed = _consume_one(prepared, campaign, key=f"{key}-evaluation")
    consumption = consumed["consumption"]
    result = consumption["result"]

    if target == "confirmed_acceptance_reward":
        assert result["analytical_acceptance"] == "unknown"
        assert result["optimizer_target"]["value"] == 0.0

        def rebind_reward(source_result):
            source_result["analytical_acceptance"] = "accepted"
            source_result["optimizer_target"]["value"] = 1.0

        update = rebind_reward
    else:
        old_utility = result["utility"]["score"]
        new_utility = 0.25 if old_utility != 0.25 else 0.75

        def rebind_utility(source_result):
            source_result["utility"]["score"] = new_utility
            source_result["optimizer_target"]["value"] = new_utility

        update = rebind_utility

    _reseal_consumption_result(consumption["consumption_id"], update)
    message = "immutable observation source"
    with pytest.raises(ValueError, match=message):
        campaigns.get_campaign_consumption(owner, consumption["consumption_id"])
    with registry_connection() as con:
        with pytest.raises(ValueError, match=message):
            campaigns._training_context(con, owner, campaign["campaign_id"])
    with pytest.raises(ValueError, match=message):
        campaigns.get_campaign(owner, campaign["campaign_id"])


def test_ablation_historical_utility_target_keeps_v1_score_as_optimizer_reward():
    prepared = _prepared(key="ablation-historical", evaluation_budget=4)
    spec, protocol = _registered_ablation_protocol(
        prepared,
        target="historical_utility",
        noise_prior="default",
        key="ablation-historical",
    )
    prepared.update(spec=spec, protocol=protocol)
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        protocol["protocol_id"],
        "two-analytes",
        17,
        "random",
        "ablation-historical-campaign",
    )
    suggestion = campaigns.suggest_campaign(
        owner, campaign["campaign_id"], 1, "ablation-historical-suggest"
    )
    evidence = _record_snapshot(
        prepared, suggestion["proposal"], key="ablation-historical-evaluation"
    )
    consumed = campaigns.consume_campaign_snapshot(
        owner,
        campaign["campaign_id"],
        suggestion["proposal"]["proposal_id"],
        evidence[-1]["snapshot_id"],
        suggestion["revision"],
        "ablation-historical-consume",
    )
    result = consumed["consumption"]["result"]
    assert result["optimizer_target"] == {
        "profile": "moses-botier-1.0.0-binary64/1",
        "name": "utility",
        "direction": "maximize",
        "status": result["utility"]["status"],
        "value": result["utility"]["score"],
    }
    assert consumed["campaign"]["history"][0]["score"] == result["utility"]["score"]
    assert consumed["campaign"]["history"][0]["optimizer_target"] == result["optimizer_target"]
    with registry_connection() as con:
        training, _ = campaigns._training_context(con, owner, campaign["campaign_id"])
    assert training == [
        {"candidate": suggestion["proposal"]["candidate"], "score": result["utility"]["score"]}
    ]


def test_ablation_optimizer_backend_binds_target_noise_prior_and_accepts_rich_provenance():
    prepared = _prepared(key="ablation-backend", evaluation_budget=5)
    spec, _ = _registered_ablation_protocol(
        prepared,
        target="confirmed_acceptance_reward",
        noise_prior="low_noise",
        key="ablation-backend",
    )
    configuration = campaign_protocol.optimizer_configuration(spec)
    training = [
        {
            "candidate": {
                "strong_solvent_fraction": 0.2 + ordinal * 0.2,
                "acquisition_duration_min": 2.0 + ordinal,
            },
            "score": float(ordinal == 1),
        }
        for ordinal in range(3)
    ]
    backend = {
        "backend_id": "bo-engine/BoTorchBackend",
        "adapter_profile": configuration["adapter_profile"],
        "backend_name": "bo-mcp",
        "backend_class": "BoTorchBackend",
        "generation_method": "bo",
        "model_type": "SingleTaskGP",
        "acquisition_method": "expected_improvement",
        "acquisition_implementation": "qLogExpectedImprovement",
        "optimization_strategy": "L-BFGS-B with random restarts",
        "source_revision": "a" * 40,
        "source_tree_sha256": "b" * 64,
        "adapter_source_sha256": "c" * 64,
        "campaign_protocol_source_sha256": "d" * 64,
        "design_kernel_source_sha256": "e" * 64,
        "distribution_versions": {"botorch": "synthetic-test-version"},
        "protocol_sha256": campaign_protocol.campaign_protocol_sha256(spec),
        "seed": 17,
        "training_sha256": campaigns.sha256(training),
        "training_count": len(training),
        "runtime": {"device": "cpu", "elapsed_seconds": 0.25},
        "settings": {
            "objective": {"name": configuration["target_name"], "direction": "maximize"},
            "noise_prior": {
                "family": "Gamma",
                "concentration": 1.1,
                "rate": 100.0,
                "space": "standardized_target_variance",
                "constraint_lower_bound": 0.0001,
                "learned": True,
            },
        },
        "warnings": [],
        "suggestion_provenance": {"generation_method": "bo"},
    }
    candidate = {"strong_solvent_fraction": 0.45, "acquisition_duration_min": 6.0}
    accepted_candidate, accepted_backend = campaigns._validate_backend_result(
        {"candidate": candidate, "backend": backend},
        spec=spec,
        seed=17,
        training=training,
    )
    assert accepted_candidate == candidate
    assert accepted_backend["design_kernel_source_sha256"] == "e" * 64

    wrong_target = copy.deepcopy(backend)
    wrong_target["settings"]["objective"]["name"] = "utility"
    with pytest.raises(ValueError, match="objective does not match"):
        campaigns._validate_backend_result(
            {"candidate": candidate, "backend": wrong_target},
            spec=spec,
            seed=17,
            training=training,
        )
    wrong_prior = copy.deepcopy(backend)
    wrong_prior["settings"]["noise_prior"]["rate"] = 0.05
    with pytest.raises(ValueError, match="noise prior does not match"):
        campaigns._validate_backend_result(
            {"candidate": candidate, "backend": wrong_prior},
            spec=spec,
            seed=17,
            training=training,
        )


def test_failed_attempt_is_not_consumed_or_added_to_optimizer_training():
    prepared = _prepared(key="ablation-failed", evaluation_budget=4)
    spec, protocol = _registered_ablation_protocol(
        prepared,
        target="confirmed_acceptance_reward",
        noise_prior="default",
        key="ablation-failed",
    )
    prepared.update(spec=spec, protocol=protocol)
    owner = prepared["owner"]
    campaign = campaigns.create_campaign(
        owner,
        protocol["protocol_id"],
        "two-analytes",
        17,
        "random",
        "ablation-failed-campaign",
    )
    suggestion = campaigns.suggest_campaign(
        owner, campaign["campaign_id"], 1, "ablation-failed-suggest"
    )
    method_id = suggestion["proposal"]["method"]["method_id"]
    experiment = experiments.create_experiment(
        owner,
        experiments.ExperimentSpec(
            title="Failed synthetic evaluation",
            source="synthetic campaign fixture",
            executable_method_id=method_id,
            observation_contract_id=prepared["contract"]["contract_id"],
            observation_producer_id=prepared["producer"]["producer_id"],
        ),
        "ablation-failed-experiment",
    )
    failed_attempt = experiments.record_attempt(
        owner,
        experiment["experiment_id"],
        experiments.AttemptSpec(
            status="failed",
            evaluator_kind="simulator",
            fidelity=1.0,
            executable_method_id=method_id,
            explanation="Synthetic evaluation failed before observation",
        ),
        "ablation-failed-attempt",
    )
    prediction = retention.predict_retention(
        owner,
        prepared["model"]["model_id"],
        method_id,
        "ablation-failed-prediction",
    )
    with pytest.raises(ValueError, match="completed"):
        gaussian_producer.record_gaussian_observation(
            owner,
            prepared["producer"]["producer_id"],
            prediction["prediction_id"],
            failed_attempt["attempt_id"],
            failed_attempt["revision"],
            "ablation-failed-run",
        )
    assert campaigns.get_campaign(owner, campaign["campaign_id"])["consumed_evaluations"] == 0
    with registry_connection() as con:
        training, source_members = campaigns._training_context(con, owner, campaign["campaign_id"])
    assert training == []
    assert source_members == []


def test_49_row_bo_proposal_provenance_and_suggestion_shape_fit_record_bound():
    """Stress the persisted proposal shape without claiming a BO run was made."""
    prepared = _prepared(key="bo-size-bound", evaluation_budget=50)
    spec, protocol = _registered_ablation_protocol(
        prepared,
        target="confirmed_acceptance_reward",
        noise_prior="low_noise",
        key="bo-size-bound",
    )
    campaign = campaigns.create_campaign(
        prepared["owner"],
        protocol["protocol_id"],
        "two-analytes",
        17,
        "bo_mcp_gp_logei",
        "bo-size-bound-campaign",
    )
    training = [
        {
            "candidate": {
                "strong_solvent_fraction": 0.1 + ordinal * 0.01,
                "acquisition_duration_min": 2.0 + ordinal * 0.1,
            },
            "score": float(ordinal % 2),
        }
        for ordinal in range(49)
    ]
    training_digest = campaigns.sha256(training)
    configuration = campaign_protocol.optimizer_configuration(spec)
    backend = {
        "backend_id": "bo-engine/BoTorchBackend",
        "adapter_profile": configuration["adapter_profile"],
        "backend_name": "bo-mcp",
        "backend_class": "BoTorchBackend",
        "generation_method": "bo",
        "model_type": "SingleTaskGP",
        "acquisition_method": "expected_improvement",
        "acquisition_implementation": "qLogExpectedImprovement",
        "optimization_strategy": "L-BFGS-B with random restarts",
        "source_revision": "b" * 40,
        "source_tree_sha256": "a" * 64,
        "adapter_source_sha256": "c" * 64,
        "campaign_protocol_source_sha256": "d" * 64,
        "design_kernel_source_sha256": "e" * 64,
        "distribution_versions": {
            "bo-engine": "0.1.0",
            "botorch": "0.16.1",
            "gpytorch": "1.15",
            "measure-service": "0.1.0.dev0",
            "torch": "2.9.1",
        },
        "protocol_sha256": campaign_protocol.campaign_protocol_sha256(spec),
        "training_sha256": training_digest,
        "training_count": len(training),
        "seed": 17,
        "runtime": {
            "python_version": "3.13.0",
            "implementation": "CPython",
            "platform": "linux",
            "device": "cpu",
            "elapsed_seconds": 1.234,
        },
        "settings": {
            "parameters": ["strong_solvent_fraction", "acquisition_duration_min"],
            "objective": {"name": configuration["target_name"], "direction": "maximize"},
            "batch_size": 1,
            "iteration": 49,
            "acquisition_method": "expected_improvement",
            "acquisition_implementation": "qLogExpectedImprovement",
            "input_transform": "BoTorch SingleTaskGP default input normalization",
            "outcome_transform": "BoTorch SingleTaskGP default outcome standardization",
            "kernel": "RBF with automatic relevance determination (ARD)",
            "acquisition_optimization": {
                "num_restarts": 14,
                "raw_samples": 512,
                "configured_num_restarts": None,
                "configured_raw_samples": None,
            },
            "backend_state": "stateless; no prior backend state supplied",
            "replay": "ordered training history is supplied on every call",
            "noise_prior": {
                "family": "Gamma",
                "concentration": 1.1,
                "rate": 100.0,
                "space": "standardized_target_variance",
                "constraint_lower_bound": 0.0001,
                "learned": True,
            },
        },
        "warnings": [],
        "suggestion_provenance": {
            "iteration": 49,
            "batch_index": 0,
            "generation_method": "bo",
            "acquisition_function": "expected_improvement",
            "model_type": "SingleTaskGP (Gaussian Process)",
            "random_seed": 17,
            "confidence_level": "medium",
        },
    }
    candidate = {"strong_solvent_fraction": 0.43, "acquisition_duration_min": 7.25}
    candidate, backend = campaigns._validate_backend_result(
        {"candidate": candidate, "backend": backend},
        spec=spec,
        seed=17,
        training=training,
    )
    source_members = [
        {
            "ordinal": ordinal,
            "consumption_id": f"consumption-{ordinal:02d}",
            "candidate_sha256": campaigns.sha256(item["candidate"]),
            "optimizer_training_included": True,
            "snapshot_id": f"snapshot-{ordinal:02d}",
            "snapshot_sha256": f"{ordinal + 1:064x}",
            "consumption_output_sha256": f"{ordinal + 50:064x}",
        }
        for ordinal, item in enumerate(training)
    ]
    method_declaration = campaign_protocol.method_declaration(spec, candidate)
    method_evaluation = campaigns.evaluate_method(method_declaration, spec.constraints)
    proposal = {
        "schema_version": campaigns.SCHEMA_VERSION,
        "proposal_id": "pending-bo-proposal",
        "campaign_id": campaign["campaign_id"],
        "ordinal": 49,
        "state": "pending",
        "candidate": candidate,
        "candidate_sha256": campaigns.sha256(candidate),
        "policy": "bo_mcp_gp_logei",
        "proposal_source": "bo_mcp_gp_logei",
        "training": {
            "count": 49,
            "sha256": training_digest,
            "snapshot_members": source_members,
        },
        "backend": backend,
        "method": {
            "method_id": "campaign-method-49",
            "input_sha256": "e" * 64,
            "canonical_program_sha256": campaigns.sha256(
                method_evaluation.canonical.model_dump(mode="json")
            ),
            "canonical_program": method_evaluation.canonical.model_dump(mode="json"),
        },
        "input_sha256": "f" * 64,
        "created_at": "2026-10-30T00:00:00Z",
    }
    pending = campaigns._proposal_summary(proposal)
    campaign_summary = {
        **campaign,
        "revision": 100,
        "state": "proposal_pending",
        "consumed_evaluations": 49,
        "budget_remaining": 1,
        "pending_proposal": pending,
        "history": [
            {
                "ordinal": ordinal,
                "consumption_id": f"consumption-{ordinal:02d}",
                "proposal_id": f"proposal-{ordinal:02d}",
                "snapshot_id": f"snapshot-{ordinal:02d}",
                "candidate": item["candidate"],
                "analytical_acceptance": "accepted",
                "runtime_preference_satisfied": True,
                "utility_status": "scored",
                "score": ordinal / 49.0,
                "evaluation_cost": 1.0,
                "runtime_min": item["candidate"]["acquisition_duration_min"],
                "optimizer_target": {
                    "profile": configuration["target_profile"],
                    "name": configuration["target_name"],
                    "direction": "maximize",
                    "status": "scored",
                    "value": item["score"],
                },
            }
            for ordinal, item in enumerate(training)
        ],
    }
    response = {
        "schema_version": campaigns.SCHEMA_VERSION,
        "campaign_id": campaign["campaign_id"],
        "revision": 100,
        "proposal": proposal,
        "campaign": campaign_summary,
    }
    campaigns._bounded(proposal, "Campaign proposal")
    campaigns._bounded(response, "49-row BO suggestion response")
    assert len(canonical(response).encode("utf-8")) < campaigns.MAX_RECORD_BYTES
