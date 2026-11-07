"""Reproduce fixed archived results with the installed, currently configured R backend."""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from measure_service import analysis_evidence
from measure_service.batch_bundle import read_bundle
from measure_service.batch_restore import restore_bundle
from measure_service.batch_snapshot import decode_snapshot
from measure_service.evidence_client import collect_chromatogram_evidence
from measure_service.storage import canonical

REFERENCE = Path(__file__).parents[1] / "docs/reference-data/chromatogram-snapshot-example"
RUNTIME_FIELDS = {"packages", "installed_r_code_md5", "r_version"}


def _scientific_value(expected, observed):
    if isinstance(expected, dict):
        assert isinstance(observed, dict) and expected.keys() == observed.keys()
        for key in expected:
            _scientific_value(expected[key], observed[key])
    elif isinstance(expected, list):
        assert isinstance(observed, list) and len(expected) == len(observed)
        for left, right in zip(expected, observed):
            _scientific_value(left, right)
    elif type(expected) in (int, float):
        assert type(observed) in (int, float)
        assert observed == pytest.approx(expected, abs=1e-10, rel=1e-8)
    else:
        assert type(expected) is type(observed) and expected == observed


@pytest.mark.r_backend
def test_fixed_archive_reproduces_two_original_successes_with_current_r(tmp_path):
    archive = REFERENCE / "output/source-self-contained.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    publication = json.loads((REFERENCE / "publication.json").read_text())
    assert digest == publication["files"]["output/source-self-contained.zip"]["sha256"]
    document = decode_snapshot(read_bundle(archive))
    assert len(document["analyses"]) == 2
    assert document["batch"]["counts"]["pending"] == 2
    destination = tmp_path / "new-store"
    imported = restore_bundle(archive, destination)
    assert (destination / "source-snapshot.zip").read_bytes() == archive.read_bytes()
    datasets = {entry["dataset_id"]: entry for entry in document["datasets"]}

    async def exercise():
        env = dict(
            os.environ,
            MEASURE_MCP_DATA_DIR=str(destination),
            MEASURE_MCP_IDENTITY="local",
            MEASURE_MCP_R=os.environ.get("MEASURE_MCP_TEST_R", "R"),
        )
        env.pop("PYTHONPATH", None)
        server = StdioServerParameters(
            command=sys.executable, args=["-m", "measure_service"], env=env
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()

                async def call(name, arguments):
                    result = await client.call_tool(name, arguments)
                    assert not result.isError, result.content
                    assert isinstance(result.structuredContent, dict)
                    return result.structuredContent

                for ordinal in range(2):
                    current = await call(
                        "measurement_get_chromatogram_batch", {"batch_id": imported["new_batch_id"]}
                    )
                    run = await call(
                        "measurement_run_chromatogram_batch",
                        {
                            "batch_id": imported["new_batch_id"],
                            "expected_revision": current["revision"],
                            "max_items": 2,
                            "idempotency_key": f"fixed-archive-{ordinal}",
                        },
                    )
                    assert len(run["items"]) == 2
                    assert all(item["state"] == "completed" for item in run["items"])
                page = await call(
                    "measurement_read_chromatogram_batch_items",
                    {"batch_id": imported["new_batch_id"], "offset": 0, "limit": 20},
                )
                assert len(page["items"]) == 4
                fresh = {item["trace_id"]: item for item in page["items"]}
                compared = []
                for original in document["analyses"]:
                    data = datasets[original["dataset_id"]]
                    record = {
                        key: original[key]
                        for key in (
                            "analysis_id",
                            "dataset_id",
                            "trace_id",
                            "kind",
                            "method",
                            "result",
                            "provenance",
                            "created_at",
                        )
                    }
                    expected = analysis_evidence.build_evidence(
                        record, data["payload"], data["input_sha256"]
                    )
                    identifier = fresh[original["trace_id"]]["analysis_id"]
                    observed = await collect_chromatogram_evidence(client, identifier)
                    assert canonical(expected["trace"]) == canonical(observed["trace"])
                    assert canonical(expected["analysis"]["method"]) == canonical(
                        observed["analysis"]["method"]
                    )
                    expected_meta = {
                        k: v for k, v in expected["result"].items() if k not in RUNTIME_FIELDS
                    }
                    observed_meta = {
                        k: v for k, v in observed["result"].items() if k not in RUNTIME_FIELDS
                    }
                    _scientific_value(expected_meta, observed_meta)
                    assert len(expected["points"]) == len(observed["points"]) == 501
                    for left, right in zip(expected["points"], observed["points"]):
                        assert (
                            type(right["sample_index"]) is int
                            and left["sample_index"] == right["sample_index"]
                        )
                        for field in ("axis", "processed_axis", "raw_signal", "processed_signal"):
                            _scientific_value(left[field], right[field])
                    assert len(expected["peaks"]) == len(observed["peaks"])
                    for left, right in zip(expected["peaks"], observed["peaks"]):
                        for field in ("peak_id", "area_basis"):
                            assert (
                                type(left[field]) is type(right[field])
                                and left[field] == right[field]
                            )
                        for field in (
                            "location",
                            "height",
                            "left_base",
                            "right_base",
                            "area",
                            "prominence",
                        ):
                            _scientific_value(left.get(field), right.get(field))
                    compact = await call("measurement_get_analysis", {"analysis_id": identifier})
                    assert "import_origin" not in compact and "input_import_origin" in compact
                    retained = await call(
                        "measurement_get_analysis",
                        {"analysis_id": imported["analysis_map"][original["analysis_id"]]},
                    )
                    assert retained["provenance"] == original["provenance"]
                    assert retained["import_origin"]["source_record_id"] == original["analysis_id"]
                    compared.append(original["trace_id"])
                assert set(compared) == {analysis["trace_id"] for analysis in document["analyses"]}
                assert (
                    len(compared) == 2
                )  # Fresh QC/unknown results have no original success to compare.

    asyncio.run(exercise())
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
    assert document["batch"]["counts"]["pending"] == 2
