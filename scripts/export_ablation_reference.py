#!/usr/bin/env python3
"""Export tidy tables from a sealed synthetic ablation evidence bundle.

This standard-library-only script verifies the complete core bundle before
projecting its fixed 600 evaluation slots into evaluation and analyte grains.
It reads evidence only; it does not rerun the optimizer or scientific kernels.
The frozen declaration must also be present at its repository path, so this is
not a portable single-file exporter.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

SOURCE_PROFILE = "synthetic-isocratic-ablation-study/1"
EXPORT_PROFILE = "measure-service-synthetic-ablation-reference/1"
EXPECTED_STUDY_SHA256 = "0c607fa590c3c5682018765f25b37c4c9820a2ef36fc3d57bb205d0f1aa31094"
DECLARATION_RELATIVE_PATH = (
    "src/measure_service/benchmarks/reference_data/synthetic-isocratic-ablation-v1.json"
)
EXPECTED_CORE_FILES = frozenset(
    {
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
    }
)
MAX_CORE_FILE_BYTES = 512 * 1024 * 1024
EVALUATION_FILE = "evaluations.csv"
ANALYTE_FILE = "analytes.csv"
METADATA_FILE = "metadata.json"
EXPORT_SEAL_FILE = "complete.json"

EVALUATION_BASE_FIELDS = (
    "task_id",
    "cohort_role",
    "seed",
    "arm_id",
    "policy",
    "optimizer_target_config",
    "surrogate_noise_prior",
    "ordinal",
    "budget",
    "evaluation_status",
    "campaign_status",
    "campaign_id",
    "attempt_status",
    "completed_simulator_attempt",
    "consumed_evaluation",
    "failure_step",
    "failure_type",
    "failure_message",
    "proposal_source",
    "strong_solvent_fraction",
    "proposed_acquisition_duration_min",
    "flow_ml_min",
    "method_id",
    "method_input_sha256",
    "canonical_program_sha256",
    "experiment_id",
    "attempt_id",
    "attempt_revision",
    "retention_prediction_id",
    "retention_prediction_input_sha256",
    "retention_prediction_output_sha256",
    "gaussian_run_id",
    "gaussian_run_input_sha256",
    "gaussian_run_output_sha256",
    "observation_id",
    "observation_input_sha256",
    "evidence_sha256",
    "assessment_sha256",
    "snapshot_id",
    "snapshot_sha256",
    "consumption_id",
    "consumption_input_sha256",
    "consumption_output_sha256",
    "analytical_acceptance",
    "analytical_acceptance_status",
    "historical_utility_status",
    "historical_utility_value",
    "utility_ge_2",
    "optimizer_reward_name",
    "optimizer_reward_profile",
    "optimizer_reward_direction",
    "optimizer_reward_status",
    "optimizer_reward_value",
    "detected_count",
    "unknown_count",
    "confirmed_absent_count",
    "simulated_acquisition_time_min",
    "runtime_preference_satisfied",
    "feasible",
    "planned_evaluation_cost",
    "planned_evaluation_cost_unit",
    "completed_attempt_evaluation_cost",
    "completed_attempt_cost_unit",
    "declared_solvent_volume_ml",
    "optimizer_walltime_seconds",
)

ANALYTE_FIELDS = (
    "task_id",
    "cohort_role",
    "seed",
    "arm_id",
    "policy",
    "optimizer_target_config",
    "surrogate_noise_prior",
    "ordinal",
    "evaluation_status",
    "campaign_id",
    "analyte_id",
    "retention_model",
    "ln_k0",
    "S1",
    "assumed_fwhm_min",
    "prediction_evidence_status",
    "prediction_profile",
    "prediction_retention_status",
    "prediction_retention_time_min",
    "prediction_dead_time_min",
    "prediction_post_column_transit_min",
    "prediction_retention_zeta_min",
    "prediction_right_censor_bound_min",
    "prediction_numerical_error_estimate_min",
    "prediction_evaluation_count",
    "gaussian_centroid_evidence_status",
    "gaussian_retention_status",
    "gaussian_centroid_retention_time_min",
    "gaussian_resolution_status",
    "gaussian_resolution",
    "assessment_status",
    "assessment_retention_status",
    "assessment_retention_time_min",
    "detection",
    "detection_status",
    "assessment_resolution_status",
    "assessment_resolution",
    "resolution_satisfied",
    "analyte_acceptance_state",
    "historical_utility_input_retention_status",
    "historical_utility_input_retention_time_min",
    "historical_utility_input_resolution_status",
    "historical_utility_input_peak_resolution",
    "historical_utility_input_status",
)

UNIT_MAP = {
    "strong_solvent_fraction": "1 (fraction)",
    "proposed_acquisition_duration_min": "min (proposed method coordinate)",
    "flow_ml_min": "mL/min (declared fixed protocol flow)",
    "historical_utility_value": "profile-defined score; see study declaration",
    "optimizer_reward_value": "profile-defined maximization target; see study declaration",
    "simulated_acquisition_time_min": "min (declared synthetic acquisition)",
    "planned_evaluation_cost": "synthetic evaluation units; not instrument spend",
    "completed_attempt_evaluation_cost": "synthetic evaluation units; not instrument spend",
    "declared_solvent_volume_ml": "mL (declared synthetic method volume)",
    "optimizer_walltime_seconds": "s (software wall time; not CPU time)",
    "ln_k0": "1 (dimensionless model parameter)",
    "S1": "1 (dimensionless model parameter)",
    "assumed_fwhm_min": "min (synthetic width assumption)",
    "prediction_retention_time_min": "min (stored ideal-retention prediction before Gaussian simulation)",
    "prediction_dead_time_min": "min (stored ideal-retention prediction)",
    "prediction_post_column_transit_min": "min (stored ideal-retention prediction)",
    "prediction_retention_zeta_min": "min (stored ideal-retention coordinate)",
    "prediction_right_censor_bound_min": "min (stored prediction censor bound)",
    "prediction_numerical_error_estimate_min": "min (stored solver error estimate)",
    "prediction_evaluation_count": "count (retention solver evaluations)",
    "gaussian_centroid_retention_time_min": "min (stored Gaussian-producer centroid)",
    "gaussian_resolution": "1 (dimensionless Gaussian resolution)",
    "assessment_retention_time_min": "min (retention evidence copied from assessment)",
    "assessment_resolution": "1 (dimensionless resolution copied from assessment)",
    "historical_utility_input_retention_time_min": "min (copied from consumed raw metrics)",
    "historical_utility_input_peak_resolution": "1 (dimensionless; copied from consumed raw metrics)",
}


class ExportError(ValueError):
    """Raised when the input bundle cannot be exported without ambiguity."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is not permitted")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key {key!r}")
        output[key] = value
    return output


