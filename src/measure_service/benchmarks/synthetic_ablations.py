"""Run the frozen matched optimizer-target and noise-prior ablation study.

The study reuses the durable campaign pipeline but defines its own immutable
five-arm declaration, readiness gate, accounting and evidence bundle. Its
observations are synthetic latent retention and Gaussian-centroid outcomes;
they are not instrument runs or a general optimizer ranking.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import stat
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from measure_methods.gaussian_observations import RESOLUTION_DEFINITION
from measure_methods.trajectories import aggregate_success_trajectories

from .. import (
    campaign_protocol,
    campaign_protocol_registry,
    campaigns,
    gaussian_producer,
    observations,
    retention,
)
from ..storage import registry_connection, registry_mutation, sha256
from . import synthetic_campaigns as shared
from .evidence import file_sha256, staged_bundle

PROFILE = "synthetic-isocratic-ablation-study/1"
STORE_PROFILE = PROFILE
PROTOCOL_PROFILE = "synthetic-isocratic-ablation/1"
DEFAULT_DOCUMENT = Path(__file__).with_name("reference_data") / (
    "synthetic-isocratic-ablation-v1.json"
)
DEFAULT_SHA256 = "0c607fa590c3c5682018765f25b37c4c9820a2ef36fc3d57bb205d0f1aa31094"
OWNER = "synthetic-ablation-runner"
PRE_FLIGHT_PROFILE = "measure-service-bo-ablation-preflight/1"
PRE_FLIGHT_KEY_PREFIX = f"syn-abl-{DEFAULT_SHA256}-preflight-"
PLANNED_ARMS = 5
PLANNED_TASKS = 4
PLANNED_SEEDS = 3
PLANNED_CAMPAIGNS_PER_ARM = PLANNED_TASKS * PLANNED_SEEDS
PLANNED_CAMPAIGN_COUNT = PLANNED_ARMS * PLANNED_CAMPAIGNS_PER_ARM
PLANNED_EVALUATIONS_PER_ARM = PLANNED_CAMPAIGNS_PER_ARM * 10
PLANNED_EVALUATION_COUNT = PLANNED_ARMS * PLANNED_EVALUATIONS_PER_ARM
COHORTS = ("regression", "held_out", "combined")
SUCCESS_SERIES = (
    shared.STRICT_SUCCESS_SERIES,
    shared.SCORE_SUCCESS_SERIES,
)
_ALLOWED_ARMS = {
    ("random-control", "random", "historical_utility", "default"),
    ("gp-utility-default", "bo_mcp_gp_logei", "historical_utility", "default"),
    ("gp-confirmed-default", "bo_mcp_gp_logei", "confirmed_acceptance_reward", "default"),
    ("gp-utility-low-noise", "bo_mcp_gp_logei", "historical_utility", "low_noise"),
    ("gp-confirmed-low-noise", "bo_mcp_gp_logei", "confirmed_acceptance_reward", "low_noise"),
}
_EVALUATION_CSV_FIELDS = (
    "arm_id",
    "cohort_role",
    "optimizer_target_config",
    "surrogate_noise_prior",
    "optimizer_target_name",
    "optimizer_target_profile",
    "optimizer_target_status",
    "optimizer_target_value",
    *shared._EVALUATION_CSV_FIELDS,
)


def _read_study_document(path: str | Path = DEFAULT_DOCUMENT) -> tuple[dict[str, Any], str]:
    """Read and validate the byte-frozen declaration and preserved V1 tasks."""
    source = Path(path)
    try:
        info = source.lstat()
    except OSError as error:
        raise ValueError("the fixed synthetic ablation declaration is unavailable") from error
    if not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024:
        raise ValueError("the fixed synthetic ablation declaration must be a bounded regular file")
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != DEFAULT_SHA256:
        raise ValueError("the fixed synthetic ablation declaration failed its SHA256 check")
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=shared._unique_object,
            parse_constant=shared._reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("the fixed synthetic ablation declaration is malformed") from error
    if not isinstance(document, dict):
        raise ValueError("the fixed synthetic ablation declaration must be an object")
    if (
        document.get("profile") != PROFILE
        or document.get("seeds") != [17, 29, 43]
        or document.get("evaluation_budget") != 10
        or document.get("initial_design_size") != 3
        or document.get("stopping_policy") != "full_budget"
        or len(document.get("tasks", [])) != PLANNED_TASKS
        or len(document.get("arms", [])) != PLANNED_ARMS
        or document.get("preflight", {}).get("expected_case_count") != 16
    ):
        raise ValueError("the fixed synthetic ablation declaration has an unsupported profile")
    task_ids = [task.get("task_id") for task in document["tasks"]]
    if len(set(task_ids)) != PLANNED_TASKS:
        raise ValueError("the ablation task IDs must be unique")
    if [task.get("cohort_role") for task in document["tasks"]] != [
        "regression",
        "regression",
        "held_out",
        "held_out",
    ]:
        raise ValueError("the ablation task cohorts differ from the frozen declaration")
    arm_keys = {
        (
            item.get("arm_id"),
            item.get("policy"),
            item.get("optimizer_target"),
            item.get("surrogate_noise_prior"),
        )
        for item in document["arms"]
    }
    if arm_keys != _ALLOWED_ARMS:
        raise ValueError("the ablation arms differ from the five frozen target/prior combinations")
    old_document, old_digest = shared._read_default_document()
    if old_digest != shared.DEFAULT_SHA256:
        raise ValueError("the original paired synthetic reference failed its SHA256 check")
    retained = [
        {key: value for key, value in task.items() if key != "cohort_role"}
        for task in document["tasks"][:2]
    ]
    if retained != old_document["tasks"]:
        raise ValueError("the original paired synthetic tasks changed in the ablation declaration")
    reporting = document.get("reporting", {})
    if (
        reporting.get("planned_campaigns_per_arm") != PLANNED_CAMPAIGNS_PER_ARM
        or reporting.get("planned_evaluations_per_arm") != PLANNED_EVALUATIONS_PER_ARM
        or reporting.get("total_planned_campaigns") != PLANNED_CAMPAIGN_COUNT
        or reporting.get("total_planned_evaluations") != PLANNED_EVALUATION_COUNT
    ):
        raise ValueError("the ablation denominators differ from the frozen study declaration")
    return document, digest


def _arm_digest(study_sha256: str, arm: dict[str, Any]) -> str:
    return sha256({"study_sha256": study_sha256, "arm": arm})


def _receipt_scopes(document: dict[str, Any], study_sha256: str) -> tuple[str, ...]:
    common = []
    for task in document["tasks"]:
        task_id = task["task_id"]
        for operation in (
            "ablation_retention_model",
            "ablation_gaussian_producer",
            "ablation_observation_contract",
        ):
            common.append(shared._key(study_sha256, operation, task_id))
    arm_prefixes = tuple(f"syn-cmp-{_arm_digest(study_sha256, arm)}-" for arm in document["arms"])
    preflight_prefix = f"syn-abl-{study_sha256}-preflight-"
    return (*common, *arm_prefixes, preflight_prefix)


def _plan(document: dict[str, Any], study_sha256: str) -> list[dict[str, Any]]:
    plans = []
    for task in document["tasks"]:
        for seed in document["seeds"]:
            for arm in document["arms"]:
                plans.append(
                    {
                        "arm": arm,
                        "arm_id": arm["arm_id"],
                        "arm_operation_sha256": _arm_digest(study_sha256, arm),
                        "task_id": task["task_id"],
                        "cohort_role": task["cohort_role"],
                        "seed": seed,
                        "policy": arm["policy"],
                        "optimizer_target_config": arm["optimizer_target"],
                        "surrogate_noise_prior": arm["surrogate_noise_prior"],
                    }
                )
    if len(plans) != PLANNED_CAMPAIGN_COUNT:
        raise ValueError("the frozen study does not produce sixty campaign identities")
    return plans


def _retention_spec(task: dict[str, Any], document: dict[str, Any], digest: str) -> dict[str, Any]:
    geometry = document["geometry"]
    return {
        "gradient_coordinate": "column_inlet_at_analyte_entry",
        "analytes": task["analytes"],
        "column_length_m": geometry["column_length_m"],
        "effective_area_m2": geometry["effective_area_m2"],
        "post_column_volume_ml": geometry["post_column_volume_ml"],
        "strong_solvent": "B",
        "parameter_origin": "synthetic",
        "parameter_reference": (
            f"Predeclared synthetic-isocratic-ablation-v1.json sha256={digest}; "
            "constructed equations, not fitted physical parameters."
        ),
    }


def _producer_spec(task: dict[str, Any], digest: str) -> dict[str, Any]:
    analyte_ids = [item["analyte_id"] for item in task["analytes"]]
    return {
        "widths": [
            {"analyte_id": analyte_id, "fwhm_min": task["fwhm_min"]} for analyte_id in analyte_ids
        ],
        "width_origin": "synthetic_assumption",
        "width_reference": (
            f"Predeclared synthetic-isocratic-ablation-v1.json sha256={digest}; "
            "fixed Gaussian FWHM assumption, not measured or fitted."
        ),
    }


def _contract_spec(task: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    limits = document["contract"]
    return {
        "required_analytes": [item["analyte_id"] for item in task["analytes"]],
        "resolution_definition": RESOLUTION_DEFINITION,
        "resolution_threshold": limits["resolution_threshold"],
        "max_acquisition_duration_min": limits["max_acquisition_duration_min"],
        "runtime_preference_min": limits["runtime_preference_min"],
        "count_threshold": limits["count_threshold"],
    }


def _protocol_spec(
    document: dict[str, Any], task_bindings: list[dict[str, Any]], arm: dict[str, Any]
) -> campaign_protocol.AblationProtocolSpec:
    protocol = campaign_protocol.parse_campaign_protocol(
        {
            "profile": PROTOCOL_PROFILE,
            "optimizer_target": arm["optimizer_target"],
            "surrogate_noise_prior": arm["surrogate_noise_prior"],
            "domain": document["domain"],
            "flow_ml_min": document["flow_ml_min"],
            "constraints": {
                "max_acquisition_duration_min": document["contract"]["max_acquisition_duration_min"]
            },
            "tasks": [
                {
                    "task_id": binding["task_id"],
                    "producer_id": binding["producer"]["producer_id"],
                    "observation_contract_id": binding["contract"]["contract_id"],
                }
                for binding in task_bindings
            ],
            "seeds": document["seeds"],
            "evaluation_budget": document["evaluation_budget"],
            "initial_design_size": document["initial_design_size"],
            "policies": ["random", "bo_mcp_gp_logei"],
            "stopping_policy": document["stopping_policy"],
            "evaluation_cost": document["evaluation_cost"],
            "evaluation_cost_unit": document["evaluation_cost_unit"],
        }
    )
    if not isinstance(protocol, campaign_protocol.AblationProtocolSpec):
        raise ValueError("the ablation declaration did not parse to its versioned protocol")
    return protocol


def _register_inputs(
    document: dict[str, Any], study_sha256: str
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    task_bindings = []
    for task in document["tasks"]:
        task_id = task["task_id"]
        model = retention.register_retention_model(
            OWNER,
            f"Predeclared ablation retention {task_id}",
            _retention_spec(task, document, study_sha256),
            shared._key(study_sha256, "ablation_retention_model", task_id),
        )
        producer = gaussian_producer.register_gaussian_producer(
            OWNER,
            f"Predeclared ablation Gaussian producer {task_id}",
            model["model_id"],
            _producer_spec(task, study_sha256),
            shared._key(study_sha256, "ablation_gaussian_producer", task_id),
        )
        contract = observations.register_observation_contract(
            OWNER,
            f"Predeclared ablation analytical contract {task_id}",
            _contract_spec(task, document),
            shared._key(study_sha256, "ablation_observation_contract", task_id),
        )
        task_bindings.append(
            {
                "task_id": task_id,
                "cohort_role": task["cohort_role"],
                "retention_model": {
                    "model_id": model["model_id"],
                    "input_sha256": model["input_sha256"],
                    "spec": model["spec"],
                    "source_fit_range_warnings": model["source_fit_range_warnings"],
                    "implementation_provenance": model["implementation_provenance"],
                },
                "producer": {
                    "producer_id": producer["producer_id"],
                    "input_sha256": producer["input_sha256"],
                    "output_sha256": producer["output_sha256"],
                    "spec": producer["spec"],
                    "implementation_provenance": producer["implementation_provenance"],
                },
                "contract": {
                    "contract_id": contract["contract_id"],
                    "input_sha256": contract["input_sha256"],
                    "spec": contract["spec"],
                    "utility_definition": contract["utility_definition"],
                    "implementation_provenance": contract["implementation_provenance"],
                },
            }
        )

    protocols: dict[str, dict[str, Any]] = {}
    protocol_rows = []
    for arm in document["arms"]:
        arm_digest = _arm_digest(study_sha256, arm)
        spec = _protocol_spec(document, task_bindings, arm)
        registered = campaign_protocol_registry.register_campaign_protocol(
            OWNER,
            f"Synthetic ablation protocol {arm['arm_id']}",
            spec,
            shared._key(arm_digest, "ablation_protocol", arm["arm_id"]),
        )
        parsed = campaign_protocol.parse_campaign_protocol(registered["spec"])
        if not isinstance(parsed, campaign_protocol.AblationProtocolSpec):
            raise ValueError("stored ablation protocol lost its versioned profile")
        entry = {
            "arm": arm,
            "arm_operation_sha256": arm_digest,
            "protocol": registered,
            "spec": parsed,
        }
        protocols[arm["arm_id"]] = entry
        protocol_rows.append(
            {
                "arm": arm,
                "arm_operation_sha256": arm_digest,
                "registered_protocol": registered,
            }
        )
    return protocols, task_bindings, protocol_rows


def _validate_existing_plan(document: dict[str, Any], study_sha256: str) -> None:
    """Refuse stored protocols or campaigns outside the frozen plan before writes."""
    expected_arms = {
        f"Synthetic ablation protocol {arm['arm_id']}": arm for arm in document["arms"]
    }
    planned_receipts = set(_receipt_scopes(document, study_sha256)[: PLANNED_TASKS * 3])
    for arm in document["arms"]:
        arm_digest = _arm_digest(study_sha256, arm)
        planned_receipts.add(shared._key(arm_digest, "ablation_protocol", arm["arm_id"]))
        for task in document["tasks"]:
            task_id = task["task_id"]
            for seed in document["seeds"]:
                policy = arm["policy"]
                planned_receipts.add(
                    shared._key(arm_digest, "campaign", task_id, seed, policy, "create")
                )
                for ordinal in range(document["evaluation_budget"]):
                    for operation in (
                        "proposal",
                        "experiment",
                        "attempt",
                        "retention",
                        "gaussian",
                        "assessment",
                        "snapshot",
                        "consumption",
                    ):
                        planned_receipts.add(
                            shared._key(
                                arm_digest,
                                "campaign",
                                task_id,
                                seed,
                                policy,
                                ordinal,
                                operation,
                            )
                        )
    with registry_connection() as con:
        protocol_rows = con.execute(
            "SELECT id, title FROM campaign_protocols WHERE owner=? ORDER BY id", (OWNER,)
        ).fetchall()
        campaign_rows = con.execute(
            "SELECT id, protocol_id, task_id, seed, policy FROM campaign_runs "
            "WHERE owner=? ORDER BY id",
            (OWNER,),
        ).fetchall()
        receipt_keys = [
            row["request_key"]
            for row in con.execute(
                "SELECT request_key FROM registry_receipts WHERE owner=? ORDER BY request_key",
                (OWNER,),
            ).fetchall()
        ]
        task_resource_rows = {
            "retention_models": con.execute(
                "SELECT id, title FROM retention_models WHERE owner=? ORDER BY id", (OWNER,)
            ).fetchall(),
            "gaussian_producers": con.execute(
                "SELECT id, title FROM gaussian_producers WHERE owner=? ORDER BY id", (OWNER,)
            ).fetchall(),
            "observation_contracts": con.execute(
                "SELECT id, title FROM observation_contracts WHERE owner=? ORDER BY id", (OWNER,)
            ).fetchall(),
        }
        task_resource_receipts = {
            row["request_key"]: dict(row)
            for row in con.execute(
                "SELECT request_key, digest, response FROM registry_receipts WHERE owner=?",
                (OWNER,),
            ).fetchall()
        }

    preflight_keys = []
    preflight_prefix = f"syn-abl-{study_sha256}-preflight-"
    for key in receipt_keys:
        if key.startswith(preflight_prefix):
            suffix = key[len(preflight_prefix) :]
            if len(suffix) != 4 or not suffix.isdecimal():
                raise ValueError("ablation registry contains a malformed preflight receipt key")
            attempt = int(suffix)
            if attempt < 1 or _preflight_key(study_sha256, attempt) != key:
                raise ValueError("ablation registry contains a malformed preflight receipt key")
            preflight_keys.append(key)
        elif key not in planned_receipts:
            raise ValueError(
                "ablation registry contains a receipt outside the frozen operation plan"
            )
    if sorted(preflight_keys) != [
        _preflight_key(study_sha256, attempt) for attempt in range(1, len(preflight_keys) + 1)
    ]:
        raise ValueError("ablation preflight receipts are not a contiguous planned sequence")
    _load_preflight_history(study_sha256, document)
    _validate_existing_task_resources(
        document, study_sha256, task_resource_rows, task_resource_receipts
    )

    protocol_arms: dict[str, dict[str, Any]] = {}
    seen_titles = set()
    for row in protocol_rows:
        arm = expected_arms.get(row["title"])
        if arm is None or row["title"] in seen_titles:
            raise ValueError("ablation registry contains a protocol outside the frozen arm plan")
        seen_titles.add(row["title"])
        registered = campaign_protocol_registry.get_campaign_protocol(OWNER, row["id"])
        spec = campaign_protocol.parse_campaign_protocol(registered["spec"])
        if not isinstance(spec, campaign_protocol.AblationProtocolSpec):
            raise ValueError("stored ablation campaign protocol has an unsupported profile")
        bindings = [
            {
                "task_id": item.task_id,
                "producer": {"producer_id": item.producer_id},
                "contract": {"contract_id": item.observation_contract_id},
            }
            for item in spec.tasks
        ]
        expected_spec = _protocol_spec(document, bindings, arm)
        if spec.model_dump(mode="json") != expected_spec.model_dump(mode="json"):
            raise ValueError("stored ablation protocol differs from the frozen scientific inputs")

        expected_tasks = {item["task_id"]: item for item in document["tasks"]}
        actual_links = {item["task_id"]: item for item in registered["tasks"]}
        if set(actual_links) != set(expected_tasks):
            raise ValueError("stored ablation protocol task links differ from the frozen plan")
        for task_id, task in expected_tasks.items():
            task_spec = next(item for item in spec.tasks if item.task_id == task_id)
            producer = gaussian_producer.get_gaussian_producer(OWNER, task_spec.producer_id)
            model_id = producer["retention_model"]["model_id"]
            model = retention.get_retention_model(OWNER, model_id)
            contract = observations.get_observation_contract(
                OWNER, task_spec.observation_contract_id
            )
            expected_model_spec = retention.RetentionModelSpec.model_validate(
                _retention_spec(task, document, study_sha256)
            ).model_dump(mode="json")
            expected_producer_spec = gaussian_producer.GaussianProducerSpec.model_validate(
                _producer_spec(task, study_sha256)
            ).model_dump(mode="json")
            expected_contract_spec = observations.ObservationContractSpec.model_validate(
                _contract_spec(task, document)
            ).model_dump(mode="json")
            if (
                model["title"] != f"Predeclared ablation retention {task_id}"
                or model["spec"] != expected_model_spec
                or producer["title"] != f"Predeclared ablation Gaussian producer {task_id}"
                or producer["spec"] != expected_producer_spec
                or producer["retention_model"]["model_id"] != model_id
                or producer["retention_model"]["input_sha256"] != model["input_sha256"]
                or contract["title"] != f"Predeclared ablation analytical contract {task_id}"
                or contract["spec"] != expected_contract_spec
            ):
                raise ValueError(
                    "stored ablation task resources differ from the frozen study inputs"
                )
            link = actual_links[task_id]
            if (
                link["producer"]["producer_id"] != producer["producer_id"]
                or link["producer"]["input_sha256"] != producer["input_sha256"]
                or link["producer"]["output_sha256"] != producer["output_sha256"]
                or link["producer"]["retention_model_id"] != model_id
                or link["producer"]["retention_model_input_sha256"] != model["input_sha256"]
                or link["contract"]["contract_id"] != contract["contract_id"]
                or link["contract"]["input_sha256"] != contract["input_sha256"]
                or link["contract"]["spec"] != expected_contract_spec
                or link["analyte_ids"] != sorted(item["analyte_id"] for item in task["analytes"])
            ):
                raise ValueError("stored ablation task resource links do not match the frozen plan")
        protocol_arms[row["id"]] = arm

    planned_tasks = {item["task_id"] for item in document["tasks"]}
    planned_seeds = set(document["seeds"])
    campaign_identities = set()
    for row in campaign_rows:
        arm = protocol_arms.get(row["protocol_id"])
        identity = (arm["arm_id"], row["task_id"], row["seed"]) if arm else None
        if (
            arm is None
            or row["task_id"] not in planned_tasks
            or row["seed"] not in planned_seeds
            or row["policy"] != arm["policy"]
            or identity in campaign_identities
        ):
            raise ValueError("ablation registry contains a campaign outside the frozen plan")
        campaign_identities.add(identity)


def _validate_existing_task_resources(
    document: dict[str, Any],
    study_sha256: str,
    rows_by_table: dict[str, list[Any]],
    receipts_by_key: dict[str, dict[str, Any]],
) -> None:
    """Validate partial common registrations before protocol links can expose them."""
    resource_types = {
        "retention_models": {
            "key_operation": "ablation_retention_model",
            "title_prefix": "Predeclared ablation retention",
            "getter": retention.get_retention_model,
            "id_field": "model_id",
        },
        "gaussian_producers": {
            "key_operation": "ablation_gaussian_producer",
            "title_prefix": "Predeclared ablation Gaussian producer",
            "getter": gaussian_producer.get_gaussian_producer,
            "id_field": "producer_id",
        },
        "observation_contracts": {
            "key_operation": "ablation_observation_contract",
            "title_prefix": "Predeclared ablation analytical contract",
            "getter": observations.get_observation_contract,
            "id_field": "contract_id",
        },
    }
    rows_by_title: dict[str, dict[str, Any]] = {table: {} for table in resource_types}
    for table, rows in rows_by_table.items():
        expected_titles = {
            f"{resource_types[table]['title_prefix']} {task['task_id']}": task
            for task in document["tasks"]
        }
        for row in rows:
            title = row["title"]
            if title not in expected_titles or title in rows_by_title[table]:
                raise ValueError("ablation registry contains an unplanned common task resource")
            rows_by_title[table][title] = dict(row)

    model_records: dict[str, dict[str, Any]] = {}
    for task in document["tasks"]:
        task_id = task["task_id"]
        title_specs = {
            "retention_models": (
                f"Predeclared ablation retention {task_id}",
                retention.RetentionModelSpec.model_validate(
                    _retention_spec(task, document, study_sha256)
                ).model_dump(mode="json"),
            ),
            "gaussian_producers": (
                f"Predeclared ablation Gaussian producer {task_id}",
                gaussian_producer.GaussianProducerSpec.model_validate(
                    _producer_spec(task, study_sha256)
                ).model_dump(mode="json"),
            ),
            "observation_contracts": (
                f"Predeclared ablation analytical contract {task_id}",
                observations.ObservationContractSpec.model_validate(
                    _contract_spec(task, document)
                ).model_dump(mode="json"),
            ),
        }
        for table, (title, expected_spec) in title_specs.items():
            metadata = resource_types[table]
            key = shared._key(study_sha256, metadata["key_operation"], task_id)
            row = rows_by_title[table].get(title)
            receipt = receipts_by_key.get(key)
            if (row is None) != (receipt is None):
                raise ValueError(
                    "ablation common task resource and its planned receipt are incomplete"
                )
            if row is None:
                continue

            record = metadata["getter"](OWNER, row["id"])
            model = model_records.get(task_id)
            if table == "retention_models":
                request = {
                    "operation": "register_retention_model",
                    "title": title,
                    "spec": expected_spec,
                }
            elif table == "gaussian_producers":
                if model is None:
                    raise ValueError(
                        "ablation Gaussian producer is missing its planned retention model"
                    )
                request = {
                    "operation": "register_gaussian_producer",
                    "title": title,
                    "retention_model_id": model["model_id"],
                    "spec": expected_spec,
                }
            else:
                request = {
                    "operation": "register_observation_contract",
                    "title": title,
                    "spec": expected_spec,
                }

            identity_sha256 = (
                sha256(request)
                if table == "gaussian_producers"
                else sha256({"title": title, "spec": expected_spec})
            )
            if (
                record.get("title") != title
                or record.get("spec") != expected_spec
                or record.get("input_sha256") != identity_sha256
                or receipt.get("digest") != sha256(request)
            ):
                raise ValueError(
                    "stored ablation common task resource differs from the frozen inputs"
                )
            try:
                receipt_response = json.loads(receipt["response"])
            except (TypeError, json.JSONDecodeError) as error:
                raise ValueError("ablation common task resource receipt is malformed") from error
            if (
                not isinstance(receipt_response, dict)
                or receipt_response.get(metadata["id_field"]) != row["id"]
                or receipt_response.get("input_sha256") != record["input_sha256"]
                or receipt_response.get("title") != title
                or receipt_response.get("spec") != expected_spec
            ):
                raise ValueError("ablation common task resource receipt does not match its record")
            if table == "retention_models":
                model_records[task_id] = record
            elif table == "gaussian_producers":
                link = record.get("retention_model")
                if model is None or link != {
                    "model_id": model["model_id"],
                    "input_sha256": model["input_sha256"],
                }:
                    raise ValueError(
                        "stored ablation Gaussian producer has an unplanned retention link"
                    )


def _common_initial_design(
    document: dict[str, Any], protocols: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = []
    for seed in document["seeds"]:
        for ordinal in range(document["initial_design_size"]):
            candidates = {
                arm_id: campaign_protocol.initial_candidate(entry["spec"], seed, ordinal)
                for arm_id, entry in protocols.items()
            }
            baseline = next(iter(candidates.values()))
            if any(candidate != baseline for candidate in candidates.values()):
                raise ValueError("ablation arms do not share identical initial LHS coordinates")
            rows.append(
                {
                    "seed": seed,
                    "ordinal": ordinal,
                    "coordinates": baseline,
                    "candidate_sha256": sha256(baseline),
                    "verified_arm_ids": sorted(candidates),
                }
            )
    return rows


def _observed_initial_design(
    campaigns_: list[dict[str, Any]],
    document: dict[str, Any],
    common_design: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare persisted initial proposals across arms with the declared LHS points."""
    expected = {(row["seed"], row["ordinal"]): row["coordinates"] for row in common_design}
    indexed = {(item["arm_id"], item["task_id"], item["seed"]): item for item in campaigns_}
    rows = []
    observed_count = 0
    matched_count = 0
    mismatch_count = 0
    planned_count = (
        len(document["arms"])
        * len(document["tasks"])
        * len(document["seeds"])
        * document["initial_design_size"]
    )
    for task in document["tasks"]:
        for seed in document["seeds"]:
            for ordinal in range(document["initial_design_size"]):
                expected_candidate = expected.get((seed, ordinal))
                has_expected = isinstance(expected_candidate, dict)
                arms = []
                for arm in document["arms"]:
                    campaign = indexed[(arm["arm_id"], task["task_id"], seed)]
                    evaluation = next(
                        item for item in campaign["evaluations"] if item["ordinal"] == ordinal
                    )
                    candidate = evaluation.get("candidate")
                    has_candidate = isinstance(candidate, dict)
                    matches = has_expected and has_candidate and candidate == expected_candidate
                    observed_count += int(has_candidate)
                    matched_count += int(matches)
                    mismatch_count += int(has_expected and has_candidate and not matches)
                    arms.append(
                        {
                            "arm_id": arm["arm_id"],
                            "candidate": candidate,
                            "candidate_sha256": sha256(candidate) if has_candidate else None,
                            "matches_predeclared_coordinate": matches,
                        }
                    )
                rows.append(
                    {
                        "task_id": task["task_id"],
                        "seed": seed,
                        "ordinal": ordinal,
                        "expected_candidate": expected_candidate,
                        "expected_coordinate_available": has_expected,
                        "expected_candidate_sha256": (
                            sha256(expected_candidate) if has_expected else None
                        ),
                        "observed_arms": arms,
                        "observed_arm_count": sum(
                            isinstance(item["candidate"], dict) for item in arms
                        ),
                        "all_arms_match": all(
                            item["matches_predeclared_coordinate"] for item in arms
                        )
                        and has_expected,
                    }
                )
    complete = (
        len(expected) == len(document["seeds"]) * document["initial_design_size"]
        and observed_count == planned_count
        and mismatch_count == 0
    )
    return {
        "profile": "synthetic-ablation-observed-initial-design/1",
        "planned_arm_candidate_count": planned_count,
        "observed_candidate_count": observed_count,
        "matched_predeclared_candidate_count": matched_count,
        "mismatched_candidate_count": mismatch_count,
        "complete_verification": complete,
        "status": "verified" if complete else "mismatch" if mismatch_count else "incomplete",
        "rows": rows,
    }


