"""Stored synthetic Gaussian observations and producer-evidence integrity."""

from __future__ import annotations

import math
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from measure_methods.gaussian_observations import RESOLUTION_DEFINITION
from measure_service import experiments, gaussian_producer, method_registry, observations, retention
from measure_service.methods import MethodDeclaration
from measure_service.storage import canonical, now, registry_connection, sha256


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    return tmp_path


def _method(owner="chemist", key="method", duration_min=10.0):
    declaration = MethodDeclaration.model_validate(
        {
            "time_unit": "min",
            "flow_unit": "mL/min",
            "solvents": ["A", "B"],
            "program": {
                "kind": "isocratic",
                "acquisition_duration": duration_min,
                "state": {"flow": 1.0, "solvent_fractions": {"A": 0.5, "B": 0.5}},
            },
        }
    )
    return method_registry.register_method(owner, "Synthetic method", declaration, None, key)


def _model_spec(
    *,
    analytes=None,
    parameter_origin="synthetic",
):
    return retention.RetentionModelSpec.model_validate(
        {
            "gradient_coordinate": "column_inlet_at_analyte_entry",
            "analytes": analytes
            or [
                {"analyte_id": "A", "model": "linear", "ln_k0": 0.0, "S1": 0.0},
                {"analyte_id": "B", "model": "linear", "ln_k0": math.log(2.0), "S1": 0.0},
            ],
            "column_length_m": 1.0,
            "effective_area_m2": 1e-6,
            "post_column_volume_ml": 0.0,
            "strong_solvent": "B",
            "parameter_origin": parameter_origin,
            "parameter_reference": "Independently specified synthetic retention coefficients",
        }
    )


def _register_model(owner="chemist", key="model", **changes):
    return retention.register_retention_model(
        owner, "Synthetic retention model", _model_spec(**changes), key
    )


def _register_producer(*, owner="chemist", key="producer", model=None, widths=None, **spec_changes):
    model = _register_model(owner=owner, key=f"{key}-model") if model is None else model
    if widths is None:
        widths = [
            {"analyte_id": "A", "fwhm_min": 0.2},
            {"analyte_id": "B", "fwhm_min": 0.2},
        ]
    spec = gaussian_producer.GaussianProducerSpec.model_validate(
        {
            "widths": widths,
            "width_origin": "synthetic_assumption",
            "width_reference": "Synthetic widths",
        }
        | spec_changes
    )
    producer = gaussian_producer.register_gaussian_producer(
        owner, "Synthetic Gaussian producer", model["model_id"], spec, key
    )
    return model, producer


def _contract(owner="chemist", key="contract", analytes=("A", "B"), **changes):
    spec = observations.ObservationContractSpec.model_validate(
        {
            "required_analytes": list(analytes),
            "resolution_definition": RESOLUTION_DEFINITION,
            "max_acquisition_duration_min": 10,
        }
        | changes
    )
    return observations.register_observation_contract(owner, "Gaussian cohort", spec, key)


def _bound_attempt(
    producer,
    contract,
    method,
    *,
    owner="chemist",
    key="attempt",
    fidelity=0.99,
    evaluator_kind="simulator",
    status="completed",
):
    experiment = experiments.create_experiment(
        owner,
        experiments.ExperimentSpec(
            title="Latent Gaussian test",
            source="synthetic fixture",
            observation_contract_id=contract["contract_id"],
            observation_producer_id=producer["producer_id"],
        ),
        f"{key}-experiment",
    )
    attempt = experiments.record_attempt(
        owner,
        experiment["experiment_id"],
        experiments.AttemptSpec(
            status=status,
            evaluator_kind=evaluator_kind,
            fidelity=fidelity,
            executable_method_id=method["method_id"],
        ),
        key,
    )
    return experiment, attempt


