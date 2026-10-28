"""Register and analyze a synthetic LC trace, then link an outcome to its analysis."""

import asyncio
import json
import math
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(data_dir: str) -> dict:
    env = dict(os.environ, MEASURE_MCP_DATA_DIR=data_dir, MEASURE_MCP_IDENTITY="local")
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

            caps = await call("measurement_capabilities", {})
            if not caps["techniques"]["generic_1d_trace"]:
                raise RuntimeError("Install the measure and recipes R backends first")
            axis = [i * 0.02 for i in range(101)]
            signal = [math.exp(-0.5 * ((x - 1) / 0.16) ** 2) for x in axis]
            text = "time,uv\n" + "".join(f"{x},{y}\n" for x, y in zip(axis, signal))
            dataset = await call(
                "measurement_register_csv",
                {
                    "title": "Synthetic LC injection",
                    "csv_text": text,
                    "axis_column": "time",
                    "signal_column": "uv",
                    "metadata": {
                        "trace_id": "uv-254",
                        "sample_id": "synthetic-1",
                        "injection_id": "injection-1",
                        "channel": "uv-254",
                        "separation": "LC",
                        "detector": "UV",
                        "axis_unit": "min",
                        "signal_unit": "mAU",
                    },
                },
            )
            link = {"dataset_id": dataset["dataset_id"], "trace_id": "uv-254"}
            analysis = await call(
                "measurement_analyze_chromatogram", {**link, "method": {"min_prominence": 0.1}}
            )
            if analysis["summary"]["peak_count"] != 1:
                raise RuntimeError("Expected one synthetic peak")
            design = await call(
                "measurement_create_experiment",
                {
                    "spec": {"title": "Synthetic pipeline check", "source": "example"},
                    "idempotency_key": "analysis-example-design",
                },
            )
            attempt = await call(
                "measurement_record_attempt",
                {
                    "experiment_id": design["experiment_id"],
                    "spec": {
                        "status": "completed",
                        "evaluator_kind": "simulator",
                        "evaluator_reference": "synthetic-gaussian-example",
                        "measurements": [link],
                    },
                    "idempotency_key": "analysis-example-attempt",
                },
            )
            saved = await call(
                "measurement_record_results",
                {
                    "attempt_id": attempt["attempt_id"],
                    "expected_attempt_revision": attempt["revision"],
                    "results": [
                        {
                            "metric": "peak_count",
                            "status": "observed",
                            "unit": "1",
                            "analysis_id": analysis["analysis_id"],
                            "summary_field": "peak_count",
                        }
                    ],
                    "idempotency_key": "analysis-example-result",
                },
            )
            outcome = saved["results"][0]
            if outcome["outcome"]["value"] != analysis["summary"]["peak_count"]:
                raise RuntimeError("Stored outcome does not match the analysis")
            return {
                "analysis_id": analysis["analysis_id"],
                "result_id": outcome["result_id"],
                "peak_count": outcome["outcome"]["value"],
                "warnings": analysis["warnings"],
                "input_sha256": analysis["provenance"]["input_sha256"],
            }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="measure-analysis-example-") as directory:
        print(json.dumps(asyncio.run(run(directory)), indent=2))
