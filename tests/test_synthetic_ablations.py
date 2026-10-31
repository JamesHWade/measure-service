"""Frozen-input, accounting and recovery checks for the matched ablation runner."""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from measure_service import (
    campaign_protocol,
    campaigns,
    experiments,
    gaussian_producer,
    observations,
    retention,
)
from measure_service.benchmarks import synthetic_ablations as ablations
from measure_service.benchmarks import synthetic_campaigns as shared
from measure_service.storage import registry_connection, sha256


def _backend_report(
    spec, study_sha256: str, *, succeed: bool, source_tree_sha256: str = "d" * 64
) -> dict:
    spec = campaign_protocol.parse_campaign_protocol(spec)
    protocol_sha = campaign_protocol.campaign_protocol_sha256(spec)
    document, _ = ablations._read_study_document()
    cases = []
    for target, prior, pattern, history_length in sorted(ablations._expected_preflight_routes()):
        spec_value = spec.model_dump(mode="json")
        spec_value.update({"optimizer_target": target, "surrogate_noise_prior": prior})
        route_spec = campaign_protocol.parse_campaign_protocol(spec_value)
        route_protocol_sha = campaign_protocol.campaign_protocol_sha256(route_spec)
        rows = ablations._expected_preflight_history(target, pattern, history_length, document)
        config = campaign_protocol.optimizer_configuration(route_spec)
        runtime = {
            "python_version": "3.13.11",
            "implementation": "CPython",
            "platform": "darwin",
            "device": "cpu",
            "elapsed_seconds": 0.1,
        }
        noise_prior = {
            "family": "Gamma",
            "concentration": config["noise_prior_params"][0],
            "rate": config["noise_prior_params"][1],
            "space": "standardized_target_variance",
            "constraint_lower_bound": 0.0001,
            "learned": True,
        }
        request = {
            "schema": "measure-service-bo-request/1",
            "spec": route_spec.model_dump(mode="json"),
            "training_data": rows,
            "seed": 17,
            "protocol_sha256": route_protocol_sha,
            "training_sha256": sha256(rows),
            "adapter_source_sha256": "a" * 64,
            "campaign_protocol_source_sha256": "b" * 64,
            "design_kernel_source_sha256": "c" * 64,
        }
        backend = {
            "backend_id": "bo-engine/BoTorchBackend",
            "adapter_profile": config["adapter_profile"],
            "backend_name": "bo-mcp",
            "backend_class": "BoTorchBackend",
            "generation_method": "bo",
            "model_type": "SingleTaskGP",
            "acquisition_method": "expected_improvement",
            "acquisition_implementation": "qLogExpectedImprovement",
            "protocol_sha256": route_protocol_sha,
            "training_sha256": sha256(rows),
            "training_count": history_length,
            "seed": 17,
            "source_revision": None,
            "source_tree_sha256": source_tree_sha256,
            "adapter_source_sha256": request["adapter_source_sha256"],
            "campaign_protocol_source_sha256": request["campaign_protocol_source_sha256"],
            "design_kernel_source_sha256": request["design_kernel_source_sha256"],
            "optimization_strategy": "expected improvement",
            "distribution_versions": {
                "bo-engine": "0.1.0",
                "botorch": "0.15.1",
                "gpytorch": "1.15.2",
                "measure-service": None,
                "torch": "2.9.0",
            },
            "settings": {
                "parameters": ["strong_solvent_fraction", "acquisition_duration_min"],
                "objective": {"name": config["target_name"], "direction": "maximize"},
                "batch_size": 1,
                "iteration": history_length,
                "acquisition_method": "expected_improvement",
                "acquisition_implementation": "qLogExpectedImprovement",
                "input_transform": "BoTorch SingleTaskGP default input normalization",
                "outcome_transform": "BoTorch SingleTaskGP default outcome standardization",
                "kernel": (
                    "RBF with automatic relevance determination (ARD); BoTorch SingleTaskGP "
                    "default, no custom covar_module"
                ),
                "acquisition_optimization": {
                    "num_restarts": 20,
                    "raw_samples": 512,
                    "configured_num_restarts": 20,
                    "configured_raw_samples": 512,
                },
                "backend_state": "stateless; no prior backend state supplied",
                "replay": "ordered training history is supplied on every call",
                "noise_prior": noise_prior,
            },
            "runtime": runtime,
            "warnings": [],
            "suggestion_provenance": {
                "generation_method": "bo",
                "iteration": history_length,
                "batch_index": 0,
                "acquisition_function": "expected_improvement",
                "model_type": "SingleTaskGP (Gaussian Process)",
                "random_seed": 17,
                "confidence_level": None,
            },
        }
        cases.append(
            {
                "case_id": f"{target}:{prior}:{pattern}:n{history_length}",
                "target": target,
                "noise_prior": prior,
                "pattern": pattern,
                "history_length": history_length,
                "attempted": True,
                "request": request,
                "success": succeed,
                "candidate": (
                    {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 6.0}
                    if succeed
                    else None
                ),
                "backend_provenance": backend if succeed else None,
                "proposal_elapsed_seconds": 0.1 if succeed else None,
                "error": None if succeed else "RuntimeError: synthetic preflight failure",
            }
        )
    return {
        "schema_version": 1,
        "profile": ablations.PRE_FLIGHT_PROFILE,
        "fixture_sha256": study_sha256,
        "study_protocol_sha256": protocol_sha,
        "seed": 17,
        "expected_case_count": 16,
        "attempted_case_count": 16,
        "success_count": 16 if succeed else 0,
        "failed_count": 0 if succeed else 16,
        "qualified": succeed,
        "preflight_elapsed_seconds": 1.6,
        "cases": cases,
    }


