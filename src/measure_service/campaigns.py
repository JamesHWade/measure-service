"""Durable sequential state for matched, predeclared synthetic campaigns.

Campaign records hold proposals, exact observation-snapshot consumption and
replayable optimizer inputs. They do not execute a physical method or keep
hidden optimizer state. The optional BO-MCP adapter is imported only when a
post-initialization BO proposal is requested.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from measure_methods.moses_objective import moses_observation_score
from measure_methods.observations import confirmed_acceptance_lower_bound

from . import (
    campaign_protocol,
    campaign_protocol_registry,
    experiments,
    gaussian_producer,
    method_registry,
    observations,
    retention,
)
from .method_backend import method_backend_info
from .methods import MethodConstraints, evaluate_method
from .storage import canonical, now, registry_connection, registry_mutation, sha256

SCHEMA_VERSION = "1.0"
MAX_RECORD_BYTES = 64_000
_CAMPAIGN_POLICIES = {"random", "bo_mcp_gp_logei"}


def _bounded(value: Any, label: str = "Campaign record") -> Any:
    if len(canonical(value).encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError(f"{label} exceeds the 64,000-byte limit")
    return value


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _source_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _is_ablation_protocol(spec: campaign_protocol.ProtocolSpec) -> bool:
    return getattr(spec, "profile", None) == "synthetic-isocratic-ablation/1"


def _optimizer_target_record(
    spec: campaign_protocol.ProtocolSpec, result: dict[str, Any]
) -> dict[str, Any] | None:
    """Describe the predeclared target without replacing historical utility."""
    if not _is_ablation_protocol(spec):
        return None
    configuration = campaign_protocol.optimizer_configuration(spec)
    target_name = configuration["target_name"]
    if target_name == "utility":
        utility = result.get("utility")
        if not isinstance(utility, dict):
            raise ValueError("Historical utility target is missing its stored score")
        return {
            "profile": configuration["target_profile"],
            "name": "utility",
            "direction": "maximize",
            "status": utility.get("status"),
            "value": utility.get("score"),
        }
    if target_name == "confirmed_acceptance_reward":
        acceptance = result.get("analytical_acceptance")
        value = confirmed_acceptance_lower_bound(acceptance)
        return {
            "profile": configuration["target_profile"],
            "name": target_name,
            "direction": "maximize",
            "status": "scored",
            "value": value,
        }
    raise ValueError("Campaign protocol selected an unsupported optimizer target")


def _implementation_provenance(*, include_target_kernel: bool = False) -> dict[str, Any]:
    provenance = {
        "measure_service": method_backend_info(),
        "campaign_adapter": {
            "module": "measure_service.campaigns",
            "source_sha256": _source_hash(__file__),
        },
        "protocol_kernel": {
            "module": "measure_service.campaign_protocol",
            "source_sha256": _source_hash(campaign_protocol.__file__),
        },
        "utility_kernel": {
            "module": "measure_methods.moses_objective",
            "source_sha256": _source_hash(
                __import__("measure_methods.moses_objective", fromlist=["__file__"]).__file__
            ),
        },
    }
    if include_target_kernel:
        target_kernel = __import__("measure_methods.observations", fromlist=["__file__"])
        provenance["optimizer_target_kernel"] = {
            "module": "measure_methods.observations",
            "source_sha256": _source_hash(target_kernel.__file__),
        }
    return provenance


def _campaign_row(con: sqlite3.Connection, owner: str, campaign_id: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM campaign_runs WHERE id=? AND owner=?", (campaign_id, owner)
    ).fetchone()
    if row is None:
        raise ValueError("Campaign was not found or is not accessible")
    return row


def _task_for_campaign(
    con: sqlite3.Connection, owner: str, row: sqlite3.Row, payload: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    protocol_row = campaign_protocol_registry._protocol_row(con, owner, row["protocol_id"])
    protocol = campaign_protocol_registry._protocol_response(protocol_row)
    if protocol["input_sha256"] != row["protocol_input_digest"]:
        raise ValueError("Campaign protocol identity is stale")
    if payload.get("protocol_output_sha256") != protocol["output_sha256"]:
        raise ValueError("Campaign protocol output identity does not match its stored declaration")
    task = next((item for item in protocol["tasks"] if item["task_id"] == row["task_id"]), None)
    if task is None or payload.get("task") != task:
        raise ValueError("Campaign task identity does not match its immutable protocol")
    spec = campaign_protocol.parse_campaign_protocol(protocol["spec"])
    return protocol, {"spec": spec, "task": task}


def _campaign_static_request(row: sqlite3.Row, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": "create_campaign",
        "protocol_id": row["protocol_id"],
        "task_id": row["task_id"],
        "seed": row["seed"],
        "policy": row["policy"],
    }


def _verify_campaign_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Stored campaign payload is malformed") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Stored campaign payload has an unsupported schema")
    if sha256(_campaign_static_request(row, payload)) != row["input_digest"]:
        raise ValueError("Stored campaign input digest does not match its contents")
    content = {key: value for key, value in payload.items() if key != "record_sha256"}
    if payload.get("record_sha256") != sha256(content):
        raise ValueError("Stored campaign content digest does not match its immutable payload")
    return payload


def _proposal_row(con: sqlite3.Connection, owner: str, proposal_id: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM campaign_proposals WHERE id=? AND owner=?", (proposal_id, owner)
    ).fetchone()
    if row is None:
        raise ValueError("Campaign proposal was not found or is not accessible")
    return row


def _proposal_response(
    con: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    verify_training: bool = True,
    replayed_training: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    campaign_context: tuple[sqlite3.Row, dict[str, Any], dict[str, Any], dict[str, Any]]
    | None = None,
) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Stored campaign proposal payload is malformed") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Stored campaign proposal has an unsupported schema")
    request = payload.get("request")
    if (
        not isinstance(request, dict)
        or request.get("operation") != "suggest_campaign"
        or request.get("campaign_id") != row["campaign_id"]
        or sha256(request) != row["input_digest"]
    ):
        raise ValueError("Stored campaign proposal input digest does not match its contents")
    content = {key: value for key, value in payload.items() if key != "record_sha256"}
    if payload.get("record_sha256") != sha256(content):
        raise ValueError("Stored campaign proposal content digest does not match its contents")
    candidate = payload.get("candidate")
    if not isinstance(candidate, dict) or sha256(candidate) != row["candidate_digest"]:
        raise ValueError("Stored campaign candidate identity does not match its contents")
    method_row = method_registry._owned_method(con, row["owner"], row["executable_method_id"])
    method = method_registry._record(method_row)
    method_request = {
        "title": method_row["title"],
        "declaration": method["evaluation"]["declaration"],
        "constraints": method["constraints"],
    }
    method_identity = payload.get("method")
    if (
        method_identity
        != {
            "method_id": method_row["id"],
            "input_sha256": method_row["input_digest"],
            "canonical_program_sha256": method_row["program_digest"],
        }
        or row["method_input_digest"] != method_row["input_digest"]
        or row["canonical_program_digest"] != method_row["program_digest"]
        or sha256(method["evaluation"]["canonical"]) != row["canonical_program_digest"]
        or sha256(method_request) != method_row["input_digest"]
    ):
        raise ValueError("Stored campaign proposal method identity is inconsistent")
    if row["state"] not in ("pending", "consumed"):
        raise ValueError("Stored campaign proposal has an unsupported state")
    if campaign_context is None:
        campaign_row = _campaign_row(con, row["owner"], row["campaign_id"])
        campaign_payload = _verify_campaign_payload(campaign_row)
        protocol, links = _task_for_campaign(con, row["owner"], campaign_row, campaign_payload)
    else:
        campaign_row, campaign_payload, protocol, links = campaign_context
    proposal_input = payload.get("proposal_input")
    if (
        not isinstance(proposal_input, dict)
        or proposal_input.get("campaign_id") != row["campaign_id"]
        or proposal_input.get("ordinal") != row["ordinal"]
        or proposal_input.get("candidate") != candidate
        or proposal_input.get("seed") != campaign_row["seed"]
        or proposal_input.get("policy") != campaign_row["policy"]
        or proposal_input.get("protocol_input_sha256") != protocol["input_sha256"]
        or payload.get("policy") != campaign_row["policy"]
        or payload.get("training", {}).get("sha256") != proposal_input.get("training_sha256")
    ):
        raise ValueError("Stored campaign proposal inputs do not match its immutable campaign")
    if payload.get("proposal_source") not in {
        "common_initial_design",
        "random_policy",
        "bo_mcp_gp_logei",
    }:
        raise ValueError("Stored campaign proposal source is unsupported")
    protocol_spec = links["spec"]
    expected_declaration = campaign_protocol.method_declaration(protocol_spec, candidate)
    if method["evaluation"]["declaration"] != expected_declaration.model_dump(mode="json"):
        raise ValueError("Stored campaign method does not implement its declared candidate")
    if method["constraints"] != protocol_spec.constraints.model_dump(mode="json"):
        raise ValueError("Stored campaign method constraints differ from its frozen protocol")
    if row["ordinal"] < protocol_spec.initial_design_size:
        if payload["proposal_source"] != "common_initial_design":
            raise ValueError("Campaign initialization must use the shared initial design")
        expected_candidate = campaign_protocol.initial_candidate(
            protocol_spec, campaign_row["seed"], row["ordinal"]
        )
        if candidate != expected_candidate:
            raise ValueError(
                "Stored initial candidate does not match its deterministic LHS coordinate"
            )
    elif campaign_row["policy"] == "random":
        if payload["proposal_source"] != "random_policy":
            raise ValueError(
                "Random campaign candidate does not match its declared proposal policy"
            )
        if candidate != campaign_protocol.random_candidate(
            protocol_spec, campaign_row["seed"], row["ordinal"]
        ):
            raise ValueError(
                "Stored random candidate does not match its deterministic campaign stream"
            )
    elif campaign_row["policy"] == "bo_mcp_gp_logei":
        if payload["proposal_source"] != "bo_mcp_gp_logei" or not isinstance(
            payload.get("backend"), dict
        ):
            raise ValueError("BO campaign proposal is missing its declared backend provenance")
    if verify_training:
        if replayed_training is None:
            training_data, source_members = _training_context(
                con, row["owner"], row["campaign_id"], before_ordinal=row["ordinal"]
            )
        else:
            training_data, source_members = replayed_training
        expected_training = {
            "count": len(training_data),
            "sha256": sha256(training_data),
            "snapshot_members": source_members,
        }
        if payload.get("training") != expected_training:
            raise ValueError("Stored proposal training inputs differ from exact consumed snapshots")
        if payload.get("proposal_input", {}).get("training_sha256") != expected_training["sha256"]:
            raise ValueError(
                "Stored proposal training digest differs from replayed campaign history"
            )
        if (
            isinstance(payload.get("backend"), dict)
            and payload["backend"].get("training_sha256") != expected_training["sha256"]
        ):
            raise ValueError("Stored BO backend provenance has a different training digest")
        if (
            _is_ablation_protocol(protocol_spec)
            and campaign_row["policy"] == "bo_mcp_gp_logei"
            and row["ordinal"] >= protocol_spec.initial_design_size
        ):
            validated_candidate, _ = _validate_backend_result(
                {"candidate": candidate, "backend": payload.get("backend")},
                spec=protocol_spec,
                seed=campaign_row["seed"],
                training=training_data,
            )
            if validated_candidate != candidate:
                raise ValueError("Stored BO campaign candidate is not in canonical numeric form")
    result = {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": row["id"],
        "campaign_id": row["campaign_id"],
        "ordinal": row["ordinal"],
        "state": row["state"],
        "candidate": candidate,
        "candidate_sha256": row["candidate_digest"],
        "policy": payload["policy"],
        "proposal_source": payload["proposal_source"],
        "training": payload["training"],
        "backend": payload.get("backend"),
        "method": {**method_identity, "canonical_program": method["evaluation"]["canonical"]},
        "input_sha256": row["input_digest"],
        "created_at": row["created_at"],
    }
    return _bounded(result, "Campaign proposal")


def _proposal_summary(proposal: dict[str, Any] | None) -> dict[str, Any] | None:
    if proposal is None:
        return None
    return {
        "proposal_id": proposal["proposal_id"],
        "ordinal": proposal["ordinal"],
        "state": proposal["state"],
        "candidate": proposal["candidate"],
        "candidate_sha256": proposal["candidate_sha256"],
        "method": {
            "method_id": proposal["method"]["method_id"],
            "input_sha256": proposal["method"]["input_sha256"],
            "canonical_program_sha256": proposal["method"]["canonical_program_sha256"],
        },
        "input_sha256": proposal["input_sha256"],
    }


def _consumption_row(con: sqlite3.Connection, owner: str, consumption_id: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM campaign_consumptions WHERE id=? AND owner=?",
        (consumption_id, owner),
    ).fetchone()
    if row is None:
        raise ValueError("Campaign consumption was not found or is not accessible")
    return row


def _consumption_response(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Stored campaign consumption payload is malformed") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Stored campaign consumption has an unsupported schema")
    request = payload.get("request")
    if not isinstance(request, dict) or sha256(request) != row["input_digest"]:
        raise ValueError("Stored campaign consumption input digest does not match its contents")
    content = {key: value for key, value in payload.items() if key != "output_sha256"}
    if (
        payload.get("output_sha256") != row["output_digest"]
        or sha256(content) != row["output_digest"]
    ):
        raise ValueError("Stored campaign consumption output digest does not match its contents")
    identity = (
        payload.get("campaign_id"),
        payload.get("proposal_id"),
        payload.get("snapshot_id"),
        payload.get("observation_id"),
        payload.get("attempt_id"),
        payload.get("attempt_revision"),
        payload.get("snapshot_sha256"),
        payload.get("observation_input_sha256"),
        payload.get("evidence_sha256"),
        payload.get("assessment_sha256"),
    )
    expected = (
        row["campaign_id"],
        row["proposal_id"],
        row["observation_snapshot_id"],
        row["observation_id"],
        row["attempt_id"],
        row["attempt_revision"],
        row["snapshot_digest"],
        row["observation_input_digest"],
        row["evidence_digest"],
        row["assessment_digest"],
    )
    if identity != expected:
        raise ValueError("Stored campaign consumption identity links are inconsistent")
    return _bounded(
        {
            "schema_version": SCHEMA_VERSION,
            "consumption_id": row["id"],
            "campaign_id": row["campaign_id"],
            "proposal_id": row["proposal_id"],
            "snapshot_id": row["observation_snapshot_id"],
            "observation_id": row["observation_id"],
            "attempt_id": row["attempt_id"],
            "attempt_revision": row["attempt_revision"],
            "snapshot_sha256": row["snapshot_digest"],
            "observation_input_sha256": row["observation_input_digest"],
            "evidence_sha256": row["evidence_digest"],
            "assessment_sha256": row["assessment_digest"],
            "result": payload["result"],
            "source": payload["source"],
            "implementation_provenance": payload["implementation_provenance"],
            "input_sha256": row["input_digest"],
            "output_sha256": row["output_digest"],
            "created_at": row["created_at"],
        },
        "Campaign consumption",
    )


def _campaign_response(con: sqlite3.Connection, owner: str, row: sqlite3.Row) -> dict[str, Any]:
    payload = _verify_campaign_payload(row)
    protocol, links = _task_for_campaign(con, owner, row, payload)
    spec = links["spec"]
    if row["seed"] not in spec.seeds or row["policy"] not in spec.policies:
        raise ValueError("Stored campaign seed or policy is outside its immutable protocol")
    consumptions = con.execute(
        "SELECT * FROM campaign_consumptions WHERE campaign_id=? AND owner=? ORDER BY attempt_id, id",
        (row["id"], owner),
    ).fetchall()
    history: list[dict[str, Any]] = []
    consumed_proposal_ids: set[str] = set()
    ablation_protocol = _is_ablation_protocol(spec)
    campaign_context = (row, payload, protocol, links)
    for consumed in consumptions:
        record = _consumption_response(consumed)
        if record["proposal_id"] in consumed_proposal_ids:
            raise ValueError("Campaign contains duplicate consumption for a proposal")
        consumed_proposal_ids.add(record["proposal_id"])
        proposal_row = _proposal_row(con, owner, record["proposal_id"])
        proposal = _proposal_response(
            con,
            proposal_row,
            verify_training=not ablation_protocol,
            campaign_context=campaign_context,
        )
        result = record["result"]
        history_row = {
            "ordinal": proposal["ordinal"],
            "consumption_id": consumed["id"],
            "proposal_id": proposal["proposal_id"],
            "snapshot_id": record["snapshot_id"],
            "candidate": proposal["candidate"],
            "analytical_acceptance": result["analytical_acceptance"],
            "runtime_preference_satisfied": result["runtime_preference_satisfied"],
            "utility_status": result["utility"]["status"],
            "score": result["utility"]["score"],
            "evaluation_cost": result["evaluation_cost"],
            "runtime_min": result["runtime_min"],
        }
        if _is_ablation_protocol(spec):
            expected_target = _optimizer_target_record(spec, result)
            if result.get("optimizer_target") != expected_target:
                raise ValueError("Stored campaign optimizer target differs from its assessment")
            history_row["optimizer_target"] = expected_target
        history.append(history_row)
    history.sort(key=lambda item: item["ordinal"])
    consumed_count = len(history)
    replayed_training = None
    if ablation_protocol:
        replayed_training = _training_context(con, owner, row["id"])
    if consumed_count > spec.evaluation_budget:
        raise ValueError("Stored campaign consumption exceeds its declared evaluation budget")
    pending_row = con.execute(
        "SELECT * FROM campaign_proposals WHERE owner=? AND campaign_id=? AND state='pending'",
        (owner, row["id"]),
    ).fetchone()
    pending = (
        None
        if pending_row is None
        else _proposal_response(
            con,
            pending_row,
            replayed_training=replayed_training,
            campaign_context=campaign_context,
        )
    )
    proposals = con.execute(
        "SELECT * FROM campaign_proposals WHERE owner=? AND campaign_id=? ORDER BY ordinal",
        (owner, row["id"]),
    ).fetchall()
    if len(proposals) != consumed_count + (1 if pending is not None else 0):
        raise ValueError("Campaign proposal and consumption histories are incomplete")
    for ordinal, proposal_row in enumerate(proposals):
        if proposal_row["ordinal"] != ordinal:
            raise ValueError("Campaign proposal ordinals must be contiguous from zero")
        if ordinal < consumed_count:
            if (
                proposal_row["state"] != "consumed"
                or proposal_row["id"] not in consumed_proposal_ids
            ):
                raise ValueError(
                    "Campaign history has an unconsumed or unlinked proposal before its tail"
                )
        elif proposal_row["state"] != "pending" or proposal_row["id"] != (
            None if pending is None else pending["proposal_id"]
        ):
            raise ValueError("Campaign history has an invalid pending proposal")
    if row["revision"] != 1 + len(proposals) + consumed_count:
        raise ValueError("Campaign revision does not match its durable state transitions")
    if consumed_count + (1 if pending is not None else 0) > spec.evaluation_budget:
        raise ValueError("Stored campaign proposals exceed its declared evaluation budget")
    state = (
        "budget_complete"
        if consumed_count == spec.evaluation_budget
        else ("proposal_pending" if pending is not None else "ready_to_suggest")
    )
    return _bounded(
        {
            "schema_version": SCHEMA_VERSION,
            "campaign_id": row["id"],
            "protocol_id": row["protocol_id"],
            "protocol_input_sha256": row["protocol_input_digest"],
            "protocol_output_sha256": protocol["output_sha256"],
            "task_id": row["task_id"],
            "task": links["task"],
            "seed": row["seed"],
            "policy": row["policy"],
            "revision": row["revision"],
            "state": state,
            "evaluation_budget": spec.evaluation_budget,
            "evaluation_cost_unit": payload["evaluation_cost_unit"],
            "consumed_evaluations": consumed_count,
            "budget_remaining": spec.evaluation_budget - consumed_count,
            "pending_proposal": _proposal_summary(pending),
            "history": history,
            "implementation_provenance": payload["implementation_provenance"],
            "input_sha256": row["input_digest"],
            "created_at": row["created_at"],
        }
    )


def create_campaign(
    owner: str, protocol_id: str, task_id: str, seed: int, policy: str, key: str
) -> dict[str, Any]:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("Campaign seed must be an integer declared by its protocol")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("Campaign task_id must be a nonblank string")
    if policy not in _CAMPAIGN_POLICIES:
        raise ValueError("Campaign policy must be random or bo_mcp_gp_logei")
    request = {
        "operation": "create_campaign",
        "protocol_id": protocol_id,
        "task_id": task_id,
        "seed": seed,
        "policy": policy,
    }

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        protocol_row = campaign_protocol_registry._protocol_row(con, owner, protocol_id)
        protocol = campaign_protocol_registry._protocol_response(protocol_row)
        spec = campaign_protocol.parse_campaign_protocol(protocol["spec"])
        if seed not in spec.seeds:
            raise ValueError("Campaign seed must be declared by its protocol")
        if policy not in spec.policies:
            raise ValueError("Campaign policy must be declared by its protocol")
        task = next((item for item in protocol["tasks"] if item["task_id"] == task_id), None)
        if task is None:
            raise ValueError("Campaign task_id must name a task in its immutable protocol")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "protocol_output_sha256": protocol["output_sha256"],
            "task": task,
            "evaluation_budget": spec.evaluation_budget,
            "initial_design_size": spec.initial_design_size,
            "evaluation_cost": spec.evaluation_cost,
            "evaluation_cost_unit": spec.evaluation_cost_unit,
            "implementation_provenance": _implementation_provenance(
                include_target_kernel=_is_ablation_protocol(spec)
            ),
        }
        payload["record_sha256"] = sha256(payload)
        _bounded(payload)
        campaign_id, created = str(uuid.uuid4()), now()
        con.execute(
            "INSERT INTO campaign_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                campaign_id,
                owner,
                protocol_id,
                protocol["input_sha256"],
                task_id,
                seed,
                policy,
                canonical(payload),
                sha256(request),
                1,
                created,
            ),
        )
        return _campaign_response(con, owner, _campaign_row(con, owner, campaign_id))

    return registry_mutation(owner, key, request, write)


def _validate_backend_result(
    result: Any, *, spec: Any, seed: int, training: list[dict]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(result, dict) or set(result) != {"candidate", "backend"}:
        raise ValueError("BO-MCP adapter must return exactly candidate and backend")
    candidate, backend = result["candidate"], result["backend"]
    if not isinstance(candidate, dict) or set(candidate) != {
        "strong_solvent_fraction",
        "acquisition_duration_min",
    }:
        raise ValueError("BO-MCP candidate must contain the two declared coordinates only")
    for key, bounds in (
        ("strong_solvent_fraction", spec.domain.strong_solvent_fraction),
        ("acquisition_duration_min", spec.domain.acquisition_duration_min),
    ):
        value = candidate[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"BO-MCP candidate {key} must be a finite numeric value")
        value = float(value)
        if not math.isfinite(value) or not bounds.lower <= value <= bounds.upper:
            raise ValueError(f"BO-MCP candidate {key} is outside its finite declared bounds")
        candidate[key] = value
    if not isinstance(backend, dict):
        raise ValueError("BO-MCP adapter provenance must be an object")
    required = {
        "backend_id",
        "backend_name",
        "generation_method",
        "model_type",
        "acquisition_method",
        "acquisition_implementation",
        "optimization_strategy",
        "source_revision",
        "source_tree_sha256",
        "distribution_versions",
        "protocol_sha256",
        "seed",
        "training_sha256",
        "training_count",
        "runtime",
        "settings",
        "warnings",
        "suggestion_provenance",
    }
    if not required <= set(backend):
        raise ValueError("BO-MCP adapter provenance is incomplete")
    if backend["backend_id"] != "bo-engine/BoTorchBackend":
        raise ValueError("BO-MCP adapter provenance names an unexpected backend")
    if backend["generation_method"] != "bo":
        raise ValueError("BO-MCP adapter did not report a model-guided proposal")
    if backend["model_type"] != "SingleTaskGP":
        raise ValueError("BO-MCP adapter did not report the declared single-task GP")
    if (
        backend["acquisition_method"] != "expected_improvement"
        or backend["acquisition_implementation"] != "qLogExpectedImprovement"
    ):
        raise ValueError("BO-MCP adapter did not report the declared expected-improvement method")
    if (
        not isinstance(backend["optimization_strategy"], str)
        or not backend["optimization_strategy"].strip()
    ):
        raise ValueError("BO-MCP adapter provenance requires its optimization strategy")
    if (
        not isinstance(backend["source_tree_sha256"], str)
        or len(backend["source_tree_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in backend["source_tree_sha256"])
    ):
        raise ValueError("BO-MCP adapter provenance requires the backend source-tree digest")
    if backend["source_revision"] is not None and (
        not isinstance(backend["source_revision"], str)
        or len(backend["source_revision"]) not in (40, 64)
        or any(character not in "0123456789abcdef" for character in backend["source_revision"])
    ):
        raise ValueError("BO-MCP source revision must be a full VCS digest or null")
    if not isinstance(backend["distribution_versions"], dict):
        raise ValueError("BO-MCP distribution_versions must be an object")
    expected_protocol_digest = campaign_protocol.campaign_protocol_sha256(spec)
    if backend["protocol_sha256"] != expected_protocol_digest:
        raise ValueError("BO-MCP protocol digest does not match the frozen campaign protocol")
    if (
        isinstance(backend["seed"], bool)
        or not isinstance(backend["seed"], int)
        or backend["seed"] != seed
    ):
        raise ValueError("BO-MCP provenance seed does not match the supplied deterministic seed")
    training_digest = sha256(training)
    if backend["training_sha256"] != training_digest:
        raise ValueError("BO-MCP provenance training digest does not match replayed inputs")
    if (
        isinstance(backend["training_count"], bool)
        or not isinstance(backend["training_count"], int)
        or backend["training_count"] != len(training)
    ):
        raise ValueError("BO-MCP provenance training count does not match replayed inputs")
    if (
        not isinstance(backend["runtime"], dict)
        or not isinstance(backend["settings"], dict)
        or not isinstance(backend["warnings"], list)
        or any(not isinstance(item, str) for item in backend["warnings"])
        or not isinstance(backend["suggestion_provenance"], dict)
    ):
        raise ValueError(
            "BO-MCP runtime, settings, warnings or suggestion provenance are malformed"
        )
    if _is_ablation_protocol(spec):
        configuration = campaign_protocol.optimizer_configuration(spec)
        expected_adapter_profile = configuration["adapter_profile"]
        expected_objective = {"name": configuration["target_name"], "direction": "maximize"}
        if backend.get("adapter_profile") != expected_adapter_profile:
            raise ValueError("BO-MCP adapter profile does not match the ablation protocol")
        if backend["settings"].get("objective") != expected_objective:
            raise ValueError("BO-MCP objective does not match the declared campaign target")
        noise_prior_params = configuration["noise_prior_params"]
        if (
            not isinstance(noise_prior_params, list)
            or len(noise_prior_params) != 2
            or backend["settings"].get("noise_prior")
            != {
                "family": "Gamma",
                "concentration": noise_prior_params[0],
                "rate": noise_prior_params[1],
                "space": "standardized_target_variance",
                "constraint_lower_bound": 0.0001,
                "learned": True,
            }
        ):
            raise ValueError("BO-MCP noise prior does not match the declared ablation protocol")
    _bounded({"candidate": candidate, "backend": backend}, "BO-MCP response")
    return candidate, backend


def _bo_candidate(
    spec: Any,
    training: list[dict[str, Any]],
    seed: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        adapter = importlib.import_module("measure_service.optimizer_backend")
    except ImportError as error:
        raise RuntimeError(
            "The optional BO-MCP backend is unavailable; the proposal was not replaced by a fallback"
        ) from error
    suggest = getattr(adapter, "suggest_candidate", None)
    if not callable(suggest):
        raise RuntimeError("The installed BO-MCP adapter does not expose suggest_candidate")
    started = time.perf_counter()
    result = suggest(spec, training, seed)
    elapsed_seconds = time.perf_counter() - started
    candidate, backend = _validate_backend_result(result, spec=spec, seed=seed, training=training)
    backend["runtime"] = {**backend["runtime"], "elapsed_seconds": elapsed_seconds}
    _bounded({"candidate": candidate, "backend": backend}, "BO-MCP response")
    return candidate, backend


def _training_context(
    con: sqlite3.Connection,
    owner: str,
    campaign_id: str,
    *,
    before_ordinal: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rebuild optimizer inputs and retain every immutable snapshot lineage link."""
    campaign_row = _campaign_row(con, owner, campaign_id)
    campaign_payload = _verify_campaign_payload(campaign_row)
    protocol, links = _task_for_campaign(con, owner, campaign_row, campaign_payload)
    spec = links["spec"]
    ablation_target = _is_ablation_protocol(spec)
    campaign_context = (campaign_row, campaign_payload, protocol, links)
    ordinal_filter = " AND p.ordinal < ?" if before_ordinal is not None else ""
    parameters: tuple[Any, ...] = (owner, campaign_id) + (
        (before_ordinal,) if before_ordinal is not None else ()
    )
    rows = con.execute(
        f"""SELECT c.*, p.ordinal, p.id AS proposal_id, p.state AS proposal_state,
        p.payload AS proposal_payload, p.input_digest AS proposal_input,
        p.candidate_digest, p.executable_method_id, p.method_input_digest,
        p.canonical_program_digest
        FROM campaign_consumptions c JOIN campaign_proposals p ON p.id=c.proposal_id
        WHERE c.owner=? AND c.campaign_id=? {ordinal_filter} ORDER BY p.ordinal""",
        parameters,
    ).fetchall()
    training: list[dict[str, Any]] = []
    source_members: list[dict[str, Any]] = []
    for row in rows:
        consumption = _consumption_response(row)
        if ablation_target:
            _verify_ablation_consumption_source(
                con, owner, row, consumption, campaign_context=campaign_context
            )
        try:
            proposal_payload = json.loads(row["proposal_payload"])
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("Stored training proposal payload is malformed") from error
        proposal_content = {
            key: value for key, value in proposal_payload.items() if key != "record_sha256"
        }
        if (
            row["proposal_state"] != "consumed"
            or not isinstance(proposal_payload, dict)
            or proposal_payload.get("record_sha256") != sha256(proposal_content)
            or not isinstance(proposal_payload.get("request"), dict)
            or sha256(proposal_payload["request"]) != row["proposal_input"]
        ):
            raise ValueError("Stored training proposal identity is inconsistent")
        if ablation_target:
            expected_prefix = {
                "count": len(training),
                "sha256": sha256(training),
                "snapshot_members": source_members,
            }
            if proposal_payload.get("training") != expected_prefix:
                raise ValueError("Stored proposal training inputs differ from consumed snapshots")
            proposal_input = proposal_payload.get("proposal_input")
            if (
                not isinstance(proposal_input, dict)
                or proposal_input.get("training_sha256") != expected_prefix["sha256"]
            ):
                raise ValueError(
                    "Stored proposal training digest differs from replayed campaign history"
                )
            stored_backend = proposal_payload.get("backend")
            if (
                isinstance(stored_backend, dict)
                and stored_backend.get("training_sha256") != expected_prefix["sha256"]
            ):
                raise ValueError(
                    "Stored BO backend training digest differs from consumed snapshots"
                )
        candidate = proposal_payload.get("candidate")
        if not isinstance(candidate, dict) or sha256(candidate) != row["candidate_digest"]:
            raise ValueError("Stored training candidate digest does not match its contents")
        method = method_registry._record(
            method_registry._owned_method(con, owner, row["executable_method_id"])
        )
        if (
            method["input_sha256"] != row["method_input_digest"]
            or method["canonical_program_sha256"] != row["canonical_program_digest"]
        ):
            raise ValueError("Stored training method identity does not match its proposal")
        stored_result = consumption["result"]
        utility = stored_result["utility"]
        if ablation_target:
            target = stored_result.get("optimizer_target")
            expected_target = _optimizer_target_record(spec, stored_result)
            if target != expected_target:
                raise ValueError("Stored training target differs from its immutable assessment")
            included = target["status"] == "scored"
            score = target["value"] if included else None
        else:
            included = utility["status"] == "scored"
            score = utility["score"] if included else None
        if included and (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            raise ValueError("Stored BO training score must be finite numeric evidence")
        if (
            ablation_target
            and campaign_row["policy"] == "bo_mcp_gp_logei"
            and row["ordinal"] >= spec.initial_design_size
        ):
            validated_candidate, _ = _validate_backend_result(
                {"candidate": candidate, "backend": stored_backend},
                spec=spec,
                seed=campaign_row["seed"],
                training=training,
            )
            if validated_candidate != candidate:
                raise ValueError("Stored BO campaign candidate is not in canonical numeric form")
        source_member = {
            "ordinal": row["ordinal"],
            "consumption_id": row["id"],
            "candidate_sha256": row["candidate_digest"],
            "optimizer_training_included": included,
            "snapshot_id": consumption["snapshot_id"],
            "snapshot_sha256": consumption["snapshot_sha256"],
            "consumption_output_sha256": consumption["output_sha256"],
        }
        source_members.append(source_member)
        if included:
            training.append({"candidate": candidate, "score": float(score)})
    return training, source_members


def _insert_campaign_method(
    con: sqlite3.Connection,
    owner: str,
    campaign_id: str,
    ordinal: int,
    declaration: Any,
    constraints: MethodConstraints,
) -> dict[str, Any]:
    from .method_registry import _owned_method, _record

    title = f"Campaign {campaign_id[:8]} evaluation {ordinal + 1}"
    declared = declaration.model_dump(mode="json")
    limited = constraints.model_dump(mode="json")
    requested = {"title": title, "declaration": declared, "constraints": limited}
    evaluation = evaluate_method(declaration, constraints)
    if not evaluation.feasible:
        raise ValueError("Campaign candidate method is not feasible under the frozen protocol")
    retained = {"evaluation": evaluation.model_dump(mode="json"), "constraints": limited}
    _bounded(retained, "Campaign executable method")
    method_id, created = str(uuid.uuid4()), now()
    con.execute(
        "INSERT INTO executable_methods VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            method_id,
            owner,
            title,
            canonical(retained),
            sha256(requested),
            sha256(evaluation.canonical.model_dump(mode="json")),
            created,
        ),
    )
    return _record(_owned_method(con, owner, method_id))


