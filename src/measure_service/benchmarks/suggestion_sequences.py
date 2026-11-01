"""Qualify fixed BO request sequences and durable duplicate-policy campaigns.

The declaration is a synthetic software-qualification input. Component rows
use fabricated scores and never enter the registry. Durable rows come only
from fresh simulator executions and immutable service observations. This
runner does not compare optimizer performance or qualify a physical method.
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from measure_methods.gaussian_observations import RESOLUTION_DEFINITION

from .. import (
    campaign_protocol,
    campaign_protocol_registry,
    campaigns,
    experiments,
    gaussian_producer,
    observations,
    optimizer_backend,
    retention,
)
from ..storage import canonical, registry_connection, sha256
from . import synthetic_campaigns as _store_helpers
from .evidence import file_sha256, staged_bundle

PROFILE = "measure-service-suggestion-sequence-qualification/1"
PROTOCOL_PROFILE = "synthetic-isocratic-ablation/2"
OWNER = "synthetic-suggestion-sequence-runner"
DECLARATION_SHA256 = "d85f83d19076e266e4e159253d35e7baa9baea2cde32db225b1be9d7bfc4f4ef"
FIXTURE_ORACLE_SHA256 = "067b276933e5f434e52a47936a8cfa8041042a71f8c52fec1903f907a9a02b79"
INITIAL_DESIGN_SHA256 = "5c705d93fce23acd29279c8edde1217396c280176a444a96ccb1ab029455b664"
ORACLE_SOURCE_SHA256 = "093a7a269a70f9ffd11a8cb31572112e627234aebbad7329b56907bfde228789"
PLANNED_COMPONENT_CASES = 12
PLANNED_COMPONENT_CALLS = 84
PLANNED_DURABLE_CASES = 16
PLANNED_ANALYTICAL_SLOTS = 160
PLANNED_GP_SUGGESTIONS = 112
EVALUATION_BUDGET = 10
INITIAL_DESIGN_SIZE = 3
SEED = 17
_MAX_INPUT_BYTES = 1024 * 1024
_MAX_WORKER_OUTPUT_BYTES = 32 * 1024 * 1024
_MAX_WORKER_STDERR_BYTES = 1024 * 1024
_STORE_MARKER = ".measure-suggestion-sequence-store.json"
_PRECALL_MARKER = "precall_manifest.json"
_FIRST_CALL_MARKER = "first_backend_call.json"
_RUN_RESERVATION_MARKER = ".suggestion-sequence-run-reservation.json"
_RUN_EVIDENCE_DIRECTORY = "suggestion-sequence-run-evidence"
_INITIAL_FABRICATED_COORDINATES = (
    {"strong_solvent_fraction": 0.25, "acquisition_duration_min": 4.0},
    {"strong_solvent_fraction": 0.75, "acquisition_duration_min": 8.0},
    {"strong_solvent_fraction": 0.25, "acquisition_duration_min": 4.0},
)
_COMPONENT_TARGETS = ("historical_utility", "confirmed_acceptance_reward")
_NOISE_PRIORS = ("default", "low_noise")
_DUPLICATE_POLICIES = ("reject", "replicate")
_PATTERNS = ("all_zero", "all_one", "mixed")
_WORKER_PROFILE = "measure-service-suggestion-sequence-worker/1"
_DURABLE_CHUNKS = ((0, 1, 2), (3, 4, 5, 6), (7, 8, 9))
DEFAULT_DECLARATION = (
    Path(__file__).parent
    / "reference_data"
    / "suggestion_sequences_v1"
    / "candidate-declaration.json"
)
_SUGGESTION_CASE_COLUMNS = (
    "case_id",
    "task_id",
    "optimizer_target",
    "surrogate_noise_prior",
    "duplicate_policy",
    "seed",
    "ordinal",
    "status",
    "proposal_available",
    "strong_solvent_fraction",
    "acquisition_duration_min",
    "proposal_id",
    "experiment_id",
    "attempt_id",
    "retention_prediction_id",
    "gaussian_run_id",
    "observation_id",
    "snapshot_id",
    "consumption_id",
    "is_repeat",
    "matching_prior_proposals",
    "oracle_status",
    "oracle_passed",
    "host_elapsed_seconds",
    "failure_type",
    "failure_message",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in qualification input: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value in qualification input: {value}")


def _read_json(path: Path, expected_sha256: str, *, maximum_bytes: int = _MAX_INPUT_BYTES):
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError(f"qualification input is unavailable: {path.name}") from error
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum_bytes:
        raise ValueError(f"qualification input must be a bounded regular file: {path.name}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError(f"qualification input failed SHA256 verification: {path.name}")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"qualification input is malformed: {path.name}") from error
    return value, raw, digest


def _read_verified_source_bytes(path: Path, expected_sha256: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"qualification source is unavailable: {path.name}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_INPUT_BYTES:
            raise ValueError("independent oracle source must be a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(_MAX_INPUT_BYTES + 1)
        after = os.fstat(descriptor)
        if (
            len(raw) > _MAX_INPUT_BYTES
            or before.st_size != after.st_size
            or len(raw) != after.st_size
        ):
            raise ValueError("independent oracle source changed or exceeded its byte limit")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_sha256:
            raise ValueError("independent oracle source failed SHA256 verification")
        return raw
    finally:
        os.close(descriptor)


def _expected_component_patterns() -> dict[tuple[str, str, str], list[float]]:
    return (
        {
            (target, prior, "all_zero"): [0.0] * EVALUATION_BUDGET
            for target in _COMPONENT_TARGETS
            for prior in _NOISE_PRIORS
        }
        | {
            (target, prior, "all_one"): [1.0] * EVALUATION_BUDGET
            for target in _COMPONENT_TARGETS
            for prior in _NOISE_PRIORS
        }
        | {
            (target, prior, "mixed"): [float(index % 2) for index in range(EVALUATION_BUDGET)]
            for target in _COMPONENT_TARGETS
            for prior in _NOISE_PRIORS
        }
    )


def _component_pattern(case: dict[str, Any]) -> str:
    case_id = case.get("case_id")
    if not isinstance(case_id, str):
        raise ValueError("component sequence case lacks its declared case identity")
    parts = case_id.split(":")
    if len(parts) != 3 or parts[2] not in _PATTERNS:
        raise ValueError("component sequence case identity does not declare a score pattern")
    return parts[2]


def _validate_declaration(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("profile") != PROFILE:
        raise ValueError("unsupported suggestion-sequence declaration profile")
    template = document.get("protocol_template")
    if (
        not isinstance(template, dict)
        or template.get("profile") != PROTOCOL_PROFILE
        or template.get("duplicate_policy") != "replicate"
        or template.get("evaluation_budget") != EVALUATION_BUDGET
        or template.get("initial_design_size") != INITIAL_DESIGN_SIZE
        or template.get("seeds") != [SEED]
    ):
        raise ValueError("protocol template differs from the frozen ablation/2 design")
    tasks = document.get("fixtures")
    if not isinstance(tasks, list) or any(not isinstance(item, dict) for item in tasks):
        raise ValueError("the frozen declaration must contain four fixture objects")
    task_ids = [item.get("task_id") for item in tasks]
    if len(tasks) != 4 or len(set(task_ids)) != 4 or any(not item for item in task_ids):
        raise ValueError("the frozen declaration must contain four unique logical fixtures")
    template_tasks = template.get("tasks")
    if not isinstance(template_tasks, list) or any(
        not isinstance(item, dict) for item in template_tasks
    ):
        raise ValueError("protocol template must preserve the four logical task objects")
    template_ids = [item.get("task_id") for item in template_tasks]
    if template_ids != task_ids:
        raise ValueError("every protocol must retain all four declared logical task IDs")
    component = document.get("component_sequences")
    if (
        not isinstance(component, dict)
        or component.get("case_count") != PLANNED_COMPONENT_CASES
        or component.get("planned_backend_calls") != PLANNED_COMPONENT_CALLS
        or component.get("initial_rows") != INITIAL_DESIGN_SIZE
        or component.get("call_ordinals") != list(range(INITIAL_DESIGN_SIZE, EVALUATION_BUDGET))
        or component.get("initial_fabricated_coordinates") != list(_INITIAL_FABRICATED_COORDINATES)
    ):
        raise ValueError("component-sequence denominators or fixed inputs changed")
    expected_patterns = _expected_component_patterns()
    cases = component.get("cases")
    if not isinstance(cases, list) or len(cases) != PLANNED_COMPONENT_CASES:
        raise ValueError("the component sequence case list has an unsupported shape")
    observed_patterns = {}
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("component sequence case must be an object")
        pattern = _component_pattern(case)
        key = (
            case.get("optimizer_target"),
            case.get("surrogate_noise_prior"),
            pattern,
        )
        if (
            key not in expected_patterns
            or case.get("case_id") != ":".join(key)
            or case.get("duplicate_policy") != "replicate"
            or case.get("seed") != SEED
            or case.get("fabricated_score_sequence_by_training_ordinal") != expected_patterns[key]
        ):
            raise ValueError("a component sequence differs from the declared fixed patterns")
        if key in observed_patterns:
            raise ValueError("duplicate component sequence case")
        observed_patterns[key] = case
    if set(observed_patterns) != set(expected_patterns):
        raise ValueError("component sequence factor coverage is incomplete")
    initial_design = document.get("initial_design")
    initial_coordinates = (
        initial_design.get("coordinates") if isinstance(initial_design, dict) else None
    )
    if not isinstance(initial_coordinates, list) or len(initial_coordinates) != INITIAL_DESIGN_SIZE:
        raise ValueError("the shared initial design must contain exactly three coordinates")
    durable = document.get("durable_campaigns")
    durable_cases = durable.get("cases") if isinstance(durable, dict) else None
    if (
        not isinstance(durable_cases, list)
        or len(durable_cases) != PLANNED_DURABLE_CASES
        or durable.get("campaign_count") != PLANNED_DURABLE_CASES
        or durable.get("planned_analytical_slots") != PLANNED_ANALYTICAL_SLOTS
        or durable.get("planned_gp_suggestion_slots") != PLANNED_GP_SUGGESTIONS
        or durable.get("planned_initial_analytical_slots") != 48
        or durable.get("planned_analyte_slots") != 320
    ):
        raise ValueError("durable campaign denominators differ from the frozen declaration")
    expected_durable = {
        (task_id, prior, duplicate_policy)
        for task_id in task_ids
        for prior in _NOISE_PRIORS
        for duplicate_policy in _DUPLICATE_POLICIES
    }
    observed_durable = set()
    for case in durable_cases:
        if not isinstance(case, dict):
            raise ValueError("durable campaign case must be an object")
        key = (case.get("task_id"), case.get("surrogate_noise_prior"), case.get("duplicate_policy"))
        if (
            key not in expected_durable
            or case.get("case_id") != ":".join(key)
            or case.get("optimizer_target") != "confirmed_acceptance_reward"
            or case.get("policy") != "bo_mcp_gp_logei"
            or case.get("seed") != SEED
            or case.get("evaluation_budget") != EVALUATION_BUDGET
            or case.get("initial_design_size") != INITIAL_DESIGN_SIZE
        ):
            raise ValueError("a durable campaign differs from the declared 16-case plan")
        if key in observed_durable:
            raise ValueError("duplicate durable campaign case")
        observed_durable.add(key)
    if observed_durable != expected_durable:
        raise ValueError("durable task/prior/duplicate-policy coverage is incomplete")
    totals = document.get("planned_totals")
    if (
        not isinstance(totals, dict)
        or totals.get("component_backend_call_slots") != PLANNED_COMPONENT_CALLS
        or totals.get("campaign_gp_suggestion_slots") != PLANNED_GP_SUGGESTIONS
        or totals.get("maximum_backend_calls_without_retries") != 196
        or totals.get("analytical_evaluation_slots") != PLANNED_ANALYTICAL_SLOTS
        or totals.get("analyte_slots") != 320
    ):
        raise ValueError("overall planned denominators differ from the frozen declaration")
    if document.get("oracle", {}).get("required_cohort_size") != (
        "len(fixture.analytes), currently2; never reuse prior three-analyte hardcoding"
    ):
        raise ValueError("oracle cohort-size contract differs from the frozen declaration")
    return document


def _load_fixed_inputs(declaration_path: str | Path):
    declaration_source = Path(declaration_path)
    document, declaration_raw, declaration_digest = _read_json(
        declaration_source, DECLARATION_SHA256
    )
    _validate_declaration(document)
    source_dir = declaration_source.parent
    oracle_data_path = source_dir / "fixture-oracle.json"
    initial_design_path = source_dir / "initial-design.json"
    oracle_source_path = source_dir / "independent_oracle.py.txt"
    if not oracle_source_path.exists():
        oracle_source_path = source_dir / "independent_oracle.py"
    fixture_oracle, oracle_data_raw, oracle_data_digest = _read_json(
        oracle_data_path, FIXTURE_ORACLE_SHA256
    )
    initial_design, initial_design_raw, initial_design_digest = _read_json(
        initial_design_path, INITIAL_DESIGN_SHA256
    )
    oracle_source_raw = _read_verified_source_bytes(oracle_source_path, ORACLE_SOURCE_SHA256)
    if not isinstance(fixture_oracle, dict) or set(fixture_oracle) != {
        item["task_id"] for item in document["fixtures"]
    }:
        raise ValueError("the independent oracle fixture cohort differs from the declaration")
    if (
        initial_design.get("campaign_design_sha256")
        != document["initial_design"].get("campaign_design_sha256")
        or initial_design.get("initial_coordinates")
        != document["initial_design"].get("coordinates")
        or initial_design.get("backend_calls") != 0
        or initial_design.get("registry_calls") != 0
    ):
        raise ValueError("the fixed initial-design record differs from the declaration")
    return {
        "document": document,
        "declaration_path": declaration_source,
        "declaration_raw": declaration_raw,
        "declaration_sha256": declaration_digest,
        "fixture_oracle": fixture_oracle,
        "fixture_oracle_raw": oracle_data_raw,
        "fixture_oracle_path": oracle_data_path,
        "fixture_oracle_sha256": oracle_data_digest,
        "initial_design": initial_design,
        "initial_design_raw": initial_design_raw,
        "initial_design_path": initial_design_path,
        "initial_design_sha256": initial_design_digest,
        "oracle_source_path": oracle_source_path,
        "oracle_source_raw": oracle_source_raw,
        "oracle_source_sha256": ORACLE_SOURCE_SHA256,
    }


def _planned_component_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for case in document["component_sequences"]["cases"]:
        for ordinal in document["component_sequences"]["call_ordinals"]:
            rows.append(
                {
                    "case_id": case["case_id"],
                    "optimizer_target": case["optimizer_target"],
                    "surrogate_noise_prior": case["surrogate_noise_prior"],
                    "duplicate_policy": case["duplicate_policy"],
                    "pattern": _component_pattern(case),
                    "seed": case["seed"],
                    "ordinal": ordinal,
                    "iteration": ordinal,
                    "status": "not_attempted_unknown",
                    "attempted": False,
                    "history": None,
                    "history_sha256": None,
                    "request": None,
                    "request_sha256": None,
                    "response": None,
                    "candidate": None,
                    "candidate_available": False,
                    "backend_provenance": None,
                    "host_elapsed_seconds": None,
                    "child_elapsed_seconds": None,
                    "started_at": None,
                    "finished_at": None,
                    "failure": None,
                }
            )
    return rows


def _planned_durable_cases(document: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for case in document["durable_campaigns"]["cases"]:
        result.append(
            {
                "case_id": case["case_id"],
                "task_id": case["task_id"],
                "optimizer_target": case["optimizer_target"],
                "surrogate_noise_prior": case["surrogate_noise_prior"],
                "duplicate_policy": case["duplicate_policy"],
                "policy": case["policy"],
                "seed": case["seed"],
                "evaluation_budget": case["evaluation_budget"],
                "initial_design_size": case["initial_design_size"],
                "campaign_id": None,
                "protocol_id": None,
                "status": "not_attempted_unknown",
                "slots": [
                    {
                        "ordinal": ordinal,
                        "iteration": ordinal,
                        "status": "not_attempted_unknown",
                        "proposal_available": False,
                        "candidate": None,
                        "duplicate_of_prior_proposals": [],
                        "suggestion_events": [],
                        "suggestion_attempt": None,
                        "proposal": None,
                        "experiment": None,
                        "attempt": None,
                        "retention_prediction": None,
                        "gaussian_observation": None,
                        "observation": None,
                        "snapshot": None,
                        "consumption": None,
                        "oracle": None,
                        "oracle_comparison": None,
                        "step_timings": [],
                        "failure": None,
                    }
                    for ordinal in range(case["evaluation_budget"])
                ],
                "worker_processes": [],
                "worker_journal_tail_error": None,
                "worker_journal_error": None,
                "restart_checkpoints": [],
                "final_campaign": None,
                "failure": None,
            }
        )
    return result


def _require_replication_profile() -> None:
    """Fail before store setup or optimizer calls when the installed service is stale."""
    if not hasattr(campaign_protocol, "ReplicationProtocolSpec"):
        raise RuntimeError(
            "the installed service lacks the frozen synthetic-isocratic-ablation/2 profile"
        )
    try:
        parsed = campaign_protocol.parse_campaign_protocol(
            {
                "profile": PROTOCOL_PROFILE,
                "optimizer_target": "confirmed_acceptance_reward",
                "surrogate_noise_prior": "default",
                "duplicate_policy": "replicate",
                "domain": {
                    "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
                    "acquisition_duration_min": {"lower": 2.0, "upper": 10.0},
                },
                "flow_ml_min": 0.5,
                "constraints": {},
                "tasks": [
                    {
                        "task_id": "probe-task",
                        "producer_id": "probe-producer",
                        "observation_contract_id": "probe-contract",
                    }
                ],
                "seeds": [SEED],
                "evaluation_budget": EVALUATION_BUDGET,
                "initial_design_size": INITIAL_DESIGN_SIZE,
                "policies": ["random", "bo_mcp_gp_logei"],
                "evaluation_cost": 1.0,
                "evaluation_cost_unit": "synthetic_evaluation",
            }
        )
    except Exception as error:
        raise RuntimeError(
            "the installed service cannot parse the frozen synthetic-isocratic-ablation/2 profile"
        ) from error
    replication_type = getattr(campaign_protocol, "ReplicationProtocolSpec")
    if not isinstance(parsed, replication_type) or parsed.duplicate_policy != "replicate":
        raise RuntimeError("the installed service does not preserve the declared duplicate policy")


def _protocol_spec_data(
    document: dict[str, Any],
    bindings: list[dict[str, Any]],
    *,
    target: str,
    prior: str,
    duplicate_policy: str,
):
    task_bindings = {item["task_id"]: item for item in bindings}
    tasks = []
    for declared in document["protocol_template"]["tasks"]:
        task_id = declared["task_id"]
        binding = task_bindings[task_id]
        tasks.append(
            {
                "task_id": task_id,
                "producer_id": binding["producer"]["producer_id"],
                "observation_contract_id": binding["contract"]["contract_id"],
            }
        )
    raw = json.loads(canonical(document["protocol_template"]))
    raw.update(
        {
            "profile": PROTOCOL_PROFILE,
            "tasks": tasks,
            "optimizer_target": target,
            "surrogate_noise_prior": prior,
            "duplicate_policy": duplicate_policy,
        }
    )
    parsed = campaign_protocol.parse_campaign_protocol(raw)
    replication_type = getattr(campaign_protocol, "ReplicationProtocolSpec", None)
    if replication_type is None or not isinstance(parsed, replication_type):
        raise RuntimeError("protocol construction did not retain ablation/2 duplicate semantics")
    if parsed.model_dump(mode="json") != raw:
        raise ValueError("normalized campaign protocol differs from its frozen declaration")
    return parsed


def _design_streams(document: dict[str, Any], bindings: list[dict[str, Any]]):
    expected_coordinates = document["initial_design"]["coordinates"]
    streams = []
    for target in _COMPONENT_TARGETS:
        for prior in _NOISE_PRIORS:
            for duplicate_policy in _DUPLICATE_POLICIES:
                spec = _protocol_spec_data(
                    document,
                    bindings,
                    target=target,
                    prior=prior,
                    duplicate_policy=duplicate_policy,
                )
                design_digest = campaign_protocol.campaign_design_sha256(spec)
                coordinates = [
                    campaign_protocol.initial_candidate(spec, SEED, ordinal)
                    for ordinal in range(INITIAL_DESIGN_SIZE)
                ]
                matches = coordinates == expected_coordinates
                if (
                    design_digest != document["initial_design"]["campaign_design_sha256"]
                    or not matches
                ):
                    raise ValueError(
                        "one of the eight declared target/prior/duplicate streams changed its design"
                    )
                streams.append(
                    {
                        "optimizer_target": target,
                        "surrogate_noise_prior": prior,
                        "duplicate_policy": duplicate_policy,
                        "campaign_design_sha256": design_digest,
                        "coordinates": coordinates,
                        "matches_fixed_initial_design": True,
                    }
                )
    if len(streams) != 8:
        raise ValueError("the predeclared eight treatment streams were not checked")
    return streams


def _component_spec_map(document: dict[str, Any], bindings: list[dict[str, Any]]):
    return {
        (target, prior): _protocol_spec_data(
            document,
            bindings,
            target=target,
            prior=prior,
            duplicate_policy="replicate",
        )
        for target in _COMPONENT_TARGETS
        for prior in _NOISE_PRIORS
    }


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_component_response(
    result: Any,
    *,
    spec: Any,
    request: dict[str, Any],
    metadata: dict[str, Any] | None,
):
    if not isinstance(result, dict) or set(result) != {"candidate", "backend"}:
        raise ValueError("optimizer returned an invalid component response shape")
    candidate, backend = result["candidate"], result["backend"]
    if not isinstance(candidate, dict) or set(candidate) != {
        "strong_solvent_fraction",
        "acquisition_duration_min",
    }:
        raise ValueError("optimizer returned an invalid candidate shape")
    for key, bounds in (
        ("strong_solvent_fraction", spec.domain.strong_solvent_fraction),
        ("acquisition_duration_min", spec.domain.acquisition_duration_min),
    ):
        value = candidate[key]
        if not _finite_number(value) or not bounds.lower <= float(value) <= bounds.upper:
            raise ValueError(f"optimizer candidate is outside its declared domain: {key}")
    if not isinstance(backend, dict):
        raise ValueError("optimizer returned no backend provenance object")
    expected_settings = campaign_protocol.optimizer_configuration(spec)
    if (
        backend.get("protocol_sha256") != request.get("protocol_sha256")
        or backend.get("training_sha256") != request.get("training_sha256")
        or backend.get("seed") != request.get("seed")
        or backend.get("training_count") != len(request.get("training_data", []))
        or backend.get("adapter_source_sha256") != request.get("adapter_source_sha256")
        or backend.get("campaign_protocol_source_sha256")
        != request.get("campaign_protocol_source_sha256")
        or backend.get("design_kernel_source_sha256") != request.get("design_kernel_source_sha256")
        or backend.get("model_type") != "SingleTaskGP"
        or backend.get("acquisition_implementation") != "qLogExpectedImprovement"
        or backend.get("generation_method") != "bo"
        or not isinstance(backend.get("warnings"), list)
        or any(not isinstance(item, str) for item in backend.get("warnings", []))
    ):
        raise ValueError("optimizer backend provenance does not match the exact request")
    if metadata is not None:
        distributions = metadata.get("distributions", {})
        versions = backend.get("distribution_versions")
        expected_versions = {
            name: distributions.get(name, {}).get("version")
            for name in ("bo-engine", "botorch", "gpytorch", "measure-service", "torch")
        }
        expected_revision = distributions.get("bo-engine", {}).get("source_revision")
        runtime = backend.get("runtime")
        if (
            versions != expected_versions
            or backend.get("source_revision") != expected_revision
            or not isinstance(runtime, dict)
            or runtime.get("python_version") != metadata.get("python_version")
        ):
            raise ValueError(
                "optimizer runtime package identity differs from the pre-call manifest"
            )
    settings = backend.get("settings")
    objective = expected_settings["target_name"]
    if not isinstance(settings, dict) or settings.get("objective") != {
        "name": objective,
        "direction": "maximize",
    }:
        raise ValueError("optimizer objective differs from the declared target")
    noise = settings.get("noise_prior")
    expected_noise = expected_settings["noise_prior_params"]
    if expected_noise is None:
        if noise is not None:
            raise ValueError("legacy optimizer route unexpectedly declares a noise prior")
    elif (
        not isinstance(noise, dict)
        or noise.get("family") != "Gamma"
        or noise.get("concentration") != expected_noise[0]
        or noise.get("rate") != expected_noise[1]
        or noise.get("space") != "standardized_target_variance"
        or noise.get("constraint_lower_bound") != 0.0001
        or noise.get("learned") is not True
    ):
        raise ValueError("optimizer noise prior differs from the declared route")
    acquisition = settings.get("acquisition_optimization")
    if not isinstance(acquisition, dict):
        raise ValueError("optimizer omitted acquisition restart/sample settings")
    if metadata is not None:
        effective = metadata.get("acquisition_optimization", {})
        if (
            acquisition.get("configured_num_restarts") != effective.get("configured_num_restarts")
            or acquisition.get("configured_raw_samples") != effective.get("configured_raw_samples")
            or acquisition.get("num_restarts") != effective.get("num_restarts")
            or acquisition.get("raw_samples") != effective.get("raw_samples")
        ):
            raise ValueError(
                "effective acquisition settings differ from the frozen public defaults"
            )
        if backend.get("source_tree_sha256") != metadata.get("bo_engine_source_tree_sha256"):
            raise ValueError("BO-MCP source tree differs from the pre-call software manifest")
    runtime = backend.get("runtime")
    if not isinstance(runtime, dict) or not _finite_number(runtime.get("elapsed_seconds")):
        raise ValueError("optimizer returned invalid child runtime evidence")
    if runtime["elapsed_seconds"] < 0:
        raise ValueError("optimizer returned negative child runtime evidence")
    return dict(candidate), backend


def _run_component_sequences(
    document: dict[str, Any],
    bindings: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None,
    first_call_marker: Path | None,
    suggest_call: Callable[[Any, list[dict[str, Any]], int], dict[str, Any]] | None = None,
    request_builder: Callable[[Any, list[dict[str, Any]], int], dict[str, Any]] | None = None,
    protocol_specs: dict[tuple[str, str], Any] | None = None,
    precall_manifest_sha256: str | None = None,
    row_callback: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    call = suggest_call or optimizer_backend.suggest_candidate
    build_request = request_builder or optimizer_backend.suggestion_request_evidence
    specs = protocol_specs or _component_spec_map(document, bindings)
    rows = _planned_component_rows(document)
    row_by_key = {(row["case_id"], row["ordinal"]): row for row in rows}
    for case in document["component_sequences"]["cases"]:
        history = [
            {
                "candidate": dict(candidate),
                "score": case["fabricated_score_sequence_by_training_ordinal"][ordinal],
            }
            for ordinal, candidate in enumerate(
                document["component_sequences"]["initial_fabricated_coordinates"]
            )
        ]
        failed = False
        for ordinal in document["component_sequences"]["call_ordinals"]:
            row = row_by_key[(case["case_id"], ordinal)]
            row["history"] = json.loads(canonical(history))
            row["history_sha256"] = sha256(history)
            if failed:
                continue
            spec = specs[(case["optimizer_target"], case["surrogate_noise_prior"])]
            started_wall = time.time()
            started = time.perf_counter()
            row["attempted"] = True
            row["started_at"] = datetime.fromtimestamp(started_wall, timezone.utc).isoformat()
            try:
                request = build_request(spec, history, case["seed"])
                row["request"] = request
                row["request_sha256"] = sha256(request)
                if first_call_marker is not None:
                    _record_first_backend_call(
                        first_call_marker,
                        {
                            "profile": PROFILE,
                            "first_call_at": row["started_at"],
                            "section": "component_sequences",
                            "case_id": case["case_id"],
                            "ordinal": ordinal,
                            "iteration": ordinal,
                            "request_sha256": row["request_sha256"],
                            "precall_manifest_sha256": precall_manifest_sha256,
                            "bo_engine_source_tree_sha256": (
                                None
                                if metadata is None
                                else metadata.get("bo_engine_source_tree_sha256")
                            ),
                        },
                    )
                response = call(spec, history, case["seed"])
                row["response"] = response
                if isinstance(response, dict) and isinstance(response.get("candidate"), dict):
                    row["candidate"] = response["candidate"]
                    row["candidate_available"] = True
                candidate, backend = _validate_component_response(
                    response, spec=spec, request=request, metadata=metadata
                )
                row["status"] = "returned"
                row["candidate"] = candidate
                row["candidate_available"] = True
                row["backend_provenance"] = backend
                row["child_elapsed_seconds"] = backend["runtime"]["elapsed_seconds"]
                append_score = case["fabricated_score_sequence_by_training_ordinal"][ordinal]
                history.append({"candidate": dict(candidate), "score": append_score})
            except Exception as error:
                row["status"] = "failed"
                row["failure"] = _error_record(error)
                row["diagnostics"] = _bounded_diagnostics(error)
                failed = True
            finally:
                elapsed = time.perf_counter() - started
                row["host_elapsed_seconds"] = (
                    elapsed if math.isfinite(elapsed) and elapsed >= 0 else None
                )
                row["finished_at"] = _utc_now()
                if row_callback is not None:
                    row_callback(json.loads(canonical(row)))
    return rows


def _error_record(error: BaseException, *, private_paths: tuple[Path, ...] = ()) -> dict[str, str]:
    try:
        message = str(error).strip()
    except Exception:
        message = ""
    if not message:
        message = f"{type(error).__name__} during qualification execution"
    for path in private_paths:
        message = message.replace(str(path), "<local-path>")
    return {"type": type(error).__name__[:128], "message": message[:1000]}


def _bounded_diagnostics(error: BaseException) -> dict[str, Any] | None:
    try:
        value = getattr(error, "diagnostics", None)
        if isinstance(value, dict) and len(canonical(value).encode("utf-8")) <= 32_768:
            return json.loads(canonical(value))
    except Exception:
        pass
    return None


def _record_first_backend_call(path: Path, event: dict[str, Any]) -> bool:
    """Create immutable pre-call evidence once; later call sites verify it."""
    encoded = (json.dumps(event, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("first-call evidence marker is not a regular file")
        existing = path.read_bytes()
        json.loads(existing.decode("utf-8"))
        return False
    with os.fdopen(descriptor, "wb") as target:
        target.write(encoded)
        target.flush()
        os.fsync(target.fileno())
    _fsync_directory(path.parent)
    return True


def _safe_tree_digest(root: Path) -> str:
    files = sorted(path for path in root.rglob("*.py") if path.is_file())
    if not files:
        raise ValueError("installed BO-MCP source tree is empty")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest()


_SIDECAR_METADATA_PROGRAM = r"""
import hashlib, importlib.metadata, importlib.util, json, platform, sys
from pathlib import Path
from bo_engine.types import AcquisitionOptimizationConfig