def _fake_campaign(plan: dict, budget: int = 10) -> dict:
    accepted = plan["arm_id"] in {"gp-confirmed-default", "gp-confirmed-low-noise"}
    document, _ = ablations._read_study_document()
    task_bindings = [
        {
            "task_id": task["task_id"],
            "producer": {"producer_id": f"producer-{task['task_id']}"},
            "contract": {"contract_id": f"contract-{task['task_id']}"},
        }
        for task in document["tasks"]
    ]
    protocol = ablations._protocol_spec(document, task_bindings, plan["arm"])
    evaluations = []
    for ordinal in range(budget):
        analytical = accepted and ordinal == 4
        score = 2.5 if ordinal == 4 else 1.25
        target = {
            "profile": (
                "confirmed-acceptance-lower-bound/1"
                if plan["optimizer_target_config"] == "confirmed_acceptance_reward"
                else "moses-botier-1.0.0-binary64/1"
            ),
            "name": (
                "confirmed_acceptance_reward"
                if plan["optimizer_target_config"] == "confirmed_acceptance_reward"
                else "utility"
            ),
            "direction": "maximize",
            "status": "scored",
            "value": float(analytical)
            if plan["optimizer_target_config"] == "confirmed_acceptance_reward"
            else score,
        }
        metrics = {
            "completed_simulator_attempt": True,
            "consumed_evaluation": True,
            "analytical_acceptance": "accepted" if analytical else "not_accepted",
            "strict_analytical_success": analytical,
            "utility_status": "scored",
            "score": score,
            "score_ge_2_success": score >= 2,
            "detected_count": 3 if analytical else 2,
            "retention_times_min": [2.0, 3.0, 4.0],
            "finite_resolution_count": 2,
            "finite_resolutions": [score, 0.8],
            "censored_analyte_count": 0,
            "runtime_preference_satisfied": True,
            "declared_evaluation_cost": 1.0,
            "declared_evaluation_cost_unit": "synthetic_evaluation",
            "simulated_acquisition_time_min": 4.0,
            "declared_solvent_use_ml": {"A": 1.0, "B": 1.0},
            "declared_solvent_volume_ml": 2.0,
            "optimizer_walltime_seconds": 0.1 if ordinal >= 3 else None,
        }
        evaluations.append(
            {
                "campaign_id": f"fake-{plan['arm_id']}-{plan['task_id']}-{plan['seed']}",
                "task_id": plan["task_id"],
                "seed": plan["seed"],
                "policy": plan["policy"],
                "ordinal": ordinal,
                "budget": budget,
                "status": "consumed",
                "proposal_id": f"proposal-{plan['arm_id']}-{ordinal}",
                "candidate": (
                    campaign_protocol.initial_candidate(protocol, plan["seed"], ordinal)
                    if ordinal < document["initial_design_size"]
                    else {
                        "strong_solvent_fraction": 0.5,
                        "acquisition_duration_min": 4.0,
                    }
                ),
                "consumption": {"result": {"optimizer_target": target}},
                "metrics": metrics,
            }
        )
    summaries = shared._campaign_summaries(evaluations, budget)
    return {
        "campaign_id": f"fake-{plan['arm_id']}-{plan['task_id']}-{plan['seed']}",
        "task_id": plan["task_id"],
        "seed": plan["seed"],
        "policy": plan["policy"],
        "status": "budget_complete",
        "failure": None,
        "evaluation_budget": budget,
        "consumed_evaluations": budget,
        "budget_remaining": 0,
        "completed_simulator_attempt_count": budget,
        "unconsumed_completed_simulator_attempt_count": 0,
        "recovered_interruptions": [],
        "pending_proposal": None,
        "campaign_revision": 1 + 2 * budget,
        "campaign_state": "budget_complete",
        "history": [],
        "evaluations": evaluations,
        "trajectory_summaries": summaries,
    }