def suggest_campaign(
    owner: str, campaign_id: str, expected_revision: int, key: str
) -> dict[str, Any]:
    _positive_integer(expected_revision, "expected_revision")
    request = {
        "operation": "suggest_campaign",
        "campaign_id": campaign_id,
        "expected_revision": expected_revision,
    }

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        row = _campaign_row(con, owner, campaign_id)
        payload = _verify_campaign_payload(row)
        protocol_response, links = _task_for_campaign(con, owner, row, payload)
        spec: campaign_protocol.CampaignProtocolSpec = links["spec"]
        if row["revision"] != expected_revision:
            raise ValueError("Campaign revision conflict")
        existing = con.execute(
            "SELECT * FROM campaign_proposals WHERE owner=? AND campaign_id=? AND state='pending'",
            (owner, campaign_id),
        ).fetchone()
        if existing is not None:
            proposal = _proposal_response(con, existing)
            return _bounded(
                {
                    "schema_version": SCHEMA_VERSION,
                    "campaign_id": campaign_id,
                    "revision": row["revision"],
                    "proposal": proposal,
                    "campaign": _campaign_response(con, owner, row),
                }
            )
        prior = con.execute(
            "SELECT COUNT(*) FROM campaign_consumptions WHERE owner=? AND campaign_id=?",
            (owner, campaign_id),
        ).fetchone()[0]
        if prior >= spec.evaluation_budget:
            raise ValueError("Campaign has consumed its full declared evaluation budget")
        ordinal = prior
        training, source_members = _training_context(
            con, owner, campaign_id, before_ordinal=ordinal
        )
        training_digest = sha256(training)
        training_record = {
            "count": len(training),
            "sha256": training_digest,
            "snapshot_members": source_members,
        }
        backend = None
        if ordinal < spec.initial_design_size:
            candidate = campaign_protocol.initial_candidate(spec, row["seed"], ordinal)
            proposal_source = "common_initial_design"
        elif row["policy"] == "random":
            candidate = campaign_protocol.random_candidate(spec, row["seed"], ordinal)
            proposal_source = "random_policy"
        else:
            candidate, backend = _bo_candidate(spec, training, row["seed"])
            proposal_source = "bo_mcp_gp_logei"
        declaration = campaign_protocol.method_declaration(spec, candidate)
        method = _insert_campaign_method(
            con, owner, campaign_id, ordinal, declaration, spec.constraints
        )
        candidate_digest = sha256(candidate)
        duplicate = con.execute(
            "SELECT id FROM campaign_proposals WHERE campaign_id=? AND candidate_digest=?",
            (campaign_id, candidate_digest),
        ).fetchone()
        if duplicate is not None:
            raise ValueError("Campaign candidate duplicates a prior proposal")
        proposal_id, created = str(uuid.uuid4()), now()
        proposal_input = {
            "protocol_input_sha256": protocol_response["input_sha256"],
            "campaign_id": campaign_id,
            "seed": row["seed"],
            "policy": row["policy"],
            "ordinal": ordinal,
            "candidate": candidate,
            "training_sha256": training_digest,
            "backend": backend,
        }
        input_digest = sha256(request)
        proposal_payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "request": request,
            "proposal_input": proposal_input,
            "candidate": candidate,
            "policy": row["policy"],
            "proposal_source": proposal_source,
            "training": training_record,
            "backend": backend,
            "method": {
                "method_id": method["method_id"],
                "input_sha256": method["input_sha256"],
                "canonical_program_sha256": method["canonical_program_sha256"],
            },
        }
        proposal_payload["record_sha256"] = sha256(proposal_payload)
        _bounded(proposal_payload, "Campaign proposal")
        con.execute(
            """INSERT INTO campaign_proposals (
              id, owner, campaign_id, ordinal, state, candidate_digest,
              executable_method_id, method_input_digest, canonical_program_digest,
              payload, input_digest, created_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
            (
                proposal_id,
                owner,
                campaign_id,
                ordinal,
                candidate_digest,
                method["method_id"],
                method["input_sha256"],
                method["canonical_program_sha256"],
                canonical(proposal_payload),
                input_digest,
                created,
            ),
        )
        updated = con.execute(
            "UPDATE campaign_runs SET revision=revision+1 WHERE id=? AND owner=? AND revision=?",
            (campaign_id, owner, expected_revision),
        )
        if updated.rowcount != 1:
            raise ValueError("Campaign revision conflict")
        fresh = _campaign_row(con, owner, campaign_id)
        proposal = _proposal_response(con, _proposal_row(con, owner, proposal_id))
        return _bounded(
            {
                "schema_version": SCHEMA_VERSION,
                "campaign_id": campaign_id,
                "revision": fresh["revision"],
                "proposal": proposal,
                "campaign": _campaign_response(con, owner, fresh),
            }
        )

    return registry_mutation(owner, key, request, write)


def _snapshot_and_observation(
    con: sqlite3.Connection,
    owner: str,
    snapshot_id: str,
    task: dict[str, Any],
) -> tuple[dict[str, Any], sqlite3.Row, dict[str, Any], dict[str, Any]]:
    snapshot_row = con.execute(
        "SELECT * FROM observation_snapshots WHERE id=? AND owner=?", (snapshot_id, owner)
    ).fetchone()
    if snapshot_row is None:
        raise ValueError("Observation snapshot was not found or is not accessible")
    try:
        snapshot_payload = json.loads(snapshot_row["payload"])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Stored observation snapshot payload is malformed") from error
    if sha256(snapshot_payload) != snapshot_row["digest"]:
        raise ValueError("Stored observation snapshot digest does not match its contents")
    if (
        snapshot_row["contract_id"] != task["contract"]["contract_id"]
        or snapshot_row["contract_input_digest"] != task["contract"]["input_sha256"]
        or snapshot_payload.get("contract_id") != task["contract"]["contract_id"]
        or snapshot_payload.get("contract_input_sha256") != task["contract"]["input_sha256"]
    ):
        raise ValueError("Observation snapshot does not use the task's frozen contract")
    members = [
        {
            "observation_id": member["observation_id"],
            "attempt_id": member["attempt_id"],
            "attempt_revision": member["attempt_revision"],
            "input_sha256": member["observation_input_digest"],
            "evidence_sha256": member["evidence_digest"],
            "assessment_sha256": member["assessment_digest"],
        }
        for member in con.execute(
            "SELECT * FROM observation_snapshot_members WHERE snapshot_id=? AND owner=? ORDER BY ordinal",
            (snapshot_id, owner),
        )
    ]
    if members != snapshot_payload.get("members") or len(members) != 1:
        raise ValueError(
            "Campaign consumption requires an intact snapshot with exactly one observation"
        )
    member = members[0]
    observation_row = con.execute(
        "SELECT * FROM analytical_observations WHERE id=? AND owner=?",
        (member["observation_id"], owner),
    ).fetchone()
    if observation_row is None:
        raise ValueError("Frozen snapshot observation is missing or inaccessible")
    observation = observations._observation_response(observation_row)
    if (
        observation_row["contract_id"] != task["contract"]["contract_id"]
        or observation_row["contract_input_digest"] != task["contract"]["input_sha256"]
        or observation_row["attempt_id"] != member["attempt_id"]
        or observation_row["attempt_revision"] != member["attempt_revision"]
        or observation_row["input_digest"] != member["input_sha256"]
        or observation_row["evidence_digest"] != member["evidence_sha256"]
        or observation_row["assessment_digest"] != member["assessment_sha256"]
    ):
        raise ValueError("Frozen snapshot observation links do not match its immutable member")
    return (
        {"snapshot_id": snapshot_row["id"], "sha256": snapshot_row["digest"], "members": members},
        observation_row,
        observation,
        snapshot_payload,
    )


def _verify_campaign_observation(
    con: sqlite3.Connection,
    owner: str,
    task: dict[str, Any],
    proposal_row: sqlite3.Row,
    observation_row: sqlite3.Row,
    observation_response: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from .method_registry import _owned_method, _record

    observation_payload = observation_response["observation"]
    evidence = observation_payload["evidence"]
    input_spec = observation_payload["input_spec"]
    contract_identity = observation_payload.get("contract", {})
    method_identity = observation_payload.get("executable_method", {})
    attempt_identity = observation_payload.get("attempt", {})
    if (
        observation_response["contract_id"] != task["contract"]["contract_id"]
        or contract_identity.get("contract_id") != task["contract"]["contract_id"]
        or contract_identity.get("input_sha256") != task["contract"]["input_sha256"]
        or observation_response["experiment_id"] != observation_row["experiment_id"]
        or attempt_identity.get("attempt_id") != observation_row["attempt_id"]
        or attempt_identity.get("revision") != observation_row["attempt_revision"]
        or method_identity.get("method_id") != proposal_row["executable_method_id"]
        or method_identity.get("input_sha256") != proposal_row["method_input_digest"]
        or method_identity.get("canonical_program_sha256")
        != proposal_row["canonical_program_digest"]
    ):
        raise ValueError(
            "Frozen observation source identities do not match its stored registry links"
        )
    if (
        input_spec.get("fidelity_role") != "high"
        or input_spec.get("observation_origin") != "simulator_latent"
        or evidence.get("fidelity_role") != "high"
        or evidence.get("observation_origin") != "simulator_latent"
        or evidence.get("attempt_evaluator_kind") != "simulator"
    ):
        raise ValueError(
            "Campaign observation must declare high-fidelity simulator_latent evidence"
        )
    if observation_row["executable_method_id"] != proposal_row["executable_method_id"]:
        raise ValueError("Observation method differs from the exact proposed executable method")
    if observation_row["method_input_digest"] != proposal_row["method_input_digest"]:
        raise ValueError("Observation method input digest differs from the proposal")
    if observation_row["canonical_program_digest"] != proposal_row["canonical_program_digest"]:
        raise ValueError("Observation canonical program differs from the proposal")
    attempt = experiments._attempt(
        con, owner, observation_row["attempt_id"], observation_row["attempt_revision"]
    )
    attempt_revision_row = con.execute(
        "SELECT payload, digest FROM attempt_revisions WHERE attempt_id=? AND revision=?",
        (observation_row["attempt_id"], observation_row["attempt_revision"]),
    ).fetchone()
    if (
        attempt_revision_row is None
        or sha256(json.loads(attempt_revision_row["payload"])) != attempt_revision_row["digest"]
        or observation_payload["attempt"]["input_sha256"] != attempt_revision_row["digest"]
        or observation_payload["attempt"]["attempt_id"] != observation_row["attempt_id"]
        or observation_payload["attempt"]["revision"] != observation_row["attempt_revision"]
    ):
        raise ValueError(
            "Frozen observation attempt identity does not match its immutable revision"
        )
    if (
        attempt["spec"].get("status") != "completed"
        or attempt["spec"].get("evaluator_kind") != "simulator"
        or attempt["spec"].get("fidelity") != 1.0
        or attempt["spec"].get("executable_method_id") != proposal_row["executable_method_id"]
    ):
        raise ValueError(
            "Campaign observation requires the exact completed high-fidelity simulator attempt"
        )
    experiment_row = experiments._owned(con, "experiments", owner, observation_row["experiment_id"])
    if sha256(json.loads(experiment_row["payload"])) != experiment_row["digest"]:
        raise ValueError("Stored campaign experiment digest does not match its contents")
    experiment = experiments._experiment(experiment_row)
    contract_link = experiment.get("observation_contract")
    producer_link = experiment.get("observation_producer")
    if (
        contract_link
        != {
            "contract_id": task["contract"]["contract_id"],
            "input_sha256": task["contract"]["input_sha256"],
        }
        or not isinstance(producer_link, dict)
        or producer_link.get("producer_id") != task["producer"]["producer_id"]
        or producer_link.get("input_sha256") != task["producer"]["input_sha256"]
    ):
        raise ValueError(
            "Campaign experiment is not bound to the task's exact producer and contract"
        )
    run_links = evidence.get("producer_evidence")
    if not isinstance(run_links, dict) or not isinstance(run_links.get("run_id"), str):
        raise ValueError("Campaign snapshot must explicitly link to its Gaussian producer run")
    run_id = run_links["run_id"]
    run_row = con.execute(
        "SELECT * FROM gaussian_observations WHERE id=? AND owner=?", (run_id, owner)
    ).fetchone()
    if run_row is None:
        raise ValueError("Campaign Gaussian producer run is missing or inaccessible")
    run = gaussian_producer._observation_response(run_row, con)
    if (
        run["producer_id"] != task["producer"]["producer_id"]
        or run["attempt_id"] != attempt["attempt_id"]
        or run["attempt_revision"] != attempt["revision"]
        or run["executable_method_id"] != proposal_row["executable_method_id"]
    ):
        raise ValueError(
            "Gaussian producer run is not bound to the exact task, attempt and proposal"
        )
    producer = gaussian_producer._producer_response(
        gaussian_producer._producer_row(con, owner, run["producer_id"])
    )
    if (
        producer["input_sha256"] != task["producer"]["input_sha256"]
        or producer["output_sha256"] != task["producer"]["output_sha256"]
    ):
        raise ValueError("Campaign Gaussian producer identity differs from the frozen protocol")
    prediction_row = con.execute(
        "SELECT * FROM retention_predictions WHERE id=? AND owner=?",
        (run["retention_prediction_id"], owner),
    ).fetchone()
    if prediction_row is None:
        raise ValueError("Campaign Gaussian producer prediction is missing")
    prediction = retention._prediction_response(prediction_row)
    prediction_model = prediction["prediction"]["model"]
    if (
        prediction_model["model_id"] != task["producer"]["retention_model_id"]
        or prediction_model["input_sha256"] != task["producer"]["retention_model_input_sha256"]
    ):
        raise ValueError("Campaign Gaussian prediction uses a different retention model")
    prediction_method = prediction["prediction"]["method"]
    if (
        prediction["method_id"] != proposal_row["executable_method_id"]
        or prediction_method.get("input_sha256") != proposal_row["method_input_digest"]
        or prediction_method.get("canonical_program_sha256")
        != proposal_row["canonical_program_digest"]
    ):
        raise ValueError("Campaign Gaussian prediction uses a different executable method")
    method_row = _owned_method(con, owner, proposal_row["executable_method_id"])
    method = _record(method_row)
    if method["canonical_program_sha256"] != proposal_row["canonical_program_digest"]:
        raise ValueError("Campaign method canonical-program identity is stale")
    expected_link = {
        "producer_id": producer["producer_id"],
        "producer_input_sha256": producer["input_sha256"],
        "run_id": run["run_id"],
        "run_input_sha256": run["input_sha256"],
        "run_output_sha256": run["output_sha256"],
        "prediction_id": prediction["prediction_id"],
        "prediction_input_sha256": prediction["input_sha256"],
        "prediction_output_sha256": prediction["output_sha256"],
    }
    if run_links != expected_link:
        raise ValueError(
            "Observation producer evidence link does not match the stored Gaussian run"
        )
    expected_cohort = task["analyte_ids"]
    model_cohort = sorted(item["analyte_id"] for item in producer["spec"]["widths"])
    run_cohort = sorted(item["analyte_id"] for item in run["observation"]["analytes"])
    if model_cohort != expected_cohort or run_cohort != expected_cohort:
        raise ValueError("Campaign Gaussian run does not cover the full predeclared analyte cohort")
    _verify_full_run_evidence(con, owner, task, observation_payload, run, observation_row)
    return run, method, {"producer": producer, "prediction": prediction, "evidence_link": run_links}


def _verify_full_run_evidence(
    con: sqlite3.Connection,
    owner: str,
    task: dict[str, Any],
    observation_payload: dict[str, Any],
    run: dict[str, Any],
    observation_row: sqlite3.Row,
) -> None:
    """Require the exact complete evidence emitted by the selected Gaussian run."""
    expected_spec = run["evidence_spec"]
    if observation_payload.get("input_spec") != expected_spec:
        raise ValueError(
            "Campaign snapshot must preserve the producer's complete confirmed analyte evidence spec"
        )
    evidence = observation_payload.get("evidence", {})
    cohort = task["analyte_ids"]
    evidence_rows = evidence.get("analytes")
    if (
        not isinstance(evidence_rows, list)
        or [row.get("analyte_id") for row in evidence_rows if isinstance(row, dict)] != cohort
        or evidence.get("required_analytes_without_declared_evidence") != []
    ):
        raise ValueError("Campaign snapshot must preserve evidence for the complete task cohort")

    generated = run["generated_results"]
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for item in generated:
        if not isinstance(item, dict):
            raise ValueError("Gaussian producer result links are malformed")
        key = (item.get("analyte_reference"), item.get("metric"))
        if key in expected:
            raise ValueError("Gaussian producer output contains duplicate result identities")
        expected[key] = item
    expected_keys = {
        (analyte_id, metric) for analyte_id in cohort for metric in ("retention_time", "resolution")
    }
    if set(expected) != expected_keys:
        raise ValueError("Gaussian producer run must contain both result links for every analyte")

    for evidence_row in evidence_rows:
        analyte_id = evidence_row["analyte_id"]
        for field, metric in (
            ("retention_result", "retention_time"),
            ("resolution_result", "resolution"),
        ):
            reference = evidence_row.get(field)
            generated_link = expected[(analyte_id, metric)]
            if not isinstance(reference, dict) or reference.get("outcome_included") is not False:
                raise ValueError("Campaign snapshot omitted a compact producer result reference")
            if reference.get("result_id") != generated_link["result_id"]:
                raise ValueError("Campaign snapshot result ID differs from the producer run")
            result_row = experiments._owned(
                con, "experiment_results", owner, generated_link["result_id"]
            )
            if (
                result_row["attempt_id"] != observation_row["attempt_id"]
                or result_row["attempt_revision"] != observation_row["attempt_revision"]
                or result_row["analyte_reference"] != analyte_id
                or result_row["metric"] != metric
                or reference.get("attempt_id") != observation_row["attempt_id"]
                or reference.get("attempt_revision") != observation_row["attempt_revision"]
                or reference.get("analyte_reference") != analyte_id
                or reference.get("metric") != metric
            ):
                raise ValueError("Campaign snapshot producer result identity is inconsistent")
            outcome = json.loads(result_row["payload"])
            if sha256(outcome) != result_row["digest"]:
                raise ValueError("Campaign producer result digest does not match its immutable row")
            raw_outcome = {key: value for key, value in outcome.items() if key != "provenance"}
            provenance = outcome.get("provenance")
            if (
                raw_outcome != generated_link.get("outcome")
                or not isinstance(provenance, dict)
                or provenance.get("value_origin") != "stored_ideal_gaussian_producer"
                or provenance.get("producer_run")
                != {
                    "run_id": run["run_id"],
                    "input_sha256": run["input_sha256"],
                    "output_sha256": run["output_sha256"],
                }
            ):
                raise ValueError(
                    "Campaign result was not emitted by the linked immutable Gaussian run"
                )
            raw_value = outcome.get("value")
            normalized_value = raw_value if outcome.get("status") == "observed" else None
            if (
                reference.get("input_sha256") != result_row["digest"]
                or reference.get("status") != outcome.get("status")
                or reference.get("raw_unit") != outcome.get("unit")
                or reference.get("raw_value") != raw_value
                or reference.get("normalized_value") != normalized_value
            ):
                raise ValueError(
                    "Campaign compact result reference differs from its immutable outcome"
                )


def _score_campaign_run(
    run: dict[str, Any], method: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any]:
    utility_definition = task["contract"]["utility_definition"]
    parameters = utility_definition["parameters"]
    analytes = run["observation"]["analytes"]
    analyte_ids = [item["analyte_id"] for item in analytes]
    if analyte_ids != task["analyte_ids"]:
        raise ValueError(
            "Stored Gaussian run analyte order differs from its predeclared task cohort"
        )
    resolutions = [
        item["resolution"] if item["resolution_status"] == "observed" else None for item in analytes
    ]
    canonical_program = method["evaluation"]["canonical"]
    duration = canonical_program["acquisition_duration_min"]
    utility = moses_observation_score(
        profile=utility_definition["profile"],
        num_compounds=len(analytes),
        peak_resolutions=resolutions,
        run_time_min=duration,
        max_run_time_min=parameters["max_run_time_min"],
        objective=utility_definition["objective"],
        compound_idx=utility_definition.get("compound_idx"),
        count_threshold=parameters["count_threshold"],
        resolution_threshold=parameters["resolution_threshold"],
        run_time_threshold_min=parameters["run_time_threshold_min"],
    )
    contract_spec = task["contract"]["spec"]
    score_result = {
        "utility": utility,
        "raw_metrics": {
            "analyte_ids": analyte_ids,
            "retention_statuses": [item["retention_status"] for item in analytes],
            "retention_times_min": [item["retention_time_min"] for item in analytes],
            "resolution_statuses": [item["resolution_status"] for item in analytes],
            "peak_resolutions": resolutions,
        },
        "runtime_min": duration,
        "analytical_acceptance": None,
        "feasible": duration <= contract_spec["max_acquisition_duration_min"],
        "runtime_preference_satisfied": duration <= contract_spec["runtime_preference_min"],
        "evaluation_cost": None,
        "evaluation_cost_unit": None,
    }
    return score_result


def _verify_ablation_consumption_source(
    con: sqlite3.Connection,
    owner: str,
    row: sqlite3.Row,
    consumption: dict[str, Any],
    *,
    campaign_context: tuple[sqlite3.Row, dict[str, Any], dict[str, Any], dict[str, Any]]
    | None = None,
) -> None:
    """Rebuild an ablation outcome from its exact immutable snapshot and producer run."""
    if campaign_context is None:
        campaign_row = _campaign_row(con, owner, row["campaign_id"])
        campaign_payload = _verify_campaign_payload(campaign_row)
        protocol, links = _task_for_campaign(con, owner, campaign_row, campaign_payload)
        campaign_context = (campaign_row, campaign_payload, protocol, links)
    campaign_row, campaign_payload, _, links = campaign_context
    spec = links["spec"]
    if not _is_ablation_protocol(spec):
        return

    proposal_row = _proposal_row(con, owner, row["proposal_id"])
    if proposal_row["campaign_id"] != row["campaign_id"] or proposal_row["state"] != "consumed":
        raise ValueError(
            "Stored ablation consumption does not link to a consumed campaign proposal"
        )
    proposal = _proposal_response(
        con,
        proposal_row,
        verify_training=False,
        campaign_context=campaign_context,
    )
    payload = json.loads(row["payload"])
    expected_proposal = {
        "ordinal": proposal["ordinal"],
        "candidate": proposal["candidate"],
        "candidate_sha256": proposal["candidate_sha256"],
        "method": proposal["method"],
    }
    if payload.get("proposal") != expected_proposal:
        raise ValueError("Stored ablation consumption proposal differs from its immutable source")

    snapshot, observation_row, observation_response, _ = _snapshot_and_observation(
        con, owner, row["observation_snapshot_id"], links["task"]
    )
    if (
        row["snapshot_digest"] != snapshot["sha256"]
        or row["observation_id"] != observation_row["id"]
        or row["attempt_id"] != observation_row["attempt_id"]
        or row["attempt_revision"] != observation_row["attempt_revision"]
        or row["observation_input_digest"] != observation_row["input_digest"]
        or row["evidence_digest"] != observation_row["evidence_digest"]
        or row["assessment_digest"] != observation_row["assessment_digest"]
    ):
        raise ValueError("Stored ablation consumption does not match its frozen snapshot member")

    run, method, source = _verify_campaign_observation(
        con,
        owner,
        links["task"],
        proposal_row,
        observation_row,
        observation_response,
    )
    expected_result = _score_campaign_run(run, method, links["task"])
    assessment = observation_response["observation"]["assessment"]
    expected_result["analytical_acceptance"] = assessment["analytical_acceptance"]
    expected_result["feasible"] = assessment["feasible"]
    expected_result["runtime_preference_satisfied"] = assessment["runtime_preference_satisfied"]
    expected_result["evaluation_cost"] = campaign_payload["evaluation_cost"]
    expected_result["evaluation_cost_unit"] = campaign_payload["evaluation_cost_unit"]
    expected_result["optimizer_target"] = _optimizer_target_record(spec, expected_result)
    if consumption["result"] != expected_result:
        raise ValueError(
            "Stored ablation utility or optimizer target differs from its immutable observation source"
        )

    expected_source = {
        "producer_id": source["producer"]["producer_id"],
        "producer_input_sha256": source["producer"]["input_sha256"],
        "producer_output_sha256": source["producer"]["output_sha256"],
        "run_id": run["run_id"],
        "run_input_sha256": run["input_sha256"],
        "run_output_sha256": run["output_sha256"],
        "prediction_id": source["prediction"]["prediction_id"],
        "prediction_input_sha256": source["prediction"]["input_sha256"],
        "prediction_output_sha256": source["prediction"]["output_sha256"],
        "retention_model_id": source["prediction"]["prediction"]["model"]["model_id"],
        "retention_model_input_sha256": source["prediction"]["prediction"]["model"]["input_sha256"],
        "producer_evidence_link": source["evidence_link"],
    }
    if consumption["source"] != expected_source:
        raise ValueError("Stored ablation source links differ from the immutable producer run")

    provenance = consumption["implementation_provenance"]
    campaign_provenance = campaign_payload["implementation_provenance"]
    if (
        not isinstance(provenance, dict)
        or provenance.get("campaign") != campaign_provenance
        or provenance.get("score_kernel") != campaign_provenance.get("utility_kernel")
        or "optimizer_target_kernel" not in campaign_provenance
    ):
        raise ValueError("Stored ablation scoring provenance differs from its frozen campaign")


def consume_campaign_snapshot(
    owner: str,
    campaign_id: str,
    proposal_id: str,
    snapshot_id: str,
    expected_revision: int,
    key: str,
) -> dict[str, Any]:
    _positive_integer(expected_revision, "expected_revision")
    request = {
        "operation": "consume_campaign_snapshot",
        "campaign_id": campaign_id,
        "proposal_id": proposal_id,
        "snapshot_id": snapshot_id,
        "expected_revision": expected_revision,
    }

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        campaign_row = _campaign_row(con, owner, campaign_id)
        campaign_payload = _verify_campaign_payload(campaign_row)
        _, links = _task_for_campaign(con, owner, campaign_row, campaign_payload)
        if campaign_row["revision"] != expected_revision:
            raise ValueError("Campaign revision conflict")
        proposal_row = _proposal_row(con, owner, proposal_id)
        if proposal_row["campaign_id"] != campaign_id or proposal_row["state"] != "pending":
            raise ValueError("Campaign proposal is not the pending proposal for this campaign")
        proposal = _proposal_response(con, proposal_row)
        snapshot, observation_row, observation_response, _ = _snapshot_and_observation(
            con, owner, snapshot_id, links["task"]
        )
        if (
            observation_row["attempt_id"] != snapshot["members"][0]["attempt_id"]
            or observation_row["attempt_revision"] != snapshot["members"][0]["attempt_revision"]
        ):
            raise ValueError(
                "Snapshot observation attempt identity does not match its frozen member"
            )
        run, method, source = _verify_campaign_observation(
            con, owner, links["task"], proposal_row, observation_row, observation_response
        )
        result = _score_campaign_run(run, method, links["task"])
        assessment = observation_response["observation"]["assessment"]
        result["analytical_acceptance"] = assessment["analytical_acceptance"]
        result["feasible"] = assessment["feasible"]
        result["runtime_preference_satisfied"] = assessment["runtime_preference_satisfied"]
        result["evaluation_cost"] = campaign_payload["evaluation_cost"]
        result["evaluation_cost_unit"] = campaign_payload["evaluation_cost_unit"]
        if _is_ablation_protocol(links["spec"]):
            result["optimizer_target"] = _optimizer_target_record(links["spec"], result)
        content: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "request": request,
            "campaign_id": campaign_id,
            "proposal_id": proposal_id,
            "snapshot_id": snapshot_id,
            "snapshot_sha256": snapshot["sha256"],
            "observation_id": observation_row["id"],
            "attempt_id": observation_row["attempt_id"],
            "attempt_revision": observation_row["attempt_revision"],
            "observation_input_sha256": observation_row["input_digest"],
            "evidence_sha256": observation_row["evidence_digest"],
            "assessment_sha256": observation_row["assessment_digest"],
            "proposal": {
                "ordinal": proposal["ordinal"],
                "candidate": proposal["candidate"],
                "candidate_sha256": proposal["candidate_sha256"],
                "method": proposal["method"],
            },
            "source": {
                "producer_id": source["producer"]["producer_id"],
                "producer_input_sha256": source["producer"]["input_sha256"],
                "producer_output_sha256": source["producer"]["output_sha256"],
                "run_id": run["run_id"],
                "run_input_sha256": run["input_sha256"],
                "run_output_sha256": run["output_sha256"],
                "prediction_id": source["prediction"]["prediction_id"],
                "prediction_input_sha256": source["prediction"]["input_sha256"],
                "prediction_output_sha256": source["prediction"]["output_sha256"],
                "retention_model_id": source["prediction"]["prediction"]["model"]["model_id"],
                "retention_model_input_sha256": source["prediction"]["prediction"]["model"][
                    "input_sha256"
                ],
                "producer_evidence_link": source["evidence_link"],
            },
            "result": result,
            "implementation_provenance": {
                "campaign": campaign_payload["implementation_provenance"],
                "score_kernel": {
                    "module": "measure_methods.moses_objective",
                    "source_sha256": _source_hash(
                        __import__(
                            "measure_methods.moses_objective", fromlist=["__file__"]
                        ).__file__
                    ),
                },
            },
        }
        output_digest = sha256(content)
        payload = {**content, "output_sha256": output_digest}
        _bounded(payload, "Campaign consumption")
        consumption_id, created = str(uuid.uuid4()), now()
        con.execute(
            """INSERT INTO campaign_consumptions (
              id, owner, campaign_id, proposal_id, observation_snapshot_id, snapshot_digest,
              observation_id, attempt_id, attempt_revision, observation_input_digest,
              evidence_digest, assessment_digest, payload, input_digest, output_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                consumption_id,
                owner,
                campaign_id,
                proposal_id,
                snapshot_id,
                snapshot["sha256"],
                observation_row["id"],
                observation_row["attempt_id"],
                observation_row["attempt_revision"],
                observation_row["input_digest"],
                observation_row["evidence_digest"],
                observation_row["assessment_digest"],
                canonical(payload),
                sha256(request),
                output_digest,
                created,
            ),
        )
        updated = con.execute(
            "UPDATE campaign_proposals SET state='consumed' WHERE id=? AND owner=? AND state='pending'",
            (proposal_id, owner),
        )
        if updated.rowcount != 1:
            raise ValueError("Campaign proposal was consumed concurrently")
        updated = con.execute(
            "UPDATE campaign_runs SET revision=revision+1 WHERE id=? AND owner=? AND revision=?",
            (campaign_id, owner, expected_revision),
        )
        if updated.rowcount != 1:
            raise ValueError("Campaign revision conflict")
        fresh_campaign = _campaign_row(con, owner, campaign_id)
        consumption = _consumption_response(_consumption_row(con, owner, consumption_id))
        return _bounded(
            {
                "schema_version": SCHEMA_VERSION,
                "campaign_id": campaign_id,
                "revision": fresh_campaign["revision"],
                "consumption": consumption,
                "campaign": _campaign_response(con, owner, fresh_campaign),
            }
        )

    return registry_mutation(owner, key, request, write)


def get_campaign(owner: str, campaign_id: str) -> dict[str, Any]:
    with registry_connection() as con:
        return _campaign_response(con, owner, _campaign_row(con, owner, campaign_id))


def get_campaign_proposal(owner: str, proposal_id: str) -> dict[str, Any]:
    with registry_connection() as con:
        return _proposal_response(con, _proposal_row(con, owner, proposal_id))


def get_campaign_consumption(owner: str, consumption_id: str) -> dict[str, Any]:
    """Retrieve one immutable campaign consumption by its owned identity."""
    with registry_connection() as con:
        row = _consumption_row(con, owner, consumption_id)
        consumption = _consumption_response(row)
        campaign_row = _campaign_row(con, owner, row["campaign_id"])
        campaign_payload = _verify_campaign_payload(campaign_row)
        protocol, links = _task_for_campaign(con, owner, campaign_row, campaign_payload)
        if _is_ablation_protocol(links["spec"]):
            _verify_ablation_consumption_source(
                con,
                owner,
                row,
                consumption,
                campaign_context=(campaign_row, campaign_payload, protocol, links),
            )
        return consumption