def _preflight_key(study_sha256: str, attempt: int) -> str:
    return f"syn-abl-{study_sha256}-preflight-{attempt:04d}"


def _preflight_request(study_sha256: str, attempt: int, protocol_sha256: str) -> dict[str, Any]:
    return {
        "operation": "qualify_synthetic_ablation_backend",
        "study_sha256": study_sha256,
        "attempt": attempt,
        "protocol_sha256": protocol_sha256,
    }


def _expected_preflight_routes() -> set[tuple[str, str, str, int]]:
    routes = set()
    for prior in ("default", "low_noise"):
        for length in (3, 9):
            routes.add(("historical_utility", prior, "historical_utility", length))
            for pattern in ("all_zero", "all_one", "alternating_zero_one"):
                routes.add(("confirmed_acceptance_reward", prior, pattern, length))
    return routes


def _expected_preflight_history(
    target: str, pattern: str, length: int, document: dict[str, Any]
) -> list[dict[str, Any]]:
    strong = document["domain"]["strong_solvent_fraction"]
    duration = document["domain"]["acquisition_duration_min"]
    strong_span = strong["upper"] - strong["lower"]
    duration_span = duration["upper"] - duration["lower"]
    rows = []
    for index in range(length):
        candidate = {
            "strong_solvent_fraction": strong["lower"] + strong_span * (index + 1) / (length + 1),
            "acquisition_duration_min": duration["lower"]
            + duration_span * (((2 * index) % length) + 1) / (length + 1),
        }
        if target == "historical_utility":
            score = 0.25 + 2.5 * index / (length - 1)
        elif pattern == "all_zero":
            score = 0.0
        elif pattern == "all_one":
            score = 1.0
        else:
            score = float(index % 2)
        rows.append({"candidate": candidate, "score": score})
    return rows


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_candidate(candidate: Any, document: dict[str, Any]) -> bool:
    if not isinstance(candidate, dict) or set(candidate) != {
        "strong_solvent_fraction",
        "acquisition_duration_min",
    }:
        return False
    for name in ("strong_solvent_fraction", "acquisition_duration_min"):
        value = candidate.get(name)
        bounds = document["domain"][name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not bounds["lower"] <= value <= bounds["upper"]
        ):
            return False
    return True