def _prepared(*, owner="chemist", key="prepared", analytes=None, width_specs=None, duration=10.0):
    if analytes is None:
        analytes = [
            {"analyte_id": "A", "model": "linear", "ln_k0": 0.0, "S1": 0.0},
            {"analyte_id": "B", "model": "linear", "ln_k0": math.log(2.0), "S1": 0.0},
        ]
    model, producer = _register_producer(
        owner=owner,
        key=f"{key}-producer",
        widths=width_specs,
    )
    if analytes != [
        {"analyte_id": "A", "model": "linear", "ln_k0": 0.0, "S1": 0.0},
        {"analyte_id": "B", "model": "linear", "ln_k0": math.log(2.0), "S1": 0.0},
    ]:
        # The common producer helper creates the default model; replace it when
        # a test needs a deliberately different retention cohort or event.
        model = _register_model(
            owner=owner,
            key=f"{key}-model-special",
            analytes=analytes,
        )
        widths = width_specs or [
            {"analyte_id": row["analyte_id"], "fwhm_min": 0.2} for row in analytes
        ]
        producer = gaussian_producer.register_gaussian_producer(
            owner,
            "Synthetic Gaussian producer",
            model["model_id"],
            gaussian_producer.GaussianProducerSpec(
                widths=widths,
                width_origin="synthetic_assumption",
                width_reference="Synthetic widths",
            ),
            f"{key}-producer-special",
        )
    method = _method(owner, f"{key}-method", duration_min=duration)
    contract = _contract(
        owner,
        f"{key}-contract",
        analytes=tuple(row["analyte_id"] for row in analytes),
        max_acquisition_duration_min=duration,
    )
    experiment, attempt = _bound_attempt(
        producer, contract, method, owner=owner, key=f"{key}-attempt"
    )
    prediction = retention.predict_retention(
        owner,
        model["model_id"],
        method["method_id"],
        f"{key}-prediction",
    )
    return model, producer, method, contract, experiment, attempt, prediction


def _record(prepared, *, owner="chemist", key="run"):
    _, producer, _, _, _, attempt, prediction = prepared
    return gaussian_producer.record_gaussian_observation(
        owner,
        producer["producer_id"],
        prediction["prediction_id"],
        attempt["attempt_id"],
        attempt["revision"],
        key,
    )


def test_producer_run_records_immutable_latent_results_and_ready_evidence():
    prepared = _prepared()
    result = _record(prepared)
    _, _, _, contract, _, _, _ = prepared

    assert result["observation"]["profile"] == "ideal-gaussian-centroid/1"
    assert result["observation"]["resolution_definition"] == RESOLUTION_DEFINITION
    assert result["observation"]["fwhm_per_sigma"] == 2.3548
    assert result["evidence_spec"]["observation_origin"] == "simulator_latent"
    assert result["evidence_spec"]["fidelity_role"] == "high"
    assert [row["analyte_id"] for row in result["evidence_spec"]["analytes"]] == ["A", "B"]
    assert all(
        "not a verified chemical identity" in row["assignment_evidence"]
        for row in result["evidence_spec"]["analytes"]
    )
    assert len(result["generated_results"]) == 4
    assert len(result["observation"]["pairs"]) == 1
    assert not {"trace", "score", "utility", "optimizer"} & set(result["observation"])
    assert gaussian_producer.get_gaussian_observation("chemist", result["run_id"]) == result

    result_records = [
        experiments.get_result("chemist", item["result_id"]) for item in result["generated_results"]
    ]
    assert all(
        row["outcome"]["provenance"]["value_origin"] == "stored_ideal_gaussian_producer"
        for row in result_records
    )
    assert all(
        row["outcome"]["provenance"]["producer_run"]["output_sha256"] == result["output_sha256"]
        for row in result_records
    )
    with registry_connection() as con:
        linked = [
            observations._result_record(
                con,
                "chemist",
                row["result_id"],
                attempt_id=result["attempt_id"],
                attempt_revision=result["attempt_revision"],
                analyte_id=row["analyte_reference"],
                metric=row["metric"],
            )
            for row in result["generated_results"]
        ]
        verified = gaussian_producer.validate_gaussian_result_evidence(
            con,
            "chemist",
            result["producer_id"],
            result["attempt_id"],
            result["attempt_revision"],
            linked,
        )
    assert verified == {
        "producer_id": result["producer"]["producer_id"],
        "producer_input_sha256": result["producer"]["input_sha256"],
        "run_id": result["run_id"],
        "run_input_sha256": result["input_sha256"],
        "run_output_sha256": result["output_sha256"],
        "prediction_id": result["prediction"]["prediction_id"],
        "prediction_input_sha256": result["prediction"]["input_sha256"],
        "prediction_output_sha256": result["prediction"]["output_sha256"],
    }
    assessed = observations.evaluate_observation(
        "chemist",
        contract["contract_id"],
        result["attempt_id"],
        result["attempt_revision"],
        result["evidence_spec"],
        "evaluate-latent-run",
    )
    assert assessed["observation"]["evidence"]["producer_evidence"] == verified


