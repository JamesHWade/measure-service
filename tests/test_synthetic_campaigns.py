"""Integration and accounting checks for the fixed synthetic comparison."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

import pytest

from measure_methods.gaussian_observations import RESOLUTION_DEFINITION
from measure_methods.trajectories import SuccessObservation, summarize_campaign_success
from measure_service import (
    campaign_protocol_registry,
    campaigns,
    gaussian_producer,
    observations,
    retention,
)
from measure_service.benchmarks import synthetic_campaigns as benchmark
from measure_service.storage import registry_connection, sha256


def _mini_protocol(owner: str, key_prefix: str = "fixture") -> dict:
    model = retention.register_retention_model(
        owner,
        "Restart fixture retention model",
        {
            "gradient_coordinate": "column_inlet_at_analyte_entry",
            "analytes": [
                {"analyte_id": "A", "model": "linear", "ln_k0": 3.0, "S1": 4.0},
                {"analyte_id": "B", "model": "linear", "ln_k0": 4.5, "S1": 7.0},
            ],
            "column_length_m": 0.1,
            "effective_area_m2": 2.5e-6,
            "strong_solvent": "B",
            "parameter_origin": "synthetic",
            "parameter_reference": "Test-only declared synthetic inputs",
        },
        f"{key_prefix}-model",
    )
    producer = gaussian_producer.register_gaussian_producer(
        owner,
        "Restart fixture widths",
        model["model_id"],
        {
            "widths": [
                {"analyte_id": "A", "fwhm_min": 0.15},
                {"analyte_id": "B", "fwhm_min": 0.15},
            ],
            "width_origin": "synthetic_assumption",
            "width_reference": "Test-only fixed synthetic width",
        },
        f"{key_prefix}-producer",
    )
    contract = observations.register_observation_contract(
        owner,
        "Restart fixture contract",
        {
            "required_analytes": ["A", "B"],
            "resolution_definition": RESOLUTION_DEFINITION,
            "resolution_threshold": 1.5,
            "max_acquisition_duration_min": 10.0,
            "runtime_preference_min": 5.0,
            "count_threshold": 0.5,
        },
        f"{key_prefix}-contract",
    )
    return campaign_protocol_registry.register_campaign_protocol(
        owner,
        "Restart fixture protocol",
        {
            "domain": {
                "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
                "acquisition_duration_min": {"lower": 2.0, "upper": 10.0},
            },
            "flow_ml_min": 0.5,
            "constraints": {"max_acquisition_duration_min": 10.0},
            "tasks": [
                {
                    "task_id": "fixture-task",
                    "producer_id": producer["producer_id"],
                    "observation_contract_id": contract["contract_id"],
                }
            ],
            "seeds": [17],
            "evaluation_budget": 4,
            "initial_design_size": 3,
            "evaluation_cost": 1.0,
            "evaluation_cost_unit": "synthetic_evaluation",
        },
        f"{key_prefix}-protocol",
    )


def test_predeclared_input_is_exact_and_tampering_is_rejected(tmp_path):
    document, digest = benchmark._read_default_document()
    assert digest == "0728c6b2e03132d2889fef030c57a5331cae24067ca3fc5e6c40256f1238b38e"
    assert [task["task_id"] for task in document["tasks"]] == [
        "parallel-linear",
        "crossing-linear",
    ]
    assert document["seeds"] == [17, 29, 43]
    assert document["evaluation_budget"] == 10
    assert document["initial_design_size"] == 3

    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps({**document, "evaluation_budget": 9}), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256"):
        benchmark._read_default_document(tampered)


def test_trajectory_accounting_keeps_fixed_denominators_and_unknown_prefixes():
    complete = summarize_campaign_success(
        [SuccessObservation(1, "high", True), SuccessObservation(2, "high", False)],
        budget=10,
    )
    partial = summarize_campaign_success(
        [SuccessObservation(1, "high", False), SuccessObservation(2, "high", None)],
        budget=10,
    )
    records = [
        {
            "task_id": "parallel-linear",
            "seed": 17,
            "policy": "random",
            "trajectory_summaries": {
                benchmark.STRICT_SUCCESS_SERIES: complete,
                benchmark.SCORE_SUCCESS_SERIES: complete,
            },
        },
        {
            "task_id": "crossing-linear",
            "seed": 29,
            "policy": "random",
            "trajectory_summaries": {
                benchmark.STRICT_SUCCESS_SERIES: partial,
                benchmark.SCORE_SUCCESS_SERIES: None,
            },
        },
    ]

    rows = benchmark._build_trajectory_rows(records)
    strict_at_one = next(
        row
        for row in rows
        if row["policy"] == "random"
        and row["series"] == benchmark.STRICT_SUCCESS_SERIES
        and row["high_fidelity_budget"] == 1
    )
    strict_at_two = next(
        row
        for row in rows
        if row["policy"] == "random"
        and row["series"] == benchmark.STRICT_SUCCESS_SERIES
        and row["high_fidelity_budget"] == 2
    )
    score_at_one = next(
        row
        for row in rows
        if row["policy"] == "random"
        and row["series"] == benchmark.SCORE_SUCCESS_SERIES
        and row["high_fidelity_budget"] == 1
    )
    assert strict_at_one["cohort_size"] == 6
    assert strict_at_one["success_count"] == 1
    assert strict_at_one["unknown_success_count"] == 4
    assert strict_at_one["observed_no_success_count"] == 1
    assert strict_at_two["success_count"] == 1
    assert strict_at_two["unknown_success_count"] == 5
    assert strict_at_two["observed_no_success_count"] == 0
    assert score_at_one["unknown_success_count"] == 5
    assert score_at_one["success_fraction_upper_bound"] == pytest.approx(1.0)


def test_resolution_availability_is_not_reported_as_detection():
    fields = benchmark._evaluation_metrics(
        {
            "consumption": {
                "result": {
                    "raw_metrics": {
                        "retention_statuses": ["eluted", "not_eluted"],
                        "retention_times_min": [2.0, None],
                        "resolution_statuses": ["observed", "observed"],
                        "peak_resolutions": [2.0, 0.8],
                    },
                    "analytical_acceptance": "not_accepted",
                    "runtime_preference_satisfied": True,
                    "utility": {"status": "scored", "score": 2.1},
                    "runtime_min": 4.0,
                    "evaluation_cost": 1.0,
                    "evaluation_cost_unit": "synthetic_evaluation",
                }
            },
            "observation": {
                "observation": {
                    "assessment": {
                        "detected_count": 1,
                        "unknown_count": 0,
                        "confirmed_absent_count": 1,
                    }
                }
            },
        }
    )
    assert fields["finite_resolution_count"] == 2
    assert fields["detected_count"] == 1
    assert fields["strict_analytical_success"] is False
    assert fields["score_ge_2_success"] is True


def test_full_setup_failure_keeps_unknown_budget_and_still_renders():
    document, _ = benchmark._read_default_document()
    campaigns = [
        benchmark._campaign_placeholder(
            task_id=task["task_id"],
            seed=seed,
            policy=policy,
            budget=document["evaluation_budget"],
            error={"type": "RuntimeError", "message": "fixture setup failure"},
        )
        for task in document["tasks"]
        for seed in document["seeds"]
        for policy in document["policies"]
    ]
    evaluations = [evaluation for campaign in campaigns for evaluation in campaign["evaluations"]]
    benchmark._validate_counts(campaigns, evaluations)
    trajectory = benchmark._build_trajectory_rows(campaigns)
    strict_first = next(
        row
        for row in trajectory
        if row["policy"] == "random"
        and row["series"] == benchmark.STRICT_SUCCESS_SERIES
        and row["high_fidelity_budget"] == 1
    )
    policy_metrics = benchmark._policy_metrics(campaigns)
    assert strict_first["cohort_size"] == 6
    assert strict_first["unknown_success_count"] == 6
    assert policy_metrics["random"]["declared_evaluation_cost_total_completed_attempts"] is None
    assert (
        policy_metrics["random"]["simulated_acquisition_time_total_min_completed_attempts"] is None
    )

    report = {
        "completed": False,
        "predeclared_input": {"sha256": "0" * 64},
        "policy_metrics": policy_metrics,
        "campaigns": campaigns,
    }
    markdown = benchmark._render_report_markdown(report)
    assert "Run status: **partial**" in markdown
    assert "unknown" in markdown


def test_output_preflight_preserves_existing_files(tmp_path):
    output = tmp_path / "existing-output"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")
    data_dir = tmp_path / "isolated-data"
    with pytest.raises(ValueError, match="absent or empty"):
        benchmark.run_synthetic_campaigns(output, data_dir=data_dir)
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not data_dir.exists()


def test_foreign_database_is_rejected_unchanged_without_publishing(tmp_path):
    data_dir = tmp_path / "foreign-store"
    data_dir.mkdir()
    database = data_dir / "measure.sqlite3"
    with sqlite3.connect(database) as con:
        con.execute("CREATE TABLE unrelated_user_data(value TEXT)")
        con.execute("INSERT INTO unrelated_user_data VALUES ('sentinel')")
    before = database.read_bytes()
    output = tmp_path / "evidence"

    with pytest.raises(ValueError, match="unknown SQLite table|unmarked SQLite store"):
        benchmark.run_synthetic_campaigns(output, data_dir=data_dir)

    assert database.read_bytes() == before
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT value FROM unrelated_user_data").fetchone() == ("sentinel",)
    assert not output.exists()


def test_marked_store_rejects_mixed_records(tmp_path, monkeypatch):
    data_dir = tmp_path / "registry"
    data_dir.mkdir()
    digest = benchmark.DEFAULT_SHA256
    benchmark._claim_or_verify_owned_store(data_dir, digest)
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(data_dir))
    _mini_protocol(
        benchmark.OWNER,
        key_prefix="unrelated-record",
    )

    with pytest.raises(ValueError, match="receipt outside this frozen runner"):
        benchmark._verify_owned_store(data_dir, digest)


def test_marked_store_rejects_a_different_frozen_input(tmp_path):
    data_dir = tmp_path / "registry"
    data_dir.mkdir()
    benchmark._claim_or_verify_owned_store(data_dir, "0" * 64)
    database = data_dir / "measure.sqlite3"
    before = database.read_bytes()

    with pytest.raises(ValueError, match="does not match this frozen runner"):
        benchmark._claim_or_verify_owned_store(data_dir, benchmark.DEFAULT_SHA256)

    assert database.read_bytes() == before


def test_post_commit_consumption_acknowledgement_is_recovered_once(tmp_path, monkeypatch):
    data_dir = tmp_path / "registry"
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(data_dir))
    owner = "synthetic-consumption-ack-test"
    protocol = _mini_protocol(owner, key_prefix="consume-ack-fixture")
    original = campaigns.consume_campaign_snapshot
    raised = False

    def raise_after_commit(*args, **kwargs):
        nonlocal raised
        response = original(*args, **kwargs)
        if not raised:
            raised = True
            raise RuntimeError("fixture lost the committed consume acknowledgement")
        return response

    monkeypatch.setattr(campaigns, "consume_campaign_snapshot", raise_after_commit)
    arguments = {
        "owner": owner,
        "protocol_id": protocol["protocol_id"],
        "task_id": "fixture-task",
        "seed": 17,
        "policy": "random",
        "default_sha256": "2" * 64,
        "planned_budget": 4,
    }
    campaign = benchmark._execute_campaign(**arguments)

    assert campaign["status"] == "budget_complete"
    assert campaign["consumed_evaluations"] == 4
    assert len(campaign["recovered_interruptions"]) == 1
    row = campaign["evaluations"][0]
    assert row["status"] == "consumed"
    assert row["acknowledgement_failure"]["step"] == "consume_campaign_snapshot"
    assert row["acknowledgement_failure"]["type"] == "RuntimeError"
    assert row["consumption_recovery"]["source"] == ("owned_campaign_history_and_consumption_read")
    assert row["consumption"]["proposal_id"] == row["proposal_id"]
    assert row["consumption"]["snapshot_id"] == row["snapshot"]["snapshot_id"]

    with registry_connection() as con:
        counts = {
            table: con.execute(f"SELECT COUNT(*) FROM {table} WHERE owner=?", (owner,)).fetchone()[
                0
            ]
            for table in ("campaign_runs", "campaign_proposals", "campaign_consumptions")
        }
    assert counts == {
        "campaign_runs": 1,
        "campaign_proposals": 4,
        "campaign_consumptions": 4,
    }

    repeated = benchmark._execute_campaign(**arguments)
    assert repeated["campaign_id"] == campaign["campaign_id"]
    assert repeated["status"] == "budget_complete"
    assert repeated["consumed_evaluations"] == 4
    assert [row["proposal_id"] for row in repeated["evaluations"]] == [
        row["proposal_id"] for row in campaign["evaluations"]
    ]
    assert [row["consumption"] for row in repeated["evaluations"]] == [
        row["consumption"] for row in campaign["evaluations"]
    ]
    with registry_connection() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM campaign_consumptions WHERE owner=?", (owner,)
            ).fetchone()[0]
            == 4
        )


def test_unconsumed_completed_attempt_keeps_declared_expenditure_and_walltime(
    tmp_path, monkeypatch
):
    data_dir = tmp_path / "registry"
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(data_dir))
    owner = "synthetic-partial-expenditure-test"
    protocol = _mini_protocol(owner, key_prefix="partial-expenditure-fixture")
    original_predict = retention.predict_retention
    prediction_calls = 0

    def fake_bo_candidate(spec, training, seed):
        return (
            {"strong_solvent_fraction": 0.31, "acquisition_duration_min": 6.5},
            {"training_sha256": sha256(training), "runtime": {"elapsed_seconds": 1.25}},
        )

    def fail_on_bo_prediction(owner, model_id, method_id, key):
        nonlocal prediction_calls
        prediction_calls += 1
        if prediction_calls == 4:
            raise RuntimeError("fixture failed after the completed simulator attempt")
        return original_predict(owner, model_id, method_id, key)

    monkeypatch.setattr(campaigns, "_bo_candidate", fake_bo_candidate)
    monkeypatch.setattr(retention, "predict_retention", fail_on_bo_prediction)
    campaign = benchmark._execute_campaign(
        owner=owner,
        protocol_id=protocol["protocol_id"],
        task_id="fixture-task",
        seed=17,
        policy="bo_mcp_gp_logei",
        default_sha256="3" * 64,
        planned_budget=4,
    )

    assert campaign["status"] == "partial_failure"
    assert campaign["consumed_evaluations"] == 3
    assert campaign["completed_simulator_attempt_count"] == 4
    assert campaign["unconsumed_completed_simulator_attempt_count"] == 1
    failed_row = campaign["evaluations"][3]
    assert failed_row["status"] == "partial_failure"
    assert failed_row["failure_step"] == "predict_retention"
    assert failed_row["metrics"]["completed_simulator_attempt"] is True
    assert failed_row["metrics"]["consumed_evaluation"] is False
    assert failed_row["metrics"]["strict_analytical_success"] is None
    assert failed_row["metrics"]["score_ge_2_success"] is None
    assert failed_row["metrics"]["optimizer_walltime_seconds"] == pytest.approx(1.25)

    policy = benchmark._policy_metrics([campaign])["bo_mcp_gp_logei"]
    completed_metrics = [
        row["metrics"]
        for row in campaign["evaluations"]
        if row.get("metrics", {}).get("completed_simulator_attempt") is True
    ]
    assert policy["consumed_evaluation_count"] == 3
    assert policy["completed_simulator_attempt_count"] == 4
    assert policy["unconsumed_completed_simulator_attempt_count"] == 1
    assert policy["declared_evaluation_cost_total_completed_attempts"] == pytest.approx(4.0)
    assert policy["declared_evaluation_cost_known_completed_attempt_count"] == 4
    assert policy["simulated_acquisition_time_total_min_completed_attempts"] == pytest.approx(
        sum(item["simulated_acquisition_time_min"] for item in completed_metrics)
    )
    assert policy["simulated_acquisition_time_known_completed_attempt_count"] == 4
    assert policy["declared_solvent_volume_known_completed_attempt_count"] == 4
    assert policy["declared_solvent_volume_total_ml_completed_attempts"] > 0
    assert policy["optimizer_walltime_sample_count"] == 1
    assert policy["optimizer_walltime_total_seconds"] == pytest.approx(1.25)


def test_random_campaign_resumes_after_a_fresh_process_exit(tmp_path, monkeypatch):
    data_dir = tmp_path / "registry"
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(data_dir))
    digest = "0" * 64
    data_dir.mkdir()
    benchmark._claim_or_verify_owned_store(data_dir, digest)
    owner = benchmark.OWNER
    protocol = _mini_protocol(owner, key_prefix=f"syn-cmp-{digest}-fixture")
    code = "\n".join(
        [
            "import os",
            "from measure_service import experiments",
            "from measure_service.benchmarks.synthetic_campaigns import _execute_campaign",
            "original = experiments.record_attempt",
            "def exit_after_attempt(*args, **kwargs):",
            "    original(*args, **kwargs)",
            "    os._exit(47)",
            "experiments.record_attempt = exit_after_attempt",
            "_execute_campaign(",
            f"    owner={owner!r}, protocol_id={protocol['protocol_id']!r}, "
            "task_id='fixture-task',",
            f"    seed=17, policy='random', default_sha256={digest!r}, planned_budget=4,",
            ")",
        ]
    )
    interrupted = subprocess.run([sys.executable, "-c", code], check=False)
    assert interrupted.returncode == 47

    resumed = benchmark._execute_campaign(
        owner=owner,
        protocol_id=protocol["protocol_id"],
        task_id="fixture-task",
        seed=17,
        policy="random",
        default_sha256=digest,
        planned_budget=4,
    )
    assert resumed["status"] == "budget_complete"
    assert resumed["consumed_evaluations"] == 4
    assert [row["status"] for row in resumed["evaluations"]] == [
        "consumed",
        "consumed",
        "consumed",
        "consumed",
    ]
    benchmark._verify_owned_store(data_dir, digest)

    repeated = benchmark._execute_campaign(
        owner=owner,
        protocol_id=protocol["protocol_id"],
        task_id="fixture-task",
        seed=17,
        policy="random",
        default_sha256=digest,
        planned_budget=4,
    )
    assert repeated["campaign_id"] == resumed["campaign_id"]
    assert repeated["consumed_evaluations"] == 4
    benchmark._verify_owned_store(data_dir, digest)