def _preflight_report_status(
    report: Any, document: dict[str, Any], study_sha256: str, protocol_sha256: str
) -> tuple[bool, str | None]:
    required = {
        "schema_version",
        "profile",
        "fixture_sha256",
        "study_protocol_sha256",
        "seed",
        "expected_case_count",
        "attempted_case_count",
        "success_count",
        "failed_count",
        "qualified",
        "preflight_elapsed_seconds",
        "cases",
    }
    if not isinstance(report, dict) or not required <= set(report):
        return False, "backend preflight report is missing required versioned fields"
    if (
        report["schema_version"] != 1
        or report["profile"] != PRE_FLIGHT_PROFILE
        or report["fixture_sha256"] != study_sha256
        or report["study_protocol_sha256"] != protocol_sha256
        or report["seed"] != document["seeds"][0]
        or report["expected_case_count"] != 16
        or report["attempted_case_count"] != 16
        or not isinstance(report["qualified"], bool)
        or not isinstance(report["cases"], list)
        or len(report["cases"]) != 16
    ):
        return False, "backend preflight report does not match the frozen sixteen-route contract"
    elapsed = report["preflight_elapsed_seconds"]
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        return False, "backend preflight elapsed time is invalid"
    expected_routes = _expected_preflight_routes()
    observed_routes = set()
    successful = 0
    study_backend_identity = None
    arm_backend_identities: dict[tuple[str, str], dict[str, Any]] = {}
    for case in report["cases"]:
        if not isinstance(case, dict):
            return False, "backend preflight case is malformed"
        route = (
            case.get("target"),
            case.get("noise_prior"),
            case.get("pattern"),
            case.get("history_length"),
        )
        if (
            route not in expected_routes
            or route in observed_routes
            or case.get("attempted") is not True
        ):
            return False, "backend preflight omitted or duplicated a declared route"
        observed_routes.add(route)
        request = case.get("request")
        request_fields = {
            "schema",
            "spec",
            "training_data",
            "seed",
            "protocol_sha256",
            "training_sha256",
            "adapter_source_sha256",
            "campaign_protocol_source_sha256",
            "design_kernel_source_sha256",
        }
        if (
            not isinstance(request, dict)
            or not request_fields <= set(request)
            or request.get("schema") != "measure-service-bo-request/1"
            or request.get("seed") != document["seeds"][0]
            or request.get("training_data")
            != _expected_preflight_history(route[0], route[2], route[3], document)
            or request.get("training_sha256") != sha256(request.get("training_data"))
            or any(
                not _is_sha256(request.get(field))
                for field in (
                    "adapter_source_sha256",
                    "campaign_protocol_source_sha256",
                    "design_kernel_source_sha256",
                )
            )
        ):
            return False, "backend preflight route lacks its exact fabricated input history"
        try:
            route_protocol = campaign_protocol.parse_campaign_protocol(request["spec"])
        except Exception:
            return False, "backend preflight route contains an invalid protocol request"
        if (
            not isinstance(route_protocol, campaign_protocol.AblationProtocolSpec)
            or route_protocol.optimizer_target != route[0]
            or route_protocol.surrogate_noise_prior != route[1]
            or route_protocol.seeds != document["seeds"]
            or route_protocol.evaluation_budget != document["evaluation_budget"]
            or route_protocol.initial_design_size != document["initial_design_size"]
            or campaign_protocol.campaign_protocol_sha256(route_protocol)
            != request.get("protocol_sha256")
            or case.get("case_id") != f"{route[0]}:{route[1]}:{route[2]}:n{route[3]}"
        ):
            return False, "backend preflight route request differs from the frozen study protocol"
        base_spec = route_protocol.model_dump(mode="json")
        base_spec["optimizer_target"] = "historical_utility"
        base_spec["surrogate_noise_prior"] = "default"
        if campaign_protocol.campaign_protocol_sha256(base_spec) != protocol_sha256:
            return False, "backend preflight route changes fixed study protocol inputs"
        if case.get("success") is True:
            backend = case.get("backend_provenance")
            configuration = campaign_protocol.optimizer_configuration(route_protocol)
            expected_prior = configuration["noise_prior_params"]
            settings = backend.get("settings") if isinstance(backend, dict) else None
            prior = settings.get("noise_prior") if isinstance(settings, dict) else None
            runtime = backend.get("runtime") if isinstance(backend, dict) else None
            versions = backend.get("distribution_versions") if isinstance(backend, dict) else None
            provenance = backend.get("suggestion_provenance") if isinstance(backend, dict) else None
            proposal_elapsed = runtime.get("elapsed_seconds") if isinstance(runtime, dict) else None
            optimization = (
                settings.get("acquisition_optimization") if isinstance(settings, dict) else None
            )
            if (
                not _valid_candidate(case.get("candidate"), document)
                or not isinstance(backend, dict)
                or backend.get("backend_id") != "bo-engine/BoTorchBackend"
                or backend.get("adapter_profile") != configuration["adapter_profile"]
                or backend.get("backend_name") != "bo-mcp"
                or backend.get("backend_class") != "BoTorchBackend"
                or backend.get("generation_method") != "bo"
                or backend.get("model_type") != "SingleTaskGP"
                or backend.get("acquisition_method") != "expected_improvement"
                or backend.get("acquisition_implementation") != "qLogExpectedImprovement"
                or not isinstance(backend.get("optimization_strategy"), str)
                or not backend["optimization_strategy"]
                or backend.get("protocol_sha256") != request["protocol_sha256"]
                or backend.get("training_sha256") != request["training_sha256"]
                or backend.get("training_count") != route[3]
                or backend.get("seed") != document["seeds"][0]
                or not _is_sha256(backend.get("source_tree_sha256"))
                or not isinstance(versions, dict)
                or set(versions) != {"bo-engine", "botorch", "gpytorch", "measure-service", "torch"}
                or any(
                    not isinstance(versions.get(name), str)
                    for name in ("bo-engine", "botorch", "gpytorch", "torch")
                )
                or (
                    versions.get("measure-service") is not None
                    and not isinstance(versions.get("measure-service"), str)
                )
                or backend.get("adapter_source_sha256") != request["adapter_source_sha256"]
                or backend.get("campaign_protocol_source_sha256")
                != request["campaign_protocol_source_sha256"]
                or backend.get("design_kernel_source_sha256")
                != request["design_kernel_source_sha256"]
                or not isinstance(settings, dict)
                or settings.get("objective")
                != {"name": configuration["target_name"], "direction": "maximize"}
                or settings.get("parameters")
                != ["strong_solvent_fraction", "acquisition_duration_min"]
                or settings.get("batch_size") != 1
                or settings.get("iteration") != route[3]
                or settings.get("acquisition_method") != "expected_improvement"
                or settings.get("acquisition_implementation") != "qLogExpectedImprovement"
                or settings.get("input_transform")
                != "BoTorch SingleTaskGP default input normalization"
                or settings.get("outcome_transform")
                != "BoTorch SingleTaskGP default outcome standardization"
                or settings.get("kernel")
                != "RBF with automatic relevance determination (ARD); BoTorch SingleTaskGP default, no custom covar_module"
                or settings.get("backend_state") != "stateless; no prior backend state supplied"
                or settings.get("replay") != "ordered training history is supplied on every call"
                or not isinstance(optimization, dict)
                or set(optimization)
                != {
                    "num_restarts",
                    "raw_samples",
                    "configured_num_restarts",
                    "configured_raw_samples",
                }
                or any(
                    isinstance(optimization.get(name), bool)
                    or not isinstance(optimization.get(name), int)
                    or optimization[name] < 1
                    for name in (
                        "num_restarts",
                        "raw_samples",
                        "configured_num_restarts",
                        "configured_raw_samples",
                    )
                )
                or not isinstance(prior, dict)
                or prior.get("family") != "Gamma"
                or prior.get("concentration") != expected_prior[0]
                or prior.get("rate") != expected_prior[1]
                or prior.get("space") != "standardized_target_variance"
                or prior.get("constraint_lower_bound") != 0.0001
                or prior.get("learned") is not True
                or not isinstance(runtime, dict)
                or runtime.get("device") != "cpu"
                or not isinstance(runtime.get("python_version"), str)
                or not isinstance(runtime.get("implementation"), str)
                or not isinstance(runtime.get("platform"), str)
                or isinstance(proposal_elapsed, bool)
                or not isinstance(proposal_elapsed, (int, float))
                or not math.isfinite(proposal_elapsed)
                or proposal_elapsed < 0
                or case.get("proposal_elapsed_seconds") != proposal_elapsed
                or not isinstance(backend.get("warnings"), list)
                or any(not isinstance(item, str) for item in backend["warnings"])
                or not isinstance(provenance, dict)
                or provenance.get("generation_method") != "bo"
                or provenance.get("iteration") != route[3]
                or provenance.get("acquisition_function") != "expected_improvement"
                or provenance.get("model_type") != "SingleTaskGP (Gaussian Process)"
                or provenance.get("batch_index") != 0
                or isinstance(provenance.get("random_seed"), bool)
                or not isinstance(provenance.get("random_seed"), int)
            ):
                return False, "backend preflight route lacks model-guided candidate provenance"
            identity = shared._optimizer_backend_identity(backend)
            study_identity = shared._optimizer_backend_study_identity(backend)
            factor_key = (route[0], route[1])
            if identity is None or study_identity is None:
                return False, "backend preflight route lacks a complete static identity"
            previous_factor_identity = arm_backend_identities.get(factor_key)
            if previous_factor_identity is not None and previous_factor_identity != identity:
                return (
                    False,
                    "backend identity or fixed settings changed within a target/prior route",
                )
            if study_backend_identity is not None and study_backend_identity != study_identity:
                return (
                    False,
                    "backend source or shared fixed settings changed between preflight routes",
                )
            arm_backend_identities[factor_key] = identity
            study_backend_identity = study_identity
            successful += 1
        elif case.get("success") is not False or case.get("error") is None:
            return False, "failed backend preflight route lacks its retained failure"
    if observed_routes != expected_routes:
        return False, "backend preflight route set differs from the frozen sixteen routes"
    if (
        report["success_count"] != successful
        or report["failed_count"] != 16 - successful
        or report["qualified"] is not (successful == 16)
    ):
        return False, "backend preflight summary counts do not match its route records"
    return successful == 16, None


