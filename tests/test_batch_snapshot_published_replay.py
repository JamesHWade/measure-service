"""Reproduce fixed archived results with the installed, currently configured R backend."""

import asyncio
import hashlib
import json
import math
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

REFERENCE = Path(__file__).parents[1] / "docs/reference-data/chromatogram-snapshot-example"
RUNTIME_FIELDS = {"packages", "installed_r_code_md5", "r_version"}
CURRENT_BACKEND_REPLAY_POLICY = "current-backend-replay/2"
PROCESSED_SIGNAL_ABS_TOLERANCE = 1e-9
REPLAY_METRIC_ABS_TOLERANCE = 1e-10
REPLAY_METRIC_REL_TOLERANCE = 1e-8


def _assert_exact(expected, observed):
    """Compare JSON structure and scalar types without numeric coercion."""
    assert type(expected) is type(observed)
    if isinstance(expected, dict):
        assert expected.keys() == observed.keys()
        for key in expected:
            _assert_exact(expected[key], observed[key])
    elif isinstance(expected, list):
        assert len(expected) == len(observed)
        for left, right in zip(expected, observed):
            _assert_exact(left, right)
    else:
        assert expected == observed


def _assert_numeric_close(expected, observed, *, abs_tolerance, rel_tolerance):
    if expected is None or observed is None:
        _assert_exact(expected, observed)
        return
    assert type(expected) is type(observed) and type(expected) in (int, float)
    assert math.isfinite(expected) and math.isfinite(observed)
    assert observed == pytest.approx(expected, abs=abs_tolerance, rel=rel_tolerance)


def _assert_point_compatible(expected, observed):
    assert type(expected) is dict and type(observed) is dict
    assert expected.keys() == observed.keys()
    for field in ("sample_index", "axis", "processed_axis", "raw_signal"):
        _assert_exact(expected[field], observed[field])
    _assert_numeric_close(
        expected["processed_signal"],
        observed["processed_signal"],
        abs_tolerance=PROCESSED_SIGNAL_ABS_TOLERANCE,
        rel_tolerance=REPLAY_METRIC_REL_TOLERANCE,
    )


def _assert_peak_compatible(expected, observed):
    assert type(expected) is dict and type(observed) is dict
    assert expected.keys() == observed.keys()
    metric_fields = {"height", "area", "prominence"}
    for field in expected:
        if field in metric_fields:
            _assert_numeric_close(
                expected[field],
                observed[field],
                abs_tolerance=REPLAY_METRIC_ABS_TOLERANCE,
                rel_tolerance=REPLAY_METRIC_REL_TOLERANCE,
            )
        else:
            _assert_exact(expected[field], observed[field])


def _without_local_analysis_identity(evidence):
    normalized = {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    normalized["analysis"] = {
        key: value
        for key, value in evidence["analysis"].items()
        if key not in {"analysis_id", "dataset_id"}
    }
    return normalized


def test_current_backend_replay_policy_changes_only_processed_signal_budget():
    assert CURRENT_BACKEND_REPLAY_POLICY == "current-backend-replay/2"
    point = {
        "sample_index": 0,
        "axis": 0.0,
        "processed_axis": 0.0,
        "raw_signal": 0.0,
        "processed_signal": 0.0,
    }
    _assert_point_compatible(point, {**point, "processed_signal": 2e-10})
    with pytest.raises(AssertionError):
        _assert_point_compatible(point, {**point, "processed_signal": 2e-9})
    for field in ("axis", "processed_axis", "raw_signal"):
        with pytest.raises(AssertionError):
            _assert_point_compatible(point, {**point, field: 2e-10})
    with pytest.raises(AssertionError):
        _assert_point_compatible(point, {**point, "sample_index": 0.0})
    with pytest.raises(AssertionError):
        _assert_point_compatible(point, {**point, "extra": "field"})

    peak = {
        "peak_id": 1,
        "location": 1.0,
        "height": 0.02,
        "left_base": 0.9,
        "right_base": 1.1,
        "area": 0.03,
        "prominence": 0.02,
        "area_basis": "observed_signal",
    }
    for field in ("location", "left_base", "right_base"):
        with pytest.raises(AssertionError):
            _assert_peak_compatible(peak, {**peak, field: peak[field] + 2e-10})
    with pytest.raises(AssertionError):
        _assert_peak_compatible(peak, {**peak, "peak_id": 1.0})
    for field in ("height", "area", "prominence"):
        _assert_peak_compatible(peak, {**peak, field: peak[field] + 1.5e-10})
        with pytest.raises(AssertionError):
            _assert_peak_compatible({**peak, field: 0.0}, {**peak, field: 2e-10})


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
                    _assert_exact(expected["trace"], observed["trace"])
                    _assert_exact(expected["analysis"]["method"], observed["analysis"]["method"])
                    _assert_exact(expected["analysis"]["kind"], observed["analysis"]["kind"])
                    _assert_exact(
                        expected["analysis"]["trace_id"], observed["analysis"]["trace_id"]
                    )
                    expected_meta = {
                        k: v for k, v in expected["result"].items() if k not in RUNTIME_FIELDS
                    }
                    observed_meta = {
                        k: v for k, v in observed["result"].items() if k not in RUNTIME_FIELDS
                    }
                    _assert_exact(expected_meta, observed_meta)
                    _assert_exact(expected["point_count"], observed["point_count"])
                    _assert_exact(expected["peak_count"], observed["peak_count"])
                    assert len(expected["points"]) == len(observed["points"]) == 501
                    for left, right in zip(expected["points"], observed["points"]):
                        _assert_point_compatible(left, right)
                    assert len(expected["peaks"]) == len(observed["peaks"])
                    for left, right in zip(expected["peaks"], observed["peaks"]):
                        _assert_peak_compatible(left, right)
                    compact = await call("measurement_get_analysis", {"analysis_id": identifier})
                    assert "import_origin" not in compact and "input_import_origin" in compact
                    retained = await call(
                        "measurement_get_analysis",
                        {"analysis_id": imported["analysis_map"][original["analysis_id"]]},
                    )
                    local_analysis_id = imported["analysis_map"][original["analysis_id"]]
                    local_dataset_id = imported["dataset_map"][original["dataset_id"]]
                    retained_evidence = await collect_chromatogram_evidence(
                        client, local_analysis_id
                    )
                    assert retained_evidence["analysis"]["analysis_id"] == local_analysis_id
                    assert retained_evidence["analysis"]["dataset_id"] == local_dataset_id
                    _assert_exact(
                        _without_local_analysis_identity(expected),
                        _without_local_analysis_identity(retained_evidence),
                    )
                    _assert_exact(retained["provenance"], original["provenance"])
                    _assert_exact(retained["method"], original["method"])
                    assert retained["import_origin"]["source_record_id"] == original["analysis_id"]
                    assert retained["import_origin"]["local_record_id"] == local_analysis_id
                    assert (
                        retained["input_import_origin"]["source_record_id"]
                        == original["dataset_id"]
                    )
                    compared.append(original["trace_id"])
                assert set(compared) == {analysis["trace_id"] for analysis in document["analyses"]}
                assert (
                    len(compared) == 2
                )  # Fresh QC/unknown results have no original success to compare.

    asyncio.run(exercise())
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
    assert document["batch"]["counts"]["pending"] == 2