def _fake_campaign_executor(monkeypatch, plans):
    by_key = {
        (item["arm_operation_sha256"], item["task_id"], item["seed"], item["policy"]): item
        for item in plans
    }

    def execute(**kwargs):
        plan = by_key[
            (
                kwargs["default_sha256"],
                kwargs["task_id"],
                kwargs["seed"],
                kwargs["policy"],
            )
        ]
        return _fake_campaign(plan, kwargs["planned_budget"])

    monkeypatch.setattr(shared, "_execute_campaign", execute)


def test_frozen_study_keeps_original_tasks_and_declares_sixty_campaigns():
    document, digest = ablations._read_study_document()
    plans = ablations._plan(document, digest)

    assert digest == ablations.DEFAULT_SHA256
    assert len(plans) == 60
    assert len({plan["arm_operation_sha256"] for plan in plans}) == 5
    assert {arm["arm_id"] for arm in document["arms"]} == {
        "random-control",
        "gp-utility-default",
        "gp-confirmed-default",
        "gp-utility-low-noise",
        "gp-confirmed-low-noise",
    }
    assert [task["cohort_role"] for task in document["tasks"]] == [
        "regression",
        "regression",
        "held_out",
        "held_out",
    ]
    assert len(plans) * document["evaluation_budget"] == 600


def test_all_arms_share_each_seeded_initial_design_coordinate():
    document, _ = ablations._read_study_document()
    bindings = [
        {
            "task_id": task["task_id"],
            "producer": {"producer_id": f"producer-{task['task_id']}"},
            "contract": {"contract_id": f"contract-{task['task_id']}"},
        }
        for task in document["tasks"]
    ]
    protocols = {
        arm["arm_id"]: {
            "spec": ablations._protocol_spec(document, bindings, arm),
        }
        for arm in document["arms"]
    }

    design = ablations._common_initial_design(document, protocols)

    assert len(design) == 9
    assert all(len(row["verified_arm_ids"]) == 5 for row in design)
    assert {(row["seed"], row["ordinal"]) for row in design} == {
        (seed, ordinal)
        for seed in document["seeds"]
        for ordinal in range(document["initial_design_size"])
    }
    assert all(len(row["candidate_sha256"]) == 64 for row in design)


def test_preflight_gate_requires_all_sixteen_model_guided_routes():
    document, digest = ablations._read_study_document()
    bindings = [
        {
            "task_id": task["task_id"],
            "producer": {"producer_id": f"producer-{task['task_id']}"},
            "contract": {"contract_id": f"contract-{task['task_id']}"},
        }
        for task in document["tasks"]
    ]
    spec = ablations._protocol_spec(document, bindings, document["arms"][0])
    good = _backend_report(spec, digest, succeed=True)
    failed = _backend_report(spec, digest, succeed=False)

    assert ablations._preflight_report_status(
        good, document, digest, campaign_protocol.campaign_protocol_sha256(spec)
    ) == (True, None)
    assert ablations._preflight_report_status(
        failed, document, digest, campaign_protocol.campaign_protocol_sha256(spec)
    ) == (False, None)
    wrong_history = copy.deepcopy(good)
    wrong_history["cases"][0]["request"]["training_data"][0]["score"] = 0.0
    wrong_history["cases"][0]["request"]["training_sha256"] = sha256(
        wrong_history["cases"][0]["request"]["training_data"]
    )
    assert (
        ablations._preflight_report_status(
            wrong_history, document, digest, campaign_protocol.campaign_protocol_sha256(spec)
        )[0]
        is False
    )
    fallback = copy.deepcopy(good)
    fallback["cases"][0]["backend_provenance"]["generation_method"] = "random"
    fallback["cases"][0]["backend_provenance"]["suggestion_provenance"]["generation_method"] = (
        "random"
    )
    assert (
        ablations._preflight_report_status(
            fallback, document, digest, campaign_protocol.campaign_protocol_sha256(spec)
        )[0]
        is False
    )
    changed_flow = copy.deepcopy(good)
    for case in changed_flow["cases"]:
        route_spec = dict(case["request"]["spec"])
        route_spec["flow_ml_min"] = 0.6
        route_spec = campaign_protocol.parse_campaign_protocol(route_spec)
        route_hash = campaign_protocol.campaign_protocol_sha256(route_spec)
        case["request"]["spec"] = route_spec.model_dump(mode="json")
        case["request"]["protocol_sha256"] = route_hash
        case["backend_provenance"]["protocol_sha256"] = route_hash
    assert (
        ablations._preflight_report_status(
            changed_flow, document, digest, campaign_protocol.campaign_protocol_sha256(spec)
        )[0]
        is False
    )
    changed_source = copy.deepcopy(good)
    changed_source["cases"][0]["backend_provenance"]["source_tree_sha256"] = "e" * 64
    assert (
        ablations._preflight_report_status(
            changed_source, document, digest, campaign_protocol.campaign_protocol_sha256(spec)
        )[0]
        is False
    )
    changed_kernel = copy.deepcopy(good)
    changed_kernel["cases"][0]["backend_provenance"]["settings"]["kernel"] = "Matern"
    assert (
        ablations._preflight_report_status(
            changed_kernel, document, digest, campaign_protocol.campaign_protocol_sha256(spec)
        )[0]
        is False
    )
    failed["attempted_case_count"] = 15
    assert (
        ablations._preflight_report_status(
            failed, document, digest, campaign_protocol.campaign_protocol_sha256(spec)
        )[0]
        is False
    )


