"""Freeze analytical evidence over MCP, then correct it without rewriting history."""

import asyncio
import copy
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(data_dir: str) -> dict:
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

            async def call(name: str, arguments: dict) -> dict:
                response = await client.call_tool(name, arguments)
                if response.isError or response.structuredContent is None:
                    raise RuntimeError(f"{name}: {response.content}")
                return response.structuredContent

            contract = await call(
                "measurement_register_observation_contract",
                {
                    "title": "Synthetic two-analyte acceptance",
                    "spec": {
                        "required_analytes": ["A", "B"],
                        "resolution_definition": "caller-declared/1",
                        "max_acquisition_duration_min": 20,
                    },
                    "idempotency_key": "contract",
                },
            )
            if (
                await call(
                    "measurement_get_observation_contract", {"contract_id": contract["contract_id"]}
                )
                != contract
            ):
                raise RuntimeError("Contract identity changed")
            method = await call(
                "measurement_register_method",
                {
                    "title": "Seven-minute scouting program",
                    "declaration": {
                        "schema_version": "1.0",
                        "time_unit": "min",
                        "flow_unit": "mL/min",
                        "solvents": ["A", "B"],
                        "program": {
                            "kind": "isocratic",
                            "acquisition_duration": 7,
                            "state": {"flow": 0.4, "solvent_fractions": {"A": 0.8, "B": 0.2}},
                        },
                    },
                    "idempotency_key": "method",
                },
            )
            design = await call(
                "measurement_create_experiment",
                {
                    "spec": {
                        "title": "Synthetic scouting",
                        "source": "synthetic example",
                        "observation_contract_id": contract["contract_id"],
                    },
                    "idempotency_key": "design",
                },
            )
            attempt = await call(
                "measurement_record_attempt",
                {
                    "experiment_id": design["experiment_id"],
                    "spec": {
                        "status": "completed",
                        "evaluator_kind": "simulator",
                        "fidelity": 1.0,
                        "executable_method_id": method["method_id"],
                    },
                    "idempotency_key": "attempt",
                },
            )
            saved = await call(
                "measurement_record_results",
                {
                    "attempt_id": attempt["attempt_id"],
                    "expected_attempt_revision": 1,
                    "results": [
                        {
                            "metric": metric,
                            "analyte_reference": analyte,
                            "unit": unit,
                            "status": "observed",
                            "value": value,
                        }
                        for analyte, metric, unit, value in [
                            ("A", "retention_time", "min", 2),
                            ("A", "resolution", "1", 2),
                            ("B", "retention_time", "s", 240),
                            ("B", "resolution", "1", 1.5),
                        ]
                    ],
                    "idempotency_key": "results",
                },
            )
            result_ids = [item["result_id"] for item in saved["results"]]
            request = {
                "contract_id": contract["contract_id"],
                "attempt_id": attempt["attempt_id"],
                "expected_attempt_revision": 1,
                "spec": {
                    "fidelity_role": "high",
                    "observation_origin": "simulator_latent",
                    "analytes": [
                        {
                            "analyte_id": analyte,
                            "assignment": "confirmed",
                            "assignment_evidence": "Caller-declared synthetic identity",
                            "retention_result_id": result_ids[index],
                            "resolution_result_id": result_ids[index + 1],
                        }
                        for analyte, index in [("A", 0), ("B", 2)]
                    ],
                },
                "idempotency_key": "accepted-observation",
            }
            accepted = await call("measurement_evaluate_observation", request)
            snapshot = await call(
                "measurement_create_observation_snapshot",
                {
                    "contract_id": contract["contract_id"],
                    "observation_ids": [accepted["observation_id"]],
                    "idempotency_key": "snapshot",
                },
            )
            correction = await call(
                "measurement_record_results",
                {
                    "attempt_id": attempt["attempt_id"],
                    "expected_attempt_revision": 1,
                    "results": [
                        {
                            "metric": "resolution",
                            "analyte_reference": "B",
                            "unit": "1",
                            "status": "observed",
                            "value": 0,
                            "supersedes_result_id": result_ids[3],
                            "explanation": "Synthetic reprocessing reports a coeluting component",
                        }
                    ],
                    "idempotency_key": "corrected-result",
                },
            )
            corrected_request = copy.deepcopy(request)
            corrected_request["spec"]["analytes"][1]["resolution_result_id"] = correction[
                "results"
            ][0]["result_id"]
            corrected_request["idempotency_key"] = "corrected-observation"
            rejected = await call("measurement_evaluate_observation", corrected_request)
            uncertain_request = copy.deepcopy(corrected_request)
            uncertain_request["spec"]["analytes"][1]["assignment"] = "ambiguous"
            uncertain_request["spec"]["analytes"][1]["assignment_evidence"] = (
                "Synthetic identity assignment is unresolved"
            )
            uncertain_request["idempotency_key"] = "uncertain-observation"
            uncertain = await call("measurement_evaluate_observation", uncertain_request)
            historical = await call(
                "measurement_get_observation", {"observation_id": accepted["observation_id"]}
            )
            retained = await call(
                "measurement_get_observation_snapshot", {"snapshot_id": snapshot["snapshot_id"]}
            )
            retry = await call("measurement_evaluate_observation", request)
            if historical != accepted or retry != accepted or retained != snapshot:
                raise RuntimeError("A correction changed historical evidence or a retry receipt")
            first = accepted["observation"]["assessment"]
            return {
                "analytical_acceptance": first["analytical_acceptance"],
                "feasible": first["feasible"],
                "runtime_preference_satisfied": first["runtime_preference_satisfied"],
                "corrected_acceptance": rejected["observation"]["assessment"][
                    "analytical_acceptance"
                ],
                "ambiguous_acceptance": uncertain["observation"]["assessment"][
                    "analytical_acceptance"
                ],
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_sha256": snapshot["sha256"],
                "historical_observation_unchanged": True,
                "origin": accepted["observation"]["evidence"]["observation_origin"],
            }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="measure-observation-example-") as directory:
        print(json.dumps(asyncio.run(run(directory)), indent=2))