@pytest.mark.parametrize(
    "widths,origin,match",
    [
        ([{"analyte_id": "A", "fwhm_min": 0.2}], "synthetic_assumption", "exactly match"),
        (
            [{"analyte_id": "A", "fwhm_min": 0.2}, {"analyte_id": "A", "fwhm_min": 0.3}],
            "synthetic_assumption",
            "unique",
        ),
        (
            [{"analyte_id": "A", "fwhm_min": True}, {"analyte_id": "B", "fwhm_min": 0.2}],
            "synthetic_assumption",
            "fwhm_min",
        ),
        (
            [{"analyte_id": "A", "fwhm_min": 0.0}, {"analyte_id": "B", "fwhm_min": 0.2}],
            "synthetic_assumption",
            "positive",
        ),
        (
            [{"analyte_id": "A", "fwhm_min": 0.2}, {"analyte_id": "B", "fwhm_min": 0.2}],
            "caller_reported",
            "literal",
        ),
    ],
)
def test_width_spec_and_cohort_are_strict(widths, origin, match):
    model = _register_model()
    if origin == "synthetic_assumption":
        with pytest.raises((ValidationError, ValueError), match=match):
            gaussian_producer.register_gaussian_producer(
                "chemist",
                "Invalid widths",
                model["model_id"],
                {"widths": widths, "width_origin": origin, "width_reference": "fixture"},
                "bad-widths",
            )
    else:
        with pytest.raises(ValidationError, match=match):
            gaussian_producer.GaussianProducerSpec.model_validate(
                {"widths": widths, "width_origin": origin, "width_reference": "fixture"}
            )


def test_registration_requires_synthetic_retention_model_and_prebinding_checks_contract():
    caller_model = _register_model(key="caller-model", parameter_origin="caller_supplied")
    with pytest.raises(ValueError, match="synthetic retention model"):
        gaussian_producer.register_gaussian_producer(
            "chemist",
            "Not a latent model",
            caller_model["model_id"],
            {
                "widths": [
                    {"analyte_id": "A", "fwhm_min": 0.2},
                    {"analyte_id": "B", "fwhm_min": 0.2},
                ],
                "width_origin": "synthetic_assumption",
                "width_reference": "fixture",
            },
            "bad-model-producer",
        )

    model, producer = _register_producer(key="contract-match")
    wrong_cohort = _contract(key="wrong-cohort", analytes=("A",))
    method = _method(key="contract-match-method")
    with pytest.raises(ValueError, match="exactly match"):
        experiments.create_experiment(
            "chemist",
            experiments.ExperimentSpec(
                title="Wrong cohort",
                source="synthetic",
                observation_contract_id=wrong_cohort["contract_id"],
                observation_producer_id=producer["producer_id"],
            ),
            "wrong-cohort-experiment",
        )
    wrong_resolution = _contract(
        key="wrong-resolution",
        analytes=("A", "B"),
        resolution_definition="other definition",
    )
    with pytest.raises(ValueError, match="resolution definition"):
        experiments.create_experiment(
            "chemist",
            experiments.ExperimentSpec(
                title="Wrong resolution",
                source="synthetic",
                observation_contract_id=wrong_resolution["contract_id"],
                observation_producer_id=producer["producer_id"],
            ),
            "wrong-resolution-experiment",
        )
    assert method["method_id"]
    assert model["model_id"]