def test_campaign_backend_drift_is_rejected_before_experiment_creation(monkeypatch):
    document, digest = ablations._read_study_document()
    bindings = [
        {
            "task_id": task["task_id"],
            "producer": {"producer_id": f"producer-{task['task_id']}"},
            "contract": {"contract_id": f"contract-{task['task_id']}"},
        }
        for task in document["tasks"]
    ]
    spec = ablations._protocol_spec(document, bindings, document["arms"][1])
    report = _backend_report(spec, digest, succeed=True)
    expected_backend = report["cases"][0]["backend_provenance"]
    expected_identity = shared._optimizer_backend_identity(expected_backend)
    changed_backend = copy.deepcopy(expected_backend)
    changed_backend["source_tree_sha256"] = "e" * 64
    proposal = {
        "proposal_id": "proposal-drift",
        "proposal_source": "optimizer",
        "backend": changed_backend,
        "candidate": {
            "strong_solvent_fraction": 0.5,
            "acquisition_duration_min": 6.0,
        },
        "candidate_sha256": "f" * 64,
        "method": {
            "method_id": "method-drift",
            "input_sha256": "a" * 64,
            "canonical_program_sha256": "b" * 64,
        },
    }
    monkeypatch.setattr(
        campaigns,
        "suggest_campaign",
        lambda *args, **kwargs: {
            "proposal": proposal,
            "campaign": {
                "task": {
                    "producer": {"producer_id": "producer-id"},
                    "contract": {"contract_id": "contract-id"},
                }
            },
        },
    )
    monkeypatch.setattr(
        experiments,
        "create_experiment",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("analytical pipeline was reached")
        ),
    )
    row = {}

    with pytest.raises(ValueError, match="differ from the selected qualified preflight"):
        shared._execute_evaluation(
            owner=ablations.OWNER,
            default_sha256=digest,
            campaign_id="campaign-drift",
            task_id=document["tasks"][0]["task_id"],
            seed=17,
            policy="bo_mcp_gp_logei",
            ordinal=3,
            expected_revision=7,
            row=row,
            expected_backend_identity=expected_identity,
            require_backend_identity=True,
        )

    assert row["failure_step"] == "preflight_backend_consistency"
    assert row["preflight_backend_validation"]["matches_selected_gate"] is False
    assert "experiment" not in row


def test_trajectories_and_pair_contrasts_keep_cohort_grains():
    document, digest = ablations._read_study_document()
    plans = ablations._plan(document, digest)
    records = [ablations._campaign_labels(_fake_campaign(plan), plan) for plan in plans]

    trajectories = ablations._trajectory_rows(records, document)
    pairs, summaries = ablations._paired_contrasts(records, document)

    assert len(trajectories) == 5 * 3 * 2 * 10
    assert len(pairs) == 8 * 12 * 2
    assert len(summaries) == 8 * 3 * 2
    assert {row["cohort_size"] for row in trajectories if row["cohort_role"] == "regression"} == {6}
    assert {row["cohort_size"] for row in trajectories if row["cohort_role"] == "held_out"} == {6}
    assert {row["cohort_size"] for row in trajectories if row["cohort_role"] == "combined"} == {12}
    random_comparisons = [
        row for row in pairs if row["contrast_kind"] == "joint_policy_vs_random_control"
    ]
    assert len(random_comparisons) == 4 * 12 * 2
    assert all("joint policy" in row["interpretation"] for row in random_comparisons)


