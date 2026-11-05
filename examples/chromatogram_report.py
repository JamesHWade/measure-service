"""Write a verified report from stored evidence, or demonstrate one synthetic LC trace.

Run from an installed environment:
    python examples/chromatogram_report.py --output-dir /tmp/new-chromatogram-report

To export an existing local analysis instead, set MEASURE_MCP_DATA_DIR and pass
--analysis-id. Output paths belong to this local client, not the MCP server.
"""

import argparse
import asyncio
import json
import math
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from measure_service.evidence_client import collect_chromatogram_evidence
from measure_service.evidence_report import write_report


async def run(output_dir: Path, *, analysis_id: str | None = None, data_dir: str | None = None):
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(f"Report output directory already exists: {output_dir}")
    environment = dict(os.environ, MEASURE_MCP_IDENTITY="local")
    if data_dir is not None:
        environment["MEASURE_MCP_DATA_DIR"] = data_dir
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "measure_service"], env=environment
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            if analysis_id is None:
                axis = [index / 100 for index in range(501)]
                signal = [
                    0.15
                    + 0.03 * value
                    + 2.0 * math.exp(-0.5 * ((value - 1.5) / 0.17) ** 2)
                    + 1.4 * math.exp(-0.5 * ((value - 3.5) / 0.23) ** 2)
                    + 0.04 * math.sin(80 * value)
                    for value in axis
                ]

                async def call(name, arguments):
                    response = await client.call_tool(name, arguments)
                    if response.isError or response.structuredContent is None:
                        raise ValueError(f"{name} failed: {response.content}")
                    return response.structuredContent

                registered = await call(
                    "measurement_register_dataset",
                    {
                        "dataset": {
                            "title": "Constructed two-peak LC trace",
                            "traces": [
                                {
                                    "trace_id": "uv-254",
                                    "sample_id": "synthetic-example",
                                    "injection_id": "injection-1",
                                    "channel": "UV 254 nm",
                                    "separation": "LC",
                                    "detector": "UV",
                                    "axis_unit": "min",
                                    "signal_unit": "mAU",
                                    "sample_type": "reference",
                                    "axis": axis,
                                    "signal": signal,
                                }
                            ],
                        }
                    },
                )
                analysis = await call(
                    "measurement_analyze_chromatogram",
                    {
                        "dataset_id": registered["dataset_id"],
                        "trace_id": "uv-254",
                        "method": {
                            "baseline": "als",
                            "baseline_lambda": 1e6,
                            "baseline_p": 0.01,
                            "smoothing": "gaussian",
                            "smoothing_sigma": 2,
                            "min_prominence": 0.3,
                            "integration_baseline": "local",
                        },
                    },
                )
                analysis_id = analysis["analysis_id"]
            bundle = await collect_chromatogram_evidence(client, analysis_id, page_size=128)
    published = write_report(bundle, output_dir)
    return {
        "profile": "measure-chromatogram-report-example/1",
        "analysis_id": analysis_id,
        "evidence_sha256": bundle["evidence_sha256"],
        "point_count": bundle["point_count"],
        "peak_count": bundle["peak_count"],
        "availability": bundle["availability"],
        "publication": published,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--analysis-id", help="Read this existing local analysis without reanalysis"
    )
    args = parser.parse_args()
    if args.analysis_id:
        result = asyncio.run(run(args.output_dir, analysis_id=args.analysis_id))
    else:
        with tempfile.TemporaryDirectory(prefix="measure-report-example-") as data_dir:
            result = asyncio.run(run(args.output_dir, data_dir=data_dir))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