def test_producer_and_prediction_owner_isolation():
    prepared = _prepared()
    _, producer, _, _, _, attempt, prediction = prepared
    with pytest.raises(ValueError, match="not found or is not accessible"):
        gaussian_producer.get_gaussian_producer("other", producer["producer_id"])
    with pytest.raises(ValueError, match="not found or is not accessible"):
        gaussian_producer.record_gaussian_observation(
            "other",
            producer["producer_id"],
            prediction["prediction_id"],
            attempt["attempt_id"],
            1,
            "foreign-run",
        )


@pytest.mark.parametrize(
    "attempt_changes,match",
    [
        (
            {"status": "planned", "evaluator_kind": "simulator", "fidelity": 0.99},
            "completed simulator",
        ),
        (
            {"status": "completed", "evaluator_kind": "instrument", "fidelity": 0.99},
            "simulator attempt",
        ),
        (
            {"status": "completed", "evaluator_kind": "simulator", "fidelity": None},
            "fidelity coordinate",
        ),
    ],
)
def test_record_requires_completed_simulator_and_declared_fidelity(attempt_changes, match):
    model, producer, method, contract, experiment, _, prediction = _prepared()
    bad_attempt = experiments.record_attempt(
        "chemist",
        experiment["experiment_id"],
        experiments.AttemptSpec(
            executable_method_id=method["method_id"],
            **attempt_changes,
        ),
        f"{match}-attempt",
    )
    with pytest.raises(ValueError, match=match):
        gaussian_producer.record_gaussian_observation(
            "chemist",
            producer["producer_id"],
            prediction["prediction_id"],
            bad_attempt["attempt_id"],
            1,
            f"{match}-run",
        )
    assert model and contract


def test_record_rejects_wrong_prediction_method_and_unbound_experiment():
    prepared = _prepared(key="wrong-method")
    model, producer, _, contract, _, attempt, _ = prepared
    other_method = _method(key="other-method", duration_min=9.0)
    wrong_prediction = retention.predict_retention(
        "chemist", model["model_id"], other_method["method_id"], "wrong-method-alt-prediction"
    )
    with pytest.raises(ValueError, match="differs from the actual attempt method"):
        gaussian_producer.record_gaussian_observation(
            "chemist",
            producer["producer_id"],
            wrong_prediction["prediction_id"],
            attempt["attempt_id"],
            1,
            "wrong-method-run",
        )
    unbound = experiments.create_experiment(
        "chemist",
        experiments.ExperimentSpec(
            title="Unbound experiment",
            source="synthetic",
            observation_contract_id=contract["contract_id"],
        ),
        "unbound-experiment",
    )
    unbound_attempt = experiments.record_attempt(
        "chemist",
        unbound["experiment_id"],
        experiments.AttemptSpec(
            status="completed",
            evaluator_kind="simulator",
            fidelity=0.99,
            executable_method_id=other_method["method_id"],
        ),
        "unbound-attempt",
    )
    with pytest.raises(ValueError, match="not bound to the selected Gaussian producer"):
        gaussian_producer.record_gaussian_observation(
            "chemist",
            producer["producer_id"],
            wrong_prediction["prediction_id"],
            unbound_attempt["attempt_id"],
            1,
            "unbound-run",
        )


def test_not_eluted_and_unpaired_resolution_remain_censored_or_missing():
    rows = [
        {"analyte_id": "A", "model": "linear", "ln_k0": 0.0, "S1": 0.0},
        {"analyte_id": "B", "model": "linear", "ln_k0": math.log(100.0), "S1": 0.0},
    ]
    prepared = _prepared(key="censored", analytes=rows)
    run = _record(prepared, key="censored-run")
    output = {item["analyte_id"]: item for item in run["observation"]["analytes"]}
    links = {(item["metric"], item["analyte_reference"]): item for item in run["generated_results"]}
    assert output["B"]["retention_status"] == "not_eluted"
    assert output["B"]["resolution_status"] == "censored"
    assert output["B"]["right_censor_bound_min"] == 10.0
    assert links[("retention_time", "B")]["outcome"]["status"] == "censored"
    assert links[("retention_time", "B")]["outcome"]["lower_bound"] == 10.0
    assert links[("resolution", "B")]["outcome"]["status"] == "missing"
    assert "retention_not_eluted" in links[("resolution", "B")]["outcome"]["explanation"]
    assert links[("resolution", "A")]["outcome"]["status"] == "missing"
    assert "no_adjacent_finite_centroid" in links[("resolution", "A")]["outcome"]["explanation"]