def _preflight_backend_identities(report: dict[str, Any]) -> dict[str, Any] | None:
    """Return the qualified source identity and each target/prior signature."""
    study_identity = None
    by_arm = {}
    for case in report.get("cases", []):
        if not isinstance(case, dict) or case.get("success") is not True:
            continue
        backend = case.get("backend_provenance")
        if not isinstance(backend, dict):
            return None
        identity = shared._optimizer_backend_identity(backend)
        study = shared._optimizer_backend_study_identity(backend)
        key = f"{case.get('target')}:{case.get('noise_prior')}"
        if identity is None or study is None:
            return None
        if key in by_arm and by_arm[key] != identity:
            return None
        if study_identity is not None and study_identity != study:
            return None
        by_arm[key] = identity
        study_identity = study
    if len(by_arm) != 4:
        return None
    return {"study": study_identity, "by_target_and_prior": by_arm}


def _expected_backend_identity(
    preflight_gate: dict[str, Any], arm: dict[str, Any]
) -> dict[str, Any] | None:
    identities = preflight_gate.get("backend_identities")
    by_arm = identities.get("by_target_and_prior") if isinstance(identities, dict) else None
    if not isinstance(by_arm, dict):
        return None
    return by_arm.get(f"{arm['optimizer_target']}:{arm['surrogate_noise_prior']}")


