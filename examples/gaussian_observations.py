"""Retain generated latent results, assess them and freeze their exact evidence."""

import asyncio
import copy
import json
import math
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

RESOLUTION_DEFINITION = "gaussian-baseline-4sigma-finite-centroids/1"


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

            async def call(name, arguments):
                response = await client.call_tool(name, arguments)
                if response.isError or response.structuredContent is None:
                    raise RuntimeError(f"{name}: {response.content}")
                return response.structuredContent

            analytes = ["A", "B", "C"]
            model = await call(
                "measurement_register_retention_model",
                {
                    "title": "Synthetic constant-retention reference",
                    "spec": {
                        "gradient_coordinate": "column_inlet_at_analyte_entry",
                        "analytes": [
                            {"analyte_id": name, "model": "linear", "ln_k0": math.log(k), "S1": 0}
                            for name, k in zip(analytes, [3, 7, 11], strict=True)
                        ],
                        "column_length_m": 0.1,
                        "effective_area_m2": 2.5e-6,
                        "strong_solvent": "B",
                        "parameter_origin": "synthetic",
                        "parameter_reference": "Declared synthetic isocratic limiting cases",
                    },
                    "idempotency_key": "model",
                },
            )
            producer = await call(
                "measurement_register_gaussian_producer",
                {
                    "title": "Declared synthetic widths",
                    "retention_model_id": model["model_id"],
                    "spec": {
                        "widths": [{"analyte_id": name, "fwhm_min": 0.4} for name in analytes],
                        "width_origin": "synthetic_assumption",
                        "width_reference": "Fixed 0.4-minute FWHM assumption for all three analytes",
                    },
                    "idempotency_key": "producer",
                },
            )
            contract = await call(
                "measurement_register_observation_contract",
                {
                    "title": "Complete synthetic cohort",
                    "spec": {
                        "required_analytes": analytes,
                        "resolution_definition": RESOLUTION_DEFINITION,
                        "resolution_threshold": 1.5,
                        "max_acquisition_duration_min": 10,
                    },
                    "idempotency_key": "contract",
                },
            )
            protocol = await call(
                "measurement_register_campaign_protocol",
                {
                    "title": "Predeclared synthetic reference campaign",
                    "spec": {
                        "domain": {
                            "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
                            "acquisition_duration_min": {"lower": 2.0, "upper": 10.0},
                        },
                        "flow_ml_min": 0.5,
                        "constraints": {"max_acquisition_duration_min": 10.0},
                        "tasks": [
                            {
                                "task_id": "synthetic-reference",
                                "producer_id": producer["producer_id"],
                                "observation_contract_id": contract["contract_id"],
                            }
                        ],
                        "seeds": [17],
                        "evaluation_budget": 10,
                        "initial_design_size": 3,
                        "evaluation_cost": 1.0,
                        "evaluation_cost_unit": "synthetic_evaluation",
                    },
                    "idempotency_key": "campaign-protocol",
                },
            )
            retained_protocol = await call(
                "measurement_get_campaign_protocol", {"protocol_id": protocol["protocol_id"]}
            )
            if retained_protocol != protocol:
                raise RuntimeError("The immutable campaign protocol changed during retrieval")

            async def observe(duration, label):
                method = await call(
                    "measurement_register_method",
                    {
                        "title": f"{duration}-minute synthetic method",
                        "declaration": {
                            "time_unit": "min",
                            "flow_unit": "mL/min",
                            "solvents": ["A", "B"],
                            "program": {
                                "kind": "isocratic",
                                "acquisition_duration": duration,
                                "state": {"flow": 0.5, "solvent_fractions": {"A": 0.8, "B": 0.2}},
                            },
                        },
                        "idempotency_key": f"{label}-method",
                    },
                )
                experiment = await call(
                    "measurement_create_experiment",
                    {
                        "spec": {
                            "title": f"Synthetic {label}",
                            "source": "ideal latent reference example",
                            "observation_contract_id": contract["contract_id"],
                            "observation_producer_id": producer["producer_id"],
                            "executable_method_id": method["method_id"],
                        },
                        "idempotency_key": f"{label}-experiment",
                    },
                )
                attempt = await call(
                    "measurement_record_attempt",
                    {
                        "experiment_id": experiment["experiment_id"],
                        "spec": {
                            "status": "completed",
                            "evaluator_kind": "simulator",
                            "fidelity": 1.0,
                            "executable_method_id": method["method_id"],
                        },
                        "idempotency_key": f"{label}-attempt",
                    },
                )
                prediction = await call(
                    "measurement_predict_retention",
                    {
                        "model_id": model["model_id"],
                        "method_id": method["method_id"],
                        "idempotency_key": f"{label}-prediction",
                    },
                )
                request = {
                    "producer_id": producer["producer_id"],
                    "prediction_id": prediction["prediction_id"],
                    "attempt_id": attempt["attempt_id"],
                    "expected_attempt_revision": 1,
                    "idempotency_key": f"{label}-generated-results",
                }
                generated = await call("measurement_record_gaussian_observation", request)
                evaluation_request = {
                    "contract_id": contract["contract_id"],
                    "attempt_id": attempt["attempt_id"],
                    "expected_attempt_revision": 1,
                    "spec": generated["evidence_spec"],
                    "idempotency_key": f"{label}-assessment",
                }
                assessment = await call("measurement_evaluate_observation", evaluation_request)
                return generated, assessment, request, evaluation_request

            complete, accepted, request, evaluation_request = await observe(7, "complete")
            snapshot = await call(
                "measurement_create_observation_snapshot",
                {
                    "contract_id": contract["contract_id"],
                    "observation_ids": [accepted["observation_id"]],
                    "idempotency_key": "accepted-snapshot",
                },
            )
            short, unknown, _, _ = await observe(3, "short")
            selected = next(
                item for item in complete["generated_results"] if item["metric"] == "resolution"
            )
            correction = await call(
                "measurement_record_results",
                {
                    "attempt_id": request["attempt_id"],
                    "expected_attempt_revision": 1,
                    "results": [
                        {
                            "metric": "resolution",
                            "analyte_reference": selected["analyte_reference"],
                            "status": "observed",
                            "unit": "1",
                            "value": 0,
                            "supersedes_result_id": selected["result_id"],
                            "explanation": "Explicit caller correction; not generated producer evidence",
                        }
                    ],
                    "idempotency_key": "caller-correction",
                },
            )
            replaced = copy.deepcopy(evaluation_request)
            replaced["idempotency_key"] = "reject-caller-replacement"
            for item in replaced["spec"]["analytes"]:
                if item["analyte_id"] == selected["analyte_reference"]:
                    item["resolution_result_id"] = correction["results"][0]["result_id"]
            rejection = await client.call_tool("measurement_evaluate_observation", replaced)
            if not rejection.isError:
                raise RuntimeError("Caller replacement was accepted as generated producer evidence")
            historical = await call(
                "measurement_get_gaussian_observation", {"run_id": complete["run_id"]}
            )
            retained = await call(
                "measurement_get_observation_snapshot", {"snapshot_id": snapshot["snapshot_id"]}
            )
            retry = await call("measurement_record_gaussian_observation", request)
            if historical != complete or retry != complete or retained != snapshot:
                raise RuntimeError(
                    "A later result correction rewrote generated evidence or history"
                )
            return {
                "campaign_protocol_profile": protocol["spec"]["profile"],
                "campaign_protocol_roundtrip": True,
                "profile": complete["observation"]["profile"],
                "resolution_definition": complete["observation"]["resolution_definition"],
                "fwhm_per_sigma": complete["observation"]["fwhm_per_sigma"],
                "complete_analytical_acceptance": accepted["observation"]["assessment"][
                    "analytical_acceptance"
                ],
                "complete_runtime_preference": accepted["observation"]["assessment"][
                    "runtime_preference_satisfied"
                ],
                "short_analytical_acceptance": unknown["observation"]["assessment"][
                    "analytical_acceptance"
                ],
                "short_censored_analytes": sum(
                    row["retention_status"] == "not_eluted"
                    for row in short["observation"]["analytes"]
                ),
                "short_has_no_fabricated_event_times": all(
                    row["retention_time_min"] is None
                    for row in short["observation"]["analytes"]
                    if row["retention_status"] == "not_eluted"
                ),
                "caller_replacement_rejected": True,
                "original_run_and_snapshot_unchanged": True,
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_sha256": snapshot["sha256"],
                "observation_origin": complete["evidence_spec"]["observation_origin"],
                "fresh_optimizer_run": False,
            }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="measure-gaussian-example-") as directory:
        print(json.dumps(asyncio.run(run(directory)), indent=2))