def test_exactly_tied_centroids_store_an_observed_zero_resolution():
    rows = [
        {"analyte_id": "A", "model": "linear", "ln_k0": 0.0, "S1": 0.0},
        {"analyte_id": "B", "model": "linear", "ln_k0": 0.0, "S1": 0.0},
    ]
    run = _record(_prepared(key="tied", analytes=rows), key="tied-run")
    by_key = {
        (item["metric"], item["analyte_reference"]): item["outcome"]
        for item in run["generated_results"]
    }
    assert run["observation"]["pairs"][0]["resolution"] == 0.0
    assert by_key[("resolution", "A")]["status"] == "observed"
    assert by_key[("resolution", "A")]["value"] == 0.0
    assert by_key[("resolution", "B")]["value"] == 0.0


@pytest.mark.parametrize("cohort_size", [10, 32])
def test_full_cohort_run_assessment_and_snapshot_stay_within_byte_limits(cohort_size):
    rows = [
        {"analyte_id": f"synthetic-analyte-{index:02d}", "model": "linear", "ln_k0": 0.0, "S1": 0.0}
        for index in range(cohort_size)
    ]
    prepared = _prepared(key=f"bounded-{cohort_size}", analytes=rows)
    run = _record(prepared, key=f"bounded-{cohort_size}-run")
    _, _, _, contract, _, _, _ = prepared
    assert len(run["generated_results"]) == 2 * cohort_size
    assert len(canonical(run).encode("utf-8")) < 64_000

    assessed = observations.evaluate_observation(
        "chemist",
        contract["contract_id"],
        run["attempt_id"],
        run["attempt_revision"],
        run["evidence_spec"],
        f"bounded-{cohort_size}-assessment",
    )
    assert len(canonical(assessed).encode("utf-8")) < 64_000
    compact_results = [
        result
        for analyte in assessed["observation"]["evidence"]["analytes"]
        for result in (analyte["retention_result"], analyte["resolution_result"])
        if result is not None
    ]
    assert len(compact_results) == 2 * cohort_size
    for reference in compact_results:
        assert reference["outcome_included"] is False
        stored = experiments.get_result("chemist", reference["result_id"])
        assert reference["input_sha256"] == stored["input_sha256"]
        assert stored["input_sha256"] == sha256(stored["outcome"])
    snapshot = observations.create_observation_snapshot(
        "chemist",
        contract["contract_id"],
        [assessed["observation_id"]],
        f"bounded-{cohort_size}-snapshot",
    )
    assert len(canonical(snapshot).encode("utf-8")) < 64_000
    assert (
        len(observations.get_observation_snapshot("chemist", snapshot["snapshot_id"])["members"])
        == 1
    )


def test_low_fidelity_producer_returns_low_role_without_physical_claims():
    prepared = _prepared(key="low")
    _, producer, method, contract, experiment, _, prediction = prepared
    low_attempt = experiments.record_attempt(
        "chemist",
        experiment["experiment_id"],
        experiments.AttemptSpec(
            status="completed",
            evaluator_kind="simulator",
            fidelity=0.5,
            executable_method_id=method["method_id"],
        ),
        "low-extra-attempt",
    )
    run = gaussian_producer.record_gaussian_observation(
        "chemist",
        producer["producer_id"],
        prediction["prediction_id"],
        low_attempt["attempt_id"],
        1,
        "low-run",
    )
    assert run["evidence_spec"]["fidelity_role"] == "low"
    assert run["attempt"]["fidelity_coordinate"] == 0.5
    assert run["observation"]["scope"]["interpretation"] == (
        "ideal_latent_model_only_not_trace_simulation_or_physical_qualification"
    )
    assert contract