def test_observed_initial_design_matches_predeclared_points_for_every_arm():
    document, digest = ablations._read_study_document()
    plans = ablations._plan(document, digest)
    records = [ablations._campaign_labels(_fake_campaign(plan), plan) for plan in plans]
    bindings = [
        {
            "task_id": task["task_id"],
            "producer": {"producer_id": f"producer-{task['task_id']}"},
            "contract": {"contract_id": f"contract-{task['task_id']}"},
        }
        for task in document["tasks"]
    ]
    protocols = {
        arm["arm_id"]: {"spec": ablations._protocol_spec(document, bindings, arm)}
        for arm in document["arms"]
    }
    common_design = ablations._common_initial_design(document, protocols)

    audit = ablations._observed_initial_design(records, document, common_design)

    assert audit["status"] == "verified"
    assert audit["planned_arm_candidate_count"] == 180
    assert audit["observed_candidate_count"] == 180
    records[0]["evaluations"][0]["candidate"]["strong_solvent_fraction"] += 0.01
    mismatched = ablations._observed_initial_design(records, document, common_design)
    assert mismatched["status"] == "mismatch"
    assert mismatched["mismatched_candidate_count"] == 1


def test_ablation_marker_rejects_unrelated_receipts(tmp_path):
    document, digest = ablations._read_study_document()
    data_dir = tmp_path / "registry"
    data_dir.mkdir()
    scopes = ablations._receipt_scopes(document, digest)
    shared._claim_or_verify_owned_store(
        data_dir,
        digest,
        profile=ablations.STORE_PROFILE,
        owner=ablations.OWNER,
        receipt_prefixes=scopes,
    )
    database = data_dir / "measure.sqlite3"
    before = database.read_bytes()
    with pytest.raises(ValueError, match="does not match this frozen runner"):
        shared._claim_or_verify_owned_store(
            data_dir,
            digest,
            profile=shared.PROFILE,
            owner=shared.OWNER,
        )
    assert database.read_bytes() == before

    with shared._scoped_data_dir(data_dir):
        from measure_service.storage import registry_mutation

        registry_mutation(
            ablations.OWNER,
            "unrelated-record-key",
            {"operation": "unrelated"},
            lambda con: {"record_id": "foreign-response"},
        )
        with pytest.raises(ValueError, match="receipt outside this frozen runner"):
            shared._verify_owned_store(
                data_dir,
                digest,
                profile=ablations.STORE_PROFILE,
                owner=ablations.OWNER,
                receipt_prefixes=scopes,
            )


def test_unplanned_campaign_is_rejected_before_registration_or_export(tmp_path, monkeypatch):
    document, digest = ablations._read_study_document()
    data_dir = tmp_path / "registry"
    data_dir.mkdir()
    scopes = ablations._receipt_scopes(document, digest)
    shared._claim_or_verify_owned_store(
        data_dir,
        digest,
        profile=ablations.STORE_PROFILE,
        owner=ablations.OWNER,
        receipt_prefixes=scopes,
    )
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(data_dir))
    with shared._scoped_data_dir(data_dir):
        protocols, _, _ = ablations._register_inputs(document, digest)
        arm = next(item for item in document["arms"] if item["arm_id"] == "gp-utility-default")
        task_id = document["tasks"][0]["task_id"]
        campaigns.create_campaign(
            ablations.OWNER,
            protocols[arm["arm_id"]]["protocol"]["protocol_id"],
            task_id,
            document["seeds"][0],
            "random",
            shared._key(
                ablations._arm_digest(digest, arm),
                "campaign",
                task_id,
                document["seeds"][0],
                "random",
                "create",
            ),
        )

    database = data_dir / "measure.sqlite3"
    before = database.read_bytes()
    output = tmp_path / "bundle"
    with pytest.raises(ValueError, match="outside the frozen"):
        ablations.run_synthetic_ablations(output, data_dir=data_dir)

    assert database.read_bytes() == before
    assert not output.exists()