def distribution(name):
    try:
        item = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return {"version": None, "source_revision": None}
    revision = None
    raw = item.read_text("direct_url.json")
    if raw:
        try:
            direct = json.loads(raw)
        except (ValueError, TypeError):
            direct = None
        vcs = direct.get("vcs_info") if isinstance(direct, dict) else None
        candidate = vcs.get("commit_id") if isinstance(vcs, dict) else None
        if isinstance(candidate, str) and len(candidate) in (40, 64):
            if all(character in "0123456789abcdefABCDEF" for character in candidate):
                revision = candidate.lower()
    return {"version": item.version, "source_revision": revision}

module_spec = importlib.util.find_spec("bo_engine")
if module_spec is None or not module_spec.submodule_search_locations:
    raise RuntimeError("public bo_engine package is unavailable")
root = Path(next(iter(module_spec.submodule_search_locations))).resolve()
files = sorted(path for path in root.rglob("*.py") if path.is_file())
digest = hashlib.sha256()
for path in files:
    digest.update(path.relative_to(root).as_posix().encode("utf-8"))
    digest.update(b"\0")
    digest.update(hashlib.sha256(path.read_bytes()).digest())
    digest.update(b"\n")
settings = AcquisitionOptimizationConfig()
restarts, samples = settings.resolve(2)
print(json.dumps({
    "profile": "measure-service-bo-metadata-only/1",
    "python_version": platform.python_version(),
    "python_implementation": platform.python_implementation(),
    "distributions": {
        name: distribution(name)
        for name in (
            "bo-engine",
            "botorch",
            "gpytorch",
            "measure-service",
            "torch",
            "numpy",
            "scipy",
        )
    },
    "bo_engine_source_tree_sha256": digest.hexdigest(),
    "acquisition_optimization": {
        "configured_num_restarts": settings.num_restarts,
        "configured_raw_samples": settings.raw_samples,
        "num_restarts": restarts,
        "raw_samples": samples,
        "dimensions": 2,
        "source": "public AcquisitionOptimizationConfig.resolve(2); no generation call",
    },
}, sort_keys=True, allow_nan=False))
"""


def _probe_sidecar_identity(timeout_seconds: float = 20.0) -> dict[str, Any]:
    executable = os.environ.get("MEASURE_BO_PYTHON")
    if not executable:
        return {"status": "unavailable", "reason": "MEASURE_BO_PYTHON is not configured"}
    interpreter = Path(executable)
    if (
        not interpreter.is_absolute()
        or not interpreter.is_file()
        or not os.access(interpreter, os.X_OK)
    ):
        return {
            "status": "unavailable",
            "reason": "MEASURE_BO_PYTHON must name an absolute executable file",
        }
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    try:
        completed = subprocess.run(
            [str(interpreter), "-c", _SIDECAR_METADATA_PROGRAM],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "unavailable", "reason": _error_record(error)["message"]}
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[:1000]
        return {
            "status": "unavailable",
            "reason": f"metadata-only sidecar probe exited {completed.returncode}: {detail}",
        }
    try:
        metadata = json.loads(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        return {
            "status": "unavailable",
            "reason": f"metadata-only sidecar probe was invalid: {error}",
        }
    if (
        not isinstance(metadata, dict)
        or metadata.get("profile") != "measure-service-bo-metadata-only/1"
    ):
        return {
            "status": "unavailable",
            "reason": "metadata-only sidecar probe returned an unsupported profile",
        }
    config = metadata.get("acquisition_optimization")
    if (
        not isinstance(config, dict)
        or config.get("configured_num_restarts") is not None
        or config.get("configured_raw_samples") is not None
        or type(config.get("num_restarts")) is not int
        or config["num_restarts"] < 1
        or type(config.get("raw_samples")) is not int
        or config["raw_samples"] < 1
        or config.get("dimensions") != 2
    ):
        return {
            "status": "unavailable",
            "reason": "public acquisition defaults were not positive integers",
        }
    return {"status": "available", **metadata}


def _distribution_identity(name: str) -> dict[str, Any]:
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return {"version": None, "source_revision": None}
    revision = None
    raw = distribution.read_text("direct_url.json")
    if raw:
        try:
            direct = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            direct = None
        vcs = direct.get("vcs_info") if isinstance(direct, dict) else None
        candidate = vcs.get("commit_id") if isinstance(vcs, dict) else None
        if isinstance(candidate, str) and len(candidate) in (40, 64):
            if all(char in "0123456789abcdefABCDEF" for char in candidate):
                revision = candidate.lower()
    return {"version": distribution.version, "source_revision": revision}


def _host_module_hashes() -> dict[str, str]:
    names = (
        "measure_service.benchmarks.suggestion_sequences",
        "measure_service.benchmarks.evidence",
        "measure_service.campaign_protocol",
        "measure_service.campaign_protocol_registry",
        "measure_service.campaign_schema",
        "measure_service.campaign_suggestion_attempts",
        "measure_service.campaigns",
        "measure_service.experiments",
        "measure_service.gaussian_producer",
        "measure_service.method_registry",
        "measure_service.methods",
        "measure_service.observations",
        "measure_service.optimizer_backend",
        "measure_service.retention",
        "measure_service.storage",
        "measure_methods.design",
        "measure_methods.gaussian_observations",
        "measure_methods.moses_objective",
        "measure_methods.observations",
        "measure_methods.retention",
    )
    result = {}
    for name in names:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        if not path.is_file():
            raise ValueError(f"installed source module is unavailable: {name}")
        result[name] = file_sha256(path)
    for package in ("measure_service", "measure_methods"):
        package_spec = importlib.util.find_spec(package)
        locations = package_spec.submodule_search_locations if package_spec else None
        if not locations:
            raise ValueError(f"installed source package is unavailable: {package}")
        package_root = Path(next(iter(locations))).resolve()
        package_files = sorted(
            path
            for path in package_root.rglob("*.py")
            if path.is_file() and "__pycache__" not in path.parts
        )
        if not package_files:
            raise ValueError(f"installed source package is empty: {package}")
        tree = hashlib.sha256()
        members = {}
        for path in package_files:
            relative = path.relative_to(package_root).as_posix()
            digest = file_sha256(path)
            members[relative] = digest
            tree.update(relative.encode("utf-8"))
            tree.update(b"\0")
            tree.update(bytes.fromhex(digest))
            tree.update(b"\n")
        result[f"{package}.__source_tree__"] = tree.hexdigest()
        result[f"{package}.__source_files__"] = members
    return result


def _software_manifest(inputs: dict[str, Any], design_streams: list[dict[str, Any]]):
    module_hashes = _host_module_hashes()
    protocol_name = "measure_service.campaign_protocol"
    protocol_hash = module_hashes[protocol_name]
    if protocol_hash != inputs["initial_design"].get("installed_protocol_sha256"):
        raise ValueError(
            "installed campaign protocol source differs from the reviewed initial design"
        )
    backend = _probe_sidecar_identity()
    return {
        "profile": "measure-service-suggestion-sequence-precall-manifest/1",
        "created_at": _utc_now(),
        "declaration_sha256": inputs["declaration_sha256"],
        "fixture_oracle_sha256": inputs["fixture_oracle_sha256"],
        "oracle_source_sha256": inputs["oracle_source_sha256"],
        "initial_design_sha256": inputs["initial_design_sha256"],
        "initial_design_campaign_sha256": inputs["initial_design"]["campaign_design_sha256"],
        "initial_design_streams": design_streams,
        "host": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "measure_service_distribution": _distribution_identity("measure-service"),
            "measure_methods_distribution": _distribution_identity("measure-methods"),
            "module_sha256": module_hashes,
        },
        "bo_sidecar": backend,
        "fixed_backend_configuration": inputs["document"]["backend_configuration"],
    }


def _registry_key(declaration_sha256: str, *parts: Any) -> str:
    return f"seq-{declaration_sha256[:16]}-{sha256(list(parts))[:24]}"


def _fixture_retention_spec(fixture: dict[str, Any], document: dict[str, Any], digest: str):
    geometry = document["geometry"]
    return {
        "gradient_coordinate": "column_inlet_at_analyte_entry",
        "analytes": fixture["analytes"],
        "column_length_m": geometry["column_length_m"],
        "effective_area_m2": geometry["effective_area_m2"],
        "post_column_volume_ml": geometry["post_column_volume_ml"],
        "strong_solvent": "B",
        "parameter_origin": "synthetic",
        "parameter_reference": (
            f"Predeclared suggestion-sequence declaration sha256={digest}; "
            "constructed analytical equations, not fitted physical parameters."
        ),
    }


def _fixture_producer_spec(fixture: dict[str, Any], digest: str):
    analyte_ids = [item["analyte_id"] for item in fixture["analytes"]]
    return {
        "widths": [
            {"analyte_id": analyte_id, "fwhm_min": fixture["fwhm_min"]}
            for analyte_id in analyte_ids
        ],
        "width_origin": "synthetic_assumption",
        "width_reference": (
            f"Predeclared suggestion-sequence declaration sha256={digest}; "
            "fixed Gaussian FWHM assumption, not measured or fitted."
        ),
    }


def _fixture_contract_spec(fixture: dict[str, Any], document: dict[str, Any]):
    contract = document["contract"]
    return {
        "required_analytes": [item["analyte_id"] for item in fixture["analytes"]],
        "resolution_definition": RESOLUTION_DEFINITION,
        "resolution_threshold": contract["resolution_threshold"],
        "max_acquisition_duration_min": contract["max_acquisition_duration_min"],
        "runtime_preference_min": contract["runtime_preference_min"],
        "count_threshold": contract["count_threshold"],
    }


def _register_fixture_inputs(document: dict[str, Any], declaration_sha256: str):
    bindings = []
    for fixture in document["fixtures"]:
        task_id = fixture["task_id"]
        model = retention.register_retention_model(
            OWNER,
            f"Predeclared suggestion-sequence retention {task_id}",
            _fixture_retention_spec(fixture, document, declaration_sha256),
            _registry_key(declaration_sha256, "retention-model", task_id),
        )
        producer = gaussian_producer.register_gaussian_producer(
            OWNER,
            f"Predeclared suggestion-sequence Gaussian producer {task_id}",
            model["model_id"],
            _fixture_producer_spec(fixture, declaration_sha256),
            _registry_key(declaration_sha256, "gaussian-producer", task_id),
        )
        contract = observations.register_observation_contract(
            OWNER,
            f"Predeclared suggestion-sequence observation contract {task_id}",
            _fixture_contract_spec(fixture, document),
            _registry_key(declaration_sha256, "observation-contract", task_id),
        )
        bindings.append(
            {
                "task_id": task_id,
                "fixture": fixture,
                "retention_model": model,
                "producer": producer,
                "contract": contract,
            }
        )
    return bindings


def _register_durable_protocols(
    document: dict[str, Any], bindings: list[dict[str, Any]], declaration_sha256: str
):
    entries = {}
    for prior in _NOISE_PRIORS:
        for duplicate_policy in _DUPLICATE_POLICIES:
            spec = _protocol_spec_data(
                document,
                bindings,
                target="confirmed_acceptance_reward",
                prior=prior,
                duplicate_policy=duplicate_policy,
            )
            key = _registry_key(declaration_sha256, "protocol", prior, duplicate_policy)
            response = campaign_protocol_registry.register_campaign_protocol(
                OWNER,
                f"Predeclared suggestion-sequence confirmed-reward {prior} {duplicate_policy}",
                spec,
                key,
            )
            stored = campaign_protocol_registry.get_campaign_protocol(
                OWNER, response["protocol_id"]
            )
            parsed = campaign_protocol.parse_campaign_protocol(stored["spec"])
            expected = spec.model_dump(mode="json")
            if parsed.model_dump(mode="json") != expected:
                raise ValueError("stored durable protocol differs from the predeclared route")
            entries[(prior, duplicate_policy)] = {
                "spec": spec,
                "registered": response,
                "stored": stored,
            }
    if len(entries) != 4:
        raise ValueError("durable protocol registry did not cover four prior/policy routes")
    return entries


def _load_decimal_oracle(source: Path, source_bytes: bytes):
    module_name = "measure_suggestion_sequence_independent_oracle"
    module = types.ModuleType(module_name)
    module.__file__ = str(source)
    module.__package__ = ""
    try:
        exec(compile(source_bytes, str(source), "exec"), module.__dict__)
    except (SyntaxError, UnicodeError) as error:
        raise ValueError("the verified independent oracle could not be loaded") from error
    if not callable(getattr(module, "evaluate", None)):
        raise ValueError("the verified independent oracle has no evaluate function")
    return module


def _json_safe(value: Any):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _decimal_from_actual(value: Any) -> Decimal | None:
    if not _finite_number(value):
        return None
    return Decimal.from_float(float(value))


def _oracle_comparison(
    oracle_module: Any,
    document: dict[str, Any],
    fixture: dict[str, Any],
    candidate: dict[str, Any],
    gaussian_observation: dict[str, Any],
    consumption: dict[str, Any],
    proposal: dict[str, Any],
):
    expected = oracle_module.evaluate(fixture, candidate, document)
    result = consumption.get("result", {})
    raw = result.get("raw_metrics", {}) if isinstance(result, dict) else {}
    tolerances = document["oracle"]["absolute_tolerances"]
    differences: dict[str, Any] = {}
    mismatches = []

    def compare_number(name: str, observed: Any, reference: Any, tolerance_name: str):
        tolerance = Decimal(str(tolerances[tolerance_name]))
        if reference is None:
            passed = observed is None
            details = {
                "expected": None,
                "observed": observed,
                "absolute_error": None,
                "tolerance": str(tolerance),
                "passed": passed,
            }
        else:
            actual = _decimal_from_actual(observed)
            if actual is None or not isinstance(reference, Decimal):
                passed = False
                details = {
                    "expected": str(reference),
                    "observed": observed,
                    "absolute_error": None,
                    "tolerance": str(tolerance),
                    "passed": False,
                }
            else:
                error = abs(actual - reference)
                passed = error <= tolerance
                details = {
                    "expected": str(reference),
                    "observed": observed,
                    "absolute_error": str(error),
                    "tolerance": str(tolerance),
                    "passed": passed,
                }
        differences[name] = details
        if not passed:
            mismatches.append(name)

    def compare_exact(name: str, observed: Any, reference: Any):
        passed = observed == reference
        differences[name] = {"expected": reference, "observed": observed, "passed": passed}
        if not passed:
            mismatches.append(name)

    cohort_size = len(fixture["analytes"])
    expected_ids = [item["analyte_id"] for item in fixture["analytes"]]
    actual_ids = raw.get("analyte_ids")
    compare_exact("analyte_ids", actual_ids, expected_ids)
    compare_exact(
        "cohort_size",
        len(actual_ids) if isinstance(actual_ids, list) else None,
        cohort_size,
    )
    compare_exact(
        "retention_statuses", raw.get("retention_statuses"), expected["retention_statuses"]
    )
    compare_exact(
        "resolution_statuses", raw.get("resolution_statuses"), expected["resolution_statuses"]
    )
    actual_times = raw.get("retention_times_min")
    actual_resolutions = raw.get("peak_resolutions")
    if not isinstance(actual_times, list) or len(actual_times) != cohort_size:
        compare_exact("retention_time_array_shape", actual_times, "full cohort")
    else:
        for index, (actual, reference) in enumerate(
            zip(actual_times, expected["observed_retention_times_min"], strict=True)
        ):
            compare_number(f"retention_time_min[{index}]", actual, reference, "retention_min")
    if not isinstance(actual_resolutions, list) or len(actual_resolutions) != cohort_size:
        compare_exact("resolution_array_shape", actual_resolutions, "full cohort")
    else:
        for index, (actual, reference) in enumerate(
            zip(actual_resolutions, expected["resolutions"], strict=True)
        ):
            compare_number(f"resolution[{index}]", actual, reference, "resolution")
    compare_exact(
        "analytical_acceptance",
        result.get("analytical_acceptance"),
        expected["analytical_acceptance"],
    )
    target = result.get("optimizer_target")
    target_value = target.get("value") if isinstance(target, dict) else None
    compare_exact(
        "confirmed_acceptance_reward", target_value, expected["confirmed_acceptance_reward"]
    )
    utility = result.get("utility")
    utility_value = utility.get("score") if isinstance(utility, dict) else None
    compare_number("historical_utility", utility_value, expected["historical_utility"], "utility")
    compare_number(
        "runtime_min",
        result.get("runtime_min"),
        expected["declared_acquisition_duration_min"],
        "aggregate",
    )
    compare_number(
        "evaluation_cost",
        result.get("evaluation_cost"),
        expected["completed_attempt_cost"],
        "aggregate",
    )
    method = proposal.get("method", {})
    program = method.get("canonical_program", {}) if isinstance(method, dict) else {}
    solvent_use = program.get("total_solvent_use_ml", {}) if isinstance(program, dict) else {}
    compare_number(
        "solvent_A_ml",
        solvent_use.get("A") if isinstance(solvent_use, dict) else None,
        expected["declared_solvent_A_ml"],
        "aggregate",
    )
    compare_number(
        "solvent_B_ml",
        solvent_use.get("B") if isinstance(solvent_use, dict) else None,
        expected["declared_solvent_B_ml"],
        "aggregate",
    )
    compare_exact("cost_unit", result.get("evaluation_cost_unit"), document["evaluation_cost_unit"])
    gaussian_output = gaussian_observation.get("observation")
    if not isinstance(gaussian_output, dict):
        compare_exact("gaussian_output_shape", gaussian_output, "full ideal-centroid observation")
    else:
        compare_exact(
            "gaussian_profile", gaussian_output.get("profile"), "ideal-gaussian-centroid/1"
        )
        compare_exact(
            "gaussian_resolution_definition",
            gaussian_output.get("resolution_definition"),
            RESOLUTION_DEFINITION,
        )
        compare_number(
            "gaussian_fwhm_per_sigma",
            gaussian_output.get("fwhm_per_sigma"),
            Decimal.from_float(2.3548),
            "aggregate",
        )
        compare_number(
            "gaussian_acquisition_duration_min",
            gaussian_output.get("acquisition_duration_min"),
            expected["declared_acquisition_duration_min"],
            "aggregate",
        )
        gaussian_analytes = gaussian_output.get("analytes")
        if not isinstance(gaussian_analytes, list) or len(gaussian_analytes) != cohort_size:
            compare_exact("gaussian_analyte_array_shape", gaussian_analytes, "full cohort")
        else:
            by_id = {
                item.get("analyte_id"): item
                for item in gaussian_analytes
                if isinstance(item, dict) and isinstance(item.get("analyte_id"), str)
            }
            compare_exact("gaussian_analyte_ids", sorted(by_id), sorted(expected_ids))
            for index, analyte_id in enumerate(expected_ids):
                observed = by_id.get(analyte_id)
                if not isinstance(observed, dict):
                    compare_exact(f"gaussian_analyte[{analyte_id}]", observed, "present")
                    continue
                compare_exact(
                    f"gaussian_retention_status[{analyte_id}]",
                    observed.get("retention_status"),
                    expected["retention_statuses"][index],
                )
                compare_number(
                    f"gaussian_retention_time_min[{analyte_id}]",
                    observed.get("retention_time_min"),
                    expected["observed_retention_times_min"][index],
                    "retention_min",
                )
                compare_number(
                    f"gaussian_fwhm_min[{analyte_id}]",
                    observed.get("fwhm_min"),
                    Decimal.from_float(float(fixture["fwhm_min"])),
                    "aggregate",
                )
                compare_exact(
                    f"gaussian_resolution_status[{analyte_id}]",
                    observed.get("resolution_status"),
                    expected["resolution_statuses"][index],
                )
                compare_number(
                    f"gaussian_resolution[{analyte_id}]",
                    observed.get("resolution"),
                    expected["resolutions"][index],
                    "resolution",
                )
                compare_number(
                    f"gaussian_right_censor_bound_min[{analyte_id}]",
                    observed.get("right_censor_bound_min"),
                    Decimal.from_float(float(candidate["acquisition_duration_min"]))
                    if expected["retention_statuses"][index] == "not_eluted"
                    else None,
                    "aggregate",
                )
                compare_exact(
                    f"gaussian_resolution_reason[{analyte_id}]",
                    observed.get("resolution_reason"),
                    (
                        "retention_not_eluted_within_acquisition_duration"
                        if expected["resolution_statuses"][index] == "censored"
                        else "no_adjacent_finite_centroid"
                        if expected["resolution_statuses"][index] == "unsupported"
                        else None
                    ),
                )
        actual_pairs = gaussian_output.get("pairs")
        if not isinstance(actual_pairs, list) or len(actual_pairs) != len(expected["pairs"]):
            compare_exact("gaussian_pair_array_shape", actual_pairs, "oracle pair count")
        else:
            actual_pair_by_ids = {}
            invalid_actual_pair_ids = []
            for index, pair in enumerate(actual_pairs):
                if not isinstance(pair, dict):
                    compare_exact(f"gaussian_pair[{index}]", pair, "pair object")
                    continue
                actual_pair_ids = pair.get("analyte_ids")
                if (
                    not isinstance(actual_pair_ids, list)
                    or len(actual_pair_ids) != 2
                    or any(not isinstance(item, str) for item in actual_pair_ids)
                ):
                    invalid_actual_pair_ids.append(actual_pair_ids)
                    continue
                pair_key = tuple(sorted(actual_pair_ids))
                if pair_key in actual_pair_by_ids:
                    invalid_actual_pair_ids.append(actual_pair_ids)
                else:
                    actual_pair_by_ids[pair_key] = pair
            expected_pair_by_ids = {
                tuple(sorted(reference_pair["analytes"])): reference_pair
                for reference_pair in expected["pairs"]
            }
            compare_exact(
                "gaussian_pair_identity_set",
                sorted(actual_pair_by_ids),
                sorted(expected_pair_by_ids),
            )
            compare_exact("gaussian_invalid_pair_ids", invalid_actual_pair_ids, [])
            for index, pair_key in enumerate(sorted(expected_pair_by_ids)):
                reference_pair = expected_pair_by_ids[pair_key]
                observed = actual_pair_by_ids.get(pair_key)
                if not isinstance(observed, dict):
                    compare_exact(f"gaussian_pair[{index}]", observed, "pair object")
                    continue
                compare_exact(
                    f"gaussian_pair_ids[{index}]",
                    observed.get("analyte_ids"),
                    list(pair_key),
                )
                compare_exact(
                    f"gaussian_pair_status[{index}]",
                    observed.get("resolution_status"),
                    "observed",
                )
                compare_number(
                    f"gaussian_pair_resolution[{index}]",
                    observed.get("resolution"),
                    reference_pair["resolution"],
                    "resolution",
                )
                compare_number(
                    f"gaussian_pair_censor_bound_min[{index}]",
                    observed.get("right_censor_bound_min"),
                    None,
                    "aggregate",
                )
                compare_exact(
                    f"gaussian_pair_resolution_reason[{index}]",
                    observed.get("resolution_reason"),
                    None,
                )
    near_boundary = bool(expected["near_censor_boundary"] or expected["near_resolution_boundary"])
    return {
        "profile": "measure-suggestion-sequence-numerical-comparison/1",
        "oracle": _json_safe(expected),
        "tolerances": tolerances,
        "differences": differences,
        "mismatches": mismatches,
        "near_boundary": {
            "censor_analyte_indices": expected["near_censor_boundary"],
            "resolution_analyte_indices": expected["near_resolution_boundary"],
        },
        "status": "unresolved_boundary" if near_boundary else "pass" if not mismatches else "fail",
        "passed": not near_boundary and not mismatches,
    }


def _resource_counts(owner: str, campaign_id: str) -> dict[str, int]:
    with registry_connection() as con:
        suggestion_attempt_table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("campaign_suggestion_attempts",),
        ).fetchone()
        suggestion_attempt_count = (
            con.execute(
                "SELECT COUNT(*) FROM campaign_suggestion_attempts WHERE owner=? AND campaign_id=?",
                (owner, campaign_id),
            ).fetchone()[0]
            if suggestion_attempt_table is not None
            else 0
        )
        return {
            "proposals": con.execute(
                "SELECT COUNT(*) FROM campaign_proposals WHERE owner=? AND campaign_id=?",
                (owner, campaign_id),
            ).fetchone()[0],
            "methods": con.execute(
                "SELECT COUNT(*) FROM executable_methods WHERE owner=?", (owner,)
            ).fetchone()[0],
            "experiments": con.execute(
                "SELECT COUNT(*) FROM experiments WHERE owner=?", (owner,)
            ).fetchone()[0],
            "attempts": con.execute(
                "SELECT COUNT(*) FROM experiment_attempts WHERE owner=?", (owner,)
            ).fetchone()[0],
            "predictions": con.execute(
                "SELECT COUNT(*) FROM retention_predictions WHERE owner=?", (owner,)
            ).fetchone()[0],
            "gaussian_observations": con.execute(
                "SELECT COUNT(*) FROM gaussian_observations WHERE owner=?", (owner,)
            ).fetchone()[0],
            "analytical_observations": con.execute(
                "SELECT COUNT(*) FROM analytical_observations WHERE owner=?", (owner,)
            ).fetchone()[0],
            "snapshots": con.execute(
                "SELECT COUNT(*) FROM observation_snapshots WHERE owner=?", (owner,)
            ).fetchone()[0],
            "consumptions": con.execute(
                "SELECT COUNT(*) FROM campaign_consumptions WHERE owner=? AND campaign_id=?",
                (owner, campaign_id),
            ).fetchone()[0],
            "suggestion_attempts": suggestion_attempt_count,
        }


def _attempt_record_for_ordinal(owner: str, campaign_id: str, ordinal: int):
    page = campaigns.list_campaign_suggestion_attempts(owner, campaign_id, limit=20)
    matches = [item for item in page["records"] if item.get("ordinal") == ordinal]
    if len(matches) != 1:
        raise ValueError("campaign suggestion attempt ledger is missing or duplicates an ordinal")
    return campaigns.get_campaign_suggestion_attempt(owner, matches[0]["suggestion_attempt_id"])


def _duplicate_matches(prior_slots: list[dict[str, Any]], candidate: dict[str, Any]):
    encoded = canonical(candidate)
    return [
        {
            "proposal_id": slot["proposal"]["proposal_id"],
            "ordinal": slot["ordinal"],
        }
        for slot in prior_slots
        if slot.get("status") == "consumed"
        and isinstance(slot.get("proposal"), dict)
        and canonical(slot["proposal"].get("candidate")) == encoded
    ]


def _expected_training(prior_slots: list[dict[str, Any]]):
    records = []
    for slot in prior_slots:
        if slot.get("status") != "consumed":
            continue
        result = slot.get("consumption", {}).get("result", {})
        target = result.get("optimizer_target") if isinstance(result, dict) else None
        if not isinstance(target, dict) or not _finite_number(target.get("value")):
            raise ValueError("a consumed prefix row has no finite declared optimizer reward")
        records.append({"candidate": slot["candidate"], "score": target["value"]})
    return records


def _timed_step(slot: dict[str, Any], name: str, function: Callable[[], Any]):
    started_at = _utc_now()
    started = time.perf_counter()
    try:
        result = function()
    except Exception as error:
        elapsed = time.perf_counter() - started
        slot["step_timings"].append(
            {
                "step": name,
                "started_at": started_at,
                "finished_at": _utc_now(),
                "elapsed_seconds": elapsed if math.isfinite(elapsed) and elapsed >= 0 else None,
                "status": "failed",
                "failure": _error_record(error),
            }
        )
        raise
    elapsed = time.perf_counter() - started
    slot["step_timings"].append(
        {
            "step": name,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "elapsed_seconds": elapsed if math.isfinite(elapsed) and elapsed >= 0 else None,
            "status": "returned",
            "response_sha256": sha256(result),
        }
    )
    return result


def _suggestion_key(declaration_sha256: str, case_id: str, ordinal: int) -> str:
    return _registry_key(declaration_sha256, "campaign", case_id, "proposal", ordinal)


def _handle_suggestion_rejection(
    *,
    slot: dict[str, Any],
    case: dict[str, Any],
    campaign: dict[str, Any],
    declaration_sha256: str,
    expected_revision: int,
    prior_slots: list[dict[str, Any]],
    first_error: BaseException,
    counts_before_original: dict[str, int],
    counts_after_original: dict[str, int],
):
    owner = OWNER
    attempt_id = getattr(first_error, "suggestion_attempt_id", None)
    if not isinstance(attempt_id, str):
        raise first_error
    attempt = campaigns.get_campaign_suggestion_attempt(owner, attempt_id)
    slot["suggestion_attempt"] = attempt
    outcome = attempt.get("outcome") or {}
    raw_candidate = outcome.get("candidate")
    if raw_candidate is None:
        untrusted = outcome.get("untrusted_returned_evidence")
        raw_candidate = untrusted.get("candidate") if isinstance(untrusted, dict) else None
    if isinstance(raw_candidate, dict):
        slot["candidate"] = raw_candidate
        slot["proposal_available"] = True
        slot["duplicate_of_prior_proposals"] = _duplicate_matches(prior_slots, raw_candidate)
    request_key = _suggestion_key(declaration_sha256, case["case_id"], slot["ordinal"])
    counts_before = _resource_counts(owner, campaign["campaign_id"])
    retry_started = time.perf_counter()
    try:
        campaigns.suggest_campaign(
            owner,
            campaign["campaign_id"],
            expected_revision,
            request_key,
        )
        retry_error = None
    except Exception as error:
        retry_error = error
    retry_elapsed = time.perf_counter() - retry_started
    counts_after = _resource_counts(owner, campaign["campaign_id"])
    slot["suggestion_events"].append(
        {
            "kind": "terminal_failure_retry",
            "request": {
                "campaign_id": campaign["campaign_id"],
                "expected_revision": expected_revision,
                "idempotency_key": request_key,
            },
            "original_error": _error_record(first_error),
            "retry_error": None if retry_error is None else _error_record(retry_error),
            "retry_suggestion_attempt_id": getattr(retry_error, "suggestion_attempt_id", None),
            "same_failure": (
                retry_error is not None
                and type(retry_error) is type(first_error)
                and str(retry_error) == str(first_error)
                and getattr(retry_error, "suggestion_attempt_id", None) == attempt_id
            ),
            "resource_counts_before": counts_before,
            "resource_counts_after": counts_after,
            "resource_counts_unchanged": counts_before == counts_after,
            "original_counts_before": counts_before_original,
            "original_counts_after": counts_after_original,
            "original_rejection_created_no_proposal_or_analysis": all(
                counts_before_original.get(name) == counts_after_original.get(name)
                for name in (
                    "proposals",
                    "methods",
                    "experiments",
                    "attempts",
                    "predictions",
                    "gaussian_observations",
                    "analytical_observations",
                    "snapshots",
                    "consumptions",
                )
            ),
            "elapsed_seconds": retry_elapsed
            if math.isfinite(retry_elapsed) and retry_elapsed >= 0
            else None,
        }
    )
    duplicate_rejection = (
        case["duplicate_policy"] == "reject"
        and bool(slot["duplicate_of_prior_proposals"])
        and "duplicates a prior proposal" in str(first_error)
    )
    if duplicate_rejection:
        slot["status"] = "duplicate_rejected"
        slot["failure"] = _error_record(first_error)
    else:
        slot["status"] = "failed"
        slot["failure"] = _error_record(first_error)
    if not slot["suggestion_events"][-1]["same_failure"]:
        slot["failure"] = {
            "type": "ReceiptReplayMismatch",
            "message": "terminal suggestion retry did not replay the same failure",
        }
        slot["status"] = "failed"
    if not slot["suggestion_events"][-1]["resource_counts_unchanged"]:
        slot["failure"] = {
            "type": "ReceiptReplaySideEffect",
            "message": "terminal suggestion replay changed durable resources",
        }
        slot["status"] = "failed"
    if (
        duplicate_rejection
        and not slot["suggestion_events"][-1]["original_rejection_created_no_proposal_or_analysis"]
    ):
        slot["failure"] = {
            "type": "DuplicateRejectionSideEffect",
            "message": "rejected duplicate changed proposal or analytical resources",
        }
        slot["status"] = "failed"


def _process_slot(
    *,
    document: dict[str, Any],
    case: dict[str, Any],
    campaign: dict[str, Any],
    protocol: dict[str, Any],
    fixture: dict[str, Any],
    oracle_module: Any,
    declaration_sha256: str,
    ordinal: int,
    prior_slots: list[dict[str, Any]],
    first_call_marker: Path | None = None,
    precall_manifest_sha256: str | None = None,
    bo_engine_source_tree_sha256: str | None = None,
):
    slot = _planned_durable_cases({"durable_campaigns": {"cases": [case]}})[0]["slots"][ordinal]
    slot["campaign_id"] = campaign["campaign_id"]
    expected_revision = campaign["revision"]
    if expected_revision != 1 + 2 * ordinal:
        slot["status"] = "failed"
        slot["failure"] = {
            "type": "CampaignRevisionMismatch",
            "message": "stored revision does not match the declared ordinal schedule",
        }
        return slot, campaign, True
    request_key = _suggestion_key(declaration_sha256, case["case_id"], ordinal)
    slot["suggestion_events"].append(
        {
            "kind": "suggestion_request",
            "request": {
                "campaign_id": campaign["campaign_id"],
                "expected_revision": expected_revision,
                "idempotency_key": request_key,
            },
            "request_sha256": sha256(
                {
                    "operation": "suggest_campaign",
                    "campaign_id": campaign["campaign_id"],
                    "expected_revision": expected_revision,
                }
            ),
        }
    )
    if ordinal >= INITIAL_DESIGN_SIZE and first_call_marker is not None:
        training = _expected_training(prior_slots)
        request = optimizer_backend.suggestion_request_evidence(protocol, training, case["seed"])
        _record_first_backend_call(
            first_call_marker,
            {
                "profile": "measure-service-suggestion-first-backend-call/1",
                "first_call_at": _utc_now(),
                "section": "durable_campaigns",
                "case_id": case["case_id"],
                "campaign_id": campaign["campaign_id"],
                "ordinal": ordinal,
                "iteration": ordinal,
                "request_sha256": sha256(request),
                "precall_manifest_sha256": precall_manifest_sha256,
                "bo_engine_source_tree_sha256": bo_engine_source_tree_sha256,
            },
        )
    counts_before_suggestion = _resource_counts(OWNER, campaign["campaign_id"])
    try:
        suggestion = _timed_step(
            slot,
            "suggest_campaign",
            lambda: campaigns.suggest_campaign(
                OWNER,
                campaign["campaign_id"],
                expected_revision,
                request_key,
            ),
        )
    except Exception as error:
        slot["suggestion_events"][-1]["error"] = _error_record(error)
        counts_after_suggestion = _resource_counts(OWNER, campaign["campaign_id"])
        slot["suggestion_events"][-1]["resource_counts_before"] = counts_before_suggestion
        slot["suggestion_events"][-1]["resource_counts_after"] = counts_after_suggestion
        slot["suggestion_events"][-1]["resource_counts_unchanged"] = (
            counts_before_suggestion == counts_after_suggestion
        )
        try:
            _handle_suggestion_rejection(
                slot=slot,
                case=case,
                campaign=campaign,
                declaration_sha256=declaration_sha256,
                expected_revision=expected_revision,
                prior_slots=prior_slots,
                first_error=error,
                counts_before_original=counts_before_suggestion,
                counts_after_original=counts_after_suggestion,
            )
        except Exception as ledger_error:
            slot["status"] = "failed"
            slot["failure"] = _error_record(ledger_error)
        return slot, campaigns.get_campaign(OWNER, campaign["campaign_id"]), True
    counts_after_suggestion = _resource_counts(OWNER, campaign["campaign_id"])
    slot["suggestion_events"][-1]["resource_counts_before"] = counts_before_suggestion
    slot["suggestion_events"][-1]["resource_counts_after"] = counts_after_suggestion
    slot["suggestion_events"][-1]["resource_counts_unchanged"] = (
        counts_before_suggestion == counts_after_suggestion
    )
    slot["suggestion_events"][-1]["response_sha256"] = sha256(suggestion)
    slot["suggestion_events"][-1]["response"] = suggestion
    proposal = suggestion.get("proposal")
    if not isinstance(proposal, dict):
        slot["status"] = "failed"
        slot["failure"] = {"type": "MalformedSuggestion", "message": "response omitted proposal"}
        return slot, campaigns.get_campaign(OWNER, campaign["campaign_id"]), True
    proposal_id = proposal.get("proposal_id")
    if not isinstance(proposal_id, str):
        slot["status"] = "failed"
        slot["failure"] = {"type": "MalformedSuggestion", "message": "proposal ID is unavailable"}
        return slot, campaigns.get_campaign(OWNER, campaign["campaign_id"]), True
    slot["proposal_available"] = True
    slot["candidate"] = proposal.get("candidate")
    slot["proposal"] = campaigns.get_campaign_proposal(OWNER, proposal_id)
    proposal = slot["proposal"]
    slot["duplicate_of_prior_proposals"] = _duplicate_matches(prior_slots, proposal["candidate"])
    expected_matches = slot["duplicate_of_prior_proposals"]
    repetition = proposal.get("repetition")
    expected_repetition = {
        "policy": case["duplicate_policy"],
        "is_repeat": bool(expected_matches),
        "first_proposal_id": None if not expected_matches else expected_matches[0]["proposal_id"],
        "matching_prior_proposals": expected_matches,
    }
    if repetition != expected_repetition:
        slot["status"] = "policy_violation"
        slot["failure"] = {
            "type": "RepetitionEvidenceMismatch",
            "message": "proposal repetition evidence differs from the admitted prior campaign rows",
        }
        return slot, campaigns.get_campaign(OWNER, campaign["campaign_id"]), True
    if expected_matches and case["duplicate_policy"] == "reject":
        slot["status"] = "policy_violation"
        slot["failure"] = {
            "type": "RejectPolicyAdmittedDuplicate",
            "message": "a duplicate proposal was admitted under reject policy",
        }
        return slot, campaigns.get_campaign(OWNER, campaign["campaign_id"]), True
    attempt = _attempt_record_for_ordinal(OWNER, campaign["campaign_id"], ordinal)
    slot["suggestion_attempt"] = attempt
    if attempt["state"] != "accepted" or attempt["outcome"].get("proposal_id") != proposal_id:
        slot["status"] = "failed"
        slot["failure"] = {
            "type": "AttemptProposalMismatch",
            "message": "accepted suggestion attempt does not bind the returned proposal",
        }
        return slot, campaigns.get_campaign(OWNER, campaign["campaign_id"]), True
    expected_training = _expected_training(prior_slots)
    actual_training = attempt.get("context", {}).get("training", {}).get("records")
    if actual_training != expected_training:
        slot["status"] = "failed"
        slot["failure"] = {
            "type": "TrainingPrefixMismatch",
            "message": "BO request training is not the exact ordered consumed prefix",
        }
        return slot, campaigns.get_campaign(OWNER, campaign["campaign_id"]), True

    counts_before_replay = _resource_counts(OWNER, campaign["campaign_id"])
    replay_started = time.perf_counter()
    replay = campaigns.suggest_campaign(
        OWNER, campaign["campaign_id"], expected_revision, request_key
    )
    replay_elapsed = time.perf_counter() - replay_started
    counts_after_replay = _resource_counts(OWNER, campaign["campaign_id"])
    replay_same = canonical(replay) == canonical(suggestion)
    slot["suggestion_events"].append(
        {
            "kind": "suggestion_receipt_replay",
            "request": {
                "campaign_id": campaign["campaign_id"],
                "expected_revision": expected_revision,
                "idempotency_key": request_key,
            },
            "response_sha256": sha256(replay),
            "same_response": replay_same,
            "resource_counts_before": counts_before_replay,
            "resource_counts_after": counts_after_replay,
            "resource_counts_unchanged": counts_before_replay == counts_after_replay,
            "elapsed_seconds": replay_elapsed
            if math.isfinite(replay_elapsed) and replay_elapsed >= 0
            else None,
        }
    )
    if not replay_same or counts_before_replay != counts_after_replay:
        slot["status"] = "failed"
        slot["failure"] = {
            "type": "SuggestionReceiptMismatch",
            "message": "same-key suggestion replay changed evidence or durable resources",
        }
        return slot, campaigns.get_campaign(OWNER, campaign["campaign_id"]), True

    task_record = suggestion["campaign"]["task"]
    producer = task_record["producer"]
    contract = task_record["contract"]
    candidate = proposal["candidate"]
    method = proposal["method"]
    slot["status"] = "proposal_admitted"
    slot["repetition"] = repetition
    slot["duplicate_of_prior_proposals"] = expected_matches
    conditions = [
        experiments.Condition(
            name="strong_solvent_fraction",
            value=candidate["strong_solvent_fraction"],
            unit="1",
        ),
        experiments.Condition(
            name="acquisition_duration_min",
            value=candidate["acquisition_duration_min"],
            unit="min",
        ),
    ]
    try:
        experiment = _timed_step(
            slot,
            "create_experiment",
            lambda: experiments.create_experiment(
                OWNER,
                experiments.ExperimentSpec(
                    title=f"Suggestion sequence {case['case_id']} evaluation {ordinal + 1}",
                    source="predeclared_synthetic_suggestion_sequence",
                    proposal_reference=proposal_id,
                    executable_method_id=method["method_id"],
                    observation_contract_id=contract["contract_id"],
                    observation_producer_id=producer["producer_id"],
                    intended_conditions=conditions,
                    description=(
                        "Fresh latent simulator execution under a fixed synthetic retention model "
                        "and Gaussian width assumption; no instrument was run."
                    ),
                ),
                _registry_key(
                    declaration_sha256, "campaign", case["case_id"], "experiment", ordinal
                ),
            ),
        )
        slot["experiment"] = experiment
        attempt_response = _timed_step(
            slot,
            "record_simulator_attempt",
            lambda: experiments.record_attempt(
                OWNER,
                experiment["experiment_id"],
                experiments.AttemptSpec(
                    status="completed",
                    evaluator_kind="simulator",
                    evaluator_reference="measure-service ideal-retention and Gaussian-centroid simulator",
                    evaluator_revision="suggestion-sequence-qualification-v1",
                    fidelity=1.0,
                    actual_conditions=conditions,
                    executable_method_id=method["method_id"],
                ),
                _registry_key(declaration_sha256, "campaign", case["case_id"], "attempt", ordinal),
            ),
        )
        slot["attempt"] = attempt_response
        prediction = _timed_step(
            slot,
            "predict_retention",
            lambda: retention.predict_retention(
                OWNER,
                producer["retention_model_id"],
                method["method_id"],
                _registry_key(
                    declaration_sha256, "campaign", case["case_id"], "retention", ordinal
                ),
            ),
        )
        slot["retention_prediction"] = prediction
        gaussian = _timed_step(
            slot,
            "record_gaussian_observation",
            lambda: gaussian_producer.record_gaussian_observation(
                OWNER,
                producer["producer_id"],
                prediction["prediction_id"],
                attempt_response["attempt_id"],
                expected_attempt_revision=attempt_response["revision"],
                key=_registry_key(
                    declaration_sha256, "campaign", case["case_id"], "gaussian", ordinal
                ),
            ),
        )
        slot["gaussian_observation"] = gaussian
        observation = _timed_step(
            slot,
            "evaluate_analytical_observation",
            lambda: observations.evaluate_observation(
                OWNER,
                contract["contract_id"],
                attempt_response["attempt_id"],
                expected_attempt_revision=attempt_response["revision"],
                spec=gaussian["evidence_spec"],
                key=_registry_key(
                    declaration_sha256, "campaign", case["case_id"], "assessment", ordinal
                ),
            ),
        )
        slot["observation"] = observation
        snapshot_response = _timed_step(
            slot,
            "create_observation_snapshot",
            lambda: observations.create_observation_snapshot(
                OWNER,
                contract["contract_id"],
                [observation["observation_id"]],
                _registry_key(declaration_sha256, "campaign", case["case_id"], "snapshot", ordinal),
            ),
        )
        snapshot = _timed_step(
            slot,
            "read_observation_snapshot",
            lambda: observations.get_observation_snapshot(OWNER, snapshot_response["snapshot_id"]),
        )
        slot["snapshot"] = snapshot
        consumption_request = {
            "campaign_id": campaign["campaign_id"],
            "proposal_id": proposal_id,
            "snapshot_id": snapshot["snapshot_id"],
            "expected_revision": suggestion["revision"],
            "idempotency_key": _registry_key(
                declaration_sha256, "campaign", case["case_id"], "consumption", ordinal
            ),
        }
        consumed = _timed_step(
            slot,
            "consume_campaign_snapshot",
            lambda: campaigns.consume_campaign_snapshot(
                OWNER,
                campaign["campaign_id"],
                proposal_id,
                snapshot["snapshot_id"],
                expected_revision=suggestion["revision"],
                key=consumption_request["idempotency_key"],
            ),
        )
        slot["consumption_request"] = consumption_request
        slot["consumption_response"] = consumed
        slot["consumption"] = consumed["consumption"]
        consume_counts = _resource_counts(OWNER, campaign["campaign_id"])
        consumption_replay_started = time.perf_counter()
        consumption_replay = campaigns.consume_campaign_snapshot(
            OWNER,
            campaign["campaign_id"],
            proposal_id,
            snapshot["snapshot_id"],
            expected_revision=suggestion["revision"],
            key=consumption_request["idempotency_key"],
        )
        consumption_replay_elapsed = time.perf_counter() - consumption_replay_started
        after_consumption_replay_counts = _resource_counts(OWNER, campaign["campaign_id"])
        replay_matches = canonical(consumption_replay) == canonical(consumed)
        slot["consumption_replay"] = {
            "response_sha256": sha256(consumption_replay),
            "same_response": replay_matches,
            "resource_counts_before": consume_counts,
            "resource_counts_after": after_consumption_replay_counts,
            "resource_counts_unchanged": consume_counts == after_consumption_replay_counts,
            "elapsed_seconds": (
                consumption_replay_elapsed
                if math.isfinite(consumption_replay_elapsed) and consumption_replay_elapsed >= 0
                else None
            ),
        }
        slot["campaign_response"] = consumed["campaign"]
        slot["oracle_comparison"] = _oracle_comparison(
            oracle_module,
            document,
            fixture,
            candidate,
            gaussian,
            slot["consumption"],
            proposal,
        )
        slot["oracle"] = slot["oracle_comparison"]["oracle"]
        if not replay_matches or consume_counts != after_consumption_replay_counts:
            slot["status"] = "failed"
            slot["failure"] = {
                "type": "ConsumptionReceiptMismatch",
                "message": "same-key consumption replay changed evidence or durable resources",
            }
            return slot, campaigns.get_campaign(OWNER, campaign["campaign_id"]), True
        if not slot["oracle_comparison"]["passed"]:
            slot["status"] = "numerical_mismatch"
            slot["failure"] = {
                "type": "IndependentOracleMismatch",
                "message": "service consumption differs from the fixed independent reference",
            }
            return slot, campaigns.get_campaign(OWNER, campaign["campaign_id"]), True
        if repetition.get("is_repeat"):
            previous_ids = {
                slot_id
                for prior in prior_slots
                if prior.get("status") == "consumed"
                for slot_id in _execution_identity_values(prior)
            }
            current_identity = _execution_identity_map(slot)
            required_identity_names = {
                "proposal_id",
                "method_id",
                "experiment_id",
                "attempt_id",
                "prediction_id",
                "gaussian_run_id",
                "observation_id",
                "snapshot_id",
                "consumption_id",
            }
            current_ids = set(current_identity.values())
            if set(current_identity) != required_identity_names or any(
                value in previous_ids for value in current_ids
            ):
                slot["status"] = "policy_violation"
                slot["failure"] = {
                    "type": "ReplicateIdentityReuse",
                    "message": "replicated candidate lacks or reuses a fresh execution identity",
                }
                return slot, campaigns.get_campaign(OWNER, campaign["campaign_id"]), True
        slot["status"] = "consumed"
        return slot, consumed["campaign"], False
    except Exception as error:
        slot["status"] = "partial_failure"
        slot["failure"] = _error_record(error)
        return slot, campaigns.get_campaign(OWNER, campaign["campaign_id"]), True


def _execution_identity_values(slot: dict[str, Any]) -> set[str]:
    return set(_execution_identity_map(slot).values())


def _execution_identity_map(slot: dict[str, Any]) -> dict[str, str]:
    definitions = (
        ("proposal_id", "proposal", "proposal_id"),
        ("method_id", "proposal", "method.method_id"),
        ("experiment_id", "experiment", "experiment_id"),
        ("attempt_id", "attempt", "attempt_id"),
        ("prediction_id", "retention_prediction", "prediction_id"),
        ("gaussian_run_id", "gaussian_observation", "run_id"),
        ("observation_id", "observation", "observation_id"),
        ("snapshot_id", "snapshot", "snapshot_id"),
        ("consumption_id", "consumption", "consumption_id"),
    )
    result = {}
    for result_name, slot_name, path in definitions:
        value = slot.get(slot_name)
        if path == "method.method_id":
            value = value.get("method") if isinstance(value, dict) else None
            value = value.get("method_id") if isinstance(value, dict) else None
        else:
            value = value.get(path) if isinstance(value, dict) else None
        if isinstance(value, str) and value:
            result[result_name] = value
    return result


def _append_worker_journal(path: Path, record: dict[str, Any]) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("durable worker journal must be a regular file")
    encoded = (canonical(record) + "\n").encode("utf-8")
    if len(encoded) > _MAX_WORKER_OUTPUT_BYTES:
        raise ValueError("one durable worker result exceeds its bounded journal limit")
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
    except FileExistsError:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        created = False
    with os.fdopen(descriptor, "ab") as destination:
        destination.write(encoded)
        destination.flush()
        os.fsync(destination.fileno())
    if created:
        _fsync_directory(path.parent)


def _strict_json_bytes(raw: bytes, *, label: str) -> Any:
    if len(raw) > _MAX_WORKER_OUTPUT_BYTES:
        raise ValueError(f"{label} exceeds its bounded protocol size")
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is not valid strict JSON") from error


def _replay_restart_receipts(campaign_id: str, prior_slot: dict[str, Any]):
    events = prior_slot.get("suggestion_events")
    original_request_event = next(
        (
            item
            for item in (events if isinstance(events, list) else [])
            if isinstance(item, dict) and item.get("kind") == "suggestion_request"
        ),
        None,
    )
    suggestion_request = (
        original_request_event.get("request") if isinstance(original_request_event, dict) else None
    )
    suggestion_response = (
        original_request_event.get("response") if isinstance(original_request_event, dict) else None
    )
    if (
        not isinstance(suggestion_request, dict)
        or not isinstance(suggestion_request.get("expected_revision"), int)
        or not isinstance(suggestion_request.get("idempotency_key"), str)
        or not isinstance(suggestion_response, dict)
    ):
        raise ValueError("restart prefix lacks the original accepted suggestion receipt")
    attempt = prior_slot.get("suggestion_attempt")
    attempt_id = attempt.get("suggestion_attempt_id") if isinstance(attempt, dict) else None
    if not isinstance(attempt_id, str):
        raise ValueError("restart prefix lacks its immutable suggestion-attempt identity")
    consumption_request = prior_slot.get("consumption_request")
    consumption_response = prior_slot.get("consumption_response")
    if (
        not isinstance(consumption_request, dict)
        or not isinstance(consumption_request.get("proposal_id"), str)
        or not isinstance(consumption_request.get("snapshot_id"), str)
        or not isinstance(consumption_request.get("expected_revision"), int)
        or not isinstance(consumption_request.get("idempotency_key"), str)
        or not isinstance(consumption_response, dict)
    ):
        raise ValueError("restart prefix lacks its original consumed-evaluation receipt")

    before_campaign = campaigns.get_campaign(OWNER, campaign_id)
    before_counts = _resource_counts(OWNER, campaign_id)
    suggestion_replay = campaigns.suggest_campaign(
        OWNER,
        campaign_id,
        suggestion_request["expected_revision"],
        suggestion_request["idempotency_key"],
    )
    after_suggestion_counts = _resource_counts(OWNER, campaign_id)
    attempt_replay = campaigns.get_campaign_suggestion_attempt(OWNER, attempt_id)
    consumption_replay = campaigns.consume_campaign_snapshot(
        OWNER,
        campaign_id,
        consumption_request["proposal_id"],
        consumption_request["snapshot_id"],
        expected_revision=consumption_request["expected_revision"],
        key=consumption_request["idempotency_key"],
    )
    after_counts = _resource_counts(OWNER, campaign_id)
    after_campaign = campaigns.get_campaign(OWNER, campaign_id)
    suggestion_same = canonical(suggestion_replay) == canonical(suggestion_response)
    attempt_same = canonical(attempt_replay) == canonical(attempt)
    consumption_same = canonical(consumption_replay) == canonical(consumption_response)
    resources_unchanged = before_counts == after_suggestion_counts == after_counts
    campaign_unchanged = (
        before_campaign["revision"] == after_campaign["revision"]
        and before_campaign["consumed_evaluations"] == after_campaign["consumed_evaluations"]
        and before_campaign["state"] == after_campaign["state"]
    )
    return {
        "profile": "measure-service-suggestion-restart-receipt-replay/1",
        "preceding_ordinal": prior_slot["ordinal"],
        "suggestion_request": suggestion_request,
        "suggestion_response": suggestion_replay,
        "suggestion_response_sha256": sha256(suggestion_replay),
        "suggestion_same_as_original": suggestion_same,
        "attempt_record": attempt_replay,
        "attempt_record_sha256": sha256(attempt_replay),
        "attempt_record_same_as_original": attempt_same,
        "consumption_request": consumption_request,
        "consumption_response": consumption_replay,
        "consumption_response_sha256": sha256(consumption_replay),
        "consumption_same_as_original": consumption_same,
        "resource_counts_before": before_counts,
        "resource_counts_after_suggestion_replay": after_suggestion_counts,
        "resource_counts_after_all_replays": after_counts,
        "resources_unchanged": resources_unchanged,
        "campaign_revision_and_budget_unchanged": campaign_unchanged,
        "passed": (
            suggestion_same
            and attempt_same
            and consumption_same
            and resources_unchanged
            and campaign_unchanged
        ),
    }


def _worker_main() -> int:
    worker_started_at = _utc_now()
    restart_replay = None
    raw = sys.stdin.buffer.read(_MAX_WORKER_OUTPUT_BYTES + 1)
    try:
        request = _strict_json_bytes(raw, label="durable worker request")
        if not isinstance(request, dict) or request.get("profile") != _WORKER_PROFILE:
            raise ValueError("unsupported durable worker request profile")
        required = {
            "profile",
            "declaration_path",
            "declaration_sha256",
            "case_id",
            "campaign_id",
            "protocol_id",
            "ordinals",
            "prior_slots",
            "manifest_module_sha256",
            "manifest_bo_sidecar",
            "manifest_bo_engine_source_tree_sha256",
            "journal_path",
            "first_call_marker_path",
            "precall_manifest_sha256",
        }
        if set(request) != required:
            raise ValueError("durable worker request fields differ from the frozen schema")
        inputs = _load_fixed_inputs(request["declaration_path"])
        if inputs["declaration_sha256"] != request["declaration_sha256"]:
            raise ValueError("durable worker declaration identity differs from the parent")
        _require_replication_profile()
        module_hashes = _host_module_hashes()
        if module_hashes != request["manifest_module_sha256"]:
            raise ValueError("installed service source changed after the pre-call manifest")
        sidecar = _probe_sidecar_identity()
        if (
            sidecar.get("status") != "available"
            or sidecar != request["manifest_bo_sidecar"]
            or sidecar.get("bo_engine_source_tree_sha256")
            != request["manifest_bo_engine_source_tree_sha256"]
        ):
            raise ValueError("installed BO-MCP sidecar changed after the pre-call manifest")
        if type(request["ordinals"]) is not list or not request["ordinals"]:
            raise ValueError("durable worker ordinals must be a nonempty array")
        ordinals = request["ordinals"]
        if any(type(item) is not int or item < 0 or item >= EVALUATION_BUDGET for item in ordinals):
            raise ValueError("durable worker ordinal is outside the planned budget")
        if ordinals != list(range(ordinals[0], ordinals[0] + len(ordinals))):
            raise ValueError("durable worker ordinals must be one contiguous chunk")
        case_matches = [
            item
            for item in inputs["document"]["durable_campaigns"]["cases"]
            if item["case_id"] == request["case_id"]
        ]
        if len(case_matches) != 1:
            raise ValueError("durable worker case is not in the verified declaration")
        case = case_matches[0]
        prior_slots = request["prior_slots"]
        if type(prior_slots) is not list or len(prior_slots) != ordinals[0]:
            raise ValueError("durable worker lacks the complete preceding campaign prefix")
        if any(
            not isinstance(item, dict)
            or item.get("ordinal") != index
            or item.get("status") != "consumed"
            for index, item in enumerate(prior_slots)
        ):
            raise ValueError("durable worker prefix is not the exact ordered consumed history")
        if ordinals[0] > 0:
            restart_replay = _replay_restart_receipts(request["campaign_id"], prior_slots[-1])
            if restart_replay["passed"] is not True:
                raise ValueError(
                    "preceding suggestion or consumption receipt changed after restart"
                )
        binding = next(
            item for item in inputs["document"]["fixtures"] if item["task_id"] == case["task_id"]
        )
        journal_path = Path(request["journal_path"])
        campaign = campaigns.get_campaign(OWNER, request["campaign_id"])
        if campaign.get("protocol_id") != request["protocol_id"]:
            raise ValueError("durable worker campaign changed its registered protocol")
        oracle_module = _load_decimal_oracle(
            inputs["oracle_source_path"], inputs["oracle_source_raw"]
        )
        stored_protocol = campaign_protocol_registry.get_campaign_protocol(
            OWNER, request["protocol_id"]
        )
        protocol_spec = campaign_protocol.parse_campaign_protocol(stored_protocol["spec"])
        current_prior_slots = list(prior_slots)
        returned_slots = []
        stopped = False
        for ordinal in ordinals:
            slot, campaign, stopped = _process_slot(
                document=inputs["document"],
                case=case,
                campaign=campaign,
                protocol=protocol_spec,
                fixture=binding,
                oracle_module=oracle_module,
                declaration_sha256=inputs["declaration_sha256"],
                ordinal=ordinal,
                prior_slots=current_prior_slots,
                first_call_marker=Path(request["first_call_marker_path"]),
                precall_manifest_sha256=request["precall_manifest_sha256"],
                bo_engine_source_tree_sha256=sidecar["bo_engine_source_tree_sha256"],
            )
            slot["iteration"] = ordinal
            returned_slots.append(slot)
            _append_worker_journal(
                journal_path,
                {
                    "profile": "measure-service-suggestion-sequence-worker-slot/1",
                    "case_id": case["case_id"],
                    "ordinal": ordinal,
                    "worker_pid": os.getpid(),
                    "recorded_at": _utc_now(),
                    "slot": slot,
                    "campaign": campaign,
                    "stopped_after_slot": stopped,
                },
            )
            if slot["status"] == "consumed":
                current_prior_slots.append(slot)
            if stopped:
                break
        response = {
            "profile": _WORKER_PROFILE,
            "status": "returned",
            "worker_pid": os.getpid(),
            "started_at": worker_started_at,
            "case_id": case["case_id"],
            "campaign_id": campaign["campaign_id"],
            "ordinals_requested": ordinals,
            "slots": returned_slots,
            "campaign": campaign,
            "stopped": stopped,
            "restart_receipt_replay": restart_replay,
        }
        encoded = (
            json.dumps(response, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n"
        ).encode()
        if len(encoded) > _MAX_WORKER_OUTPUT_BYTES:
            raise ValueError("durable worker response exceeds its bounded output size")
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        return 0
    except BaseException as error:
        failure = _error_record(error)
        response = {
            "profile": _WORKER_PROFILE,
            "status": "error",
            "worker_pid": os.getpid(),
            "failure": failure,
            "restart_receipt_replay": restart_replay,
        }
        sys.stdout.write(json.dumps(response, sort_keys=True, allow_nan=False) + "\n")
        sys.stdout.flush()
        return 2


def _invoke_worker(
    *,
    stage: Path,
    data_path: Path,
    declaration_path: Path,
    declaration_sha256: str,
    case: dict[str, Any],
    campaign_id: str,
    protocol_id: str,
    ordinals: tuple[int, ...],
    prior_slots: list[dict[str, Any]],
    manifest: dict[str, Any],
):
    request = {
        "profile": _WORKER_PROFILE,
        "declaration_path": str(declaration_path),
        "declaration_sha256": declaration_sha256,
        "case_id": case["case_id"],
        "campaign_id": campaign_id,
        "protocol_id": protocol_id,
        "ordinals": list(ordinals),
        "prior_slots": prior_slots,
        "manifest_module_sha256": manifest["host"]["module_sha256"],
        "manifest_bo_sidecar": manifest["bo_sidecar"],
        "manifest_bo_engine_source_tree_sha256": manifest["bo_sidecar"][
            "bo_engine_source_tree_sha256"
        ],
        "journal_path": str(stage / "durable_worker_journal.jsonl"),
        "first_call_marker_path": str(stage / _FIRST_CALL_MARKER),
        "precall_manifest_sha256": file_sha256(stage / _PRECALL_MARKER),
    }
    request_bytes = (canonical(request) + "\n").encode("utf-8")
    if len(request_bytes) > _MAX_WORKER_OUTPUT_BYTES:
        raise ValueError("durable worker request exceeds its bounded input size")
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["MEASURE_MCP_DATA_DIR"] = str(data_path)
    started_at = _utc_now()
    started = time.perf_counter()
    timed_out = False
    output_limit_exceeded = False
    process_group_id = None
    with (
        tempfile.TemporaryFile() as input_spool,
        tempfile.TemporaryFile() as stdout_spool,
        tempfile.TemporaryFile() as stderr_spool,
    ):
        input_spool.write(request_bytes)
        input_spool.seek(0)
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "measure_service.benchmarks.suggestion_sequences",
                    "--worker",
                ],
                stdin=input_spool,
                stdout=stdout_spool,
                stderr=stderr_spool,
                env=environment,
                start_new_session=os.name == "posix",
            )
            if os.name == "posix":
                process_group_id = process.pid
        except OSError as error:
            return {
                "status": "failed",
                "pid": None,
                "started_at": started_at,
                "finished_at": _utc_now(),
                "host_elapsed_seconds": None,
                "return_code": None,
                "stderr_excerpt": "",
                "stderr_bytes": 0,
                "stdout_bytes": 0,
                "output_limit_exceeded": False,
                "parse_failure": _error_record(error),
                "response": None,
            }
        try:
            while process.poll() is None:
                stdout_size = os.fstat(stdout_spool.fileno()).st_size
                stderr_size = os.fstat(stderr_spool.fileno()).st_size
                if stdout_size > _MAX_WORKER_OUTPUT_BYTES or stderr_size > _MAX_WORKER_STDERR_BYTES:
                    output_limit_exceeded = True
                    break
                if time.perf_counter() - started >= 300.0:
                    timed_out = True
                    break
                time.sleep(0.02)
            if timed_out or output_limit_exceeded:
                _terminate_worker_process(process, process_group_id=process_group_id)
            else:
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _terminate_worker_process(process, process_group_id=process_group_id)
            stdout_size = os.fstat(stdout_spool.fileno()).st_size
            stderr_size = os.fstat(stderr_spool.fileno()).st_size
            if stdout_size > _MAX_WORKER_OUTPUT_BYTES or stderr_size > _MAX_WORKER_STDERR_BYTES:
                output_limit_exceeded = True
            stdout_spool.seek(0)
            stdout = stdout_spool.read(_MAX_WORKER_OUTPUT_BYTES + 1)
            stderr_spool.seek(0)
            stderr = stderr_spool.read(_MAX_WORKER_STDERR_BYTES + 1)
        except BaseException:
            _terminate_worker_process(process, process_group_id=process_group_id)
            raise
    elapsed = time.perf_counter() - started
    result = None
    parse_failure = None
    if (
        not timed_out
        and not output_limit_exceeded
        and stdout
        and len(stdout) <= _MAX_WORKER_OUTPUT_BYTES
    ):
        try:
            result = _strict_json_bytes(stdout, label="durable worker response")
        except ValueError as error:
            parse_failure = _error_record(error)
    elif not timed_out and not output_limit_exceeded:
        parse_failure = {
            "type": "MissingWorkerResponse",
            "message": "durable worker did not produce a bounded response",
        }
    response = {
        "status": (
            "timed_out"
            if timed_out
            else "output_limit_exceeded"
            if output_limit_exceeded
            else "returned"
            if process.returncode == 0
            else "failed"
        ),
        "pid": process.pid,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "host_elapsed_seconds": elapsed if math.isfinite(elapsed) and elapsed >= 0 else None,
        "return_code": process.returncode,
        "stderr_excerpt": stderr.decode("utf-8", errors="replace")[:1000],
        "stdout_bytes": stdout_size,
        "stderr_bytes": stderr_size,
        "stderr_truncated": stderr_size > 1000,
        "output_limit_exceeded": output_limit_exceeded,
        "parse_failure": parse_failure,
        "response": result,
    }
    return response


def _terminate_worker_process(process: Any, *, process_group_id: int | None) -> None:
    """Terminate and reap a worker, including a sidecar child in its process group."""

    def signal_group(sig: int) -> bool:
        if process_group_id is None:
            return False
        try:
            os.killpg(process_group_id, sig)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return False

    if not signal_group(signal.SIGTERM) and process.poll() is None:
        try:
            process.terminate()
        except (AttributeError, OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # The leader may exit on SIGTERM while an optimizer descendant ignores
        # it. Escalate the private session group even after reaping the leader.
        if not signal_group(signal.SIGKILL) and process.poll() is None:
            try:
                process.kill()
            except (AttributeError, OSError, ProcessLookupError):
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            if not signal_group(signal.SIGKILL):
                process.kill()
        process.wait()


class _JournalRecords(list[dict[str, Any]]):
    def __init__(self):
        super().__init__()
        self.missing = False
        self.missing_error: dict[str, Any] | None = None
        self.tail_error: dict[str, Any] | None = None


def _journal_records(path: Path, case_id: str) -> list[dict[str, Any]]:
    if not path.exists():
        records = _JournalRecords()
        records.missing = True
        records.missing_error = {
            "profile": "measure-service-suggestion-sequence-journal-missing/1",
            "message": "durable worker did not create its expected journal file",
        }
        return records
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > _MAX_WORKER_OUTPUT_BYTES * 64
    ):
        raise ValueError("durable worker journal is invalid or exceeds its declared bound")
    records = _JournalRecords()
    with path.open("rb") as source:
        line_number = 0
        while raw := source.readline():
            line_number += 1
            if not raw.strip():
                continue
            try:
                record = _strict_json_bytes(raw, label="durable worker journal row")
                if (
                    not isinstance(record, dict)
                    or record.get("profile") != "measure-service-suggestion-sequence-worker-slot/1"
                    or not isinstance(record.get("case_id"), str)
                    or not record["case_id"]
                    or type(record.get("ordinal")) is not int
                    or not isinstance(record.get("slot"), dict)
                ):
                    raise ValueError("durable worker journal row failed its identity checks")
            except ValueError as error:
                remainder = source.read()
                if remainder.strip():
                    raise ValueError(
                        f"durable worker journal has an invalid nonfinal row at line {line_number}"
                    ) from error
                records.tail_error = {
                    "profile": "measure-service-suggestion-sequence-journal-tail-error/1",
                    "line_number": line_number,
                    "size_bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "terminated_by_newline": raw.endswith(b"\n"),
                    "error": _error_record(error),
                }
                break
            if record["case_id"] == case_id:
                records.append(record)
    return records


def _retain_journal_tail_error(
    case: dict[str, Any],
    records: list[dict[str, Any]],
    process_record: dict[str, Any] | None = None,
) -> None:
    missing_error = getattr(records, "missing_error", None)
    if missing_error is not None:
        case["worker_journal_error"] = missing_error
        if process_record is not None:
            process_record["journal_error"] = missing_error
    tail_error = getattr(records, "tail_error", None)
    if tail_error is None:
        return
    case["worker_journal_tail_error"] = tail_error
    if process_record is not None:
        process_record["journal_tail_error"] = tail_error


def _attempt_snapshot_for_ordinal(owner: str, campaign_id: str, ordinal: int):
    page = campaigns.list_campaign_suggestion_attempts(owner, campaign_id, limit=20)
    matches = [item for item in page["records"] if item.get("ordinal") == ordinal]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("campaign ledger has duplicate records for a planned ordinal")
    return campaigns.get_campaign_suggestion_attempt(owner, matches[0]["suggestion_attempt_id"])


def _registry_receipt_response(owner: str, key: str):
    with registry_connection() as con:
        row = con.execute(
            "SELECT response FROM registry_receipts WHERE owner=? AND request_key=?",
            (owner, key),
        ).fetchone()
    if row is None:
        return None
    response = _strict_json_bytes(row["response"].encode("utf-8"), label="registry receipt")
    if not isinstance(response, dict):
        raise ValueError("persisted registry receipt response is not an object")
    return response


def _reconcile_durable_case_from_store(
    case: dict[str, Any], declaration_sha256: str
) -> dict[str, Any]:
    """Recover committed source records independently of worker status or journal delivery."""
    campaign_id = case.get("campaign_id")
    if not isinstance(campaign_id, str):
        result = {
            "profile": "measure-service-suggestion-sequence-store-reconciliation/1",
            "status": "unavailable",
            "campaign_id": None,
            "consumed_ordinals": [],
            "completed_simulator_attempts": [],
            "attempted_suggestion_ordinals": [],
            "per_ordinal": [],
            "error": "campaign was not created",
        }
        case["store_reconciliation"] = result
        return result

    try:
        with registry_connection() as con:
            proposal_rows = con.execute(
                "SELECT id, ordinal, payload FROM campaign_proposals "
                "WHERE owner=? AND campaign_id=? ORDER BY ordinal",
                (OWNER, campaign_id),
            ).fetchall()
            consumption_rows = con.execute(
                """SELECT p.ordinal, c.id, c.proposal_id, c.attempt_id,
                          c.attempt_revision, c.observation_id
                   FROM campaign_consumptions c
                   JOIN campaign_proposals p ON p.id=c.proposal_id
                   WHERE c.owner=? AND c.campaign_id=? ORDER BY p.ordinal""",
                (OWNER, campaign_id),
            ).fetchall()
            experiment_rows = con.execute(
                "SELECT id, payload FROM experiments WHERE owner=? ORDER BY created_at, id",
                (OWNER,),
            ).fetchall()
    except Exception as error:
        result = {
            "profile": "measure-service-suggestion-sequence-store-reconciliation/1",
            "status": "unavailable",
            "campaign_id": campaign_id,
            "consumed_ordinals": [],
            "completed_simulator_attempts": [],
            "attempted_suggestion_ordinals": [],
            "proposal_ordinals": [],
            "per_ordinal": [],
            "source_errors": [{"component": "owned_registry_query", **_error_record(error)}],
        }
        case["store_reconciliation"] = result
        return result

    proposals_by_ordinal = {}
    for row in proposal_rows:
        ordinal = row["ordinal"]
        if type(ordinal) is int and 0 <= ordinal < EVALUATION_BUDGET:
            proposals_by_ordinal[ordinal] = {
                "proposal_id": row["id"],
                "proposal": _strict_json_bytes(
                    row["payload"].encode("utf-8"), label="campaign proposal payload"
                ),
            }
    consumptions_by_ordinal = {}
    verified_consumptions = {}
    source_errors = []
    for row in consumption_rows:
        ordinal = next(
            (
                key
                for key, proposal in proposals_by_ordinal.items()
                if proposal["proposal_id"] == row["proposal_id"]
            ),
            None,
        )
        if ordinal is None:
            continue
        consumptions_by_ordinal[ordinal] = {
            "consumption_id": row["id"],
            "proposal_id": row["proposal_id"],
            "attempt_id": row["attempt_id"],
            "attempt_revision": row["attempt_revision"],
            "observation_id": row["observation_id"],
        }
        try:
            verified_consumptions[ordinal] = campaigns.get_campaign_consumption(OWNER, row["id"])
        except Exception as error:
            verified_consumptions[ordinal] = {"verification_error": _error_record(error)}
            source_errors.append(
                {"ordinal": ordinal, "component": "campaign_consumption", **_error_record(error)}
            )

    experiments_by_proposal = {}
    for row in experiment_rows:
        try:
            payload = _strict_json_bytes(row["payload"].encode("utf-8"), label="experiment payload")
        except ValueError:
            continue
        proposal_reference = (
            payload.get("proposal_reference") if isinstance(payload, dict) else None
        )
        if isinstance(proposal_reference, str):
            experiments_by_proposal[proposal_reference] = row["id"]

    component_names = (
        "proposal",
        "experiment",
        "attempt",
        "retention",
        "gaussian",
        "assessment",
        "snapshot",
        "consumption",
    )
    attempted_ordinals = set()
    try:
        ledger = campaigns.list_campaign_suggestion_attempts(OWNER, campaign_id, limit=20)
        attempted_ordinals = {
            item["ordinal"]
            for item in ledger.get("records", [])
            if isinstance(item, dict)
            and type(item.get("ordinal")) is int
            and 0 <= item["ordinal"] < EVALUATION_BUDGET
        }
    except Exception as error:
        ledger = None
        source_errors.append({"component": "suggestion_ledger", **_error_record(error)})

    completed_attempt_records = []
    per_ordinal = []
    for ordinal in range(EVALUATION_BUDGET):
        keys = {
            name: _registry_key(declaration_sha256, "campaign", case["case_id"], name, ordinal)
            for name in component_names
        }
        receipts = {}
        for name, key in keys.items():
            try:
                receipts[name] = _registry_receipt_response(OWNER, key)
            except Exception as error:
                receipts[name] = None
                receipt_error = _error_record(error)
                source_errors.append(
                    {"ordinal": ordinal, "component": f"receipt:{name}", **receipt_error}
                )
                break
        else:
            receipt_error = None

        proposal_row = proposals_by_ordinal.get(ordinal)
        suggestion = receipts.get("proposal")
        proposal_id = proposal_row.get("proposal_id") if isinstance(proposal_row, dict) else None
        if proposal_id is None and isinstance(suggestion, dict):
            raw_proposal = suggestion.get("proposal")
            proposal_id = (
                raw_proposal.get("proposal_id") if isinstance(raw_proposal, dict) else None
            )
        proposal = None
        if isinstance(proposal_id, str):
            try:
                proposal = campaigns.get_campaign_proposal(OWNER, proposal_id)
            except Exception as error:
                source_errors.append(
                    {"ordinal": ordinal, "component": "campaign_proposal", **_error_record(error)}
                )
                proposal = None

        experiment = receipts.get("experiment")
        experiment_id = experiment.get("experiment_id") if isinstance(experiment, dict) else None
        if not isinstance(experiment_id, str) and isinstance(proposal_id, str):
            experiment_id = experiments_by_proposal.get(proposal_id)
        attempt = receipts.get("attempt")
        if isinstance(experiment_id, str):
            try:
                experiment = experiments.get_experiment(OWNER, experiment_id)
            except Exception as error:
                source_errors.append(
                    {"ordinal": ordinal, "component": "experiment", **_error_record(error)}
                )
                pass
        attempt_records = []
        if isinstance(experiment_id, str):
            with registry_connection() as con:
                attempt_rows = con.execute(
                    """SELECT a.id, a.latest_revision FROM experiment_attempts a
                       WHERE a.owner=? AND a.experiment_id=? ORDER BY a.created_at, a.id""",
                    (OWNER, experiment_id),
                ).fetchall()
            for attempt_row in attempt_rows:
                try:
                    record = experiments.get_attempt(OWNER, attempt_row["id"])
                except Exception as error:
                    source_errors.append(
                        {"ordinal": ordinal, "component": "attempt", **_error_record(error)}
                    )
                    continue
                attempt_records.append(record)
                if _slot_has_completed_simulator_attempt({"attempt": record}):
                    completed_attempt_records.append(
                        {
                            "ordinal": ordinal,
                            "attempt_id": record["attempt_id"],
                            "experiment_id": experiment_id,
                            "revision": record["revision"],
                            "evaluator_kind": record["spec"].get("evaluator_kind"),
                            "fidelity": record["spec"].get("fidelity"),
                        }
                    )
            if attempt_records:
                attempt = attempt_records[-1]

        slot = case["slots"][ordinal]
        recovered_values = {
            "proposal": proposal,
            "experiment": experiment,
            "attempt": attempt,
        }
        for field, value in recovered_values.items():
            if value is not None and slot.get(field) is None:
                slot[field] = value
        if isinstance(proposal, dict):
            slot["proposal_available"] = True
            slot["candidate"] = proposal.get("candidate")
            slot["repetition"] = proposal.get("repetition")
            if isinstance(proposal.get("candidate"), dict):
                slot["duplicate_of_prior_proposals"] = _duplicate_matches(
                    case["slots"][:ordinal], proposal["candidate"]
                )
        retention_response = receipts.get("retention")
        retention_id = (
            retention_response.get("prediction_id")
            if isinstance(retention_response, dict)
            else None
        )
        if isinstance(retention_id, str):
            try:
                retention_response = retention.get_retention_prediction(OWNER, retention_id)
            except Exception as error:
                source_errors.append(
                    {
                        "ordinal": ordinal,
                        "component": "retention_prediction",
                        **_error_record(error),
                    }
                )
                pass
        gaussian_response = receipts.get("gaussian")
        gaussian_id = (
            gaussian_response.get("run_id") if isinstance(gaussian_response, dict) else None
        )
        if isinstance(gaussian_id, str):
            try:
                gaussian_response = gaussian_producer.get_gaussian_observation(OWNER, gaussian_id)
            except Exception as error:
                source_errors.append(
                    {
                        "ordinal": ordinal,
                        "component": "gaussian_observation",
                        **_error_record(error),
                    }
                )
                pass
        observation_response = receipts.get("assessment")
        observation_id = (
            observation_response.get("observation_id")
            if isinstance(observation_response, dict)
            else None
        )
        if isinstance(observation_id, str):
            try:
                observation_response = observations.get_observation(OWNER, observation_id)
            except Exception as error:
                source_errors.append(
                    {
                        "ordinal": ordinal,
                        "component": "analytical_observation",
                        **_error_record(error),
                    }
                )
                pass
        recovered_source_records = {
            "retention_prediction": retention_response,
            "gaussian_observation": gaussian_response,
            "observation": observation_response,
        }
        for field, value in recovered_source_records.items():
            if value is not None and slot.get(field) is None:
                slot[field] = value
        snapshot_response = receipts.get("snapshot")
        snapshot_id = (
            snapshot_response.get("snapshot_id") if isinstance(snapshot_response, dict) else None
        )
        if not isinstance(snapshot_id, str):
            consumed_row = consumptions_by_ordinal.get(ordinal)
            if isinstance(consumed_row, dict):
                snapshot_id = verified_consumptions.get(ordinal, {}).get("snapshot_id")
        snapshot = None
        if isinstance(snapshot_id, str):
            try:
                snapshot = observations.get_observation_snapshot(OWNER, snapshot_id)
            except Exception as error:
                source_errors.append(
                    {
                        "ordinal": ordinal,
                        "component": "observation_snapshot",
                        **_error_record(error),
                    }
                )
                pass
        if snapshot is not None and slot.get("snapshot") is None:
            slot["snapshot"] = snapshot
        consumption_response = receipts.get("consumption")
        verified = verified_consumptions.get(ordinal)
        consumption = (
            verified
            if isinstance(verified, dict) and isinstance(verified.get("consumption_id"), str)
            else None
        )
        if isinstance(consumption, dict) and slot.get("consumption") is None:
            slot["consumption"] = consumption
        if isinstance(consumption_response, dict) and slot.get("consumption_response") is None:
            slot["consumption_response"] = consumption_response
        if isinstance(consumption_response, dict) and slot.get("campaign_response") is None:
            slot["campaign_response"] = consumption_response.get("campaign")
        reconciliation = {
            "receipt_presence": {
                name: isinstance(receipts.get(name), dict) for name in component_names
            },
            "receipt_response_sha256": {
                name: sha256(response)
                for name, response in receipts.items()
                if isinstance(response, dict)
            },
            "receipt_read_error": receipt_error,
            "proposal_id": proposal_id,
            "experiment_id": experiment_id,
            "attempt_ids": [item.get("attempt_id") for item in attempt_records],
            "completed_simulator_attempt_ids": [
                item["attempt_id"]
                for item in completed_attempt_records
                if item["ordinal"] == ordinal
            ],
            "consumption_id": consumptions_by_ordinal.get(ordinal, {}).get("consumption_id"),
            "observation_id": consumptions_by_ordinal.get(ordinal, {}).get("observation_id"),
            "source_identities": {
                "proposal_id": proposal_id,
                "method_id": (
                    proposal.get("method", {}).get("method_id")
                    if isinstance(proposal, dict) and isinstance(proposal.get("method"), dict)
                    else None
                ),
                "experiment_id": experiment_id,
                "attempt_ids": [item.get("attempt_id") for item in attempt_records],
                "retention_prediction_id": (
                    retention_response.get("prediction_id")
                    if isinstance(retention_response, dict)
                    else None
                ),
                "gaussian_run_id": (
                    gaussian_response.get("run_id") if isinstance(gaussian_response, dict) else None
                ),
                "analytical_observation_id": (
                    observation_response.get("observation_id")
                    if isinstance(observation_response, dict)
                    else None
                ),
                "snapshot_id": snapshot_id,
                "consumption_id": consumptions_by_ordinal.get(ordinal, {}).get("consumption_id"),
            },
            "consumption_public_getter_verified": (
                isinstance(verified_consumptions.get(ordinal), dict)
                and isinstance(verified_consumptions[ordinal].get("consumption_id"), str)
            ),
            "suggestion_ledger_present": ordinal in attempted_ordinals,
            "timing_evidence": {
                step: next(
                    (
                        item.get("elapsed_seconds")
                        for item in slot.get("step_timings", [])
                        if isinstance(item, dict) and item.get("step") == step
                    ),
                    None,
                )
                for step in (
                    "suggest_campaign",
                    "create_experiment",
                    "record_simulator_attempt",
                    "predict_retention",
                    "record_gaussian_observation",
                    "evaluate_analytical_observation",
                    "create_observation_snapshot",
                    "consume_campaign_snapshot",
                )
            },
        }
        if case.get("campaign_id") and slot.get("reconciliation"):
            prior = slot["reconciliation"]
            if canonical(prior) != canonical(reconciliation):
                slot["reconciliation_conflict"] = {
                    "previous_sha256": sha256(prior),
                    "reconciled_sha256": sha256(reconciliation),
                }
        slot["reconciliation"] = reconciliation
        if isinstance(attempt, dict) and slot.get("attempt") is None:
            slot["attempt"] = attempt
        per_ordinal.append(
            {
                "ordinal": ordinal,
                "proposal_id": proposal_id,
                "experiment_id": experiment_id,
                "attempt_ids": reconciliation["attempt_ids"],
                "completed_simulator_attempt_ids": reconciliation[
                    "completed_simulator_attempt_ids"
                ],
                "consumption_id": reconciliation["consumption_id"],
                "observation_id": consumptions_by_ordinal.get(ordinal, {}).get("observation_id"),
                "source_identities": reconciliation["source_identities"],
                "receipt_presence": reconciliation["receipt_presence"],
            }
        )

    result = {
        "profile": "measure-service-suggestion-sequence-store-reconciliation/1",
        "status": "reconciled" if not source_errors else "partial",
        "campaign_id": campaign_id,
        "consumed_ordinals": sorted(consumptions_by_ordinal),
        "completed_simulator_attempts": completed_attempt_records,
        "attempted_suggestion_ordinals": sorted(attempted_ordinals),
        "proposal_ordinals": sorted(proposals_by_ordinal),
        "per_ordinal": per_ordinal,
        "source_errors": source_errors,
        "unknown_timing_slots": [
            ordinal
            for ordinal in range(EVALUATION_BUDGET)
            if any(
                value is None
                for value in case["slots"][ordinal]["reconciliation"]["timing_evidence"].values()
            )
        ],
    }
    case["store_reconciliation"] = result
    return result


def _safe_reconcile_durable_case_from_store(
    case: dict[str, Any], declaration_sha256: str
) -> dict[str, Any]:
    try:
        return _reconcile_durable_case_from_store(case, declaration_sha256)
    except Exception as error:
        result = {
            "profile": "measure-service-suggestion-sequence-store-reconciliation/1",
            "status": "unavailable",
            "campaign_id": case.get("campaign_id"),
            "consumed_ordinals": [],
            "completed_simulator_attempts": [],
            "attempted_suggestion_ordinals": [],
            "proposal_ordinals": [],
            "per_ordinal": [],
            "source_errors": [{"component": "reconciliation", **_error_record(error)}],
        }
        case["store_reconciliation"] = result
        return result


def _campaign_failure(case: dict[str, Any], error: BaseException, paths: tuple[Path, ...] = ()):
    record = _error_record(error, private_paths=paths)
    case["status"] = "partial_failure"
    case["failure"] = record
    return record


def _run_durable_case(
    *,
    case: dict[str, Any],
    protocol_entry: dict[str, Any],
    fixture: dict[str, Any],
    declaration_path: Path,
    declaration_sha256: str,
    manifest: dict[str, Any],
    stage: Path,
    data_path: Path,
):
    case["protocol_id"] = protocol_entry["registered"]["protocol_id"]
    case["registered_protocol"] = protocol_entry["stored"]
    try:
        created = campaigns.create_campaign(
            OWNER,
            case["protocol_id"],
            case["task_id"],
            case["seed"],
            case["policy"],
            _registry_key(declaration_sha256, "campaign", case["case_id"], "create"),
        )
        case["campaign_create_response"] = created
        case["campaign_id"] = created["campaign_id"]
        if (
            created.get("evaluation_budget") != case["evaluation_budget"]
            or created.get("revision") != 1
            or created.get("state") != "ready_to_suggest"
        ):
            raise ValueError("new durable campaign differs from its frozen budget or initial state")
    except Exception as error:
        case["failure"] = _campaign_failure(case, error, (data_path, declaration_path.parent))
        _safe_reconcile_durable_case_from_store(case, declaration_sha256)
        return case

    stopped = False
    journal_path = stage / "durable_worker_journal.jsonl"
    journaled_ordinals: dict[int, dict[str, Any]] = {}
    for chunk_index, ordinals in enumerate(_DURABLE_CHUNKS):
        if stopped:
            break
        expected_start = ordinals[0]
        prior_slots = case["slots"][:expected_start]
        if len(prior_slots) != expected_start or any(
            slot.get("status") != "consumed" for slot in prior_slots
        ):
            case["failure"] = {
                "type": "RestartPrefixUnavailable",
                "message": "a fresh worker was not started without the exact consumed prefix",
            }
            case["status"] = "partial_failure"
            break
        try:
            before = campaigns.get_campaign(OWNER, case["campaign_id"])
            before_counts = _resource_counts(OWNER, case["campaign_id"])
            if before["consumed_evaluations"] != expected_start:
                raise ValueError("restart boundary does not match the consumed evaluation count")
            worker = _invoke_worker(
                stage=stage,
                data_path=data_path,
                declaration_path=declaration_path,
                declaration_sha256=declaration_sha256,
                case=case,
                campaign_id=case["campaign_id"],
                protocol_id=case["protocol_id"],
                ordinals=ordinals,
                prior_slots=prior_slots,
                manifest=manifest,
            )
            case["worker_processes"].append(
                {
                    key: worker.get(key)
                    for key in (
                        "status",
                        "pid",
                        "started_at",
                        "finished_at",
                        "host_elapsed_seconds",
                        "return_code",
                        "stderr_excerpt",
                        "stdout_bytes",
                        "stderr_bytes",
                        "stderr_truncated",
                        "output_limit_exceeded",
                        "parse_failure",
                    )
                }
            )
            worker_response = worker.get("response")
            process_record = case["worker_processes"][-1]
            if isinstance(worker_response, dict):
                process_record["restart_receipt_replay"] = worker_response.get(
                    "restart_receipt_replay"
                )
                process_record["worker_failure"] = worker_response.get("failure")
            records = _journal_records(journal_path, case["case_id"])
            _retain_journal_tail_error(case, records, process_record)
            if records.missing:
                raise ValueError("durable worker journal is missing; no slot rows were accepted")
            if records.tail_error is not None:
                raise ValueError(
                    "durable worker journal has an incomplete trailing row; validated prefix retained"
                )
            for record in records:
                ordinal = record["ordinal"]
                if ordinal in journaled_ordinals:
                    if canonical(journaled_ordinals[ordinal]) != canonical(record):
                        raise ValueError("durable worker journal has conflicting ordinal evidence")
                journaled_ordinals[ordinal] = record
            response = worker.get("response")
            if (
                worker.get("status") != "returned"
                or not isinstance(response, dict)
                or response.get("profile") != _WORKER_PROFILE
                or response.get("status") != "returned"
                or response.get("case_id") != case["case_id"]
                or response.get("campaign_id") != case["campaign_id"]
            ):
                raise RuntimeError("fresh durable worker failed before returning its fixed chunk")
            returned = response.get("slots")
            if not isinstance(returned, list) or not returned:
                raise ValueError("durable worker returned no slot records for an attempted chunk")
            returned_ordinals = [item.get("ordinal") for item in returned if isinstance(item, dict)]
            if returned_ordinals != [item["ordinal"] for item in returned]:
                raise ValueError("durable worker returned a malformed slot identity")
            if returned_ordinals != [item for item in ordinals if item <= max(returned_ordinals)]:
                raise ValueError("durable worker omitted a slot before its stop point")
            for slot in returned:
                ordinal = slot["ordinal"]
                journal_record = journaled_ordinals.get(ordinal)
                if (
                    journal_record is None
                    or canonical(journal_record.get("slot")) != canonical(slot)
                    or journal_record.get("campaign", {}).get("revision")
                    != response.get("campaign", {}).get("revision")
                    and ordinal == returned_ordinals[-1]
                ):
                    raise ValueError("durable slot output differs from its fsynced worker journal")
                case["slots"][ordinal] = slot
            after = campaigns.get_campaign(OWNER, case["campaign_id"])
            after_counts = _resource_counts(OWNER, case["campaign_id"])
            prior_worker_pids = {item.get("pid") for item in case["worker_processes"][:-1]}
            checkpoint = {
                "chunk_index": chunk_index,
                "ordinals": list(ordinals),
                "worker_pid": worker.get("pid"),
                "process_status": worker.get("status"),
                "consumed_before": before["consumed_evaluations"],
                "consumed_after": after["consumed_evaluations"],
                "last_consumed_ordinal_before_restart": (
                    None if before["consumed_evaluations"] == 0 else expected_start - 1
                ),
                "revision_before": before["revision"],
                "revision_after": after["revision"],
                "resource_counts_before": before_counts,
                "resource_counts_after": after_counts,
                "worker_elapsed_seconds": worker.get("host_elapsed_seconds"),
                "fresh_process_verified": (
                    worker.get("pid") != os.getpid() and worker.get("pid") not in prior_worker_pids
                ),
                "prior_consumed_prefix_sha256": sha256(prior_slots),
                "restart_receipt_replay": response.get("restart_receipt_replay"),
                "journaled_ordinals": sorted(
                    item for item in journaled_ordinals if item in ordinals
                ),
            }
            if checkpoint["fresh_process_verified"] is not True:
                raise ValueError("durable campaign restart did not use a fresh service process")
            if checkpoint["consumed_before"] != expected_start:
                raise ValueError("durable restart began from a different consumed count")
            expected_replay_ordinal = None if expected_start == 0 else expected_start - 1
            if expected_replay_ordinal is None:
                if checkpoint["restart_receipt_replay"] is not None:
                    raise ValueError("first process unexpectedly has a prior receipt to replay")
            elif (
                not isinstance(checkpoint["restart_receipt_replay"], dict)
                or checkpoint["restart_receipt_replay"].get("preceding_ordinal")
                != expected_replay_ordinal
                or checkpoint["restart_receipt_replay"].get("passed") is not True
            ):
                raise ValueError("fresh worker did not replay the preceding exact-key receipts")
            if checkpoint["consumed_after"] != expected_start + sum(
                item["status"] == "consumed" for item in returned
            ):
                raise ValueError("worker consumed count differs from its returned slot sequence")
            case["restart_checkpoints"].append(checkpoint)
            case["final_campaign"] = after
            stopped = bool(response.get("stopped"))
            if stopped:
                last = returned[-1]
                expected_stop = (
                    case["duplicate_policy"] == "reject"
                    and last.get("status") == "duplicate_rejected"
                )
                if not expected_stop:
                    case["failure"] = {
                        "type": "UnexpectedDurableStop",
                        "message": "durable campaign stopped outside the declared reject-duplicate case",
                    }
                    case["status"] = "partial_failure"
                    break
        except Exception as error:
            case["failure"] = _campaign_failure(case, error, (data_path, declaration_path.parent))
            try:
                case["final_campaign"] = campaigns.get_campaign(OWNER, case["campaign_id"])
            except Exception:
                pass
            # Keep every fsynced result; query the public ledger for an in-flight or
            # completed slot whose worker response was lost.
            try:
                journal_records = _journal_records(journal_path, case["case_id"])
                _retain_journal_tail_error(case, journal_records)
            except Exception as journal_error:
                case["worker_journal_error"] = _error_record(journal_error, private_paths=(stage,))
                journal_records = []
            for record in journal_records:
                case["slots"][record["ordinal"]] = record["slot"]
            for ordinal in ordinals:
                if case["slots"][ordinal]["status"] != "not_attempted_unknown":
                    continue
                try:
                    attempt = _attempt_snapshot_for_ordinal(OWNER, case["campaign_id"], ordinal)
                except Exception as ledger_error:
                    case["slots"][ordinal]["ledger_read_error"] = _error_record(ledger_error)
                    continue
                if attempt is not None:
                    case["slots"][ordinal]["suggestion_attempt"] = attempt
                    case["slots"][ordinal]["status"] = "interrupted_unknown"
                    case["slots"][ordinal]["failure"] = {
                        "type": "DurableWorkerInterrupted",
                        "message": "worker stopped before emitting a complete slot record; inspect the retained attempt state",
                    }
            stopped = True
            break

    if not case.get("failure"):
        try:
            campaign = campaigns.get_campaign(OWNER, case["campaign_id"])
            case["final_campaign"] = campaign
            consumed = campaign["consumed_evaluations"]
            duplicate_rejected = any(
                slot["status"] == "duplicate_rejected" for slot in case["slots"]
            )
            if consumed == case["evaluation_budget"]:
                case["status"] = "budget_complete"
            elif duplicate_rejected and case["duplicate_policy"] == "reject":
                case["status"] = "stopped_expected_duplicate_rejection"
            else:
                case["status"] = "partial_failure"
                case["failure"] = {
                    "type": "CampaignIncomplete",
                    "message": (
                        "campaign ended before its declared budget without an expected "
                        "duplicate rejection"
                    ),
                }
        except Exception as error:
            _campaign_failure(case, error, (data_path, declaration_path.parent))
    _safe_reconcile_durable_case_from_store(case, declaration_sha256)
    return case


def _component_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    planned = PLANNED_COMPONENT_CALLS
    attempted = sum(row.get("attempted") is True for row in rows)
    returned = sum(row.get("status") == "returned" for row in rows)
    failed = sum(row.get("status") == "failed" for row in rows)
    unknown = planned - attempted
    case_results = []
    by_case: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_case.setdefault(row["case_id"], []).append(row)
    for case_id, case_rows in sorted(by_case.items()):
        case_rows.sort(key=lambda row: row["ordinal"])
        returned_count = sum(row["status"] == "returned" for row in case_rows)
        case_results.append(
            {
                "case_id": case_id,
                "planned_calls": EVALUATION_BUDGET - INITIAL_DESIGN_SIZE,
                "attempted_calls": sum(row["attempted"] is True for row in case_rows),
                "returned_calls": returned_count,
                "complete": returned_count == EVALUATION_BUDGET - INITIAL_DESIGN_SIZE,
                "first_failure": next(
                    (row["failure"] for row in case_rows if row.get("failure")), None
                ),
            }
        )
    return {
        "status": "pass" if returned == planned else "fail",
        "planned_calls": planned,
        "attempted_calls": attempted,
        "returned_calls": returned,
        "failed_calls": failed,
        "unattempted_unknown_calls": unknown,
        "case_count": len(case_results),
        "complete_case_count": sum(item["complete"] for item in case_results),
        "cases": case_results,
        "component_readiness": returned == planned and len(case_results) == PLANNED_COMPONENT_CASES,
        "fabricated_scores_registered_as_observations": False,
    }


def _durable_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    slots = [slot for case in cases for slot in case["slots"]]
    consumed_ordinals_by_case = {}
    attempted_ordinals_by_case = {}
    completed_attempts_by_case = {}
    completed_attempt_ordinals_by_case = {}
    observation_ids = set()
    for case in cases:
        case_id = case["case_id"]
        reconciliation = case.get("store_reconciliation", {})
        known_consumed = set()
        known_attempted = set()
        known_completed = set()
        known_completed_ordinals = []
        if reconciliation.get("status") in {"reconciled", "partial"}:
            known_consumed.update(reconciliation.get("consumed_ordinals", []))
            known_attempted.update(reconciliation.get("attempted_suggestion_ordinals", []))
            for item in reconciliation.get("completed_simulator_attempts", []):
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("attempt_id"), str)
                    and type(item.get("ordinal")) is int
                    and item["attempt_id"] not in known_completed
                ):
                    known_completed.add(item["attempt_id"])
                    known_completed_ordinals.append(item["ordinal"])
            observation_ids.update(
                item.get("observation_id")
                for item in reconciliation.get("per_ordinal", [])
                if isinstance(item, dict) and isinstance(item.get("observation_id"), str)
            )
        for slot in case["slots"]:
            if _slot_has_consumption(slot):
                known_consumed.add(slot["ordinal"])
            if slot.get("suggestion_events"):
                known_attempted.add(slot["ordinal"])
            if _slot_has_completed_simulator_attempt(slot):
                attempt_id = slot.get("attempt", {}).get("attempt_id")
                if isinstance(attempt_id, str) and attempt_id not in known_completed:
                    known_completed.add(attempt_id)
                    known_completed_ordinals.append(slot["ordinal"])
            observation = slot.get("observation")
            if isinstance(observation, dict) and isinstance(observation.get("observation_id"), str):
                observation_ids.add(observation["observation_id"])
            reconciliation_slot = slot.get("reconciliation")
            if isinstance(reconciliation_slot, dict):
                if reconciliation_slot.get("suggestion_ledger_present") is True:
                    known_attempted.add(slot["ordinal"])
                if isinstance(reconciliation_slot.get("observation_id"), str):
                    observation_ids.add(reconciliation_slot["observation_id"])
        consumed_ordinals_by_case[case_id] = known_consumed
        attempted_ordinals_by_case[case_id] = known_attempted
        completed_attempts_by_case[case_id] = known_completed
        completed_attempt_ordinals_by_case[case_id] = known_completed_ordinals
    consumed = sum(len(items) for items in consumed_ordinals_by_case.values())
    attempted = sum(len(items) for items in attempted_ordinals_by_case.values())
    completed_attempts = sum(len(items) for items in completed_attempts_by_case.values())
    consumed_status = sum(slot.get("status") == "consumed" for slot in slots)
    attempts_without_consumption = sum(
        ordinal not in consumed_ordinals_by_case[case_id]
        for case_id, ordinals in completed_attempt_ordinals_by_case.items()
        for ordinal in ordinals
    )
    observations = len(observation_ids)
    known_costs = [
        slot["consumption"]["result"].get("evaluation_cost")
        for slot in slots
        if _slot_has_consumption(slot)
        and isinstance(slot.get("consumption", {}).get("result"), dict)
        and _finite_number(slot["consumption"]["result"].get("evaluation_cost"))
    ]
    duplicate_rejected = sum(slot.get("status") == "duplicate_rejected" for slot in slots)
    unknown = sum(slot.get("status") == "not_attempted_unknown" for slot in slots)
    interrupted = sum(slot.get("status") == "interrupted_unknown" for slot in slots)
    failures = sum(
        slot.get("status")
        in {
            "failed",
            "partial_failure",
            "policy_violation",
            "numerical_mismatch",
            "interrupted_unknown",
        }
        for slot in slots
    )
    replicate = [case for case in cases if case["duplicate_policy"] == "replicate"]
    reject = [case for case in cases if case["duplicate_policy"] == "reject"]
    replicate_complete = sum(
        len(consumed_ordinals_by_case.get(case["case_id"], set())) == case["evaluation_budget"]
        for case in replicate
    )
    reject_complete = sum(
        len(consumed_ordinals_by_case.get(case["case_id"], set())) == case["evaluation_budget"]
        for case in reject
    )
    expected_reject_count = sum(
        case.get("status") == "stopped_expected_duplicate_rejection" for case in reject
    )
    numerical_rows = [
        slot.get("oracle_comparison")
        for slot in slots
        if isinstance(slot.get("oracle_comparison"), dict)
    ]
    oracle_passes = sum(item.get("passed") is True for item in numerical_rows)
    oracle_failures = sum(item.get("status") == "fail" for item in numerical_rows)
    near_boundary = sum(item.get("status") == "unresolved_boundary" for item in numerical_rows)
    repeat_admitted = 0
    reject_replayed = 0
    duplicates_by_prior = {}
    for prior in _NOISE_PRIORS:
        admitted = []
        rejected = []
        for case in cases:
            if case["surrogate_noise_prior"] != prior:
                continue
            for slot in case["slots"]:
                if (
                    case["duplicate_policy"] == "replicate"
                    and _slot_has_consumption(slot)
                    and isinstance(slot.get("repetition"), dict)
                    and slot["repetition"].get("is_repeat") is True
                ):
                    identities = _execution_identity_map(slot)
                    admitted.append(
                        {
                            "case_id": case["case_id"],
                            "ordinal": slot["ordinal"],
                            "proposal_id": slot.get("proposal", {}).get("proposal_id"),
                            "identity_fields_complete": len(identities) == 9,
                        }
                    )
                if (
                    case["duplicate_policy"] == "reject"
                    and slot.get("status") == "duplicate_rejected"
                ):
                    event = next(
                        (
                            item
                            for item in slot.get("suggestion_events", [])
                            if item.get("kind") == "terminal_failure_retry"
                        ),
                        {},
                    )
                    rejected.append(
                        {
                            "case_id": case["case_id"],
                            "ordinal": slot["ordinal"],
                            "candidate_available": slot.get("proposal_available") is True,
                            "same_failure_replayed": event.get("same_failure") is True,
                            "no_proposal_or_analysis_side_effect": event.get(
                                "original_rejection_created_no_proposal_or_analysis"
                            )
                            is True,
                        }
                    )
        repeat_admitted += len(admitted)
        reject_replayed += len(rejected)
        duplicates_by_prior[prior] = {
            "replicate_admitted_repeat_count": len(admitted),
            "replicate_examples": admitted,
            "reject_durable_rejection_count": len(rejected),
            "reject_examples": rejected,
            "replicate_coverage": any(item["identity_fields_complete"] for item in admitted),
            "reject_coverage": any(
                item["candidate_available"]
                and item["same_failure_replayed"]
                and item["no_proposal_or_analysis_side_effect"]
                for item in rejected
            ),
        }
    duplicate_coverage = all(
        item["replicate_coverage"] and item["reject_coverage"]
        for item in duplicates_by_prior.values()
    )
    valid_duplicate_failures = all(
        slot.get("status") != "duplicate_rejected"
        or (
            case["duplicate_policy"] == "reject"
            and bool(slot.get("duplicate_of_prior_proposals"))
            and any(
                event.get("kind") == "terminal_failure_retry"
                and event.get("same_failure") is True
                and event.get("resource_counts_unchanged") is True
                and event.get("original_rejection_created_no_proposal_or_analysis") is True
                for event in slot.get("suggestion_events", [])
            )
        )
        for case in cases
        for slot in case["slots"]
    )
    unknown_allowed = all(
        slot.get("status") != "not_attempted_unknown"
        or (
            case["duplicate_policy"] == "reject"
            and any(item.get("status") == "duplicate_rejected" for item in case["slots"])
            and slot["ordinal"]
            > next(
                item["ordinal"]
                for item in case["slots"]
                if item.get("status") == "duplicate_rejected"
            )
        )
        for case in cases
        for slot in case["slots"]
    )
    restart_evidence_by_case = {
        case["case_id"]: {
            "boundaries_before": [
                item.get("consumed_before") for item in case.get("restart_checkpoints", [])
            ],
            "fresh_processes": all(
                item.get("fresh_process_verified") is True
                for item in case.get("restart_checkpoints", [])
            ),
            "receipt_replays_passed": all(
                (
                    item.get("restart_receipt_replay") is None
                    if item.get("consumed_before") == 0
                    else isinstance(item.get("restart_receipt_replay"), dict)
                    and item["restart_receipt_replay"].get("passed") is True
                    and item["restart_receipt_replay"].get("preceding_ordinal")
                    == item.get("consumed_before") - 1
                )
                for item in case.get("restart_checkpoints", [])
            ),
            "required_completed_budget_boundaries": (
                case.get("status") != "budget_complete"
                or [item.get("consumed_before") for item in case.get("restart_checkpoints", [])]
                == [0, 3, 7]
            ),
        }
        for case in cases
    }
    restart_evidence = all(
        item["fresh_processes"]
        and item["receipt_replays_passed"]
        and item["required_completed_budget_boundaries"]
        for item in restart_evidence_by_case.values()
    )
    nonnumeric_failures = sum(
        slot.get("status")
        in {"failed", "partial_failure", "policy_violation", "interrupted_unknown"}
        for slot in slots
    )
    policy_compliance = (
        nonnumeric_failures == 0
        and valid_duplicate_failures
        and unknown_allowed
        and restart_evidence
    )
    numerical_validation = (
        bool(numerical_rows)
        and oracle_passes == len(numerical_rows)
        and oracle_failures == 0
        and near_boundary == 0
    )
    store_reconciliation_complete = len(cases) == PLANNED_DURABLE_CASES and all(
        case.get("store_reconciliation", {}).get("status") == "reconciled"
        and len(case.get("store_reconciliation", {}).get("per_ordinal", [])) == EVALUATION_BUDGET
        for case in cases
    )
    return {
        "planned_campaigns": PLANNED_DURABLE_CASES,
        "recorded_campaigns": len(cases),
        "planned_suggestion_slots": PLANNED_ANALYTICAL_SLOTS,
        "planned_gp_suggestion_slots": PLANNED_GP_SUGGESTIONS,
        "planned_analyte_slots": 320,
        "attempted_suggestion_slots": attempted,
        "consumed_evaluations": consumed,
        "consumed_evaluations_by_status": consumed_status,
        "consumed_evaluations_by_case": {
            case_id: len(ordinals) for case_id, ordinals in consumed_ordinals_by_case.items()
        },
        "consumed_status_slot_count": consumed_status,
        "completed_simulator_attempt_count": completed_attempts,
        "completed_simulator_attempts_without_consumption": attempts_without_consumption,
        "analytical_observation_count": observations,
        "known_consumed_evaluation_cost_total": math.fsum(known_costs) if known_costs else None,
        "known_consumed_evaluation_cost_count": len(known_costs),
        "unknown_consumed_evaluation_cost_count": consumed - len(known_costs),
        "store_reconciliation_complete": store_reconciliation_complete,
        "store_reconciliation_by_case": {
            case["case_id"]: case.get("store_reconciliation", {}).get("status", "unavailable")
            for case in cases
        },
        "durable_attempt_and_consumption_accounting_source": (
            "union of fsynced slot evidence and independently queried owned SQLite source records"
        ),
        "correctly_rejected_duplicate_slots": duplicate_rejected,
        "failed_or_interrupted_slots": failures,
        "not_attempted_unknown_slots": unknown,
        "interrupted_unknown_slots": interrupted,
        "replicate_campaigns": len(replicate),
        "replicate_budget_complete_campaigns": replicate_complete,
        "replicate_budget_completion": replicate_complete == len(replicate) == 8,
        "reject_campaigns": len(reject),
        "reject_budget_complete_campaigns": reject_complete,
        "reject_expected_duplicate_stop_campaigns": expected_reject_count,
        "reject_budget_completion_status": "descriptive_only",
        "numerical_comparisons_available": len(numerical_rows),
        "numerical_comparisons_passed": oracle_passes,
        "numerical_comparisons_failed": oracle_failures,
        "near_boundary_comparisons": near_boundary,
        "numerical_validation": numerical_validation,
        "durable_policy_compliance": policy_compliance,
        "fresh_process_restart_evidence": restart_evidence,
        "restart_evidence_by_case": restart_evidence_by_case,
        "declared_restart_schedule": {
            "after_consumed_ordinals": [2, 6],
            "fresh_process_chunks": [list(chunk) for chunk in _DURABLE_CHUNKS],
        },
        "unknown_slots_are_only_post_expected_rejects": unknown_allowed,
        "repeat_coverage_by_prior": duplicates_by_prior,
        "repeat_coverage": duplicate_coverage,
        "replicate_repeat_admissions": repeat_admitted,
        "reject_repeat_receipts": reject_replayed,
    }


def _qualification_report(
    *,
    declaration_sha256: str,
    manifest: dict[str, Any],
    component_rows: list[dict[str, Any]],
    durable_cases: list[dict[str, Any]],
    registration_failure: dict[str, Any] | None,
):
    component = _component_summary(component_rows)
    durable = _durable_summary(durable_cases)
    all_proposals = all(
        isinstance(_execution_identity_map(item).get("proposal_id"), str)
        for case in durable_cases
        for item in case["slots"]
        if _slot_has_consumption(item)
        or (
            isinstance(item.get("reconciliation"), dict)
            and isinstance(item["reconciliation"].get("consumption_id"), str)
        )
    )
    durable["proposal_identifiers_complete_for_consumed"] = all_proposals
    criteria = {
        "component_readiness": component["component_readiness"],
        "durable_policy_compliance": durable["durable_policy_compliance"]
        and all_proposals
        and registration_failure is None,
        "store_reconciliation_complete": durable["store_reconciliation_complete"],
        "replicate_budget_completion": durable["replicate_budget_completion"],
        "real_duplicate_coverage": durable["repeat_coverage"],
        "numerical_validation": durable["numerical_validation"],
    }
    qualified = all(criteria.values())
    return {
        "profile": PROFILE,
        "declaration_sha256": declaration_sha256,
        "status": "qualified" if qualified else "not_qualified",
        "qualified": qualified,
        "claim_scope": {
            "qualified": (
                "fixed-sequence public-backend operation, durable service request/receipt and "
                "duplicate-policy accounting, immutable synthetic execution lineage, and "
                "agreement with the independent idealized retention/Gaussian oracle"
            ),
            "not_qualified": [
                "fresh optimizer fit, physical parameter validation, chromatogram or noise behavior, "
                "instrument performance, transfer to unselected tasks, optimizer superiority, or "
                "a success-rate estimate"
            ],
            "simulation_interpretation": (
                "Every analytical outcome is a deterministic synthetic latent retention and "
                "Gaussian-centroid execution under the declared fixtures. Repeated candidates "
                "create separate executions and spend; they are not independent measurement noise."
            ),
        },
        "planned_denominators": {
            "component_cases": PLANNED_COMPONENT_CASES,
            "component_backend_call_slots": PLANNED_COMPONENT_CALLS,
            "durable_campaigns": PLANNED_DURABLE_CASES,
            "durable_suggestion_slots": PLANNED_ANALYTICAL_SLOTS,
            "durable_gp_suggestion_slots": PLANNED_GP_SUGGESTIONS,
            "analyte_slots": 320,
            "durable_reject_policy_slots": 80,
            "durable_replicate_policy_slots": 80,
            "maximum_backend_calls_without_retries": 196,
        },
        "criteria": criteria,
        "component": component,
        "durable": durable,
        "registration_failure": registration_failure,
        "precall_software_manifest": {
            "profile": manifest["profile"],
            "created_at": manifest["created_at"],
            "declaration_sha256": manifest["declaration_sha256"],
            "fixture_oracle_sha256": manifest["fixture_oracle_sha256"],
            "initial_design_sha256": manifest["initial_design_sha256"],
            "host": manifest["host"],
            "bo_sidecar": manifest["bo_sidecar"],
            "fixed_backend_configuration": manifest["fixed_backend_configuration"],
        },
        "duplicate_policy_note": (
            "Reject-policy budget completion is descriptive and not required after a correctly "
            "recorded duplicate rejection. Replicate-policy campaigns must consume all ten slots."
        ),
    }


def _slot_has_consumption(slot: dict[str, Any]) -> bool:
    value = slot.get("consumption")
    return isinstance(value, dict) and isinstance(value.get("consumption_id"), str)


def _slot_has_completed_simulator_attempt(slot: dict[str, Any]) -> bool:
    attempt = slot.get("attempt")
    spec = attempt.get("spec", {}) if isinstance(attempt, dict) else {}
    fidelity = spec.get("fidelity") if isinstance(spec, dict) else None
    return (
        isinstance(spec, dict)
        and spec.get("status") == "completed"
        and spec.get("evaluator_kind") == "simulator"
        and isinstance(fidelity, (int, float))
        and not isinstance(fidelity, bool)
        and fidelity == 1.0
    )


def _slot_csv_row(case: dict[str, Any], slot: dict[str, Any]):
    candidate = slot.get("candidate") if isinstance(slot.get("candidate"), dict) else {}
    repetition = slot.get("repetition") if isinstance(slot.get("repetition"), dict) else {}
    comparison = (
        slot.get("oracle_comparison") if isinstance(slot.get("oracle_comparison"), dict) else {}
    )
    identity = _execution_identity_map(slot)
    failure = slot.get("failure") if isinstance(slot.get("failure"), dict) else {}
    return {
        "case_id": case["case_id"],
        "task_id": case["task_id"],
        "optimizer_target": case["optimizer_target"],
        "surrogate_noise_prior": case["surrogate_noise_prior"],
        "duplicate_policy": case["duplicate_policy"],
        "seed": case["seed"],
        "ordinal": slot["ordinal"],
        "status": slot["status"],
        "proposal_available": slot["proposal_available"],
        "strong_solvent_fraction": candidate.get("strong_solvent_fraction"),
        "acquisition_duration_min": candidate.get("acquisition_duration_min"),
        "proposal_id": identity.get("proposal_id"),
        "experiment_id": identity.get("experiment_id"),
        "attempt_id": identity.get("attempt_id"),
        "retention_prediction_id": identity.get("prediction_id"),
        "gaussian_run_id": identity.get("gaussian_run_id"),
        "observation_id": identity.get("observation_id"),
        "snapshot_id": identity.get("snapshot_id"),
        "consumption_id": identity.get("consumption_id"),
        "is_repeat": repetition.get("is_repeat"),
        "matching_prior_proposals": slot.get("duplicate_of_prior_proposals", []),
        "oracle_status": comparison.get("status"),
        "oracle_passed": comparison.get("passed"),
        "host_elapsed_seconds": next(
            (
                item.get("elapsed_seconds")
                for item in slot.get("step_timings", [])
                if item.get("step") == "suggest_campaign"
            ),
            None,
        ),
        "failure_type": failure.get("type"),
        "failure_message": failure.get("message"),
    }


def _write_json(path: Path, value: Any):
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_jsonl(path: Path, values):
    path.write_text("".join(canonical(value) + "\n" for value in values), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]):
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=list(_SUGGESTION_CASE_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: canonical(value)
                    if isinstance(value, (dict, list))
                    else ""
                    if value is None
                    else value
                    for key, value in row.items()
                }
            )


def _report_markdown(report: dict[str, Any]):
    lines = [
        "# Synthetic suggestion-sequence qualification",
        "",
        f"Status: **{report['status']}**",
        "",
        "This report qualifies only the declared fixed candidate sequences, service evidence path, "
        "duplicate-policy handling, and agreement with the independent synthetic oracle. It does "
        "not establish optimizer superiority, physical instrument performance, fitted retention "
        "parameters, or real-world acceptance probabilities.",
        "",
        "## Fixed denominators",
        "",
        f"- Component sequences: {report['planned_denominators']['component_cases']} cases, "
        f"{report['planned_denominators']['component_backend_call_slots']} planned backend calls.",
        f"- Durable campaigns: {report['planned_denominators']['durable_campaigns']} cases, "
        f"{report['planned_denominators']['durable_suggestion_slots']} planned slots and "
        f"{report['planned_denominators']['analyte_slots']} analyte rows.",
        "- Service restarts occur after consumption of ordinals 2 and 6. Reject-policy "
        "completion is descriptive after an expected duplicate rejection; "
        "replicate-policy campaigns are expected to consume all ten evaluations.",
        "",
        "## Criteria",
        "",
    ]
    for name, passed in report["criteria"].items():
        lines.append(f"- {name}: {'pass' if passed else 'fail/unknown'}")
    lines.extend(
        [
            "",
            "## Evidence interpretation",
            "",
            "All values are deterministic synthetic executions under the fixed declared retention "
            "models and Gaussian width assumptions. Repeated coordinates are separate modeled "
            "executions and spend; they do not represent independently sampled experimental noise.",
            "",
            "See `component_calls.jsonl`, `durable_campaigns.json`, `durable_worker_journal.jsonl`, "
            "`slots.csv`, and the sealed `measure.sqlite3` backup for row-level requests, responses, "
            "attempt receipts, source lineage, and remaining unknown slots.",
            "",
        ]
    )
    return "\n".join(lines)


def _placeholder_bindings(document: dict[str, Any]):
    return [
        {
            "task_id": fixture["task_id"],
            "producer": {"producer_id": f"pending-producer-{index}"},
            "contract": {"contract_id": f"pending-contract-{index}"},
        }
        for index, fixture in enumerate(document["fixtures"])
    ]


def _ensure_new_data_dir(data_dir: str | Path, output_dir: Path) -> Path:
    supplied = Path(data_dir).expanduser()
    if (supplied / _RUN_RESERVATION_MARKER).exists():
        raise ValueError(
            "qualification data_dir already has a reserved run; resumption is not allowed"
        )
    path = _store_helpers._validate_data_dir(data_dir, output_dir)
    if path.exists():
        if any(path.iterdir()):
            raise ValueError("qualification data_dir must be new or empty; resuming is not allowed")
    return path


def _reserve_qualification_run(
    data_path: Path, declaration_sha256: str, output_dir: Path
) -> tuple[Path, dict[str, Any]]:
    """Atomically fence one irreversible run before the first backend dispatch."""
    if data_path.is_symlink():
        raise ValueError("qualification data_dir must not be a symlink")
    data_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not data_path.is_dir():
        raise ValueError("qualification data_dir is not a directory")
    run_dir = data_path / _RUN_EVIDENCE_DIRECTORY
    marker_path = data_path / _RUN_RESERVATION_MARKER
    reservation = {
        "profile": "measure-service-suggestion-sequence-run-reservation/1",
        "run_id": secrets.token_hex(16),
        "created_at": _utc_now(),
        "pid": os.getpid(),
        "declaration_sha256": declaration_sha256,
        "persistent_evidence_directory": str(run_dir),
        "output_directory": str(output_dir),
        "resume_policy": "never; a new run requires a new data_dir",
    }
    encoded = (json.dumps(reservation, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    try:
        descriptor = os.open(
            marker_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError as error:
        raise ValueError(
            "qualification data_dir was reserved by another or previous run; resumption is not allowed"
        ) from error
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        directory_descriptor = os.open(data_path, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        run_dir.mkdir(mode=0o700, exist_ok=False)
        (run_dir / "run_reservation.json").write_bytes(encoded)
        _write_json(
            run_dir / "run_reservation_seal.json",
            {
                "reservation_sha256": hashlib.sha256(encoded).hexdigest(),
                "reservation_marker": _RUN_RESERVATION_MARKER,
            },
        )
        _fsync_file(run_dir / "run_reservation.json")
        _fsync_file(run_dir / "run_reservation_seal.json")
        _fsync_directory(run_dir)
        _fsync_directory(data_path)
    except BaseException:
        # Keep the exclusive marker after any partial setup so another invocation
        # cannot consume or overwrite this run's state.
        raise
    return run_dir, reservation


@contextmanager
def _persistent_run_evidence_directory(path: Path):
    if path.is_symlink() or not path.is_dir():
        raise ValueError("persistent run evidence directory is unavailable")
    try:
        yield path
    except BaseException as error:
        try:
            interruption_path = path / "run_interruption.json"
            _write_json(
                interruption_path,
                {
                    "profile": "measure-service-suggestion-sequence-run-interruption/1",
                    "recorded_at": _utc_now(),
                    "evidence_directory": str(path),
                    "failure": _error_record(error),
                    "resume_policy": "never; inspect retained evidence and use a new data_dir",
                },
            )
            _fsync_file(interruption_path)
            _fsync_directory(path)
        except Exception:
            pass
        raise


def _publish_persistent_run(target: Path, run_dir: Path) -> None:
    try:
        with staged_bundle(target, input_dirs=(run_dir,)) as stage:
            for item in sorted(run_dir.iterdir()):
                if item.is_symlink() or not item.is_file():
                    raise ValueError("persistent run evidence contains a non-regular file")
                shutil.copyfile(item, stage / item.name)
    except BaseException as error:
        try:
            publication_path = run_dir / "publication_failure.json"
            _write_json(
                publication_path,
                {
                    "profile": "measure-service-suggestion-sequence-publication-failure/1",
                    "recorded_at": _utc_now(),
                    "persistent_evidence_directory": str(run_dir),
                    "failure": _error_record(error),
                    "resume_policy": "never; inspect retained evidence and use a new data_dir",
                },
            )
            _fsync_file(publication_path)
            _fsync_directory(run_dir)
        except Exception:
            pass
        raise


def _copy_verified_inputs(stage: Path, inputs: dict[str, Any]):
    files = (
        ("declaration.json", inputs["declaration_raw"]),
        ("fixture-oracle.json", inputs["fixture_oracle_raw"]),
        ("initial-design.json", inputs["initial_design_raw"]),
        (inputs["oracle_source_path"].name, inputs["oracle_source_raw"]),
    )
    for name, raw in files:
        destination = stage / name
        destination.write_bytes(raw)
        _fsync_file(destination)
    _fsync_directory(stage)
    return {
        name: {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
        for name, raw in files
    }


def _run_suggestion_sequence_worker_from_cli() -> int:
    return _worker_main()


def run_suggestion_sequence_qualification(
    output_dir: str | Path,
    *,
    data_dir: str | Path,
    declaration_path: str | Path = DEFAULT_DECLARATION,
) -> dict[str, Any]:
    """Run the predeclared component and durable sequence qualification.

    This explicit function can make real backend calls through the configured
    public sidecar. It refuses a nonempty data directory, stale installed
    profile, mismatched source manifest, or missing optional BO runtime.
    """
    inputs = _load_fixed_inputs(declaration_path)
    document = inputs["document"]
    _require_replication_profile()
    target = _store_helpers._preflight_output(output_dir)
    data_path = _ensure_new_data_dir(data_dir, target)
    placeholders = _placeholder_bindings(document)
    design_streams = _design_streams(document, placeholders)
    manifest = _software_manifest(inputs, design_streams)
    if manifest["bo_sidecar"].get("status") != "available":
        raise RuntimeError(
            "qualified public BO-MCP sidecar metadata is unavailable before first call"
        )

    component_rows = _planned_component_rows(document)
    durable_cases = _planned_durable_cases(document)
    registration_failure = None
    copied_input_hashes = {}
    run_dir, reservation = _reserve_qualification_run(
        data_path, inputs["declaration_sha256"], target
    )
    with _persistent_run_evidence_directory(run_dir) as stage:
        reservation_bytes = (data_path / _RUN_RESERVATION_MARKER).read_bytes()
        reservation_sha256 = hashlib.sha256(reservation_bytes).hexdigest()
        copied_input_hashes = _copy_verified_inputs(stage, inputs)
        with _store_helpers._scoped_data_dir(data_path):
            _store_helpers._claim_or_verify_owned_store(
                data_path,
                inputs["declaration_sha256"],
                profile=PROFILE,
                owner=OWNER,
                receipt_prefixes=(f"seq-{inputs['declaration_sha256'][:16]}-",),
            )
            bindings = []
            protocols = {}
            try:
                bindings = _register_fixture_inputs(document, inputs["declaration_sha256"])
                protocols = _register_durable_protocols(
                    document, bindings, inputs["declaration_sha256"]
                )
                actual_design = _design_streams(document, bindings)
                if actual_design != design_streams:
                    raise ValueError(
                        "registered opaque task links changed the shared design streams"
                    )
            except Exception as error:
                registration_failure = _error_record(
                    error, private_paths=(data_path, inputs["declaration_path"].parent)
                )
            manifest["initial_design_streams"] = design_streams
            manifest["registry_bindings"] = [
                {
                    "task_id": item["task_id"],
                    "retention_model_id": item["retention_model"]["model_id"],
                    "producer_id": item["producer"]["producer_id"],
                    "contract_id": item["contract"]["contract_id"],
                    "model_input_sha256": item["retention_model"]["input_sha256"],
                    "producer_input_sha256": item["producer"]["input_sha256"],
                    "contract_input_sha256": item["contract"]["input_sha256"],
                }
                for item in bindings
            ]
            manifest["registered_protocols"] = [
                {
                    "optimizer_target": "confirmed_acceptance_reward",
                    "surrogate_noise_prior": prior,
                    "duplicate_policy": duplicate,
                    "protocol_id": entry["registered"]["protocol_id"],
                    "protocol_input_sha256": entry["registered"]["input_sha256"],
                    "protocol_output_sha256": entry["registered"]["output_sha256"],
                }
                for (prior, duplicate), entry in sorted(protocols.items())
            ]
            precall_path = stage / _PRECALL_MARKER
            _write_json(precall_path, manifest)
            _fsync_file(precall_path)
            _fsync_directory(stage)
            precall_digest = file_sha256(stage / _PRECALL_MARKER)
            first_marker = stage / _FIRST_CALL_MARKER
            if registration_failure is None:
                try:
                    component_rows = _run_component_sequences(
                        document,
                        bindings,
                        metadata=manifest["bo_sidecar"],
                        first_call_marker=first_marker,
                        protocol_specs=_component_spec_map(document, bindings),
                        precall_manifest_sha256=precall_digest,
                        row_callback=lambda item: _append_worker_journal(
                            stage / "component_calls.journal.jsonl",
                            {
                                "profile": "measure-service-suggestion-sequence-component-call/1",
                                "case_id": item["case_id"],
                                "ordinal": item["ordinal"],
                                "row": item,
                            },
                        ),
                    )
                except Exception as error:
                    registration_failure = _error_record(
                        error, private_paths=(data_path, inputs["declaration_path"].parent)
                    )
                for case in durable_cases:
                    if registration_failure is not None:
                        case["status"] = "not_attempted_unknown"
                        case["failure"] = registration_failure
                        continue
                    fixture = next(item for item in bindings if item["task_id"] == case["task_id"])[
                        "fixture"
                    ]
                    protocol_entry = protocols[
                        (case["surrogate_noise_prior"], case["duplicate_policy"])
                    ]
                    _run_durable_case(
                        case=case,
                        protocol_entry=protocol_entry,
                        fixture=fixture,
                        declaration_path=inputs["declaration_path"],
                        declaration_sha256=inputs["declaration_sha256"],
                        manifest=manifest,
                        stage=stage,
                        data_path=data_path,
                    )
            else:
                for case in durable_cases:
                    case["status"] = "not_attempted_unknown"
                    case["failure"] = registration_failure
            if not first_marker.exists():
                _write_json(
                    first_marker,
                    {
                        "profile": "measure-service-suggestion-first-backend-call/1",
                        "status": "not_called",
                        "reason": (
                            "registration failed before an optimizer call"
                            if registration_failure
                            else "component call loop did not reach an optimizer invocation"
                        ),
                    },
                )
            try:
                current_hashes = _host_module_hashes()
                manifest["host_source_unchanged_after_run"] = (
                    current_hashes == manifest["host"]["module_sha256"]
                )
                if not manifest["host_source_unchanged_after_run"]:
                    registration_failure = registration_failure or {
                        "type": "InstalledSourceChanged",
                        "message": "installed Python source changed during qualification",
                    }
            except Exception as error:
                manifest["host_source_unchanged_after_run"] = False
                registration_failure = registration_failure or _error_record(error)
            if (data_path / "measure.sqlite3").is_file():
                _store_helpers._verify_owned_store(
                    data_path,
                    inputs["declaration_sha256"],
                    profile=PROFILE,
                    owner=OWNER,
                    receipt_prefixes=(f"seq-{inputs['declaration_sha256'][:16]}-",),
                )
                _store_helpers._backup_database(stage / "measure.sqlite3")
        if not (stage / "component_calls.jsonl").exists():
            _write_jsonl(stage / "component_calls.jsonl", component_rows)
        else:
            # The journal is per-call durable progress; the normalized rows file
            # is the complete fixed denominator including unattempted unknowns.
            _write_jsonl(stage / "component_calls.jsonl", component_rows)
        report = _qualification_report(
            declaration_sha256=inputs["declaration_sha256"],
            manifest=manifest,
            component_rows=component_rows,
            durable_cases=durable_cases,
            registration_failure=registration_failure,
        )
        report["copied_verified_inputs"] = copied_input_hashes
        report["first_backend_call_marker_sha256"] = file_sha256(first_marker)
        report["persistent_run_evidence_directory"] = str(run_dir)
        report["run_reservation_sha256"] = reservation_sha256
        _write_json(stage / "report.json", report)
        (stage / "report.md").write_text(_report_markdown(report), encoding="utf-8")
        _write_json(
            stage / "durable_campaigns.json", {"profile": PROFILE, "campaigns": durable_cases}
        )
        _write_csv(
            stage / "slots.csv",
            [_slot_csv_row(case, slot) for case in durable_cases for slot in case["slots"]],
        )
        _write_jsonl(stage / "component_calls.jsonl", component_rows)
        if not (stage / "durable_worker_journal.jsonl").exists():
            (stage / "durable_worker_journal.jsonl").write_text("", encoding="utf-8")
        if not (stage / "component_calls.journal.jsonl").exists():
            (stage / "component_calls.journal.jsonl").write_text("", encoding="utf-8")
        for item in stage.iterdir():
            if item.is_file() and not item.is_symlink():
                _fsync_file(item)
        _fsync_directory(stage)

    _publish_persistent_run(target, run_dir)
    complete = json.loads((target / "complete.json").read_text(encoding="utf-8"))
    return {
        **report,
        "artifact_directory": str(target),
        "persistent_run_evidence_directory": str(run_dir),
        "run_reservation": reservation,
        "run_reservation_sha256": reservation_sha256,
        "complete_sha256": file_sha256(target / "complete.json"),
        "artifact_file_count": len(complete["files"]),
    }


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        raise SystemExit(_run_suggestion_sequence_worker_from_cli())
    raise SystemExit("This benchmark is invoked through its service API or benchmark CLI.")