def _check_finite(value: Any, context: str = "JSON value") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{context} contains a non-finite number")
    if isinstance(value, dict):
        for child in value.values():
            _check_finite(child, context)
    elif isinstance(value, list):
        for child in value:
            _check_finite(child, context)


def _load_json(data: bytes, context: str) -> Any:
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _check_finite(value, context)
        return value
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ExportError(f"{context} is not valid bounded UTF-8 JSON: {error}") from error


def _safe_basename(name: Any) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise ExportError(f"sealed file name is not a basename: {name!r}")
    return name


def _read_regular_file(path: Path, *, context: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise ExportError(f"{context} is unavailable: {error}") from error
    if not stat.S_ISREG(before.st_mode):
        raise ExportError(f"{context} must be a regular non-symlink file")
    if before.st_size > MAX_CORE_FILE_BYTES:
        raise ExportError(f"{context} exceeds the {MAX_CORE_FILE_BYTES}-byte limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ExportError(f"{context} could not be opened safely: {error}") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise ExportError(f"{context} changed while it was being opened")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            data = stream.read(MAX_CORE_FILE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > MAX_CORE_FILE_BYTES:
        raise ExportError(f"{context} exceeds the {MAX_CORE_FILE_BYTES}-byte limit")
    return data


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_bundle(path: str | Path) -> Path:
    raw = Path(path).expanduser()
    try:
        info = raw.lstat()
    except OSError as error:
        raise ExportError(f"input bundle directory is unavailable: {error}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise ExportError("input bundle must be a real directory, not a symlink")
    try:
        return raw.resolve(strict=True)
    except OSError as error:
        raise ExportError(f"input bundle directory cannot be resolved: {error}") from error


def _read_jsonl(data: bytes, context: str) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ExportError(f"{context} is not UTF-8") from error
    rows = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise ExportError(f"{context} contains a blank line at {line_number}")
        value = _load_json(line.encode("utf-8"), f"{context} line {line_number}")
        if not isinstance(value, dict):
            raise ExportError(f"{context} line {line_number} must be a JSON object")
        rows.append(value)
    return rows


def _verify_sealed_core(bundle_dir: str | Path) -> dict[str, Any]:
    bundle = _resolve_bundle(bundle_dir)
    complete_bytes = _read_regular_file(bundle / "complete.json", context="complete.json")
    complete = _load_json(complete_bytes, "complete.json")
    if not isinstance(complete, dict) or complete.get("schema_version") != 1:
        raise ExportError("complete.json has an unsupported seal schema")
    manifest = complete.get("files")
    if not isinstance(manifest, dict) or not manifest:
        raise ExportError("complete.json does not contain a file manifest")
    manifest_names = [_safe_basename(name) for name in manifest]
    if set(manifest_names) != EXPECTED_CORE_FILES:
        unknown = sorted(set(manifest_names) - EXPECTED_CORE_FILES)
        missing = sorted(EXPECTED_CORE_FILES - set(manifest_names))
        raise ExportError(
            f"sealed core manifest filenames differ from the fixed profile; "
            f"unknown={unknown}, missing={missing}"
        )
    verified_files: dict[str, bytes] = {}
    source_manifest = {}
    for raw_name, entry in sorted(manifest.items()):
        name = _safe_basename(raw_name)
        if name == "complete.json":
            raise ExportError("complete.json cannot seal itself")
        if not isinstance(entry, dict) or set(entry) != {"sha256", "size_bytes"}:
            raise ExportError(f"seal entry for {name!r} has an unsupported shape")
        expected_hash = entry.get("sha256")
        expected_size = entry.get("size_bytes")
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(char not in "0123456789abcdef" for char in expected_hash)
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise ExportError(f"seal entry for {name!r} has invalid hash or size values")
        data = _read_regular_file(bundle / name, context=f"sealed core file {name}")
        actual_hash = _sha256(data)
        if len(data) != expected_size or actual_hash != expected_hash:
            raise ExportError(f"sealed core file {name!r} does not match its SHA256 and size")
        verified_files[name] = data
        source_manifest[name] = {"sha256": actual_hash, "size_bytes": len(data)}

    required = {"protocol.json", "report.json", "evaluations.jsonl", "campaigns.jsonl"}
    if not required <= set(verified_files):
        missing = sorted(required - set(verified_files))
        raise ExportError(f"sealed core lacks required evidence files: {missing}")
    report = _load_json(verified_files["report.json"], "report.json")
    protocol = _load_json(verified_files["protocol.json"], "protocol.json")
    if not isinstance(report, dict) or report.get("profile") != SOURCE_PROFILE:
        raise ExportError(f"report.json profile must be {SOURCE_PROFILE!r}")
    if not isinstance(protocol, dict) or protocol.get("profile") != SOURCE_PROFILE:
        raise ExportError(f"protocol.json profile must be {SOURCE_PROFILE!r}")
    evidence_files = report.get("evidence_files")
    if not isinstance(evidence_files, list) or any(
        not isinstance(name, str) for name in evidence_files
    ):
        raise ExportError("report.json does not declare its sealed evidence file list")
    safe_evidence_files = [_safe_basename(name) for name in evidence_files]
    if len(set(safe_evidence_files)) != len(safe_evidence_files):
        raise ExportError("report.json contains duplicate sealed evidence file names")
    if set(safe_evidence_files) != set(manifest):
        raise ExportError("report.json evidence file list differs from complete.json")
    if not {"preflight.json", "report.md", "measure.sqlite3"} <= set(verified_files):
        raise ExportError("sealed ablation core is missing a required audit artifact")
    return {
        "bundle_dir": bundle,
        "complete_sha256": _sha256(complete_bytes),
        "complete": complete,
        "manifest": source_manifest,
        "files": verified_files,
        "report": report,
        "protocol": protocol,
        "evaluations": _read_jsonl(verified_files["evaluations.jsonl"], "evaluations.jsonl"),
        "campaigns": _read_jsonl(verified_files["campaigns.jsonl"], "campaigns.jsonl"),
    }


def _load_frozen_declaration() -> tuple[dict[str, Any], str]:
    repository_root = Path(__file__).resolve().parents[1]
    declaration_path = repository_root / DECLARATION_RELATIVE_PATH
    data = _read_regular_file(declaration_path, context="frozen study declaration")
    digest = _sha256(data)
    if digest != EXPECTED_STUDY_SHA256:
        raise ExportError("bundled frozen study declaration failed its SHA256 check")
    declaration = _load_json(data, "frozen study declaration")
    if not isinstance(declaration, dict):
        raise ExportError("frozen study declaration must be an object")
    return declaration, digest


def _planned_rows(
    core: dict[str, Any], declaration: dict[str, Any], digest: str
) -> tuple[list[dict[str, Any]], dict[tuple[str, str, int], dict[str, Any]]]:
    protocol = core["protocol"]
    report = core["report"]
    if (
        protocol.get("study_sha256") != digest
        or report.get("study_input", {}).get("sha256") != digest
        or report.get("implementation_provenance", {}).get("study_input_sha256") != digest
        or protocol.get("study_declaration") != declaration
    ):
        raise ExportError("sealed bundle does not contain the exact frozen study declaration")
    if declaration.get("profile") != SOURCE_PROFILE:
        raise ExportError("frozen declaration profile differs from the sealed ablation profile")
    declared_arms = declaration.get("arms")
    declared_tasks = declaration.get("tasks")
    seeds = declaration.get("seeds")
    budget = declaration.get("evaluation_budget")
    if (
        not isinstance(declared_arms, list)
        or not isinstance(declared_tasks, list)
        or not isinstance(seeds, list)
        or isinstance(budget, bool)
        or not isinstance(budget, int)
    ):
        raise ExportError("frozen declaration is missing the fixed arms, tasks, seeds or budget")
    if (
        len(declared_arms) != 5
        or len(declared_tasks) != 4
        or seeds != [17, 29, 43]
        or budget != 10
        or any(
            not isinstance(task.get("analytes"), list) or len(task["analytes"]) != 3
            for task in declared_tasks
        )
    ):
        raise ExportError(
            "frozen declaration does not describe the expected five-arm 600-slot study"
        )
    declared_arm_ids = [arm.get("arm_id") for arm in declared_arms]
    declared_task_ids = [task.get("task_id") for task in declared_tasks]
    if (
        any(
            not isinstance(value, str) or not value
            for value in declared_arm_ids + declared_task_ids
        )
        or len(set(declared_arm_ids)) != len(declared_arm_ids)
        or len(set(declared_task_ids)) != len(declared_task_ids)
    ):
        raise ExportError("frozen declaration contains duplicate or invalid arm/task identities")
    protocol_arm_rows = protocol.get("arms")
    if not isinstance(protocol_arm_rows, list):
        raise ExportError("protocol.json is missing registered arm records")
    protocol_arms = [row.get("arm") for row in protocol_arm_rows if isinstance(row, dict)]
    if protocol_arms != declared_arms:
        raise ExportError("registered protocol arms differ from the frozen declaration")
    denominators = report.get("planned_denominators")
    expected_campaigns = len(declared_arms) * len(declared_tasks) * len(seeds)
    expected_evaluations = expected_campaigns * budget
    if (
        not isinstance(denominators, dict)
        or denominators.get("campaigns_total") != expected_campaigns
        or denominators.get("evaluations_total") != expected_evaluations
        or expected_campaigns != 60
        or expected_evaluations != 600
    ):
        raise ExportError("report planned denominators do not match the frozen 60/600 study")

    arms = {arm["arm_id"]: arm for arm in declared_arms}
    tasks = {task["task_id"]: task for task in declared_tasks}
    expected_evaluations_set = {
        (arm_id, task_id, seed, ordinal)
        for arm_id in arms
        for task_id in tasks
        for seed in seeds
        for ordinal in range(budget)
    }
    evaluation_rows = core["evaluations"]
    seen_evaluations = set()
    for row in evaluation_rows:
        identity = (row.get("arm_id"), row.get("task_id"), row.get("seed"), row.get("ordinal"))
        if (
            not isinstance(identity[0], str)
            or not isinstance(identity[1], str)
            or isinstance(identity[2], bool)
            or not isinstance(identity[2], int)
            or isinstance(identity[3], bool)
            or not isinstance(identity[3], int)
        ):
            raise ExportError("evaluations.jsonl contains an invalid planned-slot identity")
        if identity not in expected_evaluations_set or identity in seen_evaluations:
            raise ExportError("evaluations.jsonl contains an unplanned or duplicate study slot")
        seen_evaluations.add(identity)
        arm_id, task_id, seed, ordinal = identity
        arm, task = arms[arm_id], tasks[task_id]
        if (
            row.get("cohort_role") != task.get("cohort_role")
            or row.get("policy") != arm.get("policy")
            or row.get("optimizer_target_config") != arm.get("optimizer_target")
            or row.get("surrogate_noise_prior") != arm.get("surrogate_noise_prior")
            or row.get("budget") != budget
            or not isinstance(row.get("status"), str)
            or not row["status"]
        ):
            raise ExportError("evaluation row factors or status differ from the frozen plan")
    if seen_evaluations != expected_evaluations_set:
        raise ExportError("evaluations.jsonl does not retain all 600 planned slots")

    expected_campaigns_set = {
        (arm_id, task_id, seed) for arm_id in arms for task_id in tasks for seed in seeds
    }
    campaign_records: dict[tuple[str, str, int], dict[str, Any]] = {}
    for campaign in core["campaigns"]:
        identity = (campaign.get("arm_id"), campaign.get("task_id"), campaign.get("seed"))
        if (
            not isinstance(identity[0], str)
            or not isinstance(identity[1], str)
            or isinstance(identity[2], bool)
            or not isinstance(identity[2], int)
        ):
            raise ExportError("campaigns.jsonl contains an invalid planned-campaign identity")
        if identity not in expected_campaigns_set or identity in campaign_records:
            raise ExportError("campaigns.jsonl contains an unplanned or duplicate campaign")
        arm_id, task_id, _ = identity
        arm, task = arms[arm_id], tasks[task_id]
        if (
            campaign.get("cohort_role") != task.get("cohort_role")
            or campaign.get("policy") != arm.get("policy")
            or campaign.get("optimizer_target_config") != arm.get("optimizer_target")
            or campaign.get("surrogate_noise_prior") != arm.get("surrogate_noise_prior")
            or campaign.get("evaluation_budget") != budget
        ):
            raise ExportError("campaign row factors differ from the frozen plan")
        campaign_records[identity] = campaign
    if set(campaign_records) != expected_campaigns_set:
        raise ExportError("campaigns.jsonl does not retain all 60 planned campaigns")
    for row in evaluation_rows:
        campaign = campaign_records[(row["arm_id"], row["task_id"], row["seed"])]
        if row.get("campaign_id") != campaign.get("campaign_id"):
            raise ExportError("campaign_id differs between the campaign and evaluation evidence")
    return evaluation_rows, campaign_records


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _attempt_status(evaluation: dict[str, Any]) -> str:
    attempt_spec = _dict(_dict(evaluation.get("attempt")).get("spec"))
    status = attempt_spec.get("status")
    return status if isinstance(status, str) else "unavailable"


def _utility_result(evaluation: dict[str, Any]) -> dict[str, Any]:
    consumption_result = _dict(_dict(evaluation.get("consumption")).get("result"))
    return consumption_result


def _assessment(evaluation: dict[str, Any]) -> dict[str, Any]:
    observation = _dict(_dict(evaluation.get("observation")).get("observation"))
    return _dict(observation.get("assessment"))


def _optimizer_target(evaluation: dict[str, Any]) -> dict[str, Any]:
    target = evaluation.get("optimizer_target_record")
    if isinstance(target, dict):
        return target
    return _dict(_utility_result(evaluation).get("optimizer_target"))


def _runtime_walltime(evaluation: dict[str, Any], metrics: dict[str, Any]) -> Any:
    value = metrics.get("optimizer_walltime_seconds")
    if value is not None:
        return value
    backend = _dict(evaluation.get("backend"))
    return _dict(backend.get("runtime")).get("elapsed_seconds")


def _solvent_names(evaluations: Iterable[dict[str, Any]]) -> list[str]:
    names = set()
    for evaluation in evaluations:
        metrics = _dict(evaluation.get("metrics"))
        solvent_use = metrics.get("declared_solvent_use_ml")
        if not isinstance(solvent_use, dict):
            method = _dict(evaluation.get("method"))
            program = _dict(method.get("canonical_program"))
            solvent_use = program.get("total_solvent_use_ml")
        if isinstance(solvent_use, dict):
            for name, value in solvent_use.items():
                if not isinstance(name, str) or not name:
                    raise ExportError("solvent resource map has a non-string solvent identity")
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    raise ExportError(f"solvent resource {name!r} is not a finite scalar")
                names.add(name)
    return sorted(names)


def _solvent_column(name: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "_" for char in name)
    slug = "_".join(part for part in slug.split("_") if part)
    if not slug:
        raise ExportError(f"solvent identity {name!r} cannot name a CSV column")
    return f"declared_solvent_{slug}_ml"


def _evaluation_projection(
    row: dict[str, Any],
    campaign: dict[str, Any],
    task: dict[str, Any],
    declaration: dict[str, Any],
    solvent_names: list[str],
) -> dict[str, Any]:
    metrics = _dict(row.get("metrics"))
    failure = _dict(row.get("failure"))
    candidate = _dict(row.get("candidate"))
    method = _dict(row.get("method"))
    result = _utility_result(row)
    utility = _dict(result.get("utility"))
    assessment = _assessment(row)
    target = _optimizer_target(row)
    assessment_acceptance = assessment.get("analytical_acceptance")
    consumed_acceptance = result.get("analytical_acceptance")
    if isinstance(consumed_acceptance, str):
        acceptance, acceptance_status = consumed_acceptance, "consumed_result"
    elif isinstance(assessment_acceptance, str):
        acceptance, acceptance_status = assessment_acceptance, "assessment_evidence"
    else:
        acceptance, acceptance_status = None, "unavailable"
    utility_status = utility.get("status")
    if not isinstance(utility_status, str):
        utility_status = "unavailable"
    attempt = _dict(row.get("attempt"))
    experiment = _dict(row.get("experiment"))
    prediction = _dict(row.get("retention_prediction"))
    gaussian = _dict(row.get("gaussian_observation"))
    observation_response = _dict(row.get("observation"))
    snapshot = _dict(row.get("snapshot"))
    consumption = _dict(row.get("consumption"))
    solvent_use = metrics.get("declared_solvent_use_ml")
    if not isinstance(solvent_use, dict):
        solvent_use = {}
    record = {
        "task_id": row["task_id"],
        "cohort_role": row["cohort_role"],
        "seed": row["seed"],
        "arm_id": row["arm_id"],
        "policy": row["policy"],
        "optimizer_target_config": row["optimizer_target_config"],
        "surrogate_noise_prior": row["surrogate_noise_prior"],
        "ordinal": row["ordinal"],
        "budget": row["budget"],
        "evaluation_status": row["status"],
        "campaign_status": campaign.get("status"),
        "campaign_id": row.get("campaign_id"),
        "attempt_status": _attempt_status(row),
        "completed_simulator_attempt": metrics.get("completed_simulator_attempt"),
        "consumed_evaluation": metrics.get("consumed_evaluation"),
        "failure_step": row.get("failure_step"),
        "failure_type": failure.get("type"),
        "failure_message": failure.get("message"),
        "proposal_source": row.get("proposal_source"),
        "strong_solvent_fraction": candidate.get("strong_solvent_fraction"),
        "proposed_acquisition_duration_min": candidate.get("acquisition_duration_min"),
        "flow_ml_min": declaration.get("flow_ml_min"),
        "method_id": method.get("method_id"),
        "method_input_sha256": method.get("input_sha256"),
        "canonical_program_sha256": method.get("canonical_program_sha256"),
        "experiment_id": experiment.get("experiment_id"),
        "attempt_id": attempt.get("attempt_id"),
        "attempt_revision": attempt.get("revision"),
        "retention_prediction_id": prediction.get("prediction_id"),
        "retention_prediction_input_sha256": prediction.get("input_sha256"),
        "retention_prediction_output_sha256": prediction.get("output_sha256"),
        "gaussian_run_id": gaussian.get("run_id"),
        "gaussian_run_input_sha256": gaussian.get("input_sha256"),
        "gaussian_run_output_sha256": gaussian.get("output_sha256"),
        "observation_id": observation_response.get("observation_id"),
        "observation_input_sha256": observation_response.get("input_sha256"),
        "evidence_sha256": observation_response.get("evidence_sha256"),
        "assessment_sha256": observation_response.get("assessment_sha256"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_sha256": snapshot.get("sha256"),
        "consumption_id": consumption.get("consumption_id"),
        "consumption_input_sha256": consumption.get("input_sha256"),
        "consumption_output_sha256": consumption.get("output_sha256"),
        "analytical_acceptance": acceptance,
        "analytical_acceptance_status": acceptance_status,
        "historical_utility_status": utility_status,
        "historical_utility_value": utility.get("score"),
        "utility_ge_2": metrics.get("score_ge_2_success"),
        "optimizer_reward_name": target.get("name"),
        "optimizer_reward_profile": target.get("profile"),
        "optimizer_reward_direction": target.get("direction"),
        "optimizer_reward_status": target.get("status", "unavailable"),
        "optimizer_reward_value": target.get("value"),
        "detected_count": metrics.get("detected_count"),
        "unknown_count": metrics.get("unknown_count"),
        "confirmed_absent_count": metrics.get("confirmed_absent_count"),
        "simulated_acquisition_time_min": metrics.get("simulated_acquisition_time_min"),
        "runtime_preference_satisfied": metrics.get("runtime_preference_satisfied"),
        "feasible": metrics.get("feasible"),
        "planned_evaluation_cost": declaration.get("evaluation_cost"),
        "planned_evaluation_cost_unit": declaration.get("evaluation_cost_unit"),
        "completed_attempt_evaluation_cost": metrics.get("declared_evaluation_cost"),
        "completed_attempt_cost_unit": metrics.get("declared_evaluation_cost_unit"),
        "declared_solvent_volume_ml": metrics.get("declared_solvent_volume_ml"),
        "optimizer_walltime_seconds": _runtime_walltime(row, metrics),
    }
    for name in solvent_names:
        record[_solvent_column(name)] = solvent_use.get(name)
    return record


def _analyte_projection(
    row: dict[str, Any], task: dict[str, Any], declaration: dict[str, Any]
) -> list[dict[str, Any]]:
    prediction = _dict(row.get("retention_prediction"))
    prediction_payload = _dict(prediction.get("prediction"))
    prediction_rows = prediction_payload.get("analytes")
    if not isinstance(prediction_rows, list):
        prediction_rows = []
    prediction_by_id = _index_analytes(prediction_rows, task)

    gaussian = _dict(row.get("gaussian_observation"))
    gaussian_observation = _dict(gaussian.get("observation"))
    simulated_rows = gaussian_observation.get("analytes")
    if not isinstance(simulated_rows, list):
        simulated_rows = []
    simulated_by_id = _index_analytes(simulated_rows, task)
    assessment = _assessment(row)
    assessment_rows = assessment.get("per_analyte")
    if not isinstance(assessment_rows, list):
        assessment_rows = []
    assessment_by_id = _index_analytes(assessment_rows, task)
    result = _utility_result(row)
    raw_metrics = _dict(result.get("raw_metrics"))
    raw_ids = raw_metrics.get("analyte_ids")
    raw_by_id: dict[str, int] = {}
    if isinstance(raw_ids, list):
        if (
            any(not isinstance(value, str) for value in raw_ids)
            or len(raw_ids) != len(set(raw_ids))
            or set(raw_ids) != {item["analyte_id"] for item in task["analytes"]}
        ):
            raise ExportError("consumed raw metrics do not align with the frozen analyte IDs")
        raw_by_id = {analyte_id: index for index, analyte_id in enumerate(raw_ids)}
    raw_fields = {
        "historical_utility_input_retention_status": raw_metrics.get("retention_statuses"),
        "historical_utility_input_retention_time_min": raw_metrics.get("retention_times_min"),
        "historical_utility_input_resolution_status": raw_metrics.get("resolution_statuses"),
        "historical_utility_input_peak_resolution": raw_metrics.get("peak_resolutions"),
    }
    for name, values in raw_fields.items():
        if values is not None and (not isinstance(values, list) or len(values) != len(raw_by_id)):
            raise ExportError(f"consumed raw metric {name!r} does not align with its analyte IDs")

    evaluation_status = row.get("status", "unknown")
    output = []
    for analyte in task["analytes"]:
        analyte_id = analyte["analyte_id"]
        predicted = prediction_by_id.get(analyte_id, {})
        prediction_result = _dict(predicted.get("result"))
        simulated = simulated_by_id.get(analyte_id, {})
        assessed = assessment_by_id.get(analyte_id, {})
        raw_index = raw_by_id.get(analyte_id)
        raw_values = {
            name: None if raw_index is None or values is None else values[raw_index]
            for name, values in raw_fields.items()
        }
        detection = assessed.get("detection")
        detection_status = (
            "unavailable" if not assessed else "unknown" if detection is None else "observed"
        )
        prediction_evidence_status = (
            "available"
            if isinstance(predicted.get("result"), dict)
            else "incomplete"
            if predicted
            else "unavailable"
        )
        output.append(
            {
                "task_id": row["task_id"],
                "cohort_role": row["cohort_role"],
                "seed": row["seed"],
                "arm_id": row["arm_id"],
                "policy": row["policy"],
                "optimizer_target_config": row["optimizer_target_config"],
                "surrogate_noise_prior": row["surrogate_noise_prior"],
                "ordinal": row["ordinal"],
                "evaluation_status": evaluation_status,
                "campaign_id": row.get("campaign_id"),
                "analyte_id": analyte_id,
                "retention_model": analyte.get("model"),
                "ln_k0": analyte.get("ln_k0"),
                "S1": analyte.get("S1"),
                "assumed_fwhm_min": task.get("fwhm_min"),
                "prediction_evidence_status": prediction_evidence_status,
                "prediction_profile": prediction_result.get("profile"),
                "prediction_retention_status": prediction_result.get("status"),
                "prediction_retention_time_min": prediction_result.get("retention_time_min"),
                "prediction_dead_time_min": prediction_result.get("dead_time_min"),
                "prediction_post_column_transit_min": prediction_result.get(
                    "post_column_transit_min"
                ),
                "prediction_retention_zeta_min": prediction_result.get("retention_zeta_min"),
                "prediction_right_censor_bound_min": prediction_result.get(
                    "right_censor_bound_min"
                ),
                "prediction_numerical_error_estimate_min": prediction_result.get(
                    "numerical_error_estimate_min"
                ),
                "prediction_evaluation_count": prediction_result.get("evaluation_count"),
                "gaussian_centroid_evidence_status": "available" if simulated else "unavailable",
                "gaussian_retention_status": simulated.get("retention_status"),
                "gaussian_centroid_retention_time_min": simulated.get("retention_time_min"),
                "gaussian_resolution_status": simulated.get("resolution_status"),
                "gaussian_resolution": simulated.get("resolution"),
                "assessment_status": "available" if assessed else "unavailable",
                "assessment_retention_status": assessed.get("retention_status"),
                "assessment_retention_time_min": assessed.get("retention_time_min"),
                "detection": detection,
                "detection_status": detection_status,
                "assessment_resolution_status": assessed.get("resolution_status"),
                "assessment_resolution": assessed.get("resolution"),
                "resolution_satisfied": assessed.get("resolution_satisfied"),
                "analyte_acceptance_state": assessed.get("acceptance_state"),
                **raw_values,
                "historical_utility_input_status": (
                    "available" if raw_index is not None else "unavailable"
                ),
            }
        )
    return output


def _index_analytes(rows: list[Any], task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    expected = {item["analyte_id"] for item in task["analytes"]}
    output = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ExportError("per-analyte evidence contains a non-object row")
        analyte_id = row.get("analyte_id")
        if not isinstance(analyte_id, str) or analyte_id not in expected or analyte_id in output:
            raise ExportError("per-analyte evidence has an unplanned or duplicate analyte ID")
        output[analyte_id] = row
    return output


def _source_provenance(core: dict[str, Any]) -> dict[str, Any]:
    protocol = core["protocol"]
    report = core["report"]
    provenance = report.get("implementation_provenance", {})
    preflight_document = _load_json(core["files"]["preflight.json"], "preflight.json")
    if not isinstance(preflight_document, dict) or not isinstance(
        preflight_document.get("attempts"), list
    ):
        raise ExportError("preflight.json does not contain an attempt history")
    preflight_attempts = []
    backend_sources = []
    seen_backend_sources = set()

    def add_backend(source: Any) -> None:
        if not isinstance(source, dict):
            return
        settings = source.get("settings")
        if isinstance(settings, dict):
            static_settings = dict(settings)
            static_settings.pop("iteration", None)
        else:
            static_settings = settings
        item = {
            key: source.get(key)
            for key in (
                "backend_id",
                "adapter_profile",
                "backend_name",
                "backend_class",
                "generation_method",
                "model_type",
                "acquisition_method",
                "acquisition_implementation",
                "source_revision",
                "source_tree_sha256",
                "adapter_source_sha256",
                "campaign_protocol_source_sha256",
                "design_kernel_source_sha256",
                "distribution_versions",
            )
        }
        item["settings"] = static_settings
        fingerprint = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if fingerprint not in seen_backend_sources:
            seen_backend_sources.add(fingerprint)
            backend_sources.append(item)

    for attempt in preflight_document["attempts"]:
        if not isinstance(attempt, dict):
            raise ExportError("preflight attempt is malformed")
        backend_report = _dict(attempt.get("backend_report"))
        cases = backend_report.get("cases", [])
        if isinstance(cases, list):
            for case in cases:
                add_backend(_dict(case).get("backend_provenance"))
        preflight_attempts.append(
            {
                "attempt": attempt.get("attempt"),
                "qualified": attempt.get("qualified"),
                "failure": attempt.get("failure"),
                "runner_elapsed_seconds": attempt.get("runner_elapsed_seconds"),
                "preflight_elapsed_seconds": backend_report.get("preflight_elapsed_seconds"),
                "report_profile": backend_report.get("profile"),
            }
        )

    observed_implementation_provenance = []
    seen_provenance = set()
    for evaluation in core["evaluations"]:
        add_backend(evaluation.get("backend"))
        _collect_implementation_provenance(
            evaluation, observed_implementation_provenance, seen_provenance
        )

    arm_protocol_provenance = []
    for arm_row in protocol.get("arms", []):
        registered = _dict(arm_row).get("registered_protocol")
        arm_protocol_provenance.append(
            {
                "arm_id": _dict(arm_row).get("arm", {}).get("arm_id"),
                "implementation_provenance": _dict(registered).get("implementation_provenance"),
            }
        )
    return {
        "report_implementation_provenance": provenance,
        "registered_task_inputs": protocol.get("registered_task_inputs", []),
        "arm_protocols": arm_protocol_provenance,
        "preflight_attempts": preflight_attempts,
        "optimizer_backend_sources": backend_sources,
        "observed_implementation_provenance": observed_implementation_provenance,
        "source_revision_policy": (
            "Source revision fields are copied as recorded, including null. The exporter does not infer a Git revision."
        ),
    }


def _collect_implementation_provenance(
    value: Any, output: list[dict[str, Any]], seen: set[str]
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "implementation_provenance" and isinstance(child, dict):
                fingerprint = json.dumps(
                    child, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    output.append(child)
            _collect_implementation_provenance(child, output, seen)
    elif isinstance(value, list):
        for child in value:
            _collect_implementation_provenance(child, output, seen)


def _csv_scalar(value: Any, field: str) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        raise ExportError(f"CSV field {field!r} would pack a structured value")
    if isinstance(value, float) and not math.isfinite(value):
        raise ExportError(f"CSV field {field!r} contains a non-finite number")
    return value


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=fields, extrasaction="raise", lineterminator="\n"
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({field: _csv_scalar(row.get(field), field) for field in fields})
    except (OSError, csv.Error) as error:
        raise ExportError(f"could not write {path.name}: {error}") from error


def _write_json(path: Path, value: Any) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
    except (OSError, TypeError, ValueError) as error:
        raise ExportError(f"could not write {path.name}: {error}") from error


def _hash_output(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"sha256": _sha256(data), "size_bytes": len(data)}


def _output_destination(output_dir: str | Path, bundle: Path) -> tuple[Path, Path]:
    raw = Path(output_dir).expanduser()
    if not raw.name or raw.name in {".", ".."}:
        raise ExportError("--output-dir must name a new directory")
    try:
        parent = raw.parent.resolve(strict=True)
    except OSError as error:
        raise ExportError(f"output parent directory must already exist: {error}") from error
    if not parent.is_dir():
        raise ExportError("output parent must be a directory")
    destination = parent / raw.name
    if os.path.lexists(destination):
        raise ExportError("output directory already exists; refusing to overwrite it")
    try:
        destination.relative_to(bundle)
    except ValueError:
        pass
    else:
        raise ExportError("output directory must be outside the sealed core bundle")
    if destination == bundle:
        raise ExportError("output directory collides with the sealed core bundle")
    return parent, destination


def export_reference(bundle_dir: str | Path, *, output_dir: str | Path) -> dict[str, Any]:
    """Verify a sealed ablation core and write its tidy public reference tables."""
    core = _verify_sealed_core(bundle_dir)
    declaration, study_digest = _load_frozen_declaration()
    evaluations, campaign_records = _planned_rows(core, declaration, study_digest)
    bundle = core["bundle_dir"]
    parent, destination = _output_destination(output_dir, bundle)
    tasks = {task["task_id"]: task for task in declaration["tasks"]}
    solvent_names = _solvent_names(evaluations)
    solvent_columns = [_solvent_column(name) for name in solvent_names]
    if len(set(solvent_columns)) != len(solvent_columns):
        raise ExportError("distinct solvent identities collide as CSV column names")
    evaluation_fields = [*EVALUATION_BASE_FIELDS, *solvent_columns]
    evaluation_rows = [
        _evaluation_projection(
            row,
            campaign_records[(row["arm_id"], row["task_id"], row["seed"])],
            tasks[row["task_id"]],
            declaration,
            solvent_names,
        )
        for row in sorted(
            evaluations,
            key=lambda item: (item["task_id"], item["seed"], item["arm_id"], item["ordinal"]),
        )
    ]
    analyte_rows = []
    for row in sorted(
        evaluations,
        key=lambda item: (item["task_id"], item["seed"], item["arm_id"], item["ordinal"]),
    ):
        analyte_rows.extend(_analyte_projection(row, tasks[row["task_id"]], declaration))
    if len(evaluation_rows) != 600 or len(analyte_rows) != 1800:
        raise ExportError(
            "public table projection did not retain 600 evaluations and 1800 analytes"
        )

    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.export-", dir=parent))
    created_files: list[Path] = []
    destination_created = False
    published = False
    try:
        _write_csv(stage / EVALUATION_FILE, evaluation_fields, evaluation_rows)
        _write_csv(stage / ANALYTE_FILE, list(ANALYTE_FIELDS), analyte_rows)
        table_hashes = {
            EVALUATION_FILE: _hash_output(stage / EVALUATION_FILE),
            ANALYTE_FILE: _hash_output(stage / ANALYTE_FILE),
        }
        metadata = {
            "schema_version": 1,
            "profile": EXPORT_PROFILE,
            "source_profile": SOURCE_PROFILE,
            "tables": {
                EVALUATION_FILE: {
                    "grain": "one row per planned (task_id, seed, arm_id, ordinal) slot",
                    "planned_rows": 600,
                    "exported_rows": len(evaluation_rows),
                    "columns": evaluation_fields,
                    "units": {
                        **{key: UNIT_MAP[key] for key in evaluation_fields if key in UNIT_MAP},
                        **{
                            column: "mL (declared method solvent use)" for column in solvent_columns
                        },
                    },
                    "sha256": table_hashes[EVALUATION_FILE]["sha256"],
                    "size_bytes": table_hashes[EVALUATION_FILE]["size_bytes"],
                },
                ANALYTE_FILE: {
                    "grain": "one row per planned evaluation and declared analyte",
                    "key": ["task_id", "seed", "arm_id", "ordinal", "analyte_id"],
                    "planned_rows": 1800,
                    "exported_rows": len(analyte_rows),
                    "columns": list(ANALYTE_FIELDS),
                    "units": {key: UNIT_MAP[key] for key in ANALYTE_FIELDS if key in UNIT_MAP},
                    "sha256": table_hashes[ANALYTE_FILE]["sha256"],
                    "size_bytes": table_hashes[ANALYTE_FILE]["size_bytes"],
                },
            },
            "study": {
                "sha256": study_digest,
                "declaration_source": DECLARATION_RELATIVE_PATH,
                "declaration": declaration,
                "planned_campaign_count": 60,
                "planned_evaluation_count": 600,
                "planned_analyte_row_count": 1800,
                "factors": declaration["factors"],
                "arms": declaration["arms"],
                "tasks": declaration["tasks"],
                "seeds": declaration["seeds"],
                "evaluation_budget": declaration["evaluation_budget"],
            },
            "source_bundle": {
                "profile": SOURCE_PROFILE,
                "complete_json_sha256": core["complete_sha256"],
                "verified_files": core["manifest"],
                "report_status": core["report"].get("status"),
                "completed": core["report"].get("completed"),
                "planned_denominators": core["report"].get("planned_denominators"),
                "study_input": core["report"].get("study_input"),
            },
            "provenance": _source_provenance(core),
            "interpretation": {
                "missing_values": "Blank cells mean evidence is unavailable or was not recorded; they are not zero.",
                "failed_and_future_slots": "The evaluation status and failure fields remain explicit. No outcome or optimizer reward is fabricated.",
                "unconsumed_completed_attempts": "Available attempt, method, simulator and evidence fields are copied while the evaluation status remains unchanged.",
                "analyte_projection": "prediction_* fields are copied from retention_prediction.prediction.analytes[*].result; gaussian_* fields are copied only from gaussian_observation.observation.analytes; assessment_* and detection fields are copied only from the stored analytical assessment; historical_utility_input_* fields are copied from consumed raw metrics.",
                "stage_boundary": "A stored retention prediction remains available when later Gaussian production fails. Prediction-stage values never fill Gaussian-centroid fields, and neither stage alone creates analytical assessment, acceptance, historical utility or optimizer reward.",
                "utility_and_reward": "Historical utility, analytical acceptance and the selected optimizer reward are separate columns.",
                "scope": "Synthetic latent chromatographic outcomes; no physical-instrument or manuscript-protocol qualification is implied.",
            },
            "execution_requirement": (
                "This standard-library-only exporter also requires the frozen study declaration "
                f"from the repository or source archive at {DECLARATION_RELATIVE_PATH}."
            ),
        }
        _write_json(stage / METADATA_FILE, metadata)
        output_files = [EVALUATION_FILE, ANALYTE_FILE, METADATA_FILE]
        export_seal = {
            "schema_version": 1,
            "profile": EXPORT_PROFILE,
            "files": {name: _hash_output(stage / name) for name in output_files},
        }
        _write_json(stage / EXPORT_SEAL_FILE, export_seal)

        destination.mkdir(mode=0o755)
        destination_created = True
        for name in (EVALUATION_FILE, ANALYTE_FILE, METADATA_FILE, EXPORT_SEAL_FILE):
            target = destination / name
            with target.open("xb") as output:
                created_files.append(target)
                with (stage / name).open("rb") as source:
                    shutil.copyfileobj(source, output)
        published = True
        return metadata
    except FileExistsError as error:
        raise ExportError(
            "output directory or file appeared during export; refusing overwrite"
        ) from error
    except OSError as error:
        raise ExportError(f"could not publish export directory: {error}") from error
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if destination_created and not published:
            for path in created_files:
                try:
                    path.unlink()
                except OSError:
                    pass
            try:
                destination.rmdir()
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bundle_dir", help="sealed synthetic-isocratic-ablation-study/1 core bundle"
    )
    parser.add_argument("--output-dir", required=True, help="new output directory (must not exist)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        metadata = export_reference(args.bundle_dir, output_dir=args.output_dir)
    except ExportError as error:
        parser.error(str(error))
    print(
        f"Wrote {metadata['tables'][EVALUATION_FILE]['exported_rows']} evaluation rows and "
        f"{metadata['tables'][ANALYTE_FILE]['exported_rows']} analyte rows to {args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