def test_retry_returns_original_receipt_after_attempt_revision_and_other_key_conflicts():
    prepared = _prepared(key="retry")
    run = _record(prepared, key="original-run-key")
    _, producer, method, _, _, attempt, prediction = prepared
    correction = experiments.AttemptSpec(
        status="completed",
        evaluator_kind="simulator",
        fidelity=0.99,
        executable_method_id=method["method_id"],
        explanation="Later bookkeeping correction",
    )
    experiments.revise_attempt(
        "chemist", attempt["attempt_id"], 1, correction, "Corrected declaration", "revise-attempt"
    )
    assert _record(prepared, key="original-run-key") == run
    with pytest.raises(ValueError, match="revision conflict"):
        gaussian_producer.record_gaussian_observation(
            "chemist",
            producer["producer_id"],
            prediction["prediction_id"],
            attempt["attempt_id"],
            1,
            "new-stale-key",
        )
    with pytest.raises(ValueError, match="already exists"):
        gaussian_producer.record_gaussian_observation(
            "chemist",
            producer["producer_id"],
            prediction["prediction_id"],
            attempt["attempt_id"],
            2,
            "new-revision-key",
        )


def test_different_key_duplicate_and_current_manual_results_are_rejected():
    prepared = _prepared(key="duplicate")
    run = _record(prepared, key="first-run")
    _, producer, _, _, _, attempt, prediction = prepared
    with pytest.raises(ValueError, match="already exists"):
        gaussian_producer.record_gaussian_observation(
            "chemist",
            producer["producer_id"],
            prediction["prediction_id"],
            attempt["attempt_id"],
            1,
            "different-run-key",
        )
    assert gaussian_producer.get_gaussian_observation("chemist", run["run_id"]) == run

    second = _prepared(key="manual-conflict")
    _, producer2, _, _, _, attempt2, prediction2 = second
    manual = experiments.ResultSpec(
        metric="retention_time",
        analyte_reference="A",
        status="observed",
        unit="min",
        value=1.0,
    )
    experiments.record_results("chemist", attempt2["attempt_id"], 1, [manual], "manual-result")
    with pytest.raises(ValueError, match="current result already exists"):
        gaussian_producer.record_gaussian_observation(
            "chemist",
            producer2["producer_id"],
            prediction2["prediction_id"],
            attempt2["attempt_id"],
            1,
            "run-after-manual",
        )


def test_result_evidence_must_match_same_stored_run_and_reject_manual_override():
    prepared = _prepared(key="evidence")
    run = _record(prepared, key="evidence-run")
    result_id = run["generated_results"][0]["result_id"]
    with registry_connection() as con:
        linked = observations._result_record(
            con,
            "chemist",
            result_id,
            attempt_id=run["attempt_id"],
            attempt_revision=run["attempt_revision"],
            analyte_id="A",
            metric="retention_time",
        )
        valid = gaussian_producer.validate_gaussian_result_evidence(
            con,
            "chemist",
            run["producer_id"],
            run["attempt_id"],
            run["attempt_revision"],
            [linked],
        )
        assert valid["run_id"] == run["run_id"]

    correction = experiments.record_results(
        "chemist",
        run["attempt_id"],
        run["attempt_revision"],
        [
            experiments.ResultSpec(
                metric="retention_time",
                analyte_reference="A",
                status="observed",
                unit="min",
                value=42.0,
                supersedes_result_id=result_id,
                explanation="Synthetic manual correction for the negative test",
            )
        ],
        "evidence-manual-correction",
    )["results"][0]
    assert _record(prepared, key="evidence-run") == run
    with registry_connection() as con:
        manual = observations._result_record(
            con,
            "chemist",
            correction["result_id"],
            attempt_id=run["attempt_id"],
            attempt_revision=run["attempt_revision"],
            analyte_id="A",
            metric="retention_time",
        )
        with pytest.raises(ValueError, match="not from the experiment's bound"):
            gaussian_producer.validate_gaussian_result_evidence(
                con,
                "chemist",
                run["producer_id"],
                run["attempt_id"],
                run["attempt_revision"],
                [manual],
            )
        assert (
            gaussian_producer.validate_gaussian_result_evidence(
                con,
                "chemist",
                run["producer_id"],
                run["attempt_id"],
                run["attempt_revision"],
                [],
            )
            is None
        )


