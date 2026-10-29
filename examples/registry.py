"""Run a complete registry workflow over stdio MCP without R."""

import asyncio
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
        MEASURE_MCP_R="/not-used-by-registry/R",
    )
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "measure_service"], env=env
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name: str, arguments: dict) -> dict:
                response = await client.call_tool(name, arguments)
                if response.isError or response.structuredContent is None:
                    raise RuntimeError(f"{name}: {response.content}")
                return response.structuredContent

            await call("measurement_registry_capabilities", {})
            design_request = {
                "spec": {
                    "title": "LC scouting",
                    "source": "manual",
                    "intended_conditions": [{"name": "flow", "value": 0.4, "unit": "mL/min"}],
                },
                "idempotency_key": "example-design-1",
            }
            design = await call("measurement_create_experiment", design_request)
            retry = await call("measurement_create_experiment", design_request)
            if retry != design:
                raise RuntimeError("Retry did not return the original experiment")
            attempt = await call(
                "measurement_record_attempt",
                {
                    "experiment_id": design["experiment_id"],
                    "spec": {
                        "status": "completed",
                        "actual_conditions": [{"name": "flow", "value": 0.395, "unit": "mL/min"}],
                        "evaluator_kind": "manual",
                    },
                    "idempotency_key": "example-attempt-1",
                },
            )
            saved = await call(
                "measurement_record_results",
                {
                    "attempt_id": attempt["attempt_id"],
                    "expected_attempt_revision": attempt["revision"],
                    "results": [
                        {
                            "metric": "blank_response",
                            "status": "observed",
                            "value": 0,
                            "unit": "mAU",
                        },
                        {
                            "metric": "impurity_concentration",
                            "status": "below_limit",
                            "upper_bound": 0.05,
                            "unit": "mg/L",
                            "explanation": "Caller reports a result below the reporting limit",
                        },
                    ],
                    "idempotency_key": "example-outcomes-1",
                },
            )
            retrieved = [
                await call("measurement_get_result", {"result_id": record["result_id"]})
                for record in saved["results"]
            ]
            outcomes = [record["outcome"] for record in retrieved]
            if outcomes[0]["value"] != 0 or outcomes[1]["status"] != "below_limit":
                raise RuntimeError("Outcome states changed during retrieval")
            return {
                "experiment_id": design["experiment_id"],
                "attempt_id": attempt["attempt_id"],
                "result_ids": [record["result_id"] for record in retrieved],
                "observed_zero": outcomes[0]["value"],
                "below_limit": outcomes[1]["upper_bound"],
            }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="measure-example-") as directory:
        print(json.dumps(asyncio.run(run(directory)), indent=2))
