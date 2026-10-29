"""Immutable analytical-observation evidence registry behavior."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from measure_service import experiments, method_registry, observations, server
from measure_service.methods import MethodDeclaration


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))


def _method(owner="chemist", key="method", duration_min=2.0):
    declaration = MethodDeclaration.model_validate(
        {
            "time_unit": "min",
            "flow_unit": "mL/min",
            "solvents": ["A"],
            "program": {
                "kind": "isocratic",
                "acquisition_duration": duration_min,
                "state": {"flow": 0.2, "solvent_fractions": {"A": 1.0}},
            },
        }
    )
    return method_registry.register_method(owner, "Observed method", declaration, None, key)


def _contract(owner="chemist", key="contract", analytes=("A",), **changes):
    values = {
        "required_analytes": list(analytes),
        "resolution_definition": "caller-declared finite neighboring peak resolution",
        "max_acquisition_duration_min": 10,
    } | changes
    spec = observations.ObservationContractSpec.model_validate(values)
    return observations.register_observation_contract(owner, "Required analytes", spec, key)


def _attempt(
    contract_id,
    *,
    owner="chemist",
    evaluator_kind="instrument",
    fidelity=0.99,
    key="attempt",
    executable_method_id=None,
    measurements=None,
):
    method = _method(owner, f"{key}-method") if executable_method_id is None else None
    executable_method_id = executable_method_id or method["method_id"]
    experiment = experiments.create_experiment(
        owner,
        experiments.ExperimentSpec(
            title="Evidence-bound run",
            source="caller-declared",
            observation_contract_id=contract_id,
        ),
        f"{key}-experiment",
    )
    run = experiments.record_attempt(
        owner,
        experiment["experiment_id"],
        experiments.AttemptSpec(
            status="completed",
            evaluator_kind=evaluator_kind,
            fidelity=fidelity,
            executable_method_id=executable_method_id,
            measurements=measurements or [],
        ),
        key,
    )
    return experiment, run


def _result(owner, attempt_id, metric, analyte, *, value=None, unit="1", status="observed", key):
    if status == "observed":
        spec = experiments.ResultSpec(
            metric=metric,
            analyte_reference=analyte,
            status=status,
            unit=unit,
            value=value,
        )
    else:
        bounds = {}
        if status == "below_limit":
            bounds["upper_bound"] = 0.5
        elif status == "censored":
            bounds["lower_bound"] = 0.5
        spec = experiments.ResultSpec(
            metric=metric,
            analyte_reference=analyte,
            status=status,
            unit=unit,
            explanation=f"Synthetic {status} evidence",
            **bounds,
        )
    recorded = experiments.record_results(owner, attempt_id, 1, [spec], key)
    return recorded["results"][0]


def _evidence(
    *,
    origin="instrument_reported",
    fidelity_role="high",
    analytes=None,
):
    return observations.ObservationEvidenceSpec.model_validate(
        {
            "fidelity_role": fidelity_role,
            "observation_origin": origin,
            "analytes": analytes or [],
        }
    )


def _analyte(
    analyte_id="A",
    *,
    assignment="confirmed",
    assignment_evidence="Synthetic assignment fixture",
    retention_result_id=None,
    resolution_result_id=None,
):
    return {
        "analyte_id": analyte_id,
        "assignment": assignment,
        "assignment_evidence": assignment_evidence,
        "retention_result_id": retention_result_id,
        "resolution_result_id": resolution_result_id,
    }


def test_contract_retains_declared_reference_utility_and_exact_experiment_link():
    contract = _contract()
    utility = contract["utility_definition"]
    assert utility["profile"] == "moses-botier-1.0.0-binary64/1"
    assert utility["objective"] == "NumPeaks-CritRes"
    assert utility["final_rule"] == "value"
    assert utility["criteria"][0]["name"] == "missing_analytes"
    assert utility["criteria"][1]["threshold"] == 1.5
    assert utility["criteria"][2]["threshold"] == 5.0
    assert "score" not in utility
    assert contract["spec"]["utility_input_definition"] == "finite-resolution-reference/1"

    method = _method()
    experiment = experiments.create_experiment(
        "chemist",
        experiments.ExperimentSpec(
            title="Bound design",
            source="caller-declared",
            observation_contract_id=contract["contract_id"],
            executable_method_id=method["method_id"],
        ),
        "bound-experiment",
    )
    assert experiment["observation_contract"] == {
        "contract_id": contract["contract_id"],
        "input_sha256": contract["input_sha256"],
    }
    assert observations.get_observation_contract("chemist", contract["contract_id"]) == contract
    with pytest.raises(ValueError, match="not found or is not accessible"):
        observations.get_observation_contract("other", contract["contract_id"])


@pytest.mark.parametrize(
    "values",
    [
        {
            "required_analytes": ["A", "A"],
            "resolution_definition": "finite",
            "max_acquisition_duration_min": 10,
        },
        {
            "required_analytes": ["  "],
            "resolution_definition": "finite",
            "max_acquisition_duration_min": 10,
        },
        {
            "required_analytes": ["A"],
            "resolution_definition": "finite",
            "max_acquisition_duration_min": float("inf"),
        },
        {
            "required_analytes": ["A"],
            "resolution_definition": "finite",
            "max_acquisition_duration_min": 4,
            "runtime_preference_min": 5,
        },
    ],
)
def test_contract_schema_rejects_unbounded_or_inconsistent_values(values):
    with pytest.raises(ValidationError):
        observations.ObservationContractSpec.model_validate(values)


def test_high_fidelity_simulation_uses_method_duration_and_inclusive_boundaries():
    contract = _contract()
    _, run = _attempt(
        contract["contract_id"], evaluator_kind="simulator", fidelity=0.99, key="sim-run"
    )
    retention = _result(
        "chemist",
        run["attempt_id"],
        "retention_time",
        "A",
        value=120,
        unit="s",
        key="retention",
    )
    resolution = _result(
        "chemist",
        run["attempt_id"],
        "resolution",
        "A",
        value=1.5,
        unit="1",
        key="resolution",
    )
    observed = observations.evaluate_observation(
        "chemist",
        contract["contract_id"],
        run["attempt_id"],
        1,
        _evidence(
            origin="simulator_latent",
            analytes=[
                _analyte(
                    retention_result_id=retention["result_id"],
                    resolution_result_id=resolution["result_id"],
                )
            ],
        ),
        "evaluate-sim",
    )
    evidence = observed["observation"]["evidence"]
    assessment = observed["observation"]["assessment"]
    assert evidence["fidelity_role"] == "high"
    assert evidence["fidelity_role_check"] == "matches_attempt_coordinate"
    assert evidence["observation_origin"] == "simulator_latent"
    assert evidence["attempt_evaluator_kind"] == "simulator"
    assert observed["observation"]["executable_method"]["acquisition_duration_min"] == 2
    linked = evidence["analytes"][0]
    assert linked["retention_result"]["raw_unit"] == "s"
    assert linked["retention_result"]["normalized_value"] == 2
    assert linked["resolution_result"]["normalized_value"] == 1.5
    assert assessment["analytical_acceptance"] == "accepted"
    assert assessment["feasible"] is True
    # High-fidelity simulation remains explicitly simulated; no physical claim is emitted.
    assert "physical_qualification" not in observed["observation"]


def test_missing_required_and_missing_or_failed_resolution_remain_unknown_not_zero():
    contract = _contract(analytes=("A", "B"))
    _, run = _attempt(contract["contract_id"], key="unknown-run")
    retention = _result(
        "chemist", run["attempt_id"], "retention_time", "A", value=1, unit="min", key="rt"
    )
    failed_rs = _result(
        "chemist",
        run["attempt_id"],
        "resolution",
        "A",
        status="failed",
        key="failed-rs",
    )
    observed = observations.evaluate_observation(
        "chemist",
        contract["contract_id"],
        run["attempt_id"],
        1,
        _evidence(
            analytes=[
                _analyte(
                    retention_result_id=retention["result_id"],
                    resolution_result_id=failed_rs["result_id"],
                )
            ]
        ),
        "unknown-evaluation",
    )
    assert observed["observation"]["assessment"]["analytical_acceptance"] == "unknown"
    assert observed["observation"]["evidence"]["required_analytes_without_declared_evidence"] == [
        "B"
    ]
    assert (
        observed["observation"]["evidence"]["analytes"][0]["resolution_result"]["status"]
        == "failed"
    )

    _, second = _attempt(contract["contract_id"], key="zero-run")
    rt = _result(
        "chemist", second["attempt_id"], "retention_time", "A", value=1, unit="min", key="zero-rt"
    )
    zero = _result(
        "chemist", second["attempt_id"], "resolution", "A", value=0, unit="1", key="zero-rs"
    )
    zero_observation = observations.evaluate_observation(
        "chemist",
        contract["contract_id"],
        second["attempt_id"],
        1,
        _evidence(
            analytes=[
                _analyte(
                    retention_result_id=rt["result_id"], resolution_result_id=zero["result_id"]
                )
            ]
        ),
        "zero-evaluation",
    )
    resolution = zero_observation["observation"]["evidence"]["analytes"][0]["resolution_result"]
    assert resolution["status"] == "observed" and resolution["normalized_value"] == 0
    assert zero_observation["observation"]["assessment"]["analytical_acceptance"] == "not_accepted"


def test_explicit_nondetection_fails_and_conflicting_fidelity_or_origin_rolls_back():
    contract = _contract()
    _, run = _attempt(
        contract["contract_id"], evaluator_kind="instrument", fidelity=0.4, key="physical-low"
    )
    absent = _result(
        "chemist",
        run["attempt_id"],
        "retention_time",
        "A",
        status="not_detected",
        unit="min",
        key="absent",
    )
    spec = _evidence(
        fidelity_role="low",
        origin="instrument_reported",
        analytes=[_analyte(retention_result_id=absent["result_id"])],
    )
    record = observations.evaluate_observation(
        "chemist", contract["contract_id"], run["attempt_id"], 1, spec, "physical-low-observation"
    )
    evidence = record["observation"]["evidence"]
    assert evidence["fidelity_role"] == "low"
    assert evidence["observation_origin"] == "instrument_reported"
    assert record["observation"]["assessment"]["analytical_acceptance"] == "not_accepted"

    with pytest.raises(ValueError, match="fidelity_role conflicts"):
        observations.evaluate_observation(
            "chemist",
            contract["contract_id"],
            run["attempt_id"],
            1,
            _evidence(fidelity_role="high", origin="instrument_reported"),
            "retry-after-validation-error",
        )
    # Validation failures commit neither the observation nor an idempotency receipt.
    fixed = _evidence(fidelity_role="low", origin="instrument_reported")
    retried = observations.evaluate_observation(
        "chemist",
        contract["contract_id"],
        run["attempt_id"],
        1,
        fixed,
        "retry-after-validation-error",
    )
    assert retried["observation"]["evidence"]["fidelity_role"] == "low"

    other_contract = _contract(key="sim-contract")
    _, simulated = _attempt(
        other_contract["contract_id"], evaluator_kind="simulator", key="sim-kind"
    )
    with pytest.raises(ValueError, match="origin conflicts"):
        observations.evaluate_observation(
            "chemist",
            other_contract["contract_id"],
            simulated["attempt_id"],
            1,
            _evidence(origin="instrument_reported"),
            "wrong-origin",
        )


def test_result_links_require_exact_current_owned_metric_and_attempt_revision():
    contract = _contract()
    _, run = _attempt(contract["contract_id"], key="link-run")
    right = _result(
        "chemist", run["attempt_id"], "retention_time", "A", value=1.2, unit="min", key="right"
    )
    with pytest.raises(ValueError, match="metric"):
        observations.evaluate_observation(
            "chemist",
            contract["contract_id"],
            run["attempt_id"],
            1,
            _evidence(analytes=[_analyte(resolution_result_id=right["result_id"])]),
            "wrong-metric",
        )
    with pytest.raises(ValueError, match="analyte"):
        observations.evaluate_observation(
            "chemist",
            contract["contract_id"],
            run["attempt_id"],
            1,
            _evidence(analytes=[_analyte("different", retention_result_id=right["result_id"])]),
            "wrong-analyte",
        )
    with pytest.raises(ValueError, match="not found or is not accessible"):
        observations.evaluate_observation(
            "other",
            contract["contract_id"],
            run["attempt_id"],
            1,
            _evidence(),
            "foreign-owner",
        )

    revised_spec = experiments.AttemptSpec.model_validate(run["spec"])
    experiments.revise_attempt(
        "chemist", run["attempt_id"], 1, revised_spec, "Synthetic revision", "revise-link-run"
    )
    with pytest.raises(ValueError, match="revision conflict"):
        observations.evaluate_observation(
            "chemist", contract["contract_id"], run["attempt_id"], 1, _evidence(), "stale-revision"
        )
    with pytest.raises(ValueError, match="attempt, revision"):
        observations.evaluate_observation(
            "chemist",
            contract["contract_id"],
            run["attempt_id"],
            2,
            _evidence(analytes=[_analyte(retention_result_id=right["result_id"])]),
            "stale-result-revision",
        )


def test_result_values_units_and_analyte_declarations_are_checked_semantically():
    contract = _contract()
    _, run = _attempt(contract["contract_id"], key="semantic-run")
    negative = _result(
        "chemist",
        run["attempt_id"],
        "retention_time",
        "A",
        value=-0.1,
        unit="min",
        key="negative-rt",
    )
    with pytest.raises(ValueError, match="finite and nonnegative"):
        observations.evaluate_observation(
            "chemist",
            contract["contract_id"],
            run["attempt_id"],
            1,
            _evidence(analytes=[_analyte(retention_result_id=negative["result_id"])]),
            "negative-rt-evaluation",
        )
    wrong_unit = _result(
        "chemist", run["attempt_id"], "resolution", "A", value=2, unit="mg/L", key="wrong-rs-unit"
    )
    with pytest.raises(ValueError, match="Resolution results must use unit 1"):
        observations.evaluate_observation(
            "chemist",
            contract["contract_id"],
            run["attempt_id"],
            1,
            _evidence(analytes=[_analyte(resolution_result_id=wrong_unit["result_id"])]),
            "wrong-rs-unit-evaluation",
        )
    with pytest.raises(ValueError, match="at most one row per analyte"):
        observations.evaluate_observation(
            "chemist",
            contract["contract_id"],
            run["attempt_id"],
            1,
            _evidence(analytes=[_analyte(), _analyte(assignment_evidence="Second row")]),
            "duplicate-analyte-evidence",
        )
    with pytest.raises(ValueError, match="outside the bound contract"):
        observations.evaluate_observation(
            "chemist",
            contract["contract_id"],
            run["attempt_id"],
            1,
            _evidence(analytes=[_analyte("extra")]),
            "extra-analyte-evidence",
        )


def test_processed_signal_requires_analysis_provenance_and_is_supported_when_linked():
    contract = _contract()
    trace = {
        "trace_id": "uv",
        "sample_id": "sample",
        "injection_id": "injection",
        "channel": "uv",
        "separation": "LC",
        "detector": "UV",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "axis": [0, 1, 2],
        "signal": [0, 2, 0],
    }
    dataset = server.register_dataset(
        "chemist", server.Dataset(title="Synthetic signal", traces=[server.Trace(**trace)])
    )
    experiment, run = _attempt(
        contract["contract_id"],
        evaluator_kind="instrument",
        fidelity=0.6,
        key="signal-run",
        measurements=[{"dataset_id": dataset["dataset_id"], "trace_id": "uv"}],
    )
    with pytest.raises(ValueError, match="supporting analysis"):
        observations.evaluate_observation(
            "chemist",
            contract["contract_id"],
            run["attempt_id"],
            1,
            _evidence(fidelity_role="low", origin="processed_signal"),
            "signal-no-analysis",
        )
    analysis = server.save_analysis(
        "chemist",
        dataset["dataset_id"],
        "uv",
        "peak_quantitation",
        {"unit": "min"},
        {"summary": {"retention": 2}, "warnings": []},
        dataset["input_sha256"],
    )
    measured = experiments.record_results(
        "chemist",
        run["attempt_id"],
        1,
        [
            experiments.ResultSpec(
                metric="retention_time",
                analyte_reference="A",
                status="observed",
                unit="min",
                analysis_id=analysis["analysis_id"],
                summary_field="retention",
            )
        ],
        "signal-result",
    )["results"][0]
    observed = observations.evaluate_observation(
        "chemist",
        contract["contract_id"],
        run["attempt_id"],
        1,
        _evidence(
            fidelity_role="low",
            origin="processed_signal",
            analytes=[_analyte(retention_result_id=measured["result_id"])],
        ),
        "signal-with-analysis",
    )
    linked = observed["observation"]["evidence"]["analytes"][0]["retention_result"]
    assert linked["outcome"]["provenance"]["analysis_id"] == analysis["analysis_id"]
    assert observed["observation"]["experiment_id"] == experiment["experiment_id"]


def test_idempotency_concurrency_persistence_and_snapshot_immutability(tmp_path):
    contract = _contract()
    _, run = _attempt(contract["contract_id"], key="snapshot-run")
    retention = _result(
        "chemist", run["attempt_id"], "retention_time", "A", value=1, unit="min", key="snapshot-rt"
    )
    evidence = _evidence(analytes=[_analyte(retention_result_id=retention["result_id"])])

    def evaluate(_):
        return observations.evaluate_observation(
            "chemist",
            contract["contract_id"],
            run["attempt_id"],
            1,
            evidence,
            "concurrent-observation",
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        records = list(pool.map(evaluate, range(5)))
    assert len({item["observation_id"] for item in records}) == 1
    record = records[0]
    assert observations.get_observation("chemist", record["observation_id"]) == record
    snapshot = observations.create_observation_snapshot(
        "chemist",
        contract["contract_id"],
        [record["observation_id"], record["observation_id"]],
        "snapshot",
    )
    assert len(snapshot["members"]) == 1
    assert observations.get_observation_snapshot("chemist", snapshot["snapshot_id"]) == snapshot
    with pytest.raises(ValueError, match="not found or is not accessible"):
        observations.get_observation_snapshot("other", snapshot["snapshot_id"])

    corrected = experiments.ResultSpec(
        metric="retention_time",
        analyte_reference="A",
        status="observed",
        unit="min",
        value=1.1,
        supersedes_result_id=retention["result_id"],
        explanation="Synthetic correction",
    )
    replacement = experiments.record_results(
        "chemist", run["attempt_id"], 1, [corrected], "correct-snapshot-result"
    )["results"][0]
    # The original receipt is replayed even though its result link is now superseded.
    replay = observations.evaluate_observation(
        "chemist", contract["contract_id"], run["attempt_id"], 1, evidence, "concurrent-observation"
    )
    assert replay == record
    with pytest.raises(ValueError, match="nonsuperseded"):
        observations.evaluate_observation(
            "chemist", contract["contract_id"], run["attempt_id"], 1, evidence, "new-stale-link"
        )
    assert observations.get_observation_snapshot("chemist", snapshot["snapshot_id"]) == snapshot
    assert observations.get_observation("chemist", record["observation_id"]) == record
    assert replacement["result_id"] != retention["result_id"]

    env = dict(os.environ, MEASURE_MCP_DATA_DIR=str(tmp_path))
    restored = json.loads(
        subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import json; from measure_service import observations; "
                f"print(json.dumps(observations.get_observation_snapshot('chemist', {snapshot['snapshot_id']!r})))",
            ],
            cwd=tmp_path,
            env=env,
            text=True,
        )
    )
    assert restored == snapshot


def test_snapshot_rejects_two_observations_from_one_physical_attempt():
    contract = _contract()
    _, run = _attempt(contract["contract_id"], key="duplicate-attempt")
    first = observations.evaluate_observation(
        "chemist", contract["contract_id"], run["attempt_id"], 1, _evidence(), "obs-first"
    )
    second = observations.evaluate_observation(
        "chemist",
        contract["contract_id"],
        run["attempt_id"],
        1,
        _evidence(analytes=[_analyte(assignment_evidence="Different declaration")]),
        "obs-second",
    )
    with pytest.raises(ValueError, match="one observation per physical attempt"):
        observations.create_observation_snapshot(
            "chemist",
            contract["contract_id"],
            [second["observation_id"], first["observation_id"]],
            "duplicate-attempt-snapshot",
        )


def test_contract_and_observation_mutations_are_owner_scoped_and_receipted():
    contract = _contract()
    replay = observations.register_observation_contract(
        "chemist",
        "Required analytes",
        observations.ObservationContractSpec.model_validate(contract["spec"]),
        "contract",
    )
    assert replay == contract
    with pytest.raises(ValueError, match="different registry request"):
        observations.register_observation_contract(
            "chemist",
            "Different title",
            observations.ObservationContractSpec.model_validate(contract["spec"]),
            "contract",
        )
    with pytest.raises(ValueError, match="not found or is not accessible"):
        observations.get_observation("other", "unknown-observation")


def test_observation_response_byte_limit_rolls_back_record_and_retry_receipt(monkeypatch):
    from measure_service.storage import canonical, registry_connection

    contract = _contract()
    _, attempt = _attempt(contract["contract_id"])
    request = _evidence(analytes=[_analyte(assignment="unassigned")])
    first = observations.evaluate_observation(
        "chemist", contract["contract_id"], attempt["attempt_id"], 1, request, "within-limit"
    )
    payload_bytes = len(canonical(first["observation"]).encode("utf-8"))
    assert len(canonical(first).encode("utf-8")) > payload_bytes + 1
    monkeypatch.setattr(observations, "MAX_RECORD_BYTES", payload_bytes + 1)
    with pytest.raises(ValueError, match="byte limit"):
        observations.evaluate_observation(
            "chemist",
            contract["contract_id"],
            attempt["attempt_id"],
            1,
            request,
            "response-too-large",
        )
    with registry_connection() as con:
        assert con.execute("SELECT COUNT(*) FROM analytical_observations").fetchone()[0] == 1
        assert (
            con.execute(
                "SELECT COUNT(*) FROM registry_receipts WHERE request_key='response-too-large'"
            ).fetchone()[0]
            == 0
        )
