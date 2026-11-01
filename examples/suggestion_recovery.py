"""Demonstrate durable recovery of a synthetic campaign suggestion failure.

The example consumes three synthetic simulator observations, then asks the
optional BO route for a fourth candidate with its sidecar runtime deliberately
removed from the MCP server environment. It reads the persisted failure and
retries the identical request after restarting the service. This is a small
readiness demonstration, not an optimizer benchmark or physical measurement.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from synthetic_campaign import RESOLUTION_DEFINITION, _call, _record_proposal_evidence

PROFILE = "synthetic-suggestion-recovery-example/1"
CAMPAIGN_PROFILE = "synthetic-isocratic-ablation/1"
TASK_ID = "suggestion-recovery-three-analyte"
SEED = 17
INITIAL_DESIGN_SIZE = 3
EVALUATION_BUDGET = 4
TARGET = "confirmed_acceptance_reward"
NOISE_PRIOR = "default"


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _content_text(response: Any) -> str:
    return "\n".join(
        block.text for block in response.content if isinstance(getattr(block, "text", None), str)
    )


@asynccontextmanager
async def _mcp_client_without_bo(data_dir: str) -> AsyncIterator[ClientSession]:
    """Start the installed service over stdio with no configured BO interpreter."""
    environment = dict(os.environ)
    environment.pop("MEASURE_BO_PYTHON", None)
    environment.update(
        MEASURE_MCP_DATA_DIR=data_dir,
        MEASURE_MCP_IDENTITY="local",
        MEASURE_MCP_R="/not-required/R",
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "measure_service"],
        env=environment,
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


async def _register_inputs(client: ClientSession) -> dict[str, Any]:
    analytes = ["A", "B", "C"]
    model = await _call(
        client,
        "measurement_register_retention_model",
        {
            "title": "Suggestion recovery synthetic retention model",
            "spec": {
                "gradient_coordinate": "column_inlet_at_analyte_entry",
                "analytes": [
                    {"analyte_id": name, "model": "linear", "ln_k0": math.log(k0), "S1": 1.0}
                    for name, k0 in (("A", 50.0), ("B", 100.0), ("C", 200.0))
                ],
                "column_length_m": 0.1,
                "effective_area_m2": 2.5e-6,
                "strong_solvent": "B",
                "parameter_origin": "synthetic",
                "parameter_reference": "Declared coefficients for a suggestion-recovery example",
            },
            "idempotency_key": "suggestion-recovery-model",
        },
    )
    producer = await _call(
        client,
        "measurement_register_gaussian_producer",
        {
            "title": "Suggestion recovery synthetic centroid producer",
            "retention_model_id": model["model_id"],
            "spec": {
                "widths": [{"analyte_id": name, "fwhm_min": 0.3} for name in analytes],
                "width_origin": "synthetic_assumption",
                "width_reference": "Fixed declared 0.3-minute FWHM for each example analyte",
            },
            "idempotency_key": "suggestion-recovery-producer",
        },
    )
    contract = await _call(
        client,
        "measurement_register_observation_contract",
        {
            "title": "Suggestion recovery complete synthetic cohort",
            "spec": {
                "required_analytes": analytes,
                "resolution_definition": RESOLUTION_DEFINITION,
                "resolution_threshold": 1.5,
                "max_acquisition_duration_min": 10.0,
                "runtime_preference_min": 5.0,
            },
            "idempotency_key": "suggestion-recovery-contract",
        },
    )
    protocol = await _call(
        client,
        "measurement_register_campaign_protocol",
        {
            "title": "Four-step synthetic suggestion recovery protocol",
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
            "idempotency_key": "suggestion-recovery-protocol",
        },
    )
    return {"model": model, "producer": producer, "contract": contract, "protocol": protocol}


async def _read_attempts(client: ClientSession, campaign_id: str) -> list[dict[str, Any]]:
    page = await _call(
        client,
        "measurement_list_campaign_suggestion_attempts",
        {"campaign_id": campaign_id, "limit": 10},
    )
    if page["next_after"] is not None:
        raise RuntimeError("The bounded example unexpectedly exceeded one attempt page")
    return page["records"]


async def run(data_dir: str) -> dict[str, Any]:
    initial_consumptions: list[dict[str, Any]] = []
    async with _mcp_client_without_bo(data_dir) as client:
        capabilities = await _call(client, "measurement_registry_capabilities", {})
        campaign_capabilities = capabilities["synthetic_campaigns"]
        if campaign_capabilities["bo_runtime_configured"]:
            raise RuntimeError("The stdio service unexpectedly retained a BO runtime")
        if CAMPAIGN_PROFILE not in campaign_capabilities["supported_profiles"]:
            raise RuntimeError("The installed service does not expose the ablation protocol")

        inputs = await _register_inputs(client)
        expected_configuration = {
            "target_name": TARGET,
            "target_profile": "confirmed-acceptance-lower-bound/1",
            "noise_prior_params": [1.1, 0.05],
            "adapter_profile": "bo-mcp-gp-logei-ablation/1",
        }
        if inputs["protocol"].get("optimizer_configuration") != expected_configuration:
            raise RuntimeError("Registered target or default noise prior differs from the example")
        campaign = await _call(
            client,
            "measurement_create_campaign",
            {
                "protocol_id": inputs["protocol"]["protocol_id"],
                "task_id": TASK_ID,
                "seed": SEED,
                "policy": "bo_mcp_gp_logei",
                "idempotency_key": "suggestion-recovery-create",
            },
        )
        for ordinal in range(INITIAL_DESIGN_SIZE):
            request = {
                "campaign_id": campaign["campaign_id"],
                "expected_revision": campaign["revision"],
                "idempotency_key": f"suggestion-recovery-suggest-{ordinal}",
            }
            suggested = await _call(client, "measurement_suggest_campaign", request)
            proposal = suggested["proposal"]
            if proposal["proposal_source"] != "common_initial_design":
                raise RuntimeError("An initial design slot did not use the declared common design")
            snapshot, _ = await _record_proposal_evidence(client, inputs, proposal, ordinal)
            consumed = await _call(
                client,
                "measurement_consume_campaign_snapshot",
                {
                    "campaign_id": campaign["campaign_id"],
                    "proposal_id": proposal["proposal_id"],
                    "snapshot_id": snapshot["snapshot_id"],
                    "expected_revision": suggested["revision"],
                    "idempotency_key": f"suggestion-recovery-consume-{ordinal}",
                },
            )
            campaign = consumed["campaign"]
            initial_consumptions.append(consumed["consumption"])

        if campaign["consumed_evaluations"] != INITIAL_DESIGN_SIZE:
            raise RuntimeError("The initial synthetic observations were not fully consumed")
        if campaign["budget_remaining"] != 1 or campaign["state"] != "ready_to_suggest":
            raise RuntimeError("Campaign budget differs from the declared four-step design")

        failed_request = {
            "campaign_id": campaign["campaign_id"],
            "expected_revision": campaign["revision"],
            "idempotency_key": "suggestion-recovery-post-initial",
        }
        response = await client.call_tool("measurement_suggest_campaign", failed_request)
        if not response.isError:
            raise RuntimeError("The BO request unexpectedly succeeded without its runtime")
        if not _content_text(response).strip():
            raise RuntimeError("The expected tool failure did not return an error message")

        summaries_before = await _read_attempts(client, campaign["campaign_id"])
        failed_summaries = [item for item in summaries_before if item["ordinal"] == 3]
        if len(summaries_before) != 4 or len(failed_summaries) != 1:
            raise RuntimeError("Expected exactly three initial attempts and one failed BO attempt")
        if [item["ordinal"] for item in summaries_before] != [0, 1, 2, 3] or [
            item["state"] for item in summaries_before
        ] != ["accepted"] * 3 + ["failed"]:
            raise RuntimeError(
                "Suggestion-attempt history differs from the expected four-step path"
            )
        failed_summary = failed_summaries[0]
        first_record = await _call(
            client,
            "measurement_get_campaign_suggestion_attempt",
            {"suggestion_attempt_id": failed_summary["suggestion_attempt_id"]},
        )
        retained_campaign = await _call(
            client,
            "measurement_get_campaign",
            {"campaign_id": campaign["campaign_id"]},
        )
        if (
            retained_campaign["consumed_evaluations"] != INITIAL_DESIGN_SIZE
            or retained_campaign["budget_remaining"] != EVALUATION_BUDGET - INITIAL_DESIGN_SIZE
            or retained_campaign["revision"] != failed_request["expected_revision"]
        ):
            raise RuntimeError("Failed suggestion changed campaign state before service restart")

    outcome = first_record["outcome"]
    diagnostics = outcome["diagnostics"]
    if (
        first_record["state"] != "failed"
        or outcome["status"] != "failed"
        or outcome["phase"] != "candidate_generation"
        or outcome["failure"]["type"] != "OptimizerBackendError"
    ):
        raise RuntimeError("The unavailable BO route was not recorded as a failed suggestion")
    elapsed = outcome["candidate_generation_elapsed_seconds"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        raise RuntimeError("The outer candidate-generation timer was not retained")
    if not math.isfinite(elapsed) or elapsed < 0:
        raise RuntimeError("The outer candidate-generation timer is invalid")
    if (
        diagnostics["phase"] != "runtime_resolution"
        or diagnostics["category"] != "runtime_missing"
        or diagnostics["subprocess_elapsed_seconds"] is not None
    ):
        raise RuntimeError("Failure diagnostics do not distinguish missing runtime from child time")

    context_training = first_record["context"]["training"]
    requested = first_record["context"]["generation"]["request"]
    if (
        context_training["count"] != INITIAL_DESIGN_SIZE
        or context_training["records"] != requested["training_data"]
        or context_training["sha256"] != requested["training_sha256"]
        or requested["seed"] != SEED
        or diagnostics["training_sha256"] != requested["training_sha256"]
        or diagnostics["request_sha256"] != _canonical_sha256(requested)
    ):
        raise RuntimeError("The failure record does not retain its exact requested training data")
    training_refs = [
        {
            "ordinal": member["ordinal"],
            "consumption_id": member["consumption_id"],
            "snapshot_id": member["snapshot_id"],
            "snapshot_sha256": member["snapshot_sha256"],
            "candidate_sha256": member["candidate_sha256"],
            "optimizer_training_included": member["optimizer_training_included"],
        }
        for member in context_training["snapshot_members"]
    ]
    if [item["ordinal"] for item in training_refs] != [0, 1, 2]:
        raise RuntimeError("The failed request did not bind all three prior consumption records")
    if len(initial_consumptions) != INITIAL_DESIGN_SIZE:
        raise RuntimeError("The example did not retain the initial synthetic consumption prefix")

    attempt_record_sha256 = _canonical_sha256(first_record)
    stable_hashes = {
        name: first_record[name]
        for name in (
            "request_sha256",
            "request_key_sha256",
            "context_sha256",
            "start_sha256",
            "outcome_sha256",
        )
    }

    # Restart against the same store and retry the identical request and key.
    async with _mcp_client_without_bo(data_dir) as restarted:
        before_retry_campaign = await _call(
            restarted,
            "measurement_get_campaign",
            {"campaign_id": campaign["campaign_id"]},
        )
        retry = await restarted.call_tool("measurement_suggest_campaign", failed_request)
        if not retry.isError or not _content_text(retry).strip():
            raise RuntimeError("Retry did not replay the original missing-runtime failure")
        summaries_after = await _read_attempts(restarted, campaign["campaign_id"])
        second_record = await _call(
            restarted,
            "measurement_get_campaign_suggestion_attempt",
            {"suggestion_attempt_id": first_record["suggestion_attempt_id"]},
        )
        after_retry_campaign = await _call(
            restarted,
            "measurement_get_campaign",
            {"campaign_id": campaign["campaign_id"]},
        )

    if summaries_after != summaries_before or second_record != first_record:
        raise RuntimeError("Same-key retry changed or duplicated its durable failure record")
    if before_retry_campaign != after_retry_campaign:
        raise RuntimeError("Failed suggestion retry changed campaign state or evaluation budget")
    if (
        after_retry_campaign["consumed_evaluations"] != INITIAL_DESIGN_SIZE
        or after_retry_campaign["budget_remaining"] != EVALUATION_BUDGET - INITIAL_DESIGN_SIZE
        or after_retry_campaign["revision"] != failed_request["expected_revision"]
    ):
        raise RuntimeError(
            "The failed suggestion changed consumed evaluations or campaign revision"
        )

    return {
        "profile": PROFILE,
        "scope": "four synthetic evaluations; readiness demonstration only, not a benchmark",
        "protocol": {
            "profile": CAMPAIGN_PROFILE,
            "optimizer_target": TARGET,
            "surrogate_noise_prior": NOISE_PRIOR,
            "seed": SEED,
            "initial_design_size": INITIAL_DESIGN_SIZE,
            "evaluation_budget": EVALUATION_BUDGET,
            "evaluation_cost_unit": "synthetic_evaluation",
        },
        "campaign": {
            "campaign_id": after_retry_campaign["campaign_id"],
            "state": after_retry_campaign["state"],
            "revision": after_retry_campaign["revision"],
            "consumed_evaluations": after_retry_campaign["consumed_evaluations"],
            "budget_remaining": after_retry_campaign["budget_remaining"],
            "initial_consumption_ids": [item["consumption_id"] for item in initial_consumptions],
        },
        "suggestion_failure": {
            "suggestion_attempt_id": first_record["suggestion_attempt_id"],
            "ordinal": first_record["ordinal"],
            "state": first_record["state"],
            "failure_type": outcome["failure"]["type"],
            "failure_phase": diagnostics["phase"],
            "failure_category": diagnostics["category"],
            "outer_candidate_generation_elapsed_seconds": elapsed,
            "child_subprocess_elapsed_seconds": diagnostics["subprocess_elapsed_seconds"],
            "requested_training_count": context_training["count"],
            "requested_training_sha256": requested["training_sha256"],
            "requested_training_references": training_refs,
            "attempt_record_sha256_before_retry": attempt_record_sha256,
            "attempt_record_sha256_after_retry": _canonical_sha256(second_record),
            "ledger_hashes": stable_hashes,
        },
        "retry": {
            "same_failure_replayed": True,
            "attempt_records_before": len(summaries_before),
            "attempt_records_after": len(summaries_after),
            "no_new_proposal_or_consumption": True,
            "optional_bo_runtime_configured_in_server": False,
        },
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="measure-suggestion-recovery-") as directory:
        print(json.dumps(asyncio.run(run(directory)), indent=2, sort_keys=True))
