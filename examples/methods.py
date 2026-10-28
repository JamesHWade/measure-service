"""Validate and retain declared chromatography methods over MCP without R."""

import asyncio
import copy
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(data_dir: str) -> dict:
    env = dict(
        os.environ,
        MEASURE_MCP_DATA_DIR=data_dir,
        MEASURE_MCP_IDENTITY="local",
        MEASURE_MCP_R="/not-used-by-method-declarations/R",
    )
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "measure_service"], env=env
    )
    declaration = {
        "schema_version": "1.0",
        "time_unit": "s",
        "flow_unit": "uL/min",
        "solvents": ["A", "B"],
        "program": {
            "kind": "gradient_absolute",
            "points": [
                {"time": 0, "state": {"flow": 1000, "solvent_fractions": {"A": 0.2, "B": 0.8}}},
                {"time": 120, "state": {"flow": 3000, "solvent_fractions": {"A": 0.8, "B": 0.2}}},
            ],
        },
    }
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name: str, arguments: dict) -> dict:
                response = await client.call_tool(name, arguments)
                if response.isError or response.structuredContent is None:
                    raise RuntimeError(f"{name}: {response.content}")
                return response.structuredContent

            evaluation = await call("measurement_validate_method", {"declaration": declaration})
            if not evaluation["feasible"]:
                raise RuntimeError("Example method should be feasible")
            request = {
                "title": "Intended gradient",
                "declaration": declaration,
                "idempotency_key": "example-method-intended",
            }
            intended = await call("measurement_register_method", request)
            if await call("measurement_register_method", request) != intended:
                raise RuntimeError("Method retry changed its immutable identity")
            reported_declaration = copy.deepcopy(declaration)
            for point in reported_declaration["program"]["points"]:
                point["state"]["flow"] *= 0.9
            reported = await call(
                "measurement_register_method",
                {
                    "title": "Separately reported simulator gradient",
                    "declaration": reported_declaration,
                    "idempotency_key": "example-method-reported",
                },
            )
            experiment = await call(
                "measurement_create_experiment",
                {
                    "spec": {
                        "title": "Gradient declaration example",
                        "source": "manual",
                        "executable_method_id": intended["method_id"],
                    },
                    "idempotency_key": "example-method-experiment",
                },
            )
            attempt = await call(
                "measurement_record_attempt",
                {
                    "experiment_id": experiment["experiment_id"],
                    "spec": {
                        "status": "completed",
                        "evaluator_kind": "simulator",
                        "executable_method_id": reported["method_id"],
                    },
                    "idempotency_key": "example-method-attempt",
                },
            )
            retained = await call("measurement_get_method", {"method_id": intended["method_id"]})
            if retained != intended:
                raise RuntimeError("Stored method changed during retrieval")
            return {
                "intended_method_id": experiment["executable_method"]["method_id"],
                "reported_method_id": attempt["executable_method"]["method_id"],
                "acquisition_duration_min": evaluation["canonical"]["acquisition_duration_min"],
                "analytical_solvent_use_ml": evaluation["canonical"]["analytical_solvent_use_ml"],
                "input_sha256": retained["input_sha256"],
            }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="measure-method-example-") as directory:
        print(json.dumps(asyncio.run(run(directory)), indent=2))