def _record_preflight_attempt(
    document: dict[str, Any], study_sha256: str, protocol: dict[str, Any], attempt: int
) -> dict[str, Any]:
    protocol_sha = campaign_protocol.campaign_protocol_sha256(protocol["spec"])
    request = _preflight_request(study_sha256, attempt, protocol_sha)
    report = None
    failure = None
    started = time.perf_counter()
    try:
        backend = importlib.import_module("measure_service.optimizer_backend")
        qualify = getattr(backend, "preflight_ablation_backend", None)
        if not callable(qualify):
            raise RuntimeError("The installed optimizer backend lacks the ablation preflight API")
        report = qualify(protocol["spec"])
        qualified, validation_error = _preflight_report_status(
            report, document, study_sha256, protocol_sha
        )
        if validation_error is not None:
            failure = {"type": "InvalidPreflightReport", "message": validation_error}
        backend_identities = (
            _preflight_backend_identities(report)
            if qualified and isinstance(report, dict)
            else None
        )
        if qualified and backend_identities is None:
            qualified = False
            failure = {
                "type": "InvalidPreflightReport",
                "message": "qualified preflight omitted stable source/settings identity",
            }
    except Exception as error:
        qualified = False
        failure = shared._error_record(error)
        backend_identities = None
    elapsed = time.perf_counter() - started
    response = {
        "schema_version": 1,
        "record_type": "ablation_backend_preflight",
        "study_sha256": study_sha256,
        "attempt": attempt,
        "request": request,
        "qualified": qualified,
        "backend_report": report,
        "backend_identities": backend_identities if qualified else None,
        "failure": failure,
        "runner_elapsed_seconds": elapsed,
    }
    key = _preflight_key(study_sha256, attempt)
    stored = registry_mutation(OWNER, key, request, lambda con: response)
    if not isinstance(stored, dict) or stored.get("record_type") != "ablation_backend_preflight":
        raise ValueError("stored preflight receipt has an unsupported response")
    return stored


def _load_preflight_history(study_sha256: str, document: dict[str, Any]) -> list[dict[str, Any]]:
    with registry_connection() as con:
        rows = con.execute(
            "SELECT request_key, digest, response FROM registry_receipts "
            "WHERE owner=? AND request_key LIKE ? ORDER BY request_key",
            (OWNER, f"syn-abl-{study_sha256}-preflight-%"),
        ).fetchall()
    history = []
    for row in rows:
        try:
            response = json.loads(row["response"])
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("stored preflight receipt response is malformed") from error
        if (
            not isinstance(response, dict)
            or response.get("record_type") != "ablation_backend_preflight"
        ):
            raise ValueError("preflight receipt namespace contains an unknown response")
        attempt = response.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("stored preflight receipt has an invalid attempt number")
        request = response.get("request")
        if (
            row["request_key"] != _preflight_key(study_sha256, attempt)
            or not isinstance(request, dict)
            or request.get("study_sha256") != study_sha256
            or request.get("attempt") != attempt
            or not isinstance(request.get("protocol_sha256"), str)
            or len(request["protocol_sha256"]) != 64
            or sha256(request) != row["digest"]
            or response.get("study_sha256") != study_sha256
        ):
            raise ValueError("stored preflight receipt request identity is inconsistent")
        if not isinstance(response.get("qualified"), bool):
            raise ValueError("stored preflight receipt has no qualification decision")
        backend_report = response.get("backend_report")
        if backend_report is None:
            if response["qualified"] or not isinstance(response.get("failure"), dict):
                raise ValueError("stored backend preflight failure evidence is incomplete")
        else:
            report_qualified, report_error = _preflight_report_status(
                backend_report,
                document,
                study_sha256,
                request["protocol_sha256"],
            )
            if response["qualified"] and (not report_qualified or report_error is not None):
                raise ValueError("stored successful preflight receipt failed report validation")
            if not response["qualified"] and report_qualified and report_error is None:
                raise ValueError("stored preflight qualification conflicts with its route evidence")
            expected_identities = (
                _preflight_backend_identities(backend_report)
                if response["qualified"] and report_qualified and report_error is None
                else None
            )
            if response.get("backend_identities") != expected_identities:
                raise ValueError("stored preflight backend identity differs from route evidence")
        history.append(response)
    history.sort(key=lambda item: item["attempt"])
    if [item["attempt"] for item in history] != list(range(1, len(history) + 1)):
        raise ValueError("stored preflight receipt attempts are not contiguous")
    return history


def _stored_campaign_index(
    protocols: dict[str, dict[str, Any]], document: dict[str, Any]
) -> dict[tuple[str, str, int], dict[str, Any]]:
    protocol_ids = {arm_id: entry["protocol"]["protocol_id"] for arm_id, entry in protocols.items()}
    expected = {
        (protocol_ids[arm["arm_id"]], task["task_id"], seed, arm["policy"]): (
            arm["arm_id"],
            task["task_id"],
            seed,
        )
        for arm in document["arms"]
        for task in document["tasks"]
        for seed in document["seeds"]
    }
    with registry_connection() as con:
        rows = con.execute(
            "SELECT id, protocol_id, task_id, seed, policy FROM campaign_runs "
            "WHERE owner=? ORDER BY protocol_id, task_id, seed, policy",
            (OWNER,),
        ).fetchall()
    stored: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in rows:
        database_key = (row["protocol_id"], row["task_id"], row["seed"], row["policy"])
        if database_key not in expected:
            raise ValueError("ablation registry contains a campaign outside the frozen plan")
        identity = expected[database_key]
        if identity in stored:
            raise ValueError("ablation registry contains duplicate campaign identities")
        record = campaigns.get_campaign(OWNER, row["id"])
        if (
            record["campaign_id"] != row["id"]
            or record["protocol_id"] != row["protocol_id"]
            or record["task_id"] != row["task_id"]
            or record["seed"] != row["seed"]
            or record["policy"] != row["policy"]
        ):
            raise ValueError("stored campaign differs from the frozen task-arm identity")
        stored[identity] = record
    return stored


def _campaign_set_complete(
    stored: dict[tuple[str, str, int], dict[str, Any]],
) -> bool:
    return len(stored) == PLANNED_CAMPAIGN_COUNT and all(
        record["state"] == "budget_complete" and record["consumed_evaluations"] == 10
        for record in stored.values()
    )


