"""Exercise the explicit duplicate-candidate policy through installed stdio MCP."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPOSITORY = Path(__file__).parents[1].resolve()
SERVER_PYTHON = os.environ.get("MEASURE_EXAMPLE_PYTHON", sys.executable)
REPLICATION_PROFILE = "synthetic-isocratic-ablation/2"
RESOLUTION_DEFINITION = "gaussian-baseline-4sigma-finite-centroids/1"
TASK_ID = "replication-mcp-task"
SEED = 17


def _child_environment(data_dir: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("MEASURE_BO_PYTHON", None)
    environment.pop("MEASURE_EXAMPLE_PYTHONPATH", None)
    environment["MEASURE_MCP_DATA_DIR"] = str(data_dir)
    environment["MEASURE_MCP_IDENTITY"] = "local"
    environment["MEASURE_MCP_R"] = "/not-required/R"

    pythonpath = os.environ.get("MEASURE_EXAMPLE_PYTHONPATH")
    if pythonpath is None:
        pythonpath = environment.get("PYTHONPATH", "")
    retained = [
        entry
        for entry in pythonpath.split(os.pathsep)
        if entry and Path(entry).resolve() not in {REPOSITORY, REPOSITORY / "src"}
    ]
    if retained:
        environment["PYTHONPATH"] = os.pathsep.join(retained)
    else:
        environment.pop("PYTHONPATH", None)
    return environment


@asynccontextmanager
async def _mcp_client(data_dir: Path, working_directory: Path) -> AsyncIterator[ClientSession]:
    parameters = StdioServerParameters(
        command=SERVER_PYTHON,
        args=["-m", "measure_service"],
        env=_child_environment(data_dir),
        cwd=str(working_directory),
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


async def _call(client: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = await client.call_tool(name, arguments)
    if response.isError or response.structuredContent is None:
        raise AssertionError(f"{name} failed: {response.content}")
    return response.structuredContent


def _assert_replication_schema(tools: list[Any]) -> None:
    registration = next(
        tool for tool in tools if tool.name == "measurement_register_campaign_protocol"
    )
    schema = registration.inputSchema
    spec_schema = schema["properties"]["spec"]
    referenced_models = {
        variant.get("$ref", "").rsplit("/", maxsplit=1)[-1]
        for variant in spec_schema.get("anyOf", [])
    }
    assert "ReplicationProtocolSpec" in referenced_models

    replication = schema["$defs"]["ReplicationProtocolSpec"]
    assert set(replication["required"]) >= {
        "profile",
        "optimizer_target",
        "surrogate_noise_prior",
        "duplicate_policy",
    }
    assert replication["properties"]["profile"]["const"] == REPLICATION_PROFILE
    assert replication["properties"]["duplicate_policy"]["enum"] == ["reject", "replicate"]


def _protocol_spec(producer_id: str, contract_id: str, duplicate_policy: str) -> dict[str, Any]:
    return {
        "profile": REPLICATION_PROFILE,
        "optimizer_target": "historical_utility",
        "surrogate_noise_prior": "default",
        "duplicate_policy": duplicate_policy,
        "domain": {
            "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
            "acquisition_duration_min": {"lower": 2.0, "upper": 10.0},
        },
        "flow_ml_min": 0.5,
        "constraints": {},
        "tasks": [
            {
                "task_id": TASK_ID,
                "producer_id": producer_id,
                "observation_contract_id": contract_id,
            }
        ],
        "seeds": [SEED],
        "evaluation_budget": 4,
        "initial_design_size": 3,
        "evaluation_cost": 1.0,
        "evaluation_cost_unit": "synthetic_evaluation",
    }


async def _register_synthetic_inputs(client: ClientSession) -> dict[str, Any]:
    analyte_ids = ["A", "B"]
    model = await _call(
        client,
        "measurement_register_retention_model",
        {
            "title": "Synthetic replication retention reference",
            "spec": {
                "gradient_coordinate": "column_inlet_at_analyte_entry",
                "analytes": [
                    {"analyte_id": "A", "model": "linear", "ln_k0": 1.0, "S1": 0.0},
                    {"analyte_id": "B", "model": "linear", "ln_k0": 2.0, "S1": 0.0},
                ],
                "column_length_m": 0.1,
                "effective_area_m2": 2.5e-6,
                "strong_solvent": "B",
                "parameter_origin": "synthetic",
                "parameter_reference": "Declared coefficients for a synthetic MCP contract test",
            },
            "idempotency_key": "replication-model",
        },
    )
    producer = await _call(
        client,
        "measurement_register_gaussian_producer",
        {
            "title": "Synthetic replication Gaussian producer",
            "retention_model_id": model["model_id"],
            "spec": {
                "widths": [
                    {"analyte_id": analyte_id, "fwhm_min": 0.4} for analyte_id in analyte_ids
                ],
                "width_origin": "synthetic_assumption",
                "width_reference": "Fixed 0.4-minute FWHM for this synthetic contract test",
            },
            "idempotency_key": "replication-producer",
        },
    )
    contract = await _call(
        client,
        "measurement_register_observation_contract",
        {
            "title": "Synthetic replication observation contract",
            "spec": {
                "required_analytes": analyte_ids,
                "resolution_definition": RESOLUTION_DEFINITION,
                "resolution_threshold": 1.5,
                "max_acquisition_duration_min": 10.0,
                "runtime_preference_min": 5.0,
            },
            "idempotency_key": "replication-contract",
        },
    )
    return {"model": model, "producer": producer, "contract": contract}


async def _campaign_state(client: ClientSession, campaign_id: str) -> dict[str, Any]:
    campaign = await _call(client, "measurement_get_campaign", {"campaign_id": campaign_id})
    attempts = await _call(
        client,
        "measurement_list_campaign_suggestion_attempts",
        {"campaign_id": campaign_id, "limit": 10},
    )
    proposal_id = campaign["pending_proposal"]["proposal_id"]
    proposal = await _call(
        client, "measurement_get_campaign_proposal", {"proposal_id": proposal_id}
    )
    return {"campaign": campaign, "attempts": attempts, "proposal": proposal}


async def _exercise_replication_policy(data_dir: Path, working_directory: Path) -> None:
    async with _mcp_client(data_dir, working_directory) as client:
        capabilities = await _call(client, "measurement_registry_capabilities", {})
        campaign_capabilities = capabilities["synthetic_campaigns"]
        assert REPLICATION_PROFILE in campaign_capabilities["supported_profiles"]
        assert campaign_capabilities["duplicate_candidates"] == {
            "legacy_profiles": "reject",
            "explicit_profile": REPLICATION_PROFILE,
            "required_policy": ["reject", "replicate"],
            "equality": "exact_normalized_candidate_sha256",
            "replicate_evidence": "fresh_execution_and_consumption_counted_per_ordinal",
        }
        assert campaign_capabilities["bo_runtime_configured"] is False
        _assert_replication_schema((await client.list_tools()).tools)

        inputs = await _register_synthetic_inputs(client)
        specs = {
            policy: _protocol_spec(
                inputs["producer"]["producer_id"],
                inputs["contract"]["contract_id"],
                policy,
            )
            for policy in ("reject", "replicate")
        }

        missing_policy = dict(specs["reject"])
        missing_policy.pop("duplicate_policy")
        missing = await client.call_tool(
            "measurement_register_campaign_protocol",
            {
                "title": "Missing replication duplicate policy",
                "spec": missing_policy,
                "idempotency_key": "replication-missing-policy",
            },
        )
        assert missing.isError

        invalid_policy = {**specs["reject"], "duplicate_policy": "allow"}
        invalid = await client.call_tool(
            "measurement_register_campaign_protocol",
            {
                "title": "Invalid replication duplicate policy",
                "spec": invalid_policy,
                "idempotency_key": "replication-invalid-policy",
            },
        )
        assert invalid.isError

        protocols = {
            policy: await _call(
                client,
                "measurement_register_campaign_protocol",
                {
                    "title": "Synthetic replication policy protocol",
                    "spec": specs[policy],
                    "idempotency_key": f"replication-protocol-{policy}",
                },
            )
            for policy in ("reject", "replicate")
        }
        assert protocols["reject"]["spec"]["duplicate_policy"] == "reject"
        assert protocols["replicate"]["spec"]["duplicate_policy"] == "replicate"
        assert protocols["reject"]["input_sha256"] != protocols["replicate"]["input_sha256"]
        assert protocols["reject"]["output_sha256"] != protocols["replicate"]["output_sha256"]

        arms: dict[str, dict[str, Any]] = {}
        for duplicate_policy in ("reject", "replicate"):
            campaign = await _call(
                client,
                "measurement_create_campaign",
                {
                    "protocol_id": protocols[duplicate_policy]["protocol_id"],
                    "task_id": TASK_ID,
                    "seed": SEED,
                    "policy": "random",
                    "idempotency_key": f"replication-campaign-{duplicate_policy}",
                },
            )
            request = {
                "campaign_id": campaign["campaign_id"],
                "expected_revision": campaign["revision"],
                "idempotency_key": f"replication-suggest-{duplicate_policy}",
            }
            receipt = await _call(client, "measurement_suggest_campaign", request)
            proposal = receipt["proposal"]
            assert proposal["policy"] == "random"
            assert proposal["proposal_source"] == "common_initial_design"
            assert proposal["backend"] is None
            assert proposal["repetition"] == {
                "policy": duplicate_policy,
                "is_repeat": False,
                "first_proposal_id": None,
                "matching_prior_proposals": [],
            }
            arms[duplicate_policy] = {
                "campaign_id": campaign["campaign_id"],
                "request": request,
                "receipt": receipt,
                "protocol_input_sha256": protocols[duplicate_policy]["input_sha256"],
                "protocol_output_sha256": protocols[duplicate_policy]["output_sha256"],
            }

        assert (
            arms["reject"]["receipt"]["proposal"]["candidate"]
            == arms["replicate"]["receipt"]["proposal"]["candidate"]
        )

        for arm in arms.values():
            state = await _campaign_state(client, arm["campaign_id"])
            assert state["campaign"]["consumed_evaluations"] == 0
            assert state["campaign"]["budget_remaining"] == 4
            assert state["campaign"]["history"] == []
            assert state["campaign"]["revision"] == 2
            assert state["campaign"]["protocol_input_sha256"] == arm["protocol_input_sha256"]
            assert state["campaign"]["protocol_output_sha256"] == arm["protocol_output_sha256"]
            assert (
                state["campaign"]["pending_proposal"]["proposal_id"]
                == (arm["receipt"]["proposal"]["proposal_id"])
            )
            assert len(state["attempts"]["records"]) == 1
            assert state["attempts"]["next_after"] is None
            arm["persisted_state"] = state

    # Starting a second stdio client launches a fresh service process over the same store.
    async with _mcp_client(data_dir, working_directory) as restarted:
        for arm in arms.values():
            replay = await _call(restarted, "measurement_suggest_campaign", arm["request"])
            assert replay == arm["receipt"]
            assert await _campaign_state(restarted, arm["campaign_id"]) == arm["persisted_state"]


def test_replication_policy_is_schema_bound_and_replays_over_stdio(tmp_path: Path) -> None:
    data_dir = tmp_path / "service-data"
    data_dir.mkdir()
    asyncio.run(_exercise_replication_policy(data_dir, tmp_path))
