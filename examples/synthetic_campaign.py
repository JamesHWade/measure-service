"""Run and verify a complete durable, synthetic campaign over stdio MCP.

The example uses a temporary SQLite store, a fixed simulator-latent Gaussian
producer, and the deterministic random campaign policy by default. The optional
``--policy bo_mcp_gp_logei`` uses a configured BO-MCP runtime. Neither needs R.
Its results are synthetic references, not physical instrument measurements.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

RESOLUTION_DEFINITION = "gaussian-baseline-4sigma-finite-centroids/1"
EVALUATION_BUDGET = 4
INITIAL_DESIGN_SIZE = 3
SEED = 17
TASK_ID = "three-analyte-reference"


@asynccontextmanager
async def _mcp_client(data_dir: str) -> AsyncIterator[ClientSession]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "measure_service"],
        env=dict(
            os.environ,
            MEASURE_MCP_DATA_DIR=data_dir,
            MEASURE_MCP_IDENTITY="local",
            MEASURE_MCP_R="/not-required/R",
        ),
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


async def _call(client: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = await client.call_tool(name, arguments)
    if response.isError or response.structuredContent is None:
        raise RuntimeError(f"{name}: {response.content}")
    return response.structuredContent


async def _register_inputs(client: ClientSession) -> dict[str, Any]:
    analyte_ids = ["A", "B", "C"]
    model = await _call(
        client,
        "measurement_register_retention_model",
        {
            "title": "Synthetic three-analyte retention reference",
            "spec": {
                "gradient_coordinate": "column_inlet_at_analyte_entry",
                "analytes": [
                    {
                        "analyte_id": analyte_id,
                        "model": "linear",
                        "ln_k0": math.log(k0),
                        "S1": slope,
                    }
                    for analyte_id, k0, slope in (
                        ("A", 50.0, 5.0),
                        ("B", 100.0, 5.0),
                        ("C", 200.0, 5.0),
                    )
                ],
                "column_length_m": 0.1,
                "effective_area_m2": 2.5e-6,
                "strong_solvent": "B",
                "parameter_origin": "synthetic",
                "parameter_reference": "Declared synthetic coefficients for a runnable example",
            },
            "idempotency_key": "synthetic-campaign-model",
        },
    )
    producer = await _call(
        client,
        "measurement_register_gaussian_producer",
        {
            "title": "Synthetic three-analyte centroid producer",
            "retention_model_id": model["model_id"],
            "spec": {
                "widths": [
                    {"analyte_id": analyte_id, "fwhm_min": 0.3} for analyte_id in analyte_ids
                ],
                "width_origin": "synthetic_assumption",
                "width_reference": "Fixed declared 0.3-minute FWHM for each example analyte",
            },
            "idempotency_key": "synthetic-campaign-producer",
        },
    )
    contract = await _call(
        client,
        "measurement_register_observation_contract",
        {
            "title": "Synthetic complete three-analyte cohort",
            "spec": {
                "required_analytes": analyte_ids,
                "resolution_definition": RESOLUTION_DEFINITION,
                "resolution_threshold": 1.5,
                "max_acquisition_duration_min": 10.0,
                "runtime_preference_min": 5.0,
            },
            "idempotency_key": "synthetic-campaign-contract",
        },
    )
    protocol = await _call(
        client,
        "measurement_register_campaign_protocol",
        {
            "title": "Four-evaluation synthetic campaign",
            "spec": {
                "domain": {
                    "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
                    "acquisition_duration_min": {"lower": 2.0, "upper": 10.0},
                },
                "flow_ml_min": 0.5,
                "constraints": {},
                "tasks": [
                    {
                        "task_id": TASK_ID,
                        "producer_id": producer["producer_id"],
                        "observation_contract_id": contract["contract_id"],
                    }
                ],
                "seeds": [SEED],
                "evaluation_budget": EVALUATION_BUDGET,
                "initial_design_size": INITIAL_DESIGN_SIZE,
                "evaluation_cost": 1.0,
                "evaluation_cost_unit": "synthetic_evaluation",
            },
            "idempotency_key": "synthetic-campaign-protocol",
        },
    )
    return {
        "model": model,
        "producer": producer,
        "contract": contract,
        "protocol": protocol,
    }


async def _record_proposal_evidence(
    client: ClientSession, inputs: dict[str, Any], proposal: dict[str, Any], ordinal: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    method_id = proposal["method"]["method_id"]
    prefix = f"synthetic-campaign-evaluation-{ordinal}"
    experiment = await _call(
        client,
        "measurement_create_experiment",
        {
            "spec": {
                "title": f"Synthetic campaign evaluation {ordinal + 1}",
                "source": "synthetic campaign example",
                "executable_method_id": method_id,
                "observation_contract_id": inputs["contract"]["contract_id"],
                "observation_producer_id": inputs["producer"]["producer_id"],
            },
            "idempotency_key": f"{prefix}-experiment",
        },
    )
    attempt = await _call(
        client,
        "measurement_record_attempt",
        {
            "experiment_id": experiment["experiment_id"],
            "spec": {
                "status": "completed",
                "evaluator_kind": "simulator",
                "fidelity": 1.0,
                "executable_method_id": method_id,
            },
            "idempotency_key": f"{prefix}-attempt",
        },
    )
    prediction = await _call(
        client,
        "measurement_predict_retention",
        {
            "model_id": inputs["model"]["model_id"],
            "method_id": method_id,
            "idempotency_key": f"{prefix}-prediction",
        },
    )
    generated = await _call(
        client,
        "measurement_record_gaussian_observation",
        {
            "producer_id": inputs["producer"]["producer_id"],
            "prediction_id": prediction["prediction_id"],
            "attempt_id": attempt["attempt_id"],
            "expected_attempt_revision": attempt["revision"],
            "idempotency_key": f"{prefix}-generated-observation",
        },
    )
    assessment = await _call(
        client,
        "measurement_evaluate_observation",
        {
            "contract_id": inputs["contract"]["contract_id"],
            "attempt_id": attempt["attempt_id"],
            "expected_attempt_revision": attempt["revision"],
            "spec": generated["evidence_spec"],
            "idempotency_key": f"{prefix}-assessment",
        },
    )
    snapshot = await _call(
        client,
        "measurement_create_observation_snapshot",
        {
            "contract_id": inputs["contract"]["contract_id"],
            "observation_ids": [assessment["observation_id"]],
            "idempotency_key": f"{prefix}-snapshot",
        },
    )
    return snapshot, assessment


async def run(data_dir: str, policy: str = "random") -> dict[str, Any]:
    if policy not in ("random", "bo_mcp_gp_logei"):
        raise ValueError("Choose random or bo_mcp_gp_logei")
    async with _mcp_client(data_dir) as client:
        capabilities = await _call(client, "measurement_registry_capabilities", {})
        campaign_capabilities = capabilities["synthetic_campaigns"]
        if (
            "random" not in campaign_capabilities["policies"]
            or campaign_capabilities["generic_bo_service"] is not False
        ):
            raise RuntimeError("Registry capabilities do not match the declared campaign example")
        inputs = await _register_inputs(client)
        campaign = await _call(
            client,
            "measurement_create_campaign",
            {
                "protocol_id": inputs["protocol"]["protocol_id"],
                "task_id": TASK_ID,
                "seed": SEED,
                "policy": policy,
                "idempotency_key": "synthetic-campaign-create",
            },
        )

        proposals: list[dict[str, Any]] = []
        snapshots: list[dict[str, Any]] = []
        assessments: list[dict[str, Any]] = []
        consumptions: list[dict[str, Any]] = []
        first_suggestion_request: dict[str, Any] | None = None
        first_suggestion_receipt: dict[str, Any] | None = None
        first_consumption_request: dict[str, Any] | None = None
        for ordinal in range(EVALUATION_BUDGET):
            suggest_request = {
                "campaign_id": campaign["campaign_id"],
                "expected_revision": campaign["revision"],
                "idempotency_key": f"synthetic-campaign-suggest-{ordinal}",
            }
            suggested = await _call(client, "measurement_suggest_campaign", suggest_request)
            proposal = await _call(
                client,
                "measurement_get_campaign_proposal",
                {"proposal_id": suggested["proposal"]["proposal_id"]},
            )
            if proposal != suggested["proposal"]:
                raise RuntimeError("Stored proposal retrieval changed its original receipt")
            if proposal["method"]["method_id"] != suggested["proposal"]["method"]["method_id"]:
                raise RuntimeError("Stored proposal method identity changed")
            if policy == "random" and proposal["backend"] is not None:
                raise RuntimeError("The random example unexpectedly used a BO backend")
            if ordinal < INITIAL_DESIGN_SIZE:
                expected_source = "common_initial_design"
            else:
                expected_source = "random_policy" if policy == "random" else "bo_mcp_gp_logei"
            if proposal["proposal_source"] != expected_source:
                raise RuntimeError("Proposal did not follow the declared campaign policy")
            if ordinal >= INITIAL_DESIGN_SIZE and policy == "bo_mcp_gp_logei":
                if (
                    not isinstance(proposal["backend"], dict)
                    or proposal["backend"].get("generation_method") != "bo"
                ):
                    raise RuntimeError(
                        "The BO example did not receive an actual model-guided proposal"
                    )

            snapshot, assessment = await _record_proposal_evidence(
                client, inputs, proposal, ordinal
            )
            consume_request = {
                "campaign_id": campaign["campaign_id"],
                "proposal_id": proposal["proposal_id"],
                "snapshot_id": snapshot["snapshot_id"],
                "expected_revision": suggested["revision"],
                "idempotency_key": f"synthetic-campaign-consume-{ordinal}",
            }
            consumed = await _call(client, "measurement_consume_campaign_snapshot", consume_request)
            proposals.append(proposal)
            snapshots.append(snapshot)
            assessments.append(assessment)
            consumptions.append(consumed["consumption"])
            if ordinal == 0:
                first_suggestion_request = suggest_request
                first_suggestion_receipt = suggested
                first_consumption_request = consume_request
            campaign = consumed["campaign"]

        if (
            first_suggestion_request is None
            or first_suggestion_receipt is None
            or first_consumption_request is None
        ):
            raise RuntimeError("The declared campaign budget did not produce its first proposal")
        first_suggestion_retry = await _call(
            client, "measurement_suggest_campaign", first_suggestion_request
        )
        first_consumption_retry = await _call(
            client,
            "measurement_consume_campaign_snapshot",
            first_consumption_request,
        )
        if first_suggestion_retry != first_suggestion_receipt:
            raise RuntimeError(
                "A retry after campaign progress changed the original proposal receipt"
            )
        if first_consumption_retry["consumption"] != consumptions[0]:
            raise RuntimeError("A retry after campaign progress changed the original consumption")

    # A second stdio client starts a fresh service process against the same SQLite store.
    async with _mcp_client(data_dir) as restarted:
        retained_campaign = await _call(
            restarted, "measurement_get_campaign", {"campaign_id": campaign["campaign_id"]}
        )
        if retained_campaign["state"] != "budget_complete":
            raise RuntimeError("Restarted service did not retain the completed campaign")
        retained_proposals = [
            await _call(
                restarted,
                "measurement_get_campaign_proposal",
                {"proposal_id": proposal["proposal_id"]},
            )
            for proposal in proposals
        ]
        retained_snapshots = [
            await _call(
                restarted,
                "measurement_get_observation_snapshot",
                {"snapshot_id": snapshot["snapshot_id"]},
            )
            for snapshot in snapshots
        ]
        retained_consumptions = [
            await _call(
                restarted,
                "measurement_get_campaign_consumption",
                {"consumption_id": consumption["consumption_id"]},
            )
            for consumption in consumptions
        ]
        suggestion_retry = await _call(
            restarted, "measurement_suggest_campaign", first_suggestion_request
        )
        consumption_retry = await _call(
            restarted,
            "measurement_consume_campaign_snapshot",
            first_consumption_request,
        )

    for original, retained in zip(proposals, retained_proposals, strict=True):
        original_identity = {key: value for key, value in original.items() if key != "state"}
        retained_identity = {key: value for key, value in retained.items() if key != "state"}
        if retained_identity != original_identity or retained["state"] != "consumed":
            raise RuntimeError("Proposal identity or stored method changed after restart")
    if retained_snapshots != snapshots:
        raise RuntimeError("A frozen observation snapshot changed after restart")
    if retained_consumptions != consumptions:
        raise RuntimeError("Stored consumption records changed after restart")
    if suggestion_retry != first_suggestion_receipt:
        raise RuntimeError("Restarted service did not return the original proposal receipt")
    if consumption_retry["consumption"] != consumptions[0]:
        raise RuntimeError("Restarted service did not return the original consumption receipt")

    history = retained_campaign["history"]
    spent = math.fsum(item["evaluation_cost"] for item in history)
    if (
        retained_campaign["consumed_evaluations"] != EVALUATION_BUDGET
        or retained_campaign["budget_remaining"] != 0
        or spent != float(EVALUATION_BUDGET)
        or any(
            item["result"]["evaluation_cost_unit"] != "synthetic_evaluation"
            for item in consumptions
        )
    ):
        raise RuntimeError("Campaign history does not match its full evaluation budget and cost")

    return {
        "profile": "durable-synthetic-campaign-example/1",
        "campaign": {
            "campaign_id": retained_campaign["campaign_id"],
            "protocol_id": retained_campaign["protocol_id"],
            "policy": retained_campaign["policy"],
            "seed": retained_campaign["seed"],
            "state": retained_campaign["state"],
            "revision": retained_campaign["revision"],
            "evaluation_budget": retained_campaign["evaluation_budget"],
            "consumed_evaluations": retained_campaign["consumed_evaluations"],
            "budget_remaining": retained_campaign["budget_remaining"],
            "evaluation_cost_total": spent,
            "evaluation_cost_unit": "synthetic_evaluation",
        },
        "capabilities": {
            "campaign_policies": campaign_capabilities["policies"],
            "random_policy_supported": "random" in campaign_capabilities["policies"],
            "generic_bo_service": campaign_capabilities["generic_bo_service"],
            "optional_bo_runtime_configured": campaign_capabilities["bo_runtime_configured"],
        },
        "records": {
            "proposal_ids": [item["proposal_id"] for item in retained_proposals],
            "method_ids": [item["method"]["method_id"] for item in retained_proposals],
            "proposal_sources": [item["proposal_source"] for item in retained_proposals],
            "candidates": [item["candidate"] for item in retained_proposals],
            "backend_provenance": [item["backend"] for item in retained_proposals],
            "snapshot_ids": [item["snapshot_id"] for item in retained_snapshots],
            "snapshot_sha256": [item["sha256"] for item in retained_snapshots],
            "consumption_ids": [item["consumption_id"] for item in retained_consumptions],
            "consumption_input_sha256": [item["input_sha256"] for item in retained_consumptions],
            "consumption_output_sha256": [item["output_sha256"] for item in retained_consumptions],
            "observation_ids": [item["observation_id"] for item in assessments],
        },
        "assertions": {
            "all_campaign_steps_used_mcp": True,
            "r_backend_required": False,
            "bo_backend_used": policy == "bo_mcp_gp_logei",
            "initial_design_size": INITIAL_DESIGN_SIZE,
            "random_post_initialization_proposal": proposals[-1]["proposal_source"]
            == "random_policy",
            "all_proposals_used_stored_methods": True,
            "first_proposal_retry_stable_after_progress_and_restart": True,
            "first_consumption_retry_stable_after_progress_and_restart": True,
            "snapshots_unchanged_after_restart": True,
            "all_consumption_digests_unchanged_after_restart": True,
            "all_three_analytes_assessed_per_evaluation": all(
                len(item["observation"]["assessment"]["per_analyte"]) == 3 for item in assessments
            ),
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", choices=("random", "bo_mcp_gp_logei"), default="random")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="measure-synthetic-campaign-") as directory:
        print(json.dumps(asyncio.run(run(directory, args.policy)), indent=2, sort_keys=True))