def _campaign_labels(campaign: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    result = dict(campaign)
    result.update(
        {
            "arm_id": plan["arm_id"],
            "cohort_role": plan["cohort_role"],
            "optimizer_target_config": plan["optimizer_target_config"],
            "surrogate_noise_prior": plan["surrogate_noise_prior"],
            "arm_operation_sha256": plan["arm_operation_sha256"],
        }
    )
    for evaluation in result["evaluations"]:
        evaluation.update(
            {
                "arm_id": plan["arm_id"],
                "cohort_role": plan["cohort_role"],
                "optimizer_target_config": plan["optimizer_target_config"],
                "surrogate_noise_prior": plan["surrogate_noise_prior"],
            }
        )
        consumption = evaluation.get("consumption")
        consumption_result = consumption.get("result") if isinstance(consumption, dict) else None
        target = (
            consumption_result.get("optimizer_target")
            if isinstance(consumption_result, dict)
            else None
        )
        evaluation["optimizer_target_record"] = target
        metrics = evaluation.get("metrics")
        if isinstance(metrics, dict):
            metrics["optimizer_target_record"] = target
    return result


def _placeholder(plan: dict[str, Any], budget: int, error: dict[str, str]) -> dict[str, Any]:
    evaluations = []
    for ordinal in range(budget):
        row = shared._empty_evaluation(
            campaign_id=None,
            task_id=plan["task_id"],
            seed=plan["seed"],
            policy=plan["policy"],
            ordinal=ordinal,
            budget=budget,
            status="preflight_blocked_unknown",
        )
        row.update(
            {
                "arm_id": plan["arm_id"],
                "cohort_role": plan["cohort_role"],
                "optimizer_target_config": plan["optimizer_target_config"],
                "surrogate_noise_prior": plan["surrogate_noise_prior"],
                "optimizer_target_record": None,
                "failure_step": "preflight_gate",
                "failure": error,
            }
        )
        evaluations.append(row)
    return {
        "campaign_id": None,
        "task_id": plan["task_id"],
        "seed": plan["seed"],
        "policy": plan["policy"],
        "arm_id": plan["arm_id"],
        "cohort_role": plan["cohort_role"],
        "optimizer_target_config": plan["optimizer_target_config"],
        "surrogate_noise_prior": plan["surrogate_noise_prior"],
        "arm_operation_sha256": plan["arm_operation_sha256"],
        "status": "partial_failure",
        "failure": {"ordinal": 0, "step": "preflight_gate", **error},
        "evaluation_budget": budget,
        "consumed_evaluations": 0,
        "budget_remaining": budget,
        "completed_simulator_attempt_count": 0,
        "unconsumed_completed_simulator_attempt_count": 0,
        "recovered_interruptions": [],
        "pending_proposal": None,
        "campaign_revision": None,
        "campaign_state": None,
        "history": [],
        "evaluations": evaluations,
        "trajectory_summaries": {series: None for series in SUCCESS_SERIES},
    }


def _validate_counts(
    campaigns_: list[dict[str, Any]],
    evaluations: list[dict[str, Any]],
    plans: list[dict[str, Any]],
) -> None:
    if len(campaigns_) != PLANNED_CAMPAIGN_COUNT or len(evaluations) != PLANNED_EVALUATION_COUNT:
        raise ValueError(
            "ablation evidence does not retain all sixty campaigns and six hundred slots"
        )
    expected_campaigns = {
        (campaign["arm_id"], campaign["task_id"], campaign["seed"]) for campaign in campaigns_
    }
    if len(expected_campaigns) != PLANNED_CAMPAIGN_COUNT:
        raise ValueError("ablation campaign evidence has duplicate arm-task-seed identities")
    planned_campaigns = {(item["arm_id"], item["task_id"], item["seed"]) for item in plans}
    if expected_campaigns != planned_campaigns:
        raise ValueError("ablation campaign identities differ from the frozen plan")
    expected_evaluations = {
        (row["arm_id"], row["task_id"], row["seed"], row["ordinal"]) for row in evaluations
    }
    if len(expected_evaluations) != PLANNED_EVALUATION_COUNT:
        raise ValueError("ablation evaluation evidence has duplicate planned identities")
    planned_evaluations = {
        (item["arm_id"], item["task_id"], item["seed"], ordinal)
        for item in plans
        for ordinal in range(10)
    }
    if expected_evaluations != planned_evaluations:
        raise ValueError("ablation evaluation identities differ from the frozen plan")
    for campaign in campaigns_:
        members = [
            row
            for row in evaluations
            if (row["arm_id"], row["task_id"], row["seed"])
            == (campaign["arm_id"], campaign["task_id"], campaign["seed"])
        ]
        if len(members) != campaign["evaluation_budget"] or sorted(
            row["ordinal"] for row in members
        ) != list(range(campaign["evaluation_budget"])):
            raise ValueError("ablation campaign and evaluation rows disagree on budget ordinals")
        if any(row["campaign_id"] != campaign["campaign_id"] for row in members):
            raise ValueError("ablation campaign and evaluation rows disagree on campaign ID")
        if campaign["consumed_evaluations"] != sum(row["status"] == "consumed" for row in members):
            raise ValueError("ablation consumption count differs from the retained evaluation rows")


def _trajectory_rows(
    campaigns_: list[dict[str, Any]], document: dict[str, Any]
) -> list[dict[str, Any]]:
    rows = []
    budget = document["evaluation_budget"]
    for arm in document["arms"]:
        arm_records = [item for item in campaigns_ if item["arm_id"] == arm["arm_id"]]
        for cohort in COHORTS:
            group = (
                arm_records
                if cohort == "combined"
                else [item for item in arm_records if item["cohort_role"] == cohort]
            )
            planned_count = len(document["tasks"]) * len(document["seeds"])
            if cohort != "combined":
                planned_count = sum(
                    item["cohort_role"] == cohort for item in document["tasks"]
                ) * len(document["seeds"])
            for series in SUCCESS_SERIES:
                summaries = [
                    item["trajectory_summaries"][series]
                    for item in group
                    if item["trajectory_summaries"].get(series) is not None
                ]
                points = aggregate_success_trajectories(
                    summaries, planned_count=planned_count, budget=budget
                )
                rows.extend(
                    {
                        "arm_id": arm["arm_id"],
                        "cohort_role": cohort,
                        "optimizer_target_config": arm["optimizer_target"],
                        "surrogate_noise_prior": arm["surrogate_noise_prior"],
                        "series": series,
                        **point,
                    }
                    for point in points
                )
    return rows


def _contrast_definitions(document: dict[str, Any]) -> list[dict[str, Any]]:
    by_config = {
        (arm["optimizer_target"], arm["surrogate_noise_prior"]): arm
        for arm in document["arms"]
        if arm["policy"] == "bo_mcp_gp_logei"
    }
    control = next(arm for arm in document["arms"] if arm["policy"] == "random")
    definitions = [
        {
            "contrast_id": "target-default-noise",
            "contrast_kind": "target_within_noise_prior",
            "reference": by_config[("historical_utility", "default")],
            "comparison": by_config[("confirmed_acceptance_reward", "default")],
            "interpretation": "optimizer target contrast at the default inferred-noise prior",
        },
        {
            "contrast_id": "target-low-noise-prior",
            "contrast_kind": "target_within_noise_prior",
            "reference": by_config[("historical_utility", "low_noise")],
            "comparison": by_config[("confirmed_acceptance_reward", "low_noise")],
            "interpretation": "optimizer target contrast at the low-noise inferred prior",
        },
        {
            "contrast_id": "noise-historical-utility",
            "contrast_kind": "noise_prior_within_target",
            "reference": by_config[("historical_utility", "default")],
            "comparison": by_config[("historical_utility", "low_noise")],
            "interpretation": "surrogate noise-prior contrast for the historical utility target",
        },
        {
            "contrast_id": "noise-confirmed-reward",
            "contrast_kind": "noise_prior_within_target",
            "reference": by_config[("confirmed_acceptance_reward", "default")],
            "comparison": by_config[("confirmed_acceptance_reward", "low_noise")],
            "interpretation": "surrogate noise-prior contrast for confirmed acceptance reward",
        },
    ]
    for arm in document["arms"]:
        if arm["policy"] == "random":
            continue
        definitions.append(
            {
                "contrast_id": f"random-control-vs-{arm['arm_id']}",
                "contrast_kind": "joint_policy_vs_random_control",
                "reference": control,
                "comparison": arm,
                "interpretation": (
                    "joint policy contrast against the shared random control; target and/or "
                    "surrogate-noise settings differ"
                ),
            }
        )
    return definitions


def _paired_contrasts(
    campaigns_: list[dict[str, Any]], document: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    indexed = {(item["arm_id"], item["task_id"], item["seed"]): item for item in campaigns_}
    task_roles = {item["task_id"]: item["cohort_role"] for item in document["tasks"]}
    pairs = []
    for contrast in _contrast_definitions(document):
        reference = contrast["reference"]["arm_id"]
        comparison = contrast["comparison"]["arm_id"]
        for task_id, cohort_role in task_roles.items():
            for seed in document["seeds"]:
                left = indexed[(reference, task_id, seed)]
                right = indexed[(comparison, task_id, seed)]
                for series in SUCCESS_SERIES:
                    left_summary = left["trajectory_summaries"].get(series)
                    right_summary = right["trajectory_summaries"].get(series)
                    left_state = (
                        "unknown"
                        if left_summary is None
                        else left_summary["trajectory"][-1]["success_state"]
                    )
                    right_state = (
                        "unknown"
                        if right_summary is None
                        else right_summary["trajectory"][-1]["success_state"]
                    )
                    left_first = (
                        None
                        if left_summary is None or left_summary["first_confirmed_success"] is None
                        else left_summary["first_confirmed_success"]["high_fidelity_index"]
                    )
                    right_first = (
                        None
                        if right_summary is None or right_summary["first_confirmed_success"] is None
                        else right_summary["first_confirmed_success"]["high_fidelity_index"]
                    )
                    pairs.append(
                        {
                            "contrast_id": contrast["contrast_id"],
                            "contrast_kind": contrast["contrast_kind"],
                            "reference_arm_id": reference,
                            "comparison_arm_id": comparison,
                            "reference_campaign_id": left.get("campaign_id"),
                            "comparison_campaign_id": right.get("campaign_id"),
                            "task_id": task_id,
                            "cohort_role": cohort_role,
                            "seed": seed,
                            "series": series,
                            "reference_state_at_budget": left_state,
                            "comparison_state_at_budget": right_state,
                            "state_changed": left_state != right_state,
                            "reference_first_confirmed_index": left_first,
                            "comparison_first_confirmed_index": right_first,
                            "comparison_minus_reference_first_index": (
                                None
                                if left_first is None or right_first is None
                                else right_first - left_first
                            ),
                            "interpretation": contrast["interpretation"],
                            "inference_scope": (
                                "descriptive matched task-seed pair; not a population estimate"
                            ),
                        }
                    )
    summaries = []
    for contrast in _contrast_definitions(document):
        for series in SUCCESS_SERIES:
            for cohort in COHORTS:
                selected = [
                    row
                    for row in pairs
                    if row["contrast_id"] == contrast["contrast_id"]
                    and row["series"] == series
                    and (cohort == "combined" or row["cohort_role"] == cohort)
                ]
                summaries.append(
                    {
                        "contrast_id": contrast["contrast_id"],
                        "contrast_kind": contrast["contrast_kind"],
                        "cohort_role": cohort,
                        "series": series,
                        "planned_pair_count": (
                            PLANNED_CAMPAIGNS_PER_ARM
                            if cohort == "combined"
                            else PLANNED_CAMPAIGNS_PER_ARM // 2
                        ),
                        "recorded_pair_count": sum(
                            row["reference_campaign_id"] is not None
                            and row["comparison_campaign_id"] is not None
                            for row in selected
                        ),
                        "state_changed_pair_count": sum(row["state_changed"] for row in selected),
                        "both_confirmed_by_budget_count": sum(
                            row["reference_state_at_budget"] == "confirmed_success"
                            and row["comparison_state_at_budget"] == "confirmed_success"
                            for row in selected
                        ),
                        "unknown_reference_pair_count": sum(
                            row["reference_state_at_budget"] == "unknown" for row in selected
                        ),
                        "unknown_comparison_pair_count": sum(
                            row["comparison_state_at_budget"] == "unknown" for row in selected
                        ),
                        "interpretation": contrast["interpretation"],
                        "inference_scope": (
                            "descriptive matched task-seed pairs; not a population estimate"
                        ),
                    }
                )
    return pairs, summaries


def _arm_metrics(
    campaign_records: list[dict[str, Any]],
    *,
    arm: dict[str, Any],
    planned_campaign_count: int,
    budget: int,
) -> dict[str, Any]:
    evaluations = [row for record in campaign_records for row in record["evaluations"]]
    consumed = [row for row in evaluations if row.get("status") == "consumed"]
    completed_attempts = [
        row
        for row in evaluations
        if row.get("metrics", {}).get("completed_simulator_attempt") is True
    ]
    consumed_metrics = [row.get("metrics", {}) for row in consumed]
    attempt_metrics = [row.get("metrics", {}) for row in completed_attempts]
    all_metrics = [
        row.get("metrics", {}) for row in evaluations if isinstance(row.get("metrics"), dict)
    ]
    acceptances = [item.get("analytical_acceptance") for item in consumed_metrics]
    strict = [item.get("strict_analytical_success") for item in consumed_metrics]
    utilities = [(item.get("utility_status"), item.get("score")) for item in consumed_metrics]
    utility_values = [
        value
        for status, value in utilities
        if status == "scored"
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ]
    target_records = [
        row.get("optimizer_target_record")
        for row in consumed
        if isinstance(row.get("optimizer_target_record"), dict)
    ]
    target_values = [
        item["value"]
        for item in target_records
        if item.get("status") == "scored"
        and isinstance(item.get("value"), (int, float))
        and not isinstance(item.get("value"), bool)
        and math.isfinite(item["value"])
    ]
    costs = [
        item["declared_evaluation_cost"]
        for item in attempt_metrics
        if isinstance(item.get("declared_evaluation_cost"), (int, float))
        and not isinstance(item["declared_evaluation_cost"], bool)
        and math.isfinite(item["declared_evaluation_cost"])
    ]
    times = [
        item["simulated_acquisition_time_min"]
        for item in attempt_metrics
        if isinstance(item.get("simulated_acquisition_time_min"), (int, float))
        and not isinstance(item["simulated_acquisition_time_min"], bool)
        and math.isfinite(item["simulated_acquisition_time_min"])
    ]
    walltimes = [
        item["optimizer_walltime_seconds"]
        for item in all_metrics
        if isinstance(item.get("optimizer_walltime_seconds"), (int, float))
        and not isinstance(item["optimizer_walltime_seconds"], bool)
        and math.isfinite(item["optimizer_walltime_seconds"])
    ]
    solvent_totals: dict[str, list[float]] = {}
    for item in attempt_metrics:
        values = item.get("declared_solvent_use_ml")
        if not isinstance(values, dict):
            continue
        for name, volume in values.items():
            if (
                isinstance(volume, (int, float))
                and not isinstance(volume, bool)
                and math.isfinite(volume)
                and volume >= 0
            ):
                solvent_totals.setdefault(name, []).append(float(volume))
    return {
        "planned_campaign_count": planned_campaign_count,
        "recorded_campaign_count": sum(
            item.get("campaign_id") is not None for item in campaign_records
        ),
        "budget_complete_campaign_count": sum(
            item["status"] == "budget_complete" for item in campaign_records
        ),
        "partial_failure_campaign_count": sum(
            item["status"] != "budget_complete" for item in campaign_records
        ),
        "planned_evaluation_count": planned_campaign_count * budget,
        "consumed_evaluation_count": len(consumed),
        "completed_simulator_attempt_count": len(completed_attempts),
        "unconsumed_completed_simulator_attempt_count": sum(
            row.get("status") != "consumed" for row in completed_attempts
        ),
        "analytical_acceptance_counts": {
            name: sum(value == name for value in acceptances)
            for name in ("accepted", "not_accepted", "unknown")
        },
        "strict_analytical_success_count": sum(value is True for value in strict),
        "strict_analytical_no_success_count": sum(value is False for value in strict),
        "strict_analytical_unknown_count": sum(value is None for value in strict)
        + (planned_campaign_count * budget - len(consumed)),
        "historical_utility_scored_count": sum(status == "scored" for status, _ in utilities),
        "historical_utility_unscored_count": sum(status != "scored" for status, _ in utilities),
        "historical_utility_unknown_planned_evaluation_count": planned_campaign_count * budget
        - sum(status == "scored" for status, _ in utilities),
        "historical_utility_score_total": math.fsum(float(value) for value in utility_values)
        if utility_values
        else None,
        "historical_utility_score_mean_scored": math.fsum(float(value) for value in utility_values)
        / len(utility_values)
        if utility_values
        else None,
        "historical_utility_score_ge_2_count": sum(
            status == "scored"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 2.0
            for status, value in utilities
        ),
        "optimizer_target_config": arm["optimizer_target"],
        "optimizer_target_record_name": (target_records[0].get("name") if target_records else None),
        "optimizer_target_profile": (target_records[0].get("profile") if target_records else None),
        "optimizer_target_scored_count": len(target_values),
        "optimizer_target_value_total": math.fsum(float(value) for value in target_values)
        if target_values
        else None,
        "optimizer_target_value_mean": math.fsum(float(value) for value in target_values)
        / len(target_values)
        if target_values
        else None,
        "optimizer_target_status_counts": dict(
            Counter(
                item["status"] for item in target_records if isinstance(item.get("status"), str)
            )
        ),
        "declared_evaluation_cost_total_completed_attempts": math.fsum(costs) if costs else None,
        "declared_evaluation_cost_unit": next(
            (
                item.get("declared_evaluation_cost_unit")
                for item in attempt_metrics
                if isinstance(item.get("declared_evaluation_cost_unit"), str)
            ),
            None,
        ),
        "declared_evaluation_cost_known_completed_attempt_count": len(costs),
        "declared_evaluation_cost_unknown_completed_attempt_count": len(completed_attempts)
        - len(costs),
        "simulated_acquisition_time_total_min_completed_attempts": math.fsum(times)
        if times
        else None,
        "simulated_acquisition_time_known_completed_attempt_count": len(times),
        "simulated_acquisition_time_unknown_completed_attempt_count": len(completed_attempts)
        - len(times),
        "declared_solvent_use_ml_total_by_solvent_completed_attempts": {
            name: math.fsum(values) for name, values in sorted(solvent_totals.items())
        },
        "declared_solvent_volume_known_completed_attempt_count": sum(
            isinstance(item.get("declared_solvent_use_ml"), dict) for item in attempt_metrics
        ),
        "declared_solvent_volume_total_ml_completed_attempts": (
            math.fsum(math.fsum(values) for values in solvent_totals.values())
            if solvent_totals
            else None
        ),
        "declared_solvent_volume_unknown_completed_attempt_count": len(completed_attempts)
        - sum(isinstance(item.get("declared_solvent_use_ml"), dict) for item in attempt_metrics),
        "optimizer_walltime_sample_count": len(walltimes),
        "optimizer_walltime_total_seconds": math.fsum(walltimes) if walltimes else None,
        "optimizer_walltime_mean_seconds": math.fsum(walltimes) / len(walltimes)
        if walltimes
        else None,
        "timing_scope": (
            "Optimizer walltime is persisted proposal walltime; acquisition duration and declared "
            "solvent volume cover completed synthetic attempts. None are physical instrument, "
            "solvent-spend or CPU-time measurements."
        ),
    }


def _all_arm_metrics(campaigns_: list[dict[str, Any]], document: dict[str, Any]) -> dict[str, Any]:
    output = {}
    for arm in document["arms"]:
        records = [item for item in campaigns_ if item["arm_id"] == arm["arm_id"]]
        output[arm["arm_id"]] = {
            "arm": arm,
            "cohorts": {
                cohort: _arm_metrics(
                    records
                    if cohort == "combined"
                    else [item for item in records if item["cohort_role"] == cohort],
                    arm=arm,
                    planned_campaign_count=(
                        PLANNED_CAMPAIGNS_PER_ARM
                        if cohort == "combined"
                        else PLANNED_CAMPAIGNS_PER_ARM // 2
                    ),
                    budget=document["evaluation_budget"],
                )
                for cohort in COHORTS
            },
        }
    return output


def _evaluation_csv_row(evaluation: dict[str, Any]) -> dict[str, Any]:
    row = shared._evaluation_csv_row(evaluation)
    target = evaluation.get("optimizer_target_record")
    row.update(
        {
            "arm_id": evaluation.get("arm_id"),
            "cohort_role": evaluation.get("cohort_role"),
            "optimizer_target_config": evaluation.get("optimizer_target_config"),
            "surrogate_noise_prior": evaluation.get("surrogate_noise_prior"),
            "optimizer_target_name": target.get("name") if isinstance(target, dict) else None,
            "optimizer_target_profile": (
                target.get("profile") if isinstance(target, dict) else None
            ),
            "optimizer_target_status": target.get("status") if isinstance(target, dict) else None,
            "optimizer_target_value": target.get("value") if isinstance(target, dict) else None,
        }
    )
    return {field: row.get(field) for field in _EVALUATION_CSV_FIELDS}


def _backend_calls(evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "arm_id": item["arm_id"],
            "task_id": item["task_id"],
            "seed": item["seed"],
            "ordinal": item["ordinal"],
            "proposal_id": item.get("proposal_id"),
            "backend": item["backend"],
        }
        for item in evaluations
        if isinstance(item.get("backend"), dict)
    ]


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Matched synthetic optimizer-target and noise-prior ablation",
        "",
        f"Run status: **{report['status']}**",
        "",
        "The predeclared study includes 60 campaigns and 600 high-fidelity synthetic evaluations, "
        "including each arm's three shared initial-design points. Cohort summaries separate the two "
        "regression tasks from the two unfiltered held-out tasks and also show their pooled "
        "constructed-task summary.",
        "",
        "Historical utility and strict analytical acceptance are reported separately. The confirmed "
        "acceptance reward is a conservative observed-confirmation target; unknown assessment is "
        "retained separately and does not label an analyte failure. Missing or failed evaluations "
        "are not zero-valued optimizer training records.",
        "",
        "Paired target contrasts hold the noise prior fixed; paired noise-prior contrasts hold the "
        "target fixed. Each GP-versus-random comparison changes a joint policy and does not isolate "
        "one causal factor. The 12 task-seed pairs are descriptive, not a population estimate or "
        "general optimizer ranking.",
        "",
        "Preflight uses fabricated histories to qualify model-guided routes. Its candidates and "
        "timings are readiness evidence, not campaign evaluations or experiment spend. Campaign "
        "acquisition durations and declared method solvent volumes are synthetic; optimizer "
        "walltime is reported separately and is not CPU time.",
        "",
        "The study uses constructed retention equations, assumed Gaussian widths, a latent "
        "simulator, no measurement noise, and no physical instrument. It does not establish "
        "manuscript-protocol match, physical-model fit, trace simulation or instrument qualification.",
        "",
        "## Planned and observed counts",
        "",
        f"- Planned campaigns: {report['planned_denominators']['campaigns_total']}.",
        f"- Planned synthetic evaluation slots: {report['planned_denominators']['evaluations_total']}.",
        f"- Completed campaigns: {report['completed_campaign_count']}.",
        f"- Consumed observations: {report['consumed_evaluation_count']}.",
        f"- Completed simulator attempts: {report['completed_simulator_attempt_count']}.",
        "",
        "Per-arm and cohort metrics, full evaluation lineage, paired contrasts and preflight cases "
        "are in the sealed evidence files.",
    ]
    return "\n".join(lines) + "\n"