@pytest.mark.parametrize(
    "resource_type", ["retention_model", "gaussian_producer", "observation_contract"]
)
def test_unplanned_partial_common_resource_is_rejected_before_export(
    tmp_path, monkeypatch, resource_type
):
    document, digest = ablations._read_study_document()
    data_dir = tmp_path / "registry"
    data_dir.mkdir()
    shared._claim_or_verify_owned_store(
        data_dir,
        digest,
        profile=ablations.STORE_PROFILE,
        owner=ablations.OWNER,
        receipt_prefixes=ablations._receipt_scopes(document, digest),
    )
    task = document["tasks"][0]
    task_id = task["task_id"]
    with shared._scoped_data_dir(data_dir):
        if resource_type == "retention_model":
            spec = ablations._retention_spec(task, document, digest)
            spec["analytes"][0]["ln_k0"] += 0.25
            retention.register_retention_model(
                ablations.OWNER,
                f"Predeclared ablation retention {task_id}",
                spec,
                shared._key(digest, "ablation_retention_model", task_id),
            )
        elif resource_type == "gaussian_producer":
            model = retention.register_retention_model(
                ablations.OWNER,
                f"Predeclared ablation retention {task_id}",
                ablations._retention_spec(task, document, digest),
                shared._key(digest, "ablation_retention_model", task_id),
            )
            spec = ablations._producer_spec(task, digest)
            spec["widths"][0]["fwhm_min"] += 0.25
            gaussian_producer.register_gaussian_producer(
                ablations.OWNER,
                f"Predeclared ablation Gaussian producer {task_id}",
                model["model_id"],
                spec,
                shared._key(digest, "ablation_gaussian_producer", task_id),
            )
        else:
            spec = ablations._contract_spec(task, document)
            spec["resolution_threshold"] += 0.1
            observations.register_observation_contract(
                ablations.OWNER,
                f"Predeclared ablation analytical contract {task_id}",
                spec,
                shared._key(digest, "ablation_observation_contract", task_id),
            )

    database = data_dir / "measure.sqlite3"
    before = database.read_bytes()
    output = tmp_path / "bundle"
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(data_dir))
    with pytest.raises(ValueError, match="common task resource differs"):
        ablations.run_synthetic_ablations(output, data_dir=data_dir)

    assert database.read_bytes() == before
    assert not output.exists()


def test_valid_partial_common_registration_can_resume(tmp_path):
    document, digest = ablations._read_study_document()
    data_dir = tmp_path / "registry"
    data_dir.mkdir()
    shared._claim_or_verify_owned_store(
        data_dir,
        digest,
        profile=ablations.STORE_PROFILE,
        owner=ablations.OWNER,
        receipt_prefixes=ablations._receipt_scopes(document, digest),
    )
    task = document["tasks"][0]
    task_id = task["task_id"]
    with shared._scoped_data_dir(data_dir):
        retention.register_retention_model(
            ablations.OWNER,
            f"Predeclared ablation retention {task_id}",
            ablations._retention_spec(task, document, digest),
            shared._key(digest, "ablation_retention_model", task_id),
        )
        ablations._validate_existing_plan(document, digest)


def test_unmarked_foreign_database_is_preserved_without_output(tmp_path):
    data_dir = tmp_path / "foreign"
    data_dir.mkdir()
    database = data_dir / "measure.sqlite3"
    with sqlite3.connect(database) as con:
        con.execute("CREATE TABLE unrelated_user_data(value TEXT)")
        con.execute("INSERT INTO unrelated_user_data VALUES ('keep-me')")
    before = database.read_bytes()
    output = tmp_path / "bundle"

    with pytest.raises(ValueError, match="unmarked SQLite store"):
        ablations.run_synthetic_ablations(output, data_dir=data_dir)

    assert database.read_bytes() == before
    assert not output.exists()


