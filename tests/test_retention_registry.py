"""Owner-scoped retention declarations and canonical-method predictions."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from measure_service import experiments, method_registry, observations, retention
from measure_service.methods import MethodDeclaration


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))


def _state(flow, fractions):
    return {"flow": flow, "solvent_fractions": fractions}


def _method_declaration(
    *,
    times=(0, 10),
    fractions=None,
    flows=(1.0, 1.0),
    time_unit="min",
    flow_unit="mL/min",
    kind="gradient_absolute",
    solvents=("A", "B"),
    pre_acquisition=False,
    post_acquisition=False,
):
    if fractions is None:
        if len(solvents) == 2:
            fractions = tuple((solvents[0], 0.8, solvents[1], 0.2) for _ in times)
        else:
            fractions = tuple((solvents[0], 0.8, solvents[1], 0.2, solvents[2], 0.0) for _ in times)

    def row(index):
        composition = {
            fractions[index][position]: fractions[index][position + 1]
            for position in range(0, len(fractions[index]), 2)
        }
        return _state(flows[index], composition)

    if kind == "isocratic":
        program = {"kind": "isocratic", "acquisition_duration": times[-1], "state": row(0)}
    elif kind == "gradient_absolute":
        program = {
            "kind": "gradient_absolute",
            "points": [{"time": time, "state": row(index)} for index, time in enumerate(times)],
        }
    elif kind == "gradient_duration":
        program = {
            "kind": "gradient_duration",
            "initial_state": row(0),
            "segments": [
                {"duration": times[index] - times[index - 1], "end_state": row(index)}
                for index in range(1, len(times))
            ],
        }
    else:
        raise AssertionError(kind)
    declaration = {
        "time_unit": time_unit,
        "flow_unit": flow_unit,
        "solvents": list(solvents),
        "program": program,
    }
    if pre_acquisition:
        declaration["pre_acquisition"] = {
            "duration": 1,
            "state": row(0),
        }
    if post_acquisition:
        declaration["post_acquisition"] = {
            "duration": 3,
            "state": row(-1),
        }
    return declaration


def _register_method(owner="chemist", key="method", **changes):
    declaration = MethodDeclaration.model_validate(_method_declaration(**changes))
    return method_registry.register_method(owner, "Synthetic LC method", declaration, None, key)


def _model_spec(*, analytes=None, **changes):
    values = {
        "gradient_coordinate": "column_inlet_at_analyte_entry",
        "analytes": analytes
        or [{"analyte_id": "A", "model": "linear", "ln_k0": math.log(3), "S1": 0}],
        "column_length_m": 1,
        "effective_area_m2": 1e-6,
        "post_column_volume_ml": 0.5,
        "strong_solvent": "B",
        "parameter_origin": "synthetic",
        "parameter_reference": "Independently specified synthetic coefficients",
    }
    return retention.RetentionModelSpec.model_validate(values | changes)


def _register_model(owner="chemist", key="model", **changes):
    return retention.register_retention_model(
        owner, "Synthetic retention model", _model_spec(**changes), key
    )


def _predict(
    owner="chemist",
    *,
    model=None,
    method=None,
    key="prediction",
    model_key="model",
    method_key="method",
    **method_changes,
):
    model = _register_model(owner, model_key) if model is None else model
    method = _register_method(owner, method_key, **method_changes) if method is None else method
    result = retention.predict_retention(owner, model["model_id"], method["method_id"], key)
    return model, method, result


def _snapshot(owner="chemist", *, key="snapshot", required_analytes=("A", "B")):
    contract = observations.register_observation_contract(
        owner,
        "Declared cohort",
        observations.ObservationContractSpec(
            required_analytes=list(required_analytes),
            resolution_definition="synthetic finite neighboring peak resolution",
            max_acquisition_duration_min=10,
        ),
        f"{key}-contract",
    )
    method = _register_method(owner, f"{key}-method", kind="isocratic")
    experiment = experiments.create_experiment(
        owner,
        experiments.ExperimentSpec(
            title="Snapshot lineage fixture",
            source="synthetic",
            observation_contract_id=contract["contract_id"],
        ),
        f"{key}-experiment",
    )
    attempt = experiments.record_attempt(
        owner,
        experiment["experiment_id"],
        experiments.AttemptSpec(
            status="completed",
            evaluator_kind="instrument",
            fidelity=1.0,
            executable_method_id=method["method_id"],
        ),
        f"{key}-attempt",
    )
    result = experiments.record_results(
        owner,
        attempt["attempt_id"],
        1,
        [
            experiments.ResultSpec(
                metric="retention_time",
                analyte_reference="A",
                status="observed",
                unit="min",
                value=2.0,
            )
        ],
        f"{key}-result",
    )["results"][0]
    observation = observations.evaluate_observation(
        owner,
        contract["contract_id"],
        attempt["attempt_id"],
        1,
        observations.ObservationEvidenceSpec(
            fidelity_role="high",
            observation_origin="instrument_reported",
            analytes=[
                {
                    "analyte_id": "A",
                    "assignment": "confirmed",
                    "assignment_evidence": "Synthetic lineage assignment",
                    "retention_result_id": result["result_id"],
                }
            ],
        ),
        f"{key}-observation",
    )
    snapshot = observations.create_observation_snapshot(
        owner, contract["contract_id"], [observation["observation_id"]], f"{key}-snapshot"
    )
    return contract, attempt, result, snapshot


def test_isocratic_prediction_uses_declared_geometry_flow_and_post_column_volume():
    model, method, result = _predict(kind="isocratic")
    analyte = result["prediction"]["analytes"][0]["result"]
    assert result["prediction"]["profile"] == "ideal-lc-retention-zeta/1"
    assert result["prediction"]["model"]["gradient_coordinate"] == "column_inlet_at_analyte_entry"
    assert result["prediction"]["method"]["canonical_program"]["acquisition_duration_min"] == 10
    assert result["prediction"]["geometry"] == {
        "column_length_m": 1,
        "effective_area_m2": 1e-6,
        "post_column_volume_ml": 0.5,
    }
    assert analyte["status"] == "eluted"
    assert analyte["dead_time_min"] == pytest.approx(1)
    assert analyte["retention_zeta_min"] == pytest.approx(3)
    assert analyte["post_column_transit_min"] == pytest.approx(0.5)
    assert analyte["retention_time_min"] == pytest.approx(4.5)
    assert model["input_sha256"] and method["canonical_program_sha256"]
    assert (
        result["prediction"]["qualification_scope"]["physical_retention_qualification_established"]
        is False
    )


def test_equivalent_seconds_and_minutes_methods_produce_the_same_prediction():
    model = _register_model(
        analytes=[{"analyte_id": "A", "model": "linear", "ln_k0": math.log(2), "S1": 1}],
        post_column_volume_ml=0,
    )
    minutes = _register_method(
        key="minutes",
        times=(0, 10),
        fractions=(("A", 0.9, "B", 0.1), ("A", 0.1, "B", 0.9)),
        flows=(1, 1),
        time_unit="min",
        flow_unit="mL/min",
    )
    seconds = _register_method(
        key="seconds",
        times=(0, 600),
        fractions=(("A", 0.9, "B", 0.1), ("A", 0.1, "B", 0.9)),
        flows=(1000, 1000),
        time_unit="s",
        flow_unit="uL/min",
    )
    first = retention.predict_retention(
        "chemist", model["model_id"], minutes["method_id"], "min-prediction"
    )
    second = retention.predict_retention(
        "chemist", model["model_id"], seconds["method_id"], "sec-prediction"
    )
    first_result = first["prediction"]["analytes"][0]["result"]
    second_result = second["prediction"]["analytes"][0]["result"]
    assert minutes["canonical_program_sha256"] == seconds["canonical_program_sha256"]
    assert first_result["retention_time_min"] == pytest.approx(
        second_result["retention_time_min"], abs=1e-7
    )
    assert first_result["retention_zeta_min"] == pytest.approx(
        second_result["retention_zeta_min"], abs=1e-7
    )


def test_duration_gradient_and_absolute_gradient_have_the_same_canonical_prediction():
    model = _register_model(post_column_volume_ml=0)
    common = {
        "times": (0, 2, 10),
        "fractions": (("A", 0.9, "B", 0.1), ("A", 0.5, "B", 0.5), ("A", 0.1, "B", 0.9)),
        "flows": (1, 1, 1),
    }
    absolute = _register_method(key="absolute", kind="gradient_absolute", **common)
    duration = _register_method(key="duration", kind="gradient_duration", **common)
    first = retention.predict_retention(
        "chemist", model["model_id"], absolute["method_id"], "absolute-prediction"
    )
    second = retention.predict_retention(
        "chemist", model["model_id"], duration["method_id"], "duration-prediction"
    )
    assert absolute["canonical_program_sha256"] == duration["canonical_program_sha256"]
    assert first["prediction"]["analytes"] == second["prediction"]["analytes"]


def test_all_supported_laws_keep_distinct_analyte_identities_and_no_width_score_outputs():
    model = _register_model(
        analytes=[
            {"analyte_id": "linear-A", "model": "linear", "ln_k0": 2.0, "S1": 5.0},
            {"analyte_id": "quadratic-B", "model": "quadratic", "ln_k0": 2.5, "S1": 6.0, "S2": 0.5},
            {"analyte_id": "target-C", "model": "neue-kuss", "ln_k0": 3.0, "S1": 7.0, "S2": 0.7},
        ],
        post_column_volume_ml=0,
    )
    method = _register_method(
        key="all-laws", times=(0, 10), fractions=(("A", 0.9, "B", 0.1), ("A", 0.1, "B", 0.9))
    )
    result = retention.predict_retention(
        "chemist", model["model_id"], method["method_id"], "all-laws-prediction"
    )
    analytes = result["prediction"]["analytes"]
    assert [item["analyte_id"] for item in analytes] == ["linear-A", "quadratic-B", "target-C"]
    assert [item["parameters"]["model"] for item in analytes] == [
        "linear",
        "quadratic",
        "neue-kuss",
    ]
    assert all(item["result"]["profile"] == "ideal-lc-retention-zeta/1" for item in analytes)
    assert not {"score", "resolution", "peak_width", "sigma"} & set(result["prediction"])
    assert model["source_fit_range_warnings"]["linear-A"] == []


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"solvents": ("A", "C")}, "strong_solvent"),
        ({"solvents": ("A", "B", "C")}, "exactly two"),
        ({"flows": (1.0, 1.1)}, "constant flow"),
        ({"pre_acquisition": True}, "pre_acquisition phase"),
    ],
)
def test_method_prediction_rejects_unsupported_solvents_flow_and_preacquisition(changes, match):
    model = _register_model()
    method = _register_method(key="unsupported", **changes)
    with pytest.raises(ValueError, match=match):
        retention.predict_retention(
            "chemist", model["model_id"], method["method_id"], "bad-prediction"
        )


def test_method_postacquisition_phase_is_retained_but_never_mapped_to_postcolumn_volume():
    base_model = _register_model(key="base-model")
    no_post = _register_method(key="no-post", kind="isocratic")
    with_post = _register_method(key="with-post", kind="isocratic", post_acquisition=True)
    first = retention.predict_retention(
        "chemist", base_model["model_id"], no_post["method_id"], "no-post-prediction"
    )
    second = retention.predict_retention(
        "chemist", base_model["model_id"], with_post["method_id"], "with-post-prediction"
    )
    assert (
        first["prediction"]["analytes"][0]["result"]
        == second["prediction"]["analytes"][0]["result"]
    )
    method_snapshot = second["prediction"]["method"]
    assert method_snapshot["post_acquisition_phase_ignored"] is not None
    assert method_snapshot["post_acquisition_is_not_post_column_volume"] is True


def test_training_snapshot_is_owner_scoped_subset_lineage_and_survives_correction():
    contract, attempt, result, snapshot = _snapshot()
    model = retention.register_retention_model(
        "chemist",
        "Lineage model",
        _model_spec(
            analytes=[{"analyte_id": "A", "model": "linear", "ln_k0": 2.0, "S1": 5.0}],
            parameter_origin="caller_supplied",
            training_snapshot_id=snapshot["snapshot_id"],
        ),
        "lineage-model",
    )
    assert model["training_snapshot"]["snapshot_id"] == snapshot["snapshot_id"]
    assert model["training_snapshot"]["sha256"] == snapshot["sha256"]
    assert model["training_snapshot"]["contract_id"] == contract["contract_id"]
    assert model["training_snapshot"]["required_analytes"] == ["A", "B"]

    with pytest.raises(ValueError, match="covered by the snapshot contract cohort"):
        retention.register_retention_model(
            "chemist",
            "Out of cohort model",
            _model_spec(
                analytes=[{"analyte_id": "C", "model": "linear", "ln_k0": 2.0, "S1": 5.0}],
                parameter_origin="caller_supplied",
                training_snapshot_id=snapshot["snapshot_id"],
            ),
            "out-of-cohort",
        )
    with pytest.raises(ValueError, match="not found or is not accessible"):
        retention.register_retention_model(
            "another-owner",
            "Foreign snapshot",
            _model_spec(training_snapshot_id=snapshot["snapshot_id"]),
            "foreign-snapshot",
        )

    corrected = experiments.ResultSpec(
        metric="retention_time",
        analyte_reference="A",
        status="observed",
        unit="min",
        value=2.2,
        supersedes_result_id=result["result_id"],
        explanation="Synthetic corrected result",
    )
    experiments.record_results("chemist", attempt["attempt_id"], 1, [corrected], "correct-lineage")
    assert observations.get_observation_snapshot("chemist", snapshot["snapshot_id"]) == snapshot
    assert retention.get_retention_model("chemist", model["model_id"]) == model
    assert model["training_snapshot"]["sha256"] == snapshot["sha256"]


def test_uneluted_prediction_is_right_censored_with_null_retention_time():
    model = _register_model(
        analytes=[{"analyte_id": "A", "model": "linear", "ln_k0": 10.0, "S1": 0}],
        column_length_m=1,
        effective_area_m2=1e-6,
        post_column_volume_ml=0,
    )
    method = _register_method(key="short-window", times=(0, 1), kind="isocratic")
    result = retention.predict_retention(
        "chemist", model["model_id"], method["method_id"], "uneluted"
    )
    analyte_result = result["prediction"]["analytes"][0]["result"]
    assert analyte_result["status"] == "not_eluted"
    assert analyte_result["retention_time_min"] is None
    assert analyte_result["right_censor_bound_min"] == 1
    assert "retention_time" not in analyte_result or analyte_result["retention_time_min"] != 0


def test_invalid_numeric_domain_rolls_back_and_source_fit_bounds_are_advisory():
    undeclared_coordinate = _model_spec().model_dump(mode="python")
    undeclared_coordinate.pop("gradient_coordinate")
    with pytest.raises(ValidationError, match="gradient_coordinate"):
        retention.RetentionModelSpec.model_validate(undeclared_coordinate)

    invalid = _model_spec(analytes=[{"analyte_id": "A", "model": "linear", "ln_k0": 1000, "S1": 0}])
    with pytest.raises(ValueError, match="finite kernel domain"):
        retention.register_retention_model("chemist", "Invalid", invalid, "retry-domain")
    valid = _model_spec(analytes=[{"analyte_id": "A", "model": "linear", "ln_k0": 0.1, "S1": 0}])
    record = retention.register_retention_model("chemist", "Invalid", valid, "retry-domain")
    warning = record["source_fit_range_warnings"]["A"]
    assert {item["parameter"] for item in warning} == {"ln_k0", "S1"}
    assert record["spec"]["analytes"][0]["ln_k0"] == 0.1

    with pytest.raises(ValidationError):
        _model_spec(analytes=[{"analyte_id": "A", "model": "linear", "ln_k0": True, "S1": 1}])
    with pytest.raises(ValidationError):
        _model_spec(analytes=[{"analyte_id": "A", "model": "linear", "ln_k0": 1, "S1": 1, "S2": 1}])


def test_solver_budget_failure_rolls_back_receipt_then_same_key_can_succeed(monkeypatch):
    model = _register_model(
        analytes=[{"analyte_id": "A", "model": "linear", "ln_k0": 2, "S1": 1}],
        post_column_volume_ml=0,
    )
    method = _register_method(
        key="budget-method",
        times=(0, 10),
        fractions=(("A", 0.9, "B", 0.1), ("A", 0.1, "B", 0.9)),
    )
    monkeypatch.setattr(retention, "MAX_EVALUATIONS_PER_ANALYTE", 1)
    with pytest.raises(RuntimeError, match="max_evaluations"):
        retention.predict_retention(
            "chemist", model["model_id"], method["method_id"], "budget-retry"
        )
    monkeypatch.setattr(retention, "MAX_EVALUATIONS_PER_ANALYTE", 20_000)
    result = retention.predict_retention(
        "chemist", model["model_id"], method["method_id"], "budget-retry"
    )
    assert result["prediction"]["analytes"][0]["result"]["status"] == "eluted"


def test_prediction_retries_concurrency_and_fresh_process_preserve_installed_provenance(tmp_path):
    model = _register_model(key="persistent-model")
    method = _register_method(key="persistent-method", kind="isocratic")

    def predict(_):
        return retention.predict_retention(
            "chemist", model["model_id"], method["method_id"], "concurrent-prediction"
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        records = list(pool.map(predict, range(5)))
    assert len({item["prediction_id"] for item in records}) == 1
    prediction = records[0]
    provenance = prediction["prediction"]["implementation_provenance"]
    assert provenance["measure_service"]["version"]
    assert len(provenance["retention_kernel"]["source_sha256"]) == 64
    assert len(provenance["service_adapter"]["source_sha256"]) == 64
    assert retention.get_retention_prediction("chemist", prediction["prediction_id"]) == prediction
    with pytest.raises(ValueError, match="not found or is not accessible"):
        retention.get_retention_prediction("other", prediction["prediction_id"])

    env = dict(os.environ, MEASURE_MCP_DATA_DIR=str(tmp_path))
    restored = json.loads(
        subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import json; from measure_service import retention; "
                f"print(json.dumps(retention.get_retention_prediction('chemist', {prediction['prediction_id']!r})))",
            ],
            cwd=tmp_path,
            env=env,
            text=True,
        )
    )
    assert restored == prediction


def test_prediction_response_limit_rolls_back_oversize_envelope(monkeypatch):
    from measure_service.storage import canonical, registry_connection

    model, method, first = _predict()
    payload_bytes = len(canonical(first["prediction"]).encode("utf-8"))
    assert len(canonical(first).encode("utf-8")) > payload_bytes + 1
    monkeypatch.setattr(retention, "MAX_RECORD_BYTES", payload_bytes + 1)
    with pytest.raises(ValueError, match="byte limit"):
        retention.predict_retention(
            "chemist", model["model_id"], method["method_id"], "oversize-response"
        )
    with registry_connection() as con:
        assert con.execute("SELECT COUNT(*) FROM retention_predictions").fetchone()[0] == 1
        assert (
            con.execute(
                "SELECT COUNT(*) FROM registry_receipts WHERE request_key='oversize-response'"
            ).fetchone()[0]
            == 0
        )
