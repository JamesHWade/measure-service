"""Evaluate an explicit ideal-retention model through the installed MCP service."""

import asyncio
import json
import math
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

            model = await call(
                "measurement_register_retention_model",
                {
                    "title": "Synthetic isocratic retention reference",
                    "spec": {
                        "gradient_coordinate": "column_inlet_at_analyte_entry",
                        "analytes": [
                            {"analyte_id": "A", "model": "linear", "ln_k0": math.log(3), "S1": 0},
                            {
                                "analyte_id": "B",
                                "model": "linear",
                                "ln_k0": math.log(1000),
                                "S1": 0,
                            },
                        ],
                        "column_length_m": 0.1,
                        "effective_area_m2": 2.5e-6,
                        "post_column_volume_ml": 0.1,
                        "strong_solvent": "B",
                        "parameter_origin": "synthetic",
                        "parameter_reference": "Independent isocratic k=3 and k=1000 limiting cases",
                    },
                    "idempotency_key": "retention-model",
                },
            )
            if (
                await call("measurement_get_retention_model", {"model_id": model["model_id"]})
                != model
            ):
                raise RuntimeError("Immutable model changed during retrieval")
            method = await call(
                "measurement_register_method",
                {
                    "title": "Declared column-inlet reference program",
                    "declaration": {
                        "time_unit": "min",
                        "flow_unit": "mL/min",
                        "solvents": ["A", "B"],
                        "program": {
                            "kind": "isocratic",
                            "acquisition_duration": 10,
                            "state": {"flow": 0.5, "solvent_fractions": {"A": 0.8, "B": 0.2}},
                        },
                    },
                    "idempotency_key": "retention-method",
                },
            )
            request = {
                "model_id": model["model_id"],
                "method_id": method["method_id"],
                "idempotency_key": "retention-prediction",
            }
            prediction = await call("measurement_predict_retention", request)
            retry = await call("measurement_predict_retention", request)
            retrieved = await call(
                "measurement_get_retention_prediction",
                {"prediction_id": prediction["prediction_id"]},
            )
            if retry != prediction or retrieved != prediction:
                raise RuntimeError("Prediction or retry identity changed")
            rows = {
                item["analyte_id"]: item["result"] for item in prediction["prediction"]["analytes"]
            }
            return {
                "profile": prediction["prediction"]["profile"],
                "eluted_retention_time_min": rows["A"]["retention_time_min"],
                "censored_status": rows["B"]["status"],
                "censored_retention_time_min": rows["B"]["retention_time_min"],
                "right_censor_bound_min": rows["B"]["right_censor_bound_min"],
                "geometry_dead_time_min": rows["A"]["dead_time_min"],
                "post_column_transit_min": rows["A"]["post_column_transit_min"],
                "prediction_sha256": prediction["output_sha256"],
                "qualification_scope": prediction["prediction"]["qualification_scope"],
            }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="measure-retention-example-") as directory:
        print(json.dumps(asyncio.run(run(directory)), indent=2))