def test_generated_result_batch_rolls_back_with_run_and_receipt_on_failure(monkeypatch):
    prepared = _prepared(key="rollback")
    _, producer, _, _, _, attempt, prediction = prepared
    original = experiments._insert_result_records

    def insert_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError("synthetic insertion failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(experiments, "_insert_result_records", insert_then_fail)
        with pytest.raises(ValueError, match="synthetic insertion failure"):
            gaussian_producer.record_gaussian_observation(
                "chemist",
                producer["producer_id"],
                prediction["prediction_id"],
                attempt["attempt_id"],
                1,
                "rollback-run",
            )
    with registry_connection() as con:
        assert con.execute("SELECT COUNT(*) FROM gaussian_observations").fetchone()[0] == 0
        assert (
            con.execute(
                "SELECT COUNT(*) FROM experiment_results WHERE attempt_id=?",
                (attempt["attempt_id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM registry_receipts WHERE owner=? AND request_key=?",
                ("chemist", "rollback-run"),
            ).fetchone()[0]
            == 0
        )
    assert _record(prepared, key="rollback-run")["attempt_id"] == attempt["attempt_id"]


def test_concurrent_same_request_key_creates_one_run():
    prepared = _prepared(key="concurrent")
    _, producer, _, _, _, attempt, prediction = prepared

    def record(_):
        return gaussian_producer.record_gaussian_observation(
            "chemist",
            producer["producer_id"],
            prediction["prediction_id"],
            attempt["attempt_id"],
            1,
            "same-run-request",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(record, range(4)))
    assert all(item == results[0] for item in results)
    with registry_connection() as con:
        assert con.execute("SELECT COUNT(*) FROM gaussian_observations").fetchone()[0] == 1
        assert (
            con.execute(
                "SELECT COUNT(*) FROM experiment_results WHERE attempt_id=?",
                (attempt["attempt_id"],),
            ).fetchone()[0]
            == 4
        )


def test_new_optional_producer_field_preserves_historical_experiment_request_hash():
    spec = experiments.ExperimentSpec(title="Legacy receipt fixture", source="manual")
    requested = experiments._requested_spec(spec)
    assert "observation_producer_id" not in requested
    request = {"operation": "create_experiment", "spec": requested}
    response = {
        "schema_version": "1.0",
        "experiment_id": "legacy-experiment",
        "spec": requested,
        "executable_method": None,
        "observation_contract": None,
        "input_sha256": "legacy-digest",
        "created_at": "2026-10-28T12:00:00+00:00",
    }
    with registry_connection(write=True) as con:
        con.execute(
            "INSERT INTO registry_receipts VALUES (?, ?, ?, ?, ?)",
            ("chemist", "legacy-experiment-key", sha256(request), canonical(response), now()),
        )
    assert experiments.create_experiment("chemist", spec, "legacy-experiment-key") == response


def test_run_retrieval_works_in_fresh_process_and_records_installed_backend_provenance(tmp_path):
    prepared = _prepared(key="subprocess")
    run = _record(prepared, key="subprocess-run")
    provenance = run["implementation_provenance"]
    assert provenance["measure_service"]["package"] == "measure-service"
    assert provenance["service_adapter"]["source_sha256"]
    assert provenance["gaussian_kernel"]["source_sha256"]
    assert provenance["resolution_helper"]["module"] == "measure_methods.chromatography"
    assert provenance["resolution_helper"]["source_sha256"]
    env = dict(os.environ, MEASURE_MCP_DATA_DIR=str(os.environ["MEASURE_MCP_DATA_DIR"]))
    script = (
        "from measure_service.gaussian_producer import get_gaussian_observation; "
        f"value=get_gaussian_observation('chemist', {run['run_id']!r}); "
        "assert value['output_sha256']; print(value['run_id'])"
    )
    response = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert response.stdout.strip() == run["run_id"]
