"""Standalone export checks using only sealed, synthetic unknown-slot fixtures."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_ablation_reference.py"
SPEC = importlib.util.spec_from_file_location("export_ablation_reference", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)

DECLARATION_PATH = (
    ROOT
    / "src"
    / "measure_service"
    / "benchmarks"
    / "reference_data"
    / "synthetic-isocratic-ablation-v1.json"
)
CORE_FILE_NAMES = (
    "campaigns.jsonl",
    "evaluations.csv",
    "evaluations.jsonl",
    "measure.sqlite3",
    "paired-comparisons.csv",
    "paired-contrast-summary.csv",
    "preflight.json",
    "protocol.json",
    "report.json",
    "report.md",
    "success-trajectories.csv",
)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unknown_evaluation(arm: dict[str, Any], task: dict[str, Any], seed: int, ordinal: int):
    return {
        "arm_id": arm["arm_id"],
        "cohort_role": task["cohort_role"],
        "optimizer_target_config": arm["optimizer_target"],
        "surrogate_noise_prior": arm["surrogate_noise_prior"],
        "policy": arm["policy"],
        "task_id": task["task_id"],
        "seed": seed,
        "ordinal": ordinal,
        "budget": 10,
        "campaign_id": None,
        "status": "preflight_blocked_unknown",
        "failure_step": "preflight_gate",
        "failure": {
            "type": "PreflightNotQualified",
            "message": "The synthetic fixture represents an unqualified preflight.",
        },
        "optimizer_target_record": None,
    }


def _unknown_campaign(arm: dict[str, Any], task: dict[str, Any], seed: int):
    return {
        "arm_id": arm["arm_id"],
        "cohort_role": task["cohort_role"],
        "optimizer_target_config": arm["optimizer_target"],
        "surrogate_noise_prior": arm["surrogate_noise_prior"],
        "policy": arm["policy"],
        "task_id": task["task_id"],
        "seed": seed,
        "campaign_id": None,
        "evaluation_budget": 10,
        "status": "partial_failure",
    }


def _synthetic_prediction(task: dict[str, Any], times: dict[str, float]) -> dict[str, Any]:
    return {
        "prediction_id": "prediction-fixture",
        "prediction": {
            "analytes": [
                {
                    "analyte_id": analyte["analyte_id"],
                    "parameters": {"model": "linear"},
                    "source_fit_range_warnings": [],
                    "result": {
                        "profile": "ideal-lc-retention-zeta/1",
                        "status": "eluted",
                        "retention_time_min": times[analyte["analyte_id"]],
                        "dead_time_min": 0.5,
                        "post_column_transit_min": 0.1,
                        "retention_zeta_min": times[analyte["analyte_id"]] - 0.6,
                        "right_censor_bound_min": None,
                        "numerical_error_estimate_min": 1e-8,
                        "evaluation_count": 17,
                        "diagnostics": [{"kind": "synthetic fixture"}],
                    },
                }
                for analyte in task["analytes"]
            ]
        },
    }


def _sealed_unknown_bundle(root: Path) -> tuple[Path, dict[str, Any]]:
    declaration_bytes = DECLARATION_PATH.read_bytes()
    digest = _digest(declaration_bytes)
    assert digest == exporter.EXPECTED_STUDY_SHA256
    declaration = json.loads(declaration_bytes)
    arms = declaration["arms"]
    tasks = declaration["tasks"]
    seeds = declaration["seeds"]
    evaluations = [
        _unknown_evaluation(arm, task, seed, ordinal)
        for arm in arms
        for task in tasks
        for seed in seeds
        for ordinal in range(declaration["evaluation_budget"])
    ]
    campaigns = [
        _unknown_campaign(arm, task, seed) for arm in arms for task in tasks for seed in seeds
    ]
    bundle = root / "sealed-core"
    bundle.mkdir()
    protocol = {
        "profile": exporter.SOURCE_PROFILE,
        "study_sha256": digest,
        "study_declaration": declaration,
        "registered_task_inputs": [],
        "arms": [
            {"arm": arm, "registered_protocol": {"implementation_provenance": None}} for arm in arms
        ],
    }
    report = {
        "schema_version": 1,
        "profile": exporter.SOURCE_PROFILE,
        "status": "partial_failure",
        "completed": False,
        "planned_denominators": {"campaigns_total": 60, "evaluations_total": 600},
        "study_input": {"sha256": digest, "bundled_path": exporter.DECLARATION_RELATIVE_PATH},
        "implementation_provenance": {
            "study_input_sha256": digest,
            "measure_service": {
                "package": "measure-service",
                "source_ref": None,
                "source_revision": None,
                "version": "fixture-version",
            },
            "runner_module_sha256": "f" * 64,
            "preflight_receipts": [],
        },
        "evidence_files": list(CORE_FILE_NAMES),
    }
    contents = {
        "protocol.json": json.dumps(protocol, sort_keys=True).encode() + b"\n",
        "report.json": json.dumps(report, sort_keys=True).encode() + b"\n",
        "evaluations.jsonl": b"".join(
            json.dumps(row, sort_keys=True).encode() + b"\n" for row in evaluations
        ),
        "campaigns.jsonl": b"".join(
            json.dumps(row, sort_keys=True).encode() + b"\n" for row in campaigns
        ),
        "preflight.json": b'{"attempts":[]}\n',
        "evaluations.csv": b"fixture source csv\n",
        "success-trajectories.csv": b"fixture source trajectories\n",
        "paired-comparisons.csv": b"fixture source pairs\n",
        "paired-contrast-summary.csv": b"fixture source contrast summary\n",
        "measure.sqlite3": b"fixture sealed database placeholder\n",
        "report.md": b"fixture sealed report\n",
    }
    for name, data in contents.items():
        (bundle / name).write_bytes(data)
    seal = {
        "schema_version": 1,
        "files": {
            name: {"sha256": _digest(data), "size_bytes": len(data)}
            for name, data in contents.items()
        },
    }
    _write_json(bundle / "complete.json", seal)
    return bundle, {"declaration": declaration, "evaluations": evaluations}


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in root.iterdir()
        if path.is_file() and not path.is_symlink()
    }


def _replace_sealed_jsonl(bundle: Path, name: str, rows: list[dict[str, Any]]) -> None:
    data = b"".join(json.dumps(row, sort_keys=True).encode() + b"\n" for row in rows)
    _write_and_reseal(bundle, {name: data})


def _write_and_reseal(bundle: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        (bundle / name).write_bytes(data)
    seal = json.loads((bundle / "complete.json").read_text())
    for name, data in files.items():
        seal["files"][name] = {"sha256": _digest(data), "size_bytes": len(data)}
    _write_json(bundle / "complete.json", seal)


def test_export_preserves_all_planned_rows_factors_units_and_missingness(tmp_path):
    bundle, fixture = _sealed_unknown_bundle(tmp_path)
    before = _file_snapshot(bundle)
    output = tmp_path / "public-reference"

    metadata = exporter.export_reference(bundle, output_dir=output)

    with (output / exporter.EVALUATION_FILE).open(newline="") as stream:
        evaluations = list(csv.DictReader(stream))
    with (output / exporter.ANALYTE_FILE).open(newline="") as stream:
        analytes = list(csv.DictReader(stream))
    assert len(evaluations) == 600
    assert len(analytes) == 1800
    evaluation_keys = {
        (row["task_id"], row["seed"], row["arm_id"], row["ordinal"]) for row in evaluations
    }
    analyte_keys = {
        (row["task_id"], row["seed"], row["arm_id"], row["ordinal"], row["analyte_id"])
        for row in analytes
    }
    assert len(evaluation_keys) == 600
    assert len(analyte_keys) == 1800
    assert metadata["study"]["tasks"] == fixture["declaration"]["tasks"]
    assert metadata["study"]["arms"] == fixture["declaration"]["arms"]
    assert metadata["study"]["factors"] == fixture["declaration"]["factors"]
    assert metadata["study"]["sha256"] == exporter.EXPECTED_STUDY_SHA256
    assert metadata["tables"][exporter.EVALUATION_FILE]["exported_rows"] == 600
    assert "observed_rows" not in metadata["tables"][exporter.EVALUATION_FILE]
    assert metadata["tables"][exporter.ANALYTE_FILE]["exported_rows"] == 1800
    assert (
        "before Gaussian simulation"
        in metadata["tables"][exporter.ANALYTE_FILE]["units"]["prediction_retention_time_min"]
    )
    assert (
        "Gaussian-producer centroid"
        in metadata["tables"][exporter.ANALYTE_FILE]["units"][
            "gaussian_centroid_retention_time_min"
        ]
    )
    assert DECLARATION_PATH.as_posix().endswith(metadata["study"]["declaration_source"])
    assert metadata["execution_requirement"].startswith(
        "This standard-library-only exporter also requires the frozen study declaration"
    )
    assert metadata["tables"][exporter.EVALUATION_FILE]["grain"] == (
        "one row per planned (task_id, seed, arm_id, ordinal) slot"
    )
    assert (
        metadata["tables"][exporter.EVALUATION_FILE]["units"]["flow_ml_min"]
        == "mL/min (declared fixed protocol flow)"
    )
    assert (
        metadata["provenance"]["report_implementation_provenance"]["measure_service"][
            "source_revision"
        ]
        is None
    )
    assert (output / "complete.json").is_file()
    for row in evaluations:
        assert row["evaluation_status"] == "preflight_blocked_unknown"
        assert row["campaign_status"] == "partial_failure"
        assert row["analytical_acceptance"] == ""
        assert row["analytical_acceptance_status"] == "unavailable"
        assert row["historical_utility_status"] == "unavailable"
        assert row["historical_utility_value"] == ""
        assert row["optimizer_reward_status"] == "unavailable"
        assert row["optimizer_reward_value"] == ""
        assert row["failure_step"] == "preflight_gate"
    for row in analytes:
        assert row["prediction_evidence_status"] == "unavailable"
        assert row["prediction_retention_time_min"] == ""
        assert row["gaussian_centroid_evidence_status"] == "unavailable"
        assert row["assessment_status"] == "unavailable"
        assert row["detection_status"] == "unavailable"
        assert row["gaussian_centroid_retention_time_min"] == ""
        assert row["gaussian_resolution"] == ""
        assert row["detection"] == ""
        assert row["historical_utility_input_status"] == "unavailable"
    assert _file_snapshot(bundle) == before


def test_unconsumed_completed_attempt_keeps_available_synthetic_evidence(tmp_path):
    bundle, fixture = _sealed_unknown_bundle(tmp_path)
    document = fixture["declaration"]
    task = document["tasks"][0]
    arm = document["arms"][0]
    seed = document["seeds"][0]
    row = next(
        item
        for item in fixture["evaluations"]
        if item["arm_id"] == arm["arm_id"]
        and item["task_id"] == task["task_id"]
        and item["seed"] == seed
        and item["ordinal"] == 3
    )
    row.update(
        {
            "status": "partial_failure",
            "campaign_id": "campaign-fixture",
            "failure_step": "consume_campaign_snapshot",
            "failure": {
                "type": "SyntheticConsumeFailure",
                "message": "The completed synthetic attempt was not consumed.",
            },
            "candidate": {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 6.0},
            "attempt": {"attempt_id": "attempt-fixture", "spec": {"status": "completed"}},
            "metrics": {
                "completed_simulator_attempt": True,
                "consumed_evaluation": False,
                "declared_evaluation_cost": 1.0,
                "declared_evaluation_cost_unit": "synthetic_evaluation",
                "simulated_acquisition_time_min": 6.0,
                "declared_solvent_volume_ml": 3.0,
                "declared_solvent_use_ml": {"A": 2.0, "B": 1.0},
            },
            "backend": {"runtime": {"elapsed_seconds": 0.25}},
            "gaussian_observation": {
                "observation": {
                    "analytes": [
                        {
                            "analyte_id": analyte["analyte_id"],
                            "retention_status": "eluted",
                            "retention_time_min": index + 1.25,
                            "resolution_status": "observed",
                            "resolution": 0.75 + index,
                        }
                        for index, analyte in enumerate(task["analytes"])
                    ]
                }
            },
            "observation": {
                "observation_id": "observation-fixture",
                "observation": {
                    "assessment": {
                        "analytical_acceptance": "not_accepted",
                        "per_analyte": [
                            {
                                "analyte_id": analyte["analyte_id"],
                                "retention_status": "eluted",
                                "retention_time_min": index + 1.25,
                                "detection": None if index == 1 else True,
                                "resolution_status": "observed",
                                "resolution": 0.75 + index,
                                "resolution_satisfied": False,
                                "acceptance_state": "failed",
                            }
                            for index, analyte in enumerate(task["analytes"])
                        ],
                    }
                },
            },
        }
    )
    campaign_rows = [
        json.loads(line) for line in (bundle / "campaigns.jsonl").read_text().splitlines()
    ]
    campaign = next(
        item
        for item in campaign_rows
        if item["arm_id"] == arm["arm_id"]
        and item["task_id"] == task["task_id"]
        and item["seed"] == seed
    )
    campaign["campaign_id"] = row["campaign_id"]
    for planned_row in fixture["evaluations"]:
        if (
            planned_row["arm_id"] == arm["arm_id"]
            and planned_row["task_id"] == task["task_id"]
            and planned_row["seed"] == seed
        ):
            planned_row["campaign_id"] = row["campaign_id"]
    _replace_sealed_jsonl(bundle, "evaluations.jsonl", fixture["evaluations"])
    _replace_sealed_jsonl(bundle, "campaigns.jsonl", campaign_rows)
    output = tmp_path / "synthetic-public-reference"

    exporter.export_reference(bundle, output_dir=output)

    with (output / exporter.EVALUATION_FILE).open(newline="") as stream:
        evaluation_rows = list(csv.DictReader(stream))
    with (output / exporter.ANALYTE_FILE).open(newline="") as stream:
        analyte_rows = list(csv.DictReader(stream))
    evaluation = next(
        item
        for item in evaluation_rows
        if item["arm_id"] == arm["arm_id"]
        and item["task_id"] == task["task_id"]
        and item["seed"] == str(seed)
        and item["ordinal"] == "3"
    )
    analytes = [
        item
        for item in analyte_rows
        if item["arm_id"] == arm["arm_id"]
        and item["task_id"] == task["task_id"]
        and item["seed"] == str(seed)
        and item["ordinal"] == "3"
    ]

    assert evaluation["evaluation_status"] == "partial_failure"
    assert evaluation["attempt_status"] == "completed"
    assert evaluation["completed_simulator_attempt"] == "True"
    assert evaluation["consumed_evaluation"] == "False"
    assert evaluation["completed_attempt_evaluation_cost"] == "1.0"
    assert evaluation["optimizer_walltime_seconds"] == "0.25"
    assert evaluation["analytical_acceptance"] == "not_accepted"
    assert evaluation["analytical_acceptance_status"] == "assessment_evidence"
    assert evaluation["historical_utility_status"] == "unavailable"
    assert evaluation["optimizer_reward_status"] == "unavailable"
    assert evaluation["declared_solvent_a_ml"] == "2.0"
    assert evaluation["declared_solvent_b_ml"] == "1.0"
    assert len(analytes) == 3
    assert analytes[0]["prediction_evidence_status"] == "unavailable"
    assert analytes[0]["gaussian_centroid_retention_time_min"] == "1.25"
    assert analytes[0]["gaussian_resolution"] == "0.75"
    assert analytes[0]["gaussian_centroid_evidence_status"] == "available"
    assert analytes[0]["assessment_status"] == "available"
    assert analytes[0]["detection_status"] == "observed"
    assert analytes[1]["detection_status"] == "unknown"


def test_prediction_evidence_survives_later_gaussian_failure_without_backfill(tmp_path):
    bundle, fixture = _sealed_unknown_bundle(tmp_path)
    document = fixture["declaration"]
    task = document["tasks"][0]
    arm = document["arms"][0]
    seed = document["seeds"][0]
    row = next(
        item
        for item in fixture["evaluations"]
        if item["arm_id"] == arm["arm_id"]
        and item["task_id"] == task["task_id"]
        and item["seed"] == seed
        and item["ordinal"] == 2
    )
    analyte_ids = [analyte["analyte_id"] for analyte in task["analytes"]]
    prediction_times = {analyte_id: index + 1.5 for index, analyte_id in enumerate(analyte_ids)}
    row.update(
        {
            "status": "partial_failure",
            "campaign_id": "campaign-gaussian-failure-fixture",
            "failure_step": "record_gaussian_observation",
            "failure": {
                "type": "SyntheticGaussianFailure",
                "message": "The Gaussian producer did not return an observation.",
            },
            "candidate": {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 6.0},
            "attempt": {"attempt_id": "attempt-gaussian-failure", "spec": {"status": "completed"}},
            "retention_prediction": _synthetic_prediction(task, prediction_times),
            "metrics": {
                "completed_simulator_attempt": True,
                "consumed_evaluation": False,
                "declared_evaluation_cost": 1.0,
                "declared_evaluation_cost_unit": "synthetic_evaluation",
                "simulated_acquisition_time_min": 6.0,
            },
        }
    )
    campaign_rows = [
        json.loads(line) for line in (bundle / "campaigns.jsonl").read_text().splitlines()
    ]
    campaign = next(
        item
        for item in campaign_rows
        if item["arm_id"] == arm["arm_id"]
        and item["task_id"] == task["task_id"]
        and item["seed"] == seed
    )
    campaign["campaign_id"] = row["campaign_id"]
    for planned_row in fixture["evaluations"]:
        if (
            planned_row["arm_id"] == arm["arm_id"]
            and planned_row["task_id"] == task["task_id"]
            and planned_row["seed"] == seed
        ):
            planned_row["campaign_id"] = row["campaign_id"]
    _replace_sealed_jsonl(bundle, "evaluations.jsonl", fixture["evaluations"])
    _replace_sealed_jsonl(bundle, "campaigns.jsonl", campaign_rows)
    output = tmp_path / "gaussian-failure-reference"

    exporter.export_reference(bundle, output_dir=output)

    with (output / exporter.EVALUATION_FILE).open(newline="") as stream:
        evaluation_rows = list(csv.DictReader(stream))
    with (output / exporter.ANALYTE_FILE).open(newline="") as stream:
        analyte_rows = list(csv.DictReader(stream))
    evaluation = next(
        item
        for item in evaluation_rows
        if item["arm_id"] == arm["arm_id"]
        and item["task_id"] == task["task_id"]
        and item["seed"] == str(seed)
        and item["ordinal"] == "2"
    )
    analytes = [
        item
        for item in analyte_rows
        if item["arm_id"] == arm["arm_id"]
        and item["task_id"] == task["task_id"]
        and item["seed"] == str(seed)
        and item["ordinal"] == "2"
    ]

    assert evaluation["attempt_status"] == "completed"
    assert evaluation["completed_simulator_attempt"] == "True"
    assert evaluation["consumed_evaluation"] == "False"
    assert evaluation["analytical_acceptance"] == ""
    assert evaluation["historical_utility_status"] == "unavailable"
    assert evaluation["optimizer_reward_status"] == "unavailable"
    assert len(analytes) == 3
    for row in analytes:
        analyte_id = row["analyte_id"]
        assert row["prediction_evidence_status"] == "available"
        assert row["prediction_profile"] == "ideal-lc-retention-zeta/1"
        assert row["prediction_retention_status"] == "eluted"
        assert float(row["prediction_retention_time_min"]) == prediction_times[analyte_id]
        assert float(row["prediction_dead_time_min"]) == 0.5
        assert float(row["prediction_post_column_transit_min"]) == 0.1
        assert float(row["prediction_retention_zeta_min"]) == prediction_times[analyte_id] - 0.6
        assert row["prediction_right_censor_bound_min"] == ""
        assert row["prediction_evaluation_count"] == "17"
        assert row["gaussian_centroid_evidence_status"] == "unavailable"
        assert row["gaussian_retention_status"] == ""
        assert row["gaussian_centroid_retention_time_min"] == ""
        assert row["gaussian_resolution_status"] == ""
        assert row["gaussian_resolution"] == ""
        assert row["assessment_status"] == "unavailable"
        assert row["detection_status"] == "unavailable"
        assert row["historical_utility_input_status"] == "unavailable"


def test_consumed_utility_acceptance_reward_and_raw_inputs_stay_separate(tmp_path):
    bundle, fixture = _sealed_unknown_bundle(tmp_path)
    document = fixture["declaration"]
    task = document["tasks"][0]
    arm = document["arms"][1]
    seed = document["seeds"][0]
    row = next(
        item
        for item in fixture["evaluations"]
        if item["arm_id"] == arm["arm_id"]
        and item["task_id"] == task["task_id"]
        and item["seed"] == seed
        and item["ordinal"] == 0
    )
    analyte_ids = [analyte["analyte_id"] for analyte in task["analytes"]]
    retention_times = {analyte_id: index + 1.5 for index, analyte_id in enumerate(analyte_ids)}
    resolutions = {analyte_id: index + 0.25 for index, analyte_id in enumerate(analyte_ids)}
    raw_ids = list(reversed(analyte_ids))
    target = {
        "profile": "accepted-reward/1",
        "name": "confirmed_acceptance_reward",
        "direction": "maximize",
        "status": "scored",
        "value": 1.0,
    }
    row.update(
        {
            "status": "consumed",
            "campaign_id": "campaign-consumed-fixture",
            "optimizer_target_record": target,
            "retention_prediction": _synthetic_prediction(task, retention_times),
            "gaussian_observation": {
                "observation": {
                    "analytes": [
                        {
                            "analyte_id": analyte_id,
                            "retention_status": "eluted",
                            "retention_time_min": retention_times[analyte_id],
                            "resolution_status": "observed",
                            "resolution": resolutions[analyte_id],
                        }
                        for analyte_id in analyte_ids
                    ]
                }
            },
            "consumption": {
                "consumption_id": "consumption-fixture",
                "result": {
                    "analytical_acceptance": "accepted",
                    "utility": {"status": "scored", "score": 1.75},
                    "optimizer_target": target,
                    "raw_metrics": {
                        "analyte_ids": raw_ids,
                        "retention_statuses": ["eluted"] * len(raw_ids),
                        "retention_times_min": [retention_times[item] for item in raw_ids],
                        "resolution_statuses": ["observed"] * len(raw_ids),
                        "peak_resolutions": [resolutions[item] for item in raw_ids],
                    },
                },
            },
            "metrics": {
                "completed_simulator_attempt": True,
                "consumed_evaluation": True,
                "score_ge_2_success": False,
            },
        }
    )
    campaign_rows = [
        json.loads(line) for line in (bundle / "campaigns.jsonl").read_text().splitlines()
    ]
    campaign = next(
        item
        for item in campaign_rows
        if item["arm_id"] == arm["arm_id"]
        and item["task_id"] == task["task_id"]
        and item["seed"] == seed
    )
    campaign["campaign_id"] = row["campaign_id"]
    for planned_row in fixture["evaluations"]:
        if (
            planned_row["arm_id"] == arm["arm_id"]
            and planned_row["task_id"] == task["task_id"]
            and planned_row["seed"] == seed
        ):
            planned_row["campaign_id"] = row["campaign_id"]
    _replace_sealed_jsonl(bundle, "evaluations.jsonl", fixture["evaluations"])
    _replace_sealed_jsonl(bundle, "campaigns.jsonl", campaign_rows)
    output = tmp_path / "consumed-public-reference"

    metadata = exporter.export_reference(bundle, output_dir=output)

    with (output / exporter.EVALUATION_FILE).open(newline="") as stream:
        evaluation_rows = list(csv.DictReader(stream))
    with (output / exporter.ANALYTE_FILE).open(newline="") as stream:
        analyte_rows = list(csv.DictReader(stream))
    evaluation = next(
        item
        for item in evaluation_rows
        if item["arm_id"] == arm["arm_id"]
        and item["task_id"] == task["task_id"]
        and item["seed"] == str(seed)
        and item["ordinal"] == "0"
    )
    analytes = [
        item
        for item in analyte_rows
        if item["arm_id"] == arm["arm_id"]
        and item["task_id"] == task["task_id"]
        and item["seed"] == str(seed)
        and item["ordinal"] == "0"
    ]

    assert evaluation["historical_utility_status"] == "scored"
    assert evaluation["historical_utility_value"] == "1.75"
    assert evaluation["utility_ge_2"] == "False"
    assert evaluation["analytical_acceptance"] == "accepted"
    assert evaluation["analytical_acceptance_status"] == "consumed_result"
    assert evaluation["optimizer_reward_profile"] == "accepted-reward/1"
    assert evaluation["optimizer_reward_name"] == "confirmed_acceptance_reward"
    assert evaluation["optimizer_reward_status"] == "scored"
    assert evaluation["optimizer_reward_value"] == "1.0"
    assert "prediction_*" in metadata["interpretation"]["analyte_projection"]
    assert "gaussian_*" in metadata["interpretation"]["analyte_projection"]
    assert "never fill Gaussian-centroid fields" in metadata["interpretation"]["stage_boundary"]
    assert len(analytes) == 3
    for row in analytes:
        analyte_id = row["analyte_id"]
        assert row["prediction_evidence_status"] == "available"
        assert row["prediction_retention_status"] == "eluted"
        assert float(row["prediction_retention_time_min"]) == retention_times[analyte_id]
        assert row["gaussian_centroid_evidence_status"] == "available"
        assert row["gaussian_retention_status"] == "eluted"
        assert float(row["gaussian_centroid_retention_time_min"]) == retention_times[analyte_id]
        assert row["gaussian_resolution_status"] == "observed"
        assert float(row["gaussian_resolution"]) == resolutions[analyte_id]
        assert row["historical_utility_input_status"] == "available"
        assert (
            float(row["historical_utility_input_retention_time_min"]) == retention_times[analyte_id]
        )
        assert float(row["historical_utility_input_peak_resolution"]) == resolutions[analyte_id]


def test_seal_hash_mismatch_refuses_output_without_changing_core(tmp_path):
    bundle, _ = _sealed_unknown_bundle(tmp_path)
    damaged = bundle / "report.md"
    damaged.write_text("tampered\n")
    before = _file_snapshot(bundle)
    output = tmp_path / "public-reference"

    with pytest.raises(exporter.ExportError, match="does not match its SHA256 and size"):
        exporter.export_reference(bundle, output_dir=output)

    assert _file_snapshot(bundle) == before
    assert not output.exists()


@pytest.mark.parametrize("existing_kind", ["empty_directory", "file"])
def test_existing_output_is_never_overwritten(tmp_path, existing_kind):
    bundle, _ = _sealed_unknown_bundle(tmp_path)
    output = tmp_path / "public-reference"
    output.mkdir() if existing_kind == "empty_directory" else output.write_text("keep\n")
    before = (
        output.read_bytes()
        if output.is_file()
        else tuple(sorted(path.name for path in output.iterdir()))
    )

    with pytest.raises(exporter.ExportError, match="already exists"):
        exporter.export_reference(bundle, output_dir=output)

    after = (
        output.read_bytes()
        if output.is_file()
        else tuple(sorted(path.name for path in output.iterdir()))
    )
    assert after == before


def test_output_inside_bundle_and_manifest_path_traversal_are_rejected(tmp_path):
    bundle, _ = _sealed_unknown_bundle(tmp_path)
    with pytest.raises(exporter.ExportError, match="outside the sealed core"):
        exporter.export_reference(bundle, output_dir=bundle / "export")

    seal_path = bundle / "complete.json"
    seal = json.loads(seal_path.read_text())
    seal["files"]["../outside"] = {"sha256": "0" * 64, "size_bytes": 0}
    _write_json(seal_path, seal)
    output = tmp_path / "public-reference"
    with pytest.raises(exporter.ExportError, match="not a basename"):
        exporter.export_reference(bundle, output_dir=output)
    assert not output.exists()


def test_sealed_file_symlink_is_rejected(tmp_path):
    bundle, _ = _sealed_unknown_bundle(tmp_path)
    victim = bundle / "report.md"
    victim.unlink()
    target = tmp_path / "outside.txt"
    target.write_text("outside\n")
    os.symlink(target, victim)
    output = tmp_path / "public-reference"

    with pytest.raises(exporter.ExportError, match="regular non-symlink"):
        exporter.export_reference(bundle, output_dir=output)

    assert victim.is_symlink()
    assert target.read_text() == "outside\n"
    assert not output.exists()


def test_unlisted_derived_files_are_ignored_preserved_and_not_exported(tmp_path):
    bundle, _ = _sealed_unknown_bundle(tmp_path)
    derived = {
        bundle / "README.md": b"Private note: unlisted text stays outside the export.\n",
        bundle / "existing-plot.png": b"\x89PNG\r\n\x1a\nsynthetic-plot-placeholder",
        bundle / "validation" / "software" / "version.txt": b"local validation record\n",
    }
    for path, content in derived.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    before = _file_snapshot(bundle)
    output = tmp_path / "public-reference"

    exporter.export_reference(bundle, output_dir=output)

    assert _file_snapshot(bundle) == before
    assert {path.relative_to(bundle): path.read_bytes() for path in derived} == {
        path.relative_to(bundle): content for path, content in derived.items()
    }
    assert {path.name for path in output.iterdir()} == {
        exporter.EVALUATION_FILE,
        exporter.ANALYTE_FILE,
        exporter.METADATA_FILE,
        exporter.EXPORT_SEAL_FILE,
    }
    output_bytes = b"".join(path.read_bytes() for path in output.iterdir() if path.is_file())
    assert b"Private note" not in output_bytes


def test_unknown_manifest_filename_is_rejected(tmp_path):
    bundle, _ = _sealed_unknown_bundle(tmp_path)
    report = json.loads((bundle / "report.json").read_text())
    report["evidence_files"].append("future-core.json")
    report_bytes = json.dumps(report, sort_keys=True).encode() + b"\n"
    _write_and_reseal(
        bundle,
        {
            "report.json": report_bytes,
            "future-core.json": b"{}\n",
        },
    )
    output = tmp_path / "public-reference"

    with pytest.raises(exporter.ExportError, match=r"unknown=\['future-core.json'\]"):
        exporter.export_reference(bundle, output_dir=output)

    assert not output.exists()
