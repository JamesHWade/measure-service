"""Run a durable confirmed-acceptance, low-noise synthetic campaign over stdio MCP.

This component example uses four single-fidelity simulator evaluations, including
three shared initial-design evaluations. Its deliberately censored synthetic
cohort keeps analytical acceptance unknown; the optimizer receives zero as the
predeclared reward for no confirmed success. It requires an installed BO-MCP
sidecar configured with ``MEASURE_BO_PYTHON`` and makes no physical claim.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from synthetic_campaign import (
    RESOLUTION_DEFINITION,
    _call,
    _mcp_client,
    _record_proposal_evidence,
)

CAMPAIGN_PROFILE = "synthetic-isocratic-ablation/1"
EXAMPLE_PROFILE = "confirmed-acceptance-ablation-example/1"
TASK_ID = "censored-three-analyte-reference"
SEED = 17
EVALUATION_BUDGET = 4
INITIAL_DESIGN_SIZE = 3
TARGET = "confirmed_acceptance_reward"
NOISE_PRIOR = "low_noise"
NOISE_PRIOR_PARAMETERS = [1.1, 100.0]
NOISE_PRIOR_FLOOR = 0.0001
OWNER = "local"


def _require_bo_runtime() -> str:
    configured = os.environ.get("MEASURE_BO_PYTHON")
    if not configured:
        raise RuntimeError(
            "This confirmed-acceptance example requires an installed BO-MCP runtime; "
            "set MEASURE_BO_PYTHON to its absolute interpreter path."
        )
    interpreter = Path(configured)
    if (
        not interpreter.is_absolute()
        or not interpreter.is_file()
        or not os.access(interpreter, os.X_OK)
    ):
        raise RuntimeError("MEASURE_BO_PYTHON must name an absolute executable file")
    return str(interpreter)


async def _register_inputs(client) -> dict[str, Any]:
    analyte_ids = ["A", "B", "C"]
    model = await _call(
        client,
        "measurement_register_retention_model",
        {
            "title": "Censored synthetic three-analyte retention model",
            "spec": {
                "gradient_coordinate": "column_inlet_at_analyte_entry",
                "analytes": [
                    {
                        "analyte_id": analyte_id,
                        "model": "linear",
                        "ln_k0": math.log(k0),
                        "S1": 1.0,
                    }
                    for analyte_id, k0 in (("A", 50.0), ("B", 100.0), ("C", 200.0))
                ],
                "column_length_m": 0.1,
                "effective_area_m2": 2.5e-6,
                "strong_solvent": "B",
                "parameter_origin": "synthetic",
                "parameter_reference": "Declared coefficients for a censored campaign example",
            },
            "idempotency_key": "confirmed-campaign-model",
        },
    )
    producer = await _call(
        client,
        "measurement_register_gaussian_producer",
        {
            "title": "Censored synthetic Gaussian centroid producer",
            "retention_model_id": model["model_id"],
            "spec": {
                "widths": [
                    {"analyte_id": analyte_id, "fwhm_min": 0.3} for analyte_id in analyte_ids
                ],
                "width_origin": "synthetic_assumption",
                "width_reference": "Fixed declared 0.3-minute FWHM per example analyte",
            },
            "idempotency_key": "confirmed-campaign-producer",
        },
    )
    contract = await _call(
        client,
        "measurement_register_observation_contract",
        {
            "title": "Censored synthetic observation contract",
            "spec": {
                "required_analytes": analyte_ids,
                "resolution_definition": RESOLUTION_DEFINITION,
                "resolution_threshold": 1.5,
                "max_acquisition_duration_min": 10.0,
                "runtime_preference_min": 5.0,
            },
            "idempotency_key": "confirmed-campaign-contract",
        },
    )
    protocol = await _call(
        client,
        "measurement_register_campaign_protocol",
        {
            "title": "Confirmed-acceptance low-noise campaign",
            "spec": {
                "profile": CAMPAIGN_PROFILE,
                "optimizer_target": TARGET,
                "surrogate_noise_prior": NOISE_PRIOR,
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
            "idempotency_key": "confirmed-campaign-protocol",
        },
    )
    return {"model": model, "producer": producer, "contract": contract, "protocol": protocol}


def _acceptance_and_reward(
    assessment: dict[str, Any], consumption: dict[str, Any]
) -> tuple[str, float]:
    original = assessment["observation"]["assessment"]["analytical_acceptance"]
    target = consumption["result"].get("optimizer_target")
    if not isinstance(target, dict) or target.get("name") != TARGET:
        raise RuntimeError("Stored campaign result omitted the declared confirmed-reward target")
    value = target.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != 0.0:
        raise RuntimeError("A censored unknown assessment must produce a finite zero reward")
    if original != "unknown":
        raise RuntimeError("The censored example unexpectedly resolved strict acceptance")
    return original, float(value)


async def _propose_and_consume(
    client,
    inputs: dict[str, Any],
    campaign: dict[str, Any],
    ordinal: int,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    request = {
        "campaign_id": campaign["campaign_id"],
        "expected_revision": campaign["revision"],
        "idempotency_key": f"confirmed-campaign-suggest-{ordinal}",
    }
    suggested = await _call(client, "measurement_suggest_campaign", request)
    proposal = await _call(
        client,
        "measurement_get_campaign_proposal",
        {"proposal_id": suggested["proposal"]["proposal_id"]},
    )
    if proposal != suggested["proposal"]:
        raise RuntimeError("Stored proposal retrieval changed its original receipt")
    expected_source = (
        "common_initial_design" if ordinal < INITIAL_DESIGN_SIZE else "bo_mcp_gp_logei"
    )
    if proposal["proposal_source"] != expected_source:
        raise RuntimeError("Campaign proposal did not use its declared design or BO route")
    if ordinal >= INITIAL_DESIGN_SIZE:
        backend = proposal.get("backend")
        if (
            not isinstance(backend, dict)
            or backend.get("generation_method") != "bo"
            or backend.get("adapter_profile") != "bo-mcp-gp-logei-ablation/1"
        ):
            raise RuntimeError("The final proposal was not generated by the declared BO route")

    snapshot, assessment = await _record_proposal_evidence(client, inputs, proposal, ordinal)
    consume_request = {
        "campaign_id": campaign["campaign_id"],
        "proposal_id": proposal["proposal_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "expected_revision": suggested["revision"],
        "idempotency_key": f"confirmed-campaign-consume-{ordinal}",
    }
    consumed = await _call(client, "measurement_consume_campaign_snapshot", consume_request)
    record = consumed["consumption"]
    _acceptance_and_reward(assessment, record)
    statuses = record["result"]["raw_metrics"]
    if (
        statuses["retention_statuses"] != ["not_eluted"] * 3
        or statuses["resolution_statuses"] != ["censored"] * 3
    ):
        raise RuntimeError("The declared synthetic reference did not remain fully censored")
    return (
        consumed["campaign"],
        proposal,
        snapshot,
        assessment,
        record,
        request,
        consume_request,
        suggested,
    )


async def run(data_dir: str) -> dict[str, Any]:
    _require_bo_runtime()
    proposals: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    assessments: list[dict[str, Any]] = []
    consumptions: list[dict[str, Any]] = []
    first_suggestion_request: dict[str, Any] | None = None
    first_suggestion_receipt: dict[str, Any] | None = None
    first_consumption_request: dict[str, Any] | None = None

    # Complete the three shared initial-design evaluations before restarting the service.
    async with _mcp_client(data_dir) as client:
        capabilities = await _call(client, "measurement_registry_capabilities", {})
        campaign_capabilities = capabilities["synthetic_campaigns"]
        if (
            CAMPAIGN_PROFILE not in campaign_capabilities["supported_profiles"]
            or TARGET not in campaign_capabilities["supported_optimizer_targets"]
            or NOISE_PRIOR not in campaign_capabilities["supported_surrogate_noise_priors"]
        ):
            raise RuntimeError("Registry capabilities do not include the declared V2 route")
        inputs = await _register_inputs(client)
        protocol = inputs["protocol"]
        configuration = protocol["optimizer_configuration"]
        if configuration != {
            "target_name": TARGET,
            "target_profile": "confirmed-acceptance-lower-bound/1",
            "noise_prior_params": NOISE_PRIOR_PARAMETERS,
            "adapter_profile": "bo-mcp-gp-logei-ablation/1",
        }:
            raise RuntimeError("Registered optimizer configuration differs from this example")
        campaign = await _call(
            client,
            "measurement_create_campaign",
            {
                "protocol_id": protocol["protocol_id"],
                "task_id": TASK_ID,
                "seed": SEED,
                "policy": "bo_mcp_gp_logei",
                "idempotency_key": "confirmed-campaign-create",
            },
        )
        for ordinal in range(INITIAL_DESIGN_SIZE):
            (
                campaign,
                proposal,
                snapshot,
                assessment,
                consumption,
                suggestion_request,
                consume_request,
                suggestion_receipt,
            ) = await _propose_and_consume(client, inputs, campaign, ordinal)
            proposals.append(proposal)
            snapshots.append(snapshot)
            assessments.append(assessment)
            consumptions.append(consumption)
            if ordinal == 0:
                first_suggestion_request = suggestion_request
                first_suggestion_receipt = suggestion_receipt
                first_consumption_request = consume_request
        if campaign["consumed_evaluations"] != INITIAL_DESIGN_SIZE:
            raise RuntimeError("The campaign did not consume exactly three initial evaluations")
        identifiers = {
            "model": inputs["model"]["model_id"],
            "producer": inputs["producer"]["producer_id"],
            "contract": inputs["contract"]["contract_id"],
            "protocol": protocol["protocol_id"],
            "campaign": campaign["campaign_id"],
        }

    if (
        first_suggestion_request is None
        or first_suggestion_receipt is None
        or first_consumption_request is None
    ):
        raise RuntimeError("The initial design did not retain its first retry receipts")

    # A new service process must recover three unknown assessments before asking the BO sidecar.
    async with _mcp_client(data_dir) as restarted:
        campaign = await _call(
            restarted, "measurement_get_campaign", {"campaign_id": identifiers["campaign"]}
        )
        if campaign["state"] != "ready_to_suggest" or campaign["consumed_evaluations"] != 3:
            raise RuntimeError("Restart did not recover the exact three-evaluation prefix")
        fourth = await _propose_and_consume(restarted, inputs, campaign, 3)
        campaign, proposal, snapshot, assessment, consumption, _, _, _ = fourth
        proposals.append(proposal)
        snapshots.append(snapshot)
        assessments.append(assessment)
        consumptions.append(consumption)

        training = proposal["training"]
        backend = proposal["backend"]
        if (
            training["count"] != INITIAL_DESIGN_SIZE
            or backend["training_count"] != INITIAL_DESIGN_SIZE
            or training["sha256"] != backend["training_sha256"]
            or len(training["snapshot_members"]) != INITIAL_DESIGN_SIZE
            or any(
                not member["optimizer_training_included"] for member in training["snapshot_members"]
            )
        ):
            raise RuntimeError("BO training did not use all three immutable zero-reward outcomes")
        prior = backend["settings"].get("noise_prior")
        if backend["settings"].get("objective") != {
            "name": TARGET,
            "direction": "maximize",
        } or prior != {
            "family": "Gamma",
            "concentration": 1.1,
            "rate": 100.0,
            "space": "standardized_target_variance",
            "constraint_lower_bound": NOISE_PRIOR_FLOOR,
            "learned": True,
        }:
            raise RuntimeError("Actual backend settings differ from the confirmed low-noise route")

        first_suggestion_retry = await _call(
            restarted, "measurement_suggest_campaign", first_suggestion_request
        )
        first_consumption_retry = await _call(
            restarted,
            "measurement_consume_campaign_snapshot",
            first_consumption_request,
        )
        if first_suggestion_retry != first_suggestion_receipt:
            raise RuntimeError("Restarted suggestion retry changed its original receipt")
        if first_consumption_retry["consumption"] != consumptions[0]:
            raise RuntimeError("Restarted consumption retry changed its original receipt")

        retained_campaign = await _call(
            restarted, "measurement_get_campaign", {"campaign_id": campaign["campaign_id"]}
        )
        retained_proposals = [
            await _call(
                restarted,
                "measurement_get_campaign_proposal",
                {"proposal_id": item["proposal_id"]},
            )
            for item in proposals
        ]
        retained_snapshots = [
            await _call(
                restarted,
                "measurement_get_observation_snapshot",
                {"snapshot_id": item["snapshot_id"]},
            )
            for item in snapshots
        ]
        retained_consumptions = [
            await _call(
                restarted,
                "measurement_get_campaign_consumption",
                {"consumption_id": item["consumption_id"]},
            )
            for item in consumptions
        ]

    if (
        retained_campaign["state"] != "budget_complete"
        or retained_campaign["consumed_evaluations"] != EVALUATION_BUDGET
    ):
        raise RuntimeError("Restarted service did not retain the complete four-evaluation campaign")
    if retained_snapshots != snapshots or retained_consumptions != consumptions:
        raise RuntimeError("Snapshot or consumption contents changed after service restart")
    for original, retained in zip(proposals, retained_proposals, strict=True):
        if {key: value for key, value in original.items() if key != "state"} != {
            key: value for key, value in retained.items() if key != "state"
        } or retained["state"] != "consumed":
            raise RuntimeError("Proposal receipt changed after service restart")

    acceptances = [
        item["observation"]["assessment"]["analytical_acceptance"] for item in assessments
    ]
    target_records = [item["result"]["optimizer_target"] for item in consumptions]
    rewards = [item["value"] for item in target_records]
    if acceptances != ["unknown"] * EVALUATION_BUDGET or rewards != [0.0] * EVALUATION_BUDGET:
        raise RuntimeError("Unknown analytical assessment was not preserved beside zero reward")
    if [item["proposal_source"] for item in retained_proposals] != [
        "common_initial_design",
        "common_initial_design",
        "common_initial_design",
        "bo_mcp_gp_logei",
    ]:
        raise RuntimeError("Campaign proposal sequence differs from the fixed four-step design")

    return {
        "profile": EXAMPLE_PROFILE,
        "owner": OWNER,
        "protocol": {
            "protocol_id": identifiers["protocol"],
            "profile": CAMPAIGN_PROFILE,
            "optimizer_target": TARGET,
            "surrogate_noise_prior": NOISE_PRIOR,
            "noise_prior_parameters": NOISE_PRIOR_PARAMETERS,
            "seed": SEED,
            "initial_design_size": INITIAL_DESIGN_SIZE,
            "evaluation_budget": EVALUATION_BUDGET,
            "evaluation_cost_unit": "synthetic_evaluation",
        },
        "campaign": {
            "campaign_id": retained_campaign["campaign_id"],
            "state": retained_campaign["state"],
            "revision": retained_campaign["revision"],
            "consumed_evaluations": retained_campaign["consumed_evaluations"],
            "budget_remaining": retained_campaign["budget_remaining"],
            "evaluation_cost_total": math.fsum(
                item["result"]["evaluation_cost"] for item in retained_consumptions
            ),
        },
        "records": {
            "proposal_ids": [item["proposal_id"] for item in retained_proposals],
            "proposal_sources": [item["proposal_source"] for item in retained_proposals],
            "candidates": [item["candidate"] for item in retained_proposals],
            "analytical_acceptance": acceptances,
            "optimizer_targets": target_records,
            "retention_statuses": [
                item["result"]["raw_metrics"]["retention_statuses"]
                for item in retained_consumptions
            ],
            "resolution_statuses": [
                item["result"]["raw_metrics"]["resolution_statuses"]
                for item in retained_consumptions
            ],
            "snapshot_ids": [item["snapshot_id"] for item in retained_snapshots],
            "snapshot_sha256": [item["sha256"] for item in retained_snapshots],
            "consumption_ids": [item["consumption_id"] for item in retained_consumptions],
            "consumption_output_sha256": [item["output_sha256"] for item in retained_consumptions],
        },
        "bo_training": {
            "count": training["count"],
            "sha256": training["sha256"],
            "snapshot_members": [
                {
                    "ordinal": member["ordinal"],
                    "snapshot_id": member["snapshot_id"],
                    "included": member["optimizer_training_included"],
                }
                for member in training["snapshot_members"]
            ],
            "backend_id": backend["backend_id"],
            "adapter_profile": backend["adapter_profile"],
            "objective": backend["settings"]["objective"],
            "noise_prior": prior,
            "elapsed_seconds": backend["runtime"]["elapsed_seconds"],
        },
        "assertions": {
            "all_campaign_steps_used_mcp": True,
            "r_backend_required": False,
            "bo_backend_used": True,
            "initial_design_counted": True,
            "all_three_initial_assessments_unknown": True,
            "all_unknowns_scored_zero_without_reclassifying_acceptance": True,
            "three_zero_rewards_used_for_actual_bo_training": True,
            "bo_suggested_after_service_restart": True,
            "proposal_and_snapshot_receipts_stable_after_restart": True,
            "first_suggestion_and_consumption_retry_stable_after_restart": True,
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="measure-confirmed-campaign-") as directory:
        print(json.dumps(asyncio.run(run(directory)), indent=2, sort_keys=True))