def test_failed_initial_preflight_publishes_all_six_hundred_as_unknown(tmp_path, monkeypatch):
    document, digest = ablations._read_study_document()

    def fail_preflight(spec):
        return _backend_report(spec, digest, succeed=False)

    monkeypatch.setattr(
        "measure_service.optimizer_backend.preflight_ablation_backend", fail_preflight
    )
    monkeypatch.setattr(
        shared,
        "_execute_campaign",
        lambda **kwargs: pytest.fail("campaign execution must be blocked after failed preflight"),
    )
    output = tmp_path / "bundle"
    data_dir = tmp_path / "registry"
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(data_dir))

    report = ablations.run_synthetic_ablations(output, data_dir=data_dir)

    rows = [json.loads(line) for line in (output / "evaluations.jsonl").read_text().splitlines()]
    assert report["completed"] is False
    assert report["planned_denominators"]["campaigns_total"] == 60
    assert report["planned_denominators"]["evaluations_total"] == 600
    assert len(rows) == 600
    assert {row["status"] for row in rows} == {"preflight_blocked_unknown"}
    assert report["consumed_evaluation_count"] == 0
    assert all(
        item["cohorts"]["combined"]["recorded_campaign_count"] == 0
        for item in report["arm_metrics"].values()
    )
    assert all(item["recorded_pair_count"] == 0 for item in report["contrast_summaries"])
    with registry_connection() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM campaign_runs WHERE owner=?", (ablations.OWNER,)
            ).fetchone()[0]
            == 0
        )
    assert (output / "complete.json").is_file()


def test_registration_failure_with_no_common_design_still_publishes_fixed_unknowns(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        ablations,
        "_register_inputs",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic setup failure")),
    )
    output = tmp_path / "bundle"
    report = ablations.run_synthetic_ablations(output, data_dir=tmp_path / "registry")
    rows = [json.loads(line) for line in (output / "evaluations.jsonl").read_text().splitlines()]

    assert report["completed"] is False
    assert report["planned_denominators"]["evaluations_total"] == 600
    assert len(rows) == 600
    assert {row["status"] for row in rows} == {"preflight_blocked_unknown"}
    assert report["observed_initial_design"]["status"] == "incomplete"
    assert report["observed_initial_design"]["planned_arm_candidate_count"] == 180
    assert report["observed_initial_design"]["observed_candidate_count"] == 0


def test_failed_resume_keeps_consumed_outcomes_and_blocks_new_proposals(tmp_path, monkeypatch):
    document, digest = ablations._read_study_document()
    data_dir = tmp_path / "registry"
    data_dir.mkdir()
    scopes = ablations._receipt_scopes(document, digest)
    shared._claim_or_verify_owned_store(
        data_dir,
        digest,
        profile=ablations.STORE_PROFILE,
        owner=ablations.OWNER,
        receipt_prefixes=scopes,
    )
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(data_dir))
    with shared._scoped_data_dir(data_dir):
        protocols, _, _ = ablations._register_inputs(document, digest)
        arm = next(item for item in document["arms"] if item["arm_id"] == "random-control")
        plan = next(
            item
            for item in ablations._plan(document, digest)
            if item["arm_id"] == "random-control"
            and item["task_id"] == document["tasks"][0]["task_id"]
            and item["seed"] == 17
        )
        original = campaigns.suggest_campaign
        calls = 0

        def fail_second_suggestion(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic interruption before proposal commit")
            return original(*args, **kwargs)

        monkeypatch.setattr(campaigns, "suggest_campaign", fail_second_suggestion)
        partial = shared._execute_campaign(
            owner=ablations.OWNER,
            protocol_id=protocols[arm["arm_id"]]["protocol"]["protocol_id"],
            task_id=plan["task_id"],
            seed=plan["seed"],
            policy=plan["policy"],
            default_sha256=plan["arm_operation_sha256"],
            planned_budget=10,
        )
        assert partial["consumed_evaluations"] == 1

    def fail_preflight(spec):
        return _backend_report(spec, digest, succeed=False)

    monkeypatch.setattr(
        "measure_service.optimizer_backend.preflight_ablation_backend", fail_preflight
    )
    output = tmp_path / "resume-bundle"
    report = ablations.run_synthetic_ablations(output, data_dir=data_dir)

    campaign = next(
        item
        for item in report["campaigns"]
        if item["arm_id"] == "random-control"
        and item["task_id"] == document["tasks"][0]["task_id"]
        and item["seed"] == 17
    )
    assert report["completed"] is False
    assert report["consumed_evaluation_count"] == 1
    assert campaign["evaluations"][0]["status"] == "consumed"
    assert campaign["evaluations"][0]["consumption"]["result"]["optimizer_target"]
    assert campaign["evaluations"][1]["status"] == "preflight_blocked_unknown"
    assert campaign["evaluations"][0]["metrics"]["score"] is not None
    assert calls == 3  # one committed proposal, one interrupted call, one replayed receipt
    with registry_connection() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM campaign_proposals WHERE owner=?", (ablations.OWNER,)
            ).fetchone()[0]
            == 1
        )