def run_synthetic_ablations(output_dir: str | Path, *, data_dir: str | Path) -> dict[str, Any]:
    """Run or resume the fixed 60-campaign matched ablation study.

    The study declaration is bundled and SHA256-verified. The explicit data
    directory is claimed for this profile before any registry write. A failed
    preflight blocks new proposals; stored consumed outcomes and completed
    attempts remain available in a recovery-only pass.
    """
    document, study_sha256 = _read_study_document()
    target = shared._preflight_output(output_dir)
    data_path = shared._validate_data_dir(data_dir, target)
    data_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    plans = _plan(document, study_sha256)
    receipt_scopes = _receipt_scopes(document, study_sha256)
    shared._claim_or_verify_owned_store(
        data_path,
        study_sha256,
        profile=STORE_PROFILE,
        owner=OWNER,
        receipt_prefixes=receipt_scopes,
    )

    with staged_bundle(target, input_dirs=(DEFAULT_DOCUMENT.parent, data_path)) as stage:
        with shared._scoped_data_dir(data_path):
            _validate_existing_plan(document, study_sha256)
            registration_failure = None
            try:
                protocols, task_bindings, protocol_rows = _register_inputs(document, study_sha256)
                common_design = _common_initial_design(document, protocols)
            except Exception as error:
                registration_failure = shared._error_record(
                    error, private_paths=(data_path, DEFAULT_DOCUMENT.parent)
                )
                protocols = {}
                task_bindings = []
                protocol_rows = []
                common_design = []

            stored = {}
            if protocols:
                stored = _stored_campaign_index(protocols, document)

            complete_before_run = not registration_failure and _campaign_set_complete(stored)
            preflight_history = []
            preflight_gate = None
            preflight_reused = False
            if protocols and not registration_failure:
                try:
                    preflight_history = _load_preflight_history(study_sha256, document)
                    saved_qualified = [item for item in preflight_history if item["qualified"]]
                    if complete_before_run and saved_qualified:
                        preflight_gate = saved_qualified[-1]
                        preflight_reused = True
                    else:
                        previous_attempt = max(
                            (item["attempt"] for item in preflight_history), default=0
                        )
                        preflight_gate = _record_preflight_attempt(
                            document,
                            study_sha256,
                            protocols[document["arms"][0]["arm_id"]]["protocol"],
                            previous_attempt + 1,
                        )
                        preflight_history = _load_preflight_history(study_sha256, document)
                except Exception as error:
                    registration_failure = shared._error_record(
                        error, private_paths=(data_path, DEFAULT_DOCUMENT.parent)
                    )

            preflight_qualified = bool(preflight_gate and preflight_gate["qualified"])
            prior_qualified_gates = [item for item in preflight_history if item["qualified"]]
            study_consistent = None
            if preflight_qualified:
                original_identity = prior_qualified_gates[0]["backend_identities"]["study"]
                selected_identity = preflight_gate["backend_identities"]["study"]
                study_consistent = original_identity == selected_identity
            gate_allowed = preflight_qualified and study_consistent is True
            gate_error = (
                None
                if gate_allowed
                else {
                    "type": (
                        "PreflightNotQualified"
                        if not preflight_qualified
                        else "PreflightStudyIdentityMismatch"
                    ),
                    "message": (
                        "The ablation backend did not qualify all sixteen frozen model-guided routes."
                        if preflight_gate is not None and not preflight_qualified
                        else (
                            "No successful preflight receipt is available."
                            if preflight_gate is None
                            else "Current backend source or fixed settings differ from the first successful study preflight."
                        )
                    ),
                }
            )
            campaign_records = []
            for plan in plans:
                identity = (plan["arm_id"], plan["task_id"], plan["seed"])
                protocol_entry = protocols.get(plan["arm_id"])
                stored_campaign = stored.get(identity)
                if registration_failure:
                    record = _placeholder(plan, document["evaluation_budget"], registration_failure)
                elif gate_allowed:
                    expected_backend_identity = (
                        _expected_backend_identity(preflight_gate, plan["arm"])
                        if plan["policy"] == "bo_mcp_gp_logei"
                        else None
                    )
                    record = shared._execute_campaign(
                        owner=OWNER,
                        protocol_id=protocol_entry["protocol"]["protocol_id"],
                        task_id=plan["task_id"],
                        seed=plan["seed"],
                        policy=plan["policy"],
                        default_sha256=plan["arm_operation_sha256"],
                        planned_budget=document["evaluation_budget"],
                        private_paths=(data_path, DEFAULT_DOCUMENT.parent),
                        expected_backend_identity=expected_backend_identity,
                    )
                    record = _campaign_labels(record, plan)
                elif stored_campaign is not None and protocol_entry is not None:
                    record = shared._execute_campaign(
                        owner=OWNER,
                        protocol_id=protocol_entry["protocol"]["protocol_id"],
                        task_id=plan["task_id"],
                        seed=plan["seed"],
                        policy=plan["policy"],
                        default_sha256=plan["arm_operation_sha256"],
                        planned_budget=document["evaluation_budget"],
                        private_paths=(data_path, DEFAULT_DOCUMENT.parent),
                        recovery_only=True,
                    )
                    record = _campaign_labels(record, plan)
                else:
                    record = _placeholder(
                        plan,
                        document["evaluation_budget"],
                        gate_error or registration_failure or {"type": "Unknown", "message": ""},
                    )
                campaign_records.append(record)

            evaluations = [
                evaluation
                for campaign in campaign_records
                for evaluation in campaign["evaluations"]
            ]
            _validate_counts(campaign_records, evaluations, plans)
            trajectory_rows = _trajectory_rows(campaign_records, document)
            paired_rows, contrast_summaries = _paired_contrasts(campaign_records, document)
            arm_metrics = _all_arm_metrics(campaign_records, document)
            backend_calls = _backend_calls(evaluations)
            initial_design_verification = _observed_initial_design(
                campaign_records, document, common_design
            )
            complete = (
                gate_allowed
                and registration_failure is None
                and initial_design_verification["complete_verification"]
                and all(item["status"] == "budget_complete" for item in campaign_records)
            )
            failures = [
                {
                    "arm_id": item["arm_id"],
                    "task_id": item["task_id"],
                    "seed": item["seed"],
                    "campaign_id": item["campaign_id"],
                    "consumed_evaluations": item["consumed_evaluations"],
                    "failure": item.get("failure"),
                }
                for item in campaign_records
                if item["status"] != "budget_complete"
            ]
            protocol_validation_failures = []
            if initial_design_verification["status"] == "mismatch":
                protocol_validation_failures.append(
                    {
                        "step": "observed_initial_design",
                        "type": "InitialDesignMismatch",
                        "mismatched_candidate_count": initial_design_verification[
                            "mismatched_candidate_count"
                        ],
                    }
                )
            report = {
                "schema_version": 1,
                "profile": PROFILE,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "completed": complete,
                "status": "budget_complete" if complete else "partial_failure",
                "study_input": {
                    "bundled_path": "reference_data/synthetic-isocratic-ablation-v1.json",
                    "sha256": study_sha256,
                    "selection": document["selection"],
                    "scope": document["scope"],
                },
                "planned_denominators": {
                    "campaigns_total": PLANNED_CAMPAIGN_COUNT,
                    "campaigns_per_arm": PLANNED_CAMPAIGNS_PER_ARM,
                    "evaluations_total": PLANNED_EVALUATION_COUNT,
                    "evaluations_per_arm": PLANNED_EVALUATIONS_PER_ARM,
                    "evaluation_budget_per_campaign": document["evaluation_budget"],
                    "initial_design_size_per_campaign": document["initial_design_size"],
                    "stopping_policy": document["stopping_policy"],
                },
                "preflight": {
                    "qualified": preflight_qualified,
                    "study_consistent": study_consistent,
                    "study_backend_identity_sha256": (
                        None
                        if not preflight_qualified
                        else sha256(preflight_gate["backend_identities"]["study"])
                    ),
                    "reused_saved_gate": preflight_reused,
                    "attempt_count": len(preflight_history),
                    "selected_attempt": None
                    if preflight_gate is None
                    else preflight_gate["attempt"],
                    "separate_from_campaign_evaluations": True,
                    "failure": gate_error,
                },
                "observed_initial_design": initial_design_verification,
                "arm_metrics": arm_metrics,
                "contrast_summaries": contrast_summaries,
                "backend_campaign_calls": backend_calls,
                "completed_campaign_count": sum(
                    item["status"] == "budget_complete" for item in campaign_records
                ),
                "consumed_evaluation_count": sum(
                    item["status"] == "consumed" for item in evaluations
                ),
                "completed_simulator_attempt_count": sum(
                    item.get("metrics", {}).get("completed_simulator_attempt") is True
                    for item in evaluations
                ),
                "campaigns": campaign_records,
                "failures": failures,
                "protocol_validation_failures": protocol_validation_failures,
                "scientific_scope": {
                    "target_and_utility_are_separate": True,
                    "confirmed_reward_is_observed_lower_bound_not_classifier": True,
                    "missing_or_execution_failure_is_zero_training_reward": False,
                    "all_zero_or_all_one_histories_are_misspecified_gaussian_heuristic": True,
                    "noise_priors_are_inferred_standardized_variance_priors": True,
                    "noise_is": document["scope"]["noise"],
                    "retention_models_are_constructed_equations_not_physical_fits": not document[
                        "scope"
                    ]["physical_model_fit"],
                    "instrument_qualified": document["scope"]["instrument_qualified"],
                    "trace_simulation_or_signal_processing_qualified": document["scope"][
                        "signal_processing_qualified"
                    ],
                    "manuscript_protocol_match": document["scope"]["manuscript_protocol_match"],
                    "general_optimizer_ranking": document["scope"]["general_optimizer_ranking"],
                    "multi_fidelity": document["scope"]["multi_fidelity"],
                },
                "implementation_provenance": {
                    "measure_service": shared.method_backend_info(),
                    "runner_module_sha256": file_sha256(__file__),
                    "study_input_sha256": study_sha256,
                    "preflight_report_profile": (
                        None
                        if preflight_gate is None
                        else (preflight_gate.get("backend_report") or {}).get("profile")
                    ),
                    "preflight_receipts": [
                        {
                            "attempt": item["attempt"],
                            "qualified": item["qualified"],
                            "request": item["request"],
                        }
                        for item in preflight_history
                    ],
                },
                "evidence_files": [
                    "protocol.json",
                    "preflight.json",
                    "evaluations.jsonl",
                    "campaigns.jsonl",
                    "evaluations.csv",
                    "success-trajectories.csv",
                    "paired-comparisons.csv",
                    "paired-contrast-summary.csv",
                    "measure.sqlite3",
                    "report.json",
                    "report.md",
                ],
            }
            protocol_document = {
                "profile": PROFILE,
                "study_declaration": document,
                "study_sha256": study_sha256,
                "baseline_reference": {
                    "path": "reference_data/synthetic-isocratic-paired-v1.json",
                    "sha256": shared.DEFAULT_SHA256,
                    "original_tasks_unchanged": True,
                },
                "registered_task_inputs": task_bindings,
                "arms": protocol_rows,
                "common_initial_design": common_design,
                "observed_initial_design": initial_design_verification,
                "preflight_required_before_campaign_suggestions": True,
                "campaign_idempotency": {
                    "common_registrations": "syn-cmp-{study SHA256}-{logical registration digest}",
                    "arm_operations": "syn-cmp-{SHA256(study SHA256, arm config)}-{logical operation digest}",
                    "preflight": "syn-abl-{study SHA256}-preflight-{attempt number}",
                },
                "registration_failure": registration_failure,
            }
            preflight_document = {
                "profile": "synthetic-isocratic-ablation-preflight-history/1",
                "study_sha256": study_sha256,
                "attempts": preflight_history,
                "selected_gate": preflight_gate,
                "gate_reused_without_backend_call": preflight_reused,
            }

            shared._write_json(stage / "protocol.json", protocol_document)
            shared._write_json(stage / "preflight.json", preflight_document)
            shared._write_jsonl(stage / "evaluations.jsonl", evaluations)
            shared._write_jsonl(stage / "campaigns.jsonl", campaign_records)
            shared._write_csv(
                stage / "evaluations.csv",
                _EVALUATION_CSV_FIELDS,
                [_evaluation_csv_row(item) for item in evaluations],
            )
            trajectory_fields = (
                "arm_id",
                "cohort_role",
                "optimizer_target_config",
                "surrogate_noise_prior",
                "series",
                "high_fidelity_budget",
                "cohort_size",
                "recorded_campaign_count",
                "observed_at_budget_count",
                "success_count",
                "unknown_success_count",
                "observed_no_success_count",
                "success_fraction",
                "success_fraction_upper_bound",
            )
            shared._write_csv(
                stage / "success-trajectories.csv", trajectory_fields, trajectory_rows
            )
            paired_fields = (
                "contrast_id",
                "contrast_kind",
                "reference_arm_id",
                "comparison_arm_id",
                "reference_campaign_id",
                "comparison_campaign_id",
                "task_id",
                "cohort_role",
                "seed",
                "series",
                "reference_state_at_budget",
                "comparison_state_at_budget",
                "state_changed",
                "reference_first_confirmed_index",
                "comparison_first_confirmed_index",
                "comparison_minus_reference_first_index",
                "interpretation",
                "inference_scope",
            )
            shared._write_csv(stage / "paired-comparisons.csv", paired_fields, paired_rows)
            summary_fields = (
                "contrast_id",
                "contrast_kind",
                "cohort_role",
                "series",
                "planned_pair_count",
                "recorded_pair_count",
                "state_changed_pair_count",
                "both_confirmed_by_budget_count",
                "unknown_reference_pair_count",
                "unknown_comparison_pair_count",
                "interpretation",
                "inference_scope",
            )
            shared._write_csv(
                stage / "paired-contrast-summary.csv", summary_fields, contrast_summaries
            )
            shared._verify_owned_store(
                data_path,
                study_sha256,
                profile=STORE_PROFILE,
                owner=OWNER,
                receipt_prefixes=receipt_scopes,
            )
            shared._backup_database(stage / "measure.sqlite3")
            report["database_backup"] = {
                "filename": "measure.sqlite3",
                "sha256": file_sha256(stage / "measure.sqlite3"),
                "size_bytes": (stage / "measure.sqlite3").stat().st_size,
                "scope": "verified online backup of the isolated owned ablation registry",
            }
            shared._write_json(stage / "report.json", report)
            (stage / "report.md").write_text(_render_markdown(report), encoding="utf-8")
    report["output_dir"] = str(target)
    return report