def test_partial_resume_rejects_backend_drift_and_preserves_known_campaign_data(
    tmp_path, monkeypatch
):
    document, digest = ablations._read_study_document()
    data_dir = tmp_path / "registry"
    data_dir.mkdir()
    scopes = ablations._receipt_scopes(document, digest)
    shared._claim_or_verify_owned_store(
        data_dir,
        digest,
        profile=ablations.STORE_PROFILE,
        owner=ablations.OWNER,
        receipt_prefixes=scopes,
    )
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(data_dir))
    with shared._scoped_data_dir(data_dir):
        protocols, _, _ = ablations._register_inputs(document, digest)
        arm = next(item for item in document["arms"] if item["arm_id"] == "random-control")
        monkeypatch.setattr(
            "measure_service.optimizer_backend.preflight_ablation_backend",
            lambda spec: _backend_report(spec, digest, succeed=True),
        )
        first_gate = ablations._record_preflight_attempt(
            document, digest, protocols[arm["arm_id"]]["protocol"], 1
        )
        assert first_gate["qualified"] is True
        plan = next(
            item
            for item in ablations._plan(document, digest)
            if item["arm_id"] == "random-control"
            and item["task_id"] == document["tasks"][0]["task_id"]
            and item["seed"] == 17
        )
        original_suggest = campaigns.suggest_campaign
        calls = 0

        def interrupt_before_next_commit(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic interruption before second proposal commit")
            return original_suggest(*args, **kwargs)

        monkeypatch.setattr(campaigns, "suggest_campaign", interrupt_before_next_commit)
        partial = shared._execute_campaign(
            owner=ablations.OWNER,
            protocol_id=protocols[arm["arm_id"]]["protocol"]["protocol_id"],
            task_id=plan["task_id"],
            seed=plan["seed"],
            policy=plan["policy"],
            default_sha256=plan["arm_operation_sha256"],
            planned_budget=10,
        )
        assert partial["consumed_evaluations"] == 1

    monkeypatch.setattr(
        "measure_service.optimizer_backend.preflight_ablation_backend",
        lambda spec: _backend_report(spec, digest, succeed=True, source_tree_sha256="e" * 64),
    )
    report = ablations.run_synthetic_ablations(tmp_path / "resume-bundle", data_dir=data_dir)
    campaign = next(
        item
        for item in report["campaigns"]
        if item["arm_id"] == "random-control"
        and item["task_id"] == document["tasks"][0]["task_id"]
        and item["seed"] == 17
    )

    assert report["preflight"]["qualified"] is True
    assert report["preflight"]["study_consistent"] is False
    assert report["preflight"]["failure"]["type"] == "PreflightStudyIdentityMismatch"
    assert report["completed"] is False
    assert report["consumed_evaluation_count"] == 1
    assert campaign["evaluations"][0]["status"] == "consumed"
    assert campaign["evaluations"][1]["status"] == "preflight_blocked_unknown"
    with registry_connection() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM campaign_proposals WHERE owner=?", (ablations.OWNER,)
            ).fetchone()[0]
            == 1
        )


def test_complete_resume_reuses_saved_gate_without_backend_import(tmp_path, monkeypatch):
    document, digest = ablations._read_study_document()
    plans = ablations._plan(document, digest)
    _fake_campaign_executor(monkeypatch, plans)
    first_gate_calls = 0

    def qualify(spec):
        nonlocal first_gate_calls
        first_gate_calls += 1
        return _backend_report(spec, digest, succeed=True)

    monkeypatch.setattr("measure_service.optimizer_backend.preflight_ablation_backend", qualify)
    data_dir = tmp_path / "registry"
    first = ablations.run_synthetic_ablations(tmp_path / "first-bundle", data_dir=data_dir)
    assert first["completed"] is True
    assert first_gate_calls == 1

    complete_stored = {
        (plan["arm_id"], plan["task_id"], plan["seed"]): {
            "state": "budget_complete",
            "consumed_evaluations": 10,
        }
        for plan in plans
    }
    monkeypatch.setattr(ablations, "_stored_campaign_index", lambda *args: complete_stored)

    def backend_must_not_be_imported(name):
        raise AssertionError(f"completed resume attempted a fresh backend import: {name}")

    monkeypatch.setattr(ablations.importlib, "import_module", backend_must_not_be_imported)
    second = ablations.run_synthetic_ablations(tmp_path / "second-bundle", data_dir=data_dir)

    assert second["completed"] is True
    assert second["preflight"]["reused_saved_gate"] is True
    assert second["preflight"]["attempt_count"] == 1
    assert first_gate_calls == 1
