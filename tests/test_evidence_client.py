"""Client collection rejects changed, incomplete or reordered MCP evidence."""

import asyncio
import copy
import csv
import json
import math
import os
import sqlite3
import sys
from collections import OrderedDict
from types import SimpleNamespace

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from measure_service import analysis_evidence as evidence
from measure_service import server
from measure_service.evidence_client import collect_chromatogram_evidence
from measure_service.evidence_report import write_report


@pytest.fixture(autouse=True)
def isolated_capture_cache(monkeypatch):
    monkeypatch.setattr(server, "_EVIDENCE_SNAPSHOTS", OrderedDict())
    monkeypatch.setattr(server, "_EVIDENCE_SNAPSHOT_BYTES", 0)


def stored_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    trace = {
        "trace_id": "uv",
        "sample_id": "sample",
        "injection_id": "injection",
        "channel": "254 nm",
        "separation": "LC",
        "detector": "UV",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "axis": [0.0, 1.0, 2.0, 3.0, 4.0],
        "signal": [-1.0, 2.0, 5.0, 2.0, -1.0],
    }
    registered = server.register_dataset(
        "owner", server.Dataset.model_validate({"title": "Evidence specimen", "traces": [trace]})
    )
    dataset, digest = server.dataset_for("owner", registered["dataset_id"])
    result = {
        "kind": "chromatogram",
        "peak_count": 1,
        "peaks": [
            {
                "peak_id": 1,
                "location": 2.0,
                "height": 5.0,
                "left_base": 0.0,
                "right_base": 4.0,
                "prominence": 6.0,
                "area": 9.0,
            }
        ],
        "processed_trace": {"axis": trace["axis"], "signal": trace["signal"]},
        "warnings": ["Synthetic stored specimen; no R calculation was performed."],
        "notes": [],
    }
    record = server.save_analysis(
        "owner",
        registered["dataset_id"],
        "uv",
        "chromatogram",
        server.ChromatogramMethod().model_dump(mode="json"),
        result,
        digest,
    )
    return evidence.build_evidence(
        server.analysis_for("owner", record["analysis_id"]), dataset, digest
    )


class PagedClient:
    def __init__(self, bundle, mutate=None):
        self.bundle = bundle
        self.snapshot = evidence.capture_evidence(bundle) if bundle is not None else None
        self.mutate = mutate
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments.copy()))
        if name == "measurement_get_chromatogram_evidence":
            data = evidence.manifest_for(self.snapshot)
        else:
            data = evidence.page_for(
                self.snapshot, arguments["kind"], arguments["offset"], arguments["limit"]
            )
        data = copy.deepcopy(data)
        if self.mutate:
            self.mutate(name, data)
        return SimpleNamespace(isError=False, structuredContent=data)


def test_collects_every_exact_page(tmp_path, monkeypatch):
    bundle = stored_bundle(tmp_path, monkeypatch)
    client = PagedClient(bundle)
    actual = asyncio.run(
        collect_chromatogram_evidence(client, bundle["analysis"]["analysis_id"], page_size=2)
    )
    assert actual == bundle
    assert [(args.get("kind"), args.get("offset")) for _, args in client.calls] == [
        (None, None),
        ("trace", 0),
        ("trace", 2),
        ("trace", 4),
        ("peaks", 0),
    ]


@pytest.mark.parametrize(
    "change", ["digest", "offset", "count", "empty", "continuation", "reorder"]
)
def test_refuses_changed_or_incomplete_pages(tmp_path, monkeypatch, change):
    bundle = stored_bundle(tmp_path, monkeypatch)

    def mutate(name, page):
        if name != "measurement_read_chromatogram_evidence" or page["kind"] != "trace":
            return
        if change == "digest":
            page["evidence_sha256"] = "0" * 64
        elif change == "offset":
            page["offset"] += 1
        elif change == "count":
            page["count"] += 1
        elif change == "empty":
            page["data"] = []
        elif change == "continuation":
            page["next_offset"] = None
        else:
            page["data"].reverse()

    client = PagedClient(bundle, mutate)
    with pytest.raises(ValueError):
        asyncio.run(
            collect_chromatogram_evidence(client, bundle["analysis"]["analysis_id"], page_size=2)
        )


@pytest.mark.parametrize("page_size", [0, -1, 1001, True, 1.5])
def test_invalid_page_size_never_requests_data(page_size):
    client = PagedClient(None)
    with pytest.raises(ValueError, match="page_size"):
        asyncio.run(collect_chromatogram_evidence(client, "analysis", page_size=page_size))
    assert client.calls == []


def test_tool_error_is_not_treated_as_data():
    class FailedClient:
        async def call_tool(self, name, arguments):
            return SimpleNamespace(isError=True, structuredContent={"secret": "not evidence"})

    with pytest.raises(ValueError, match="successful structured"):
        asyncio.run(collect_chromatogram_evidence(FailedClient(), "missing"))


def test_public_tools_are_owned_and_read_existing_results(tmp_path, monkeypatch):
    bundle = stored_bundle(tmp_path, monkeypatch)
    analysis_id = bundle["analysis"]["analysis_id"]
    monkeypatch.setattr(server, "identity", lambda ctx: ctx)

    def must_not_reanalyze(*args, **kwargs):
        raise AssertionError("Retrieval must not invoke an R worker")

    monkeypatch.setattr(server, "run_worker", must_not_reanalyze)
    assert server.measurement_get_chromatogram_evidence(
        analysis_id, "owner"
    ) == evidence.manifest_for(evidence.capture_evidence(bundle))
    page = server.measurement_read_chromatogram_evidence(
        analysis_id, "trace", "owner", evidence_sha256=bundle["evidence_sha256"], offset=1, limit=2
    )
    assert page["data"] == bundle["points"][1:3]
    for name, args in (
        ("measurement_get_chromatogram_evidence", (analysis_id, "other-owner")),
        ("measurement_read_chromatogram_evidence", (analysis_id, "trace", "other-owner")),
        ("measurement_get_chromatogram_evidence", ("nonexistent", "owner")),
    ):
        with pytest.raises(ValueError, match="not found or is not accessible"):
            kwargs = {"evidence_sha256": bundle["evidence_sha256"]} if "read_" in name else {}
            getattr(server, name)(*args, **kwargs)
    record = server.analysis_for("owner", analysis_id)
    forged = server.save_analysis(
        "other-owner",
        record["dataset_id"],
        record["trace_id"],
        record["kind"],
        record["method"],
        record["result"],
        record["provenance"]["input_sha256"],
    )
    with pytest.raises(ValueError, match="Dataset was not found or is not accessible"):
        server.measurement_get_chromatogram_evidence(forged["analysis_id"], "other-owner")


def test_captured_pages_do_not_reread_or_revalidate_source(tmp_path, monkeypatch):
    bundle = stored_bundle(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "identity", lambda ctx: ctx)
    analysis_id = bundle["analysis"]["analysis_id"]
    manifest = server.measurement_get_chromatogram_evidence(analysis_id, "owner")

    def forbidden(*args, **kwargs):
        raise AssertionError("A captured page must not read or revalidate the whole source")

    monkeypatch.setattr(server, "_owned_chromatogram_evidence", forbidden)
    monkeypatch.setattr(evidence, "validate_evidence", forbidden)
    for index in range(bundle["point_count"]):
        page = server.measurement_read_chromatogram_evidence(
            analysis_id, "trace", "owner", manifest["evidence_sha256"], offset=index, limit=1
        )
        assert page["data"] == bundle["points"][index : index + 1]
    with pytest.raises(ValueError, match="not found or is not accessible"):
        server.measurement_read_chromatogram_evidence(
            analysis_id, "trace", "another-owner", manifest["evidence_sha256"]
        )


def test_capture_eviction_requires_an_explicit_new_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "identity", lambda ctx: ctx)
    monkeypatch.setattr(server, "MAX_EVIDENCE_SNAPSHOTS", 2)
    captures = []
    for _ in range(3):
        bundle = stored_bundle(tmp_path, monkeypatch)
        identifier = bundle["analysis"]["analysis_id"]
        manifest = server.measurement_get_chromatogram_evidence(identifier, "owner")
        captures.append((identifier, manifest))
    first_id, first_manifest = captures[0]
    with pytest.raises(ValueError, match="request a manifest"):
        server.measurement_read_chromatogram_evidence(
            first_id, "trace", "owner", first_manifest["evidence_sha256"]
        )
    assert len(server._EVIDENCE_SNAPSHOTS) <= 2
    assert server._EVIDENCE_SNAPSHOT_BYTES <= server.MAX_EVIDENCE_CACHE_BYTES
    restored = server.measurement_get_chromatogram_evidence(first_id, "owner")
    assert restored == first_manifest
    page = server.measurement_read_chromatogram_evidence(
        first_id, "trace", "owner", restored["evidence_sha256"], limit=1
    )
    assert len(page["data"]) == 1


def test_capture_cannot_cross_store_or_replaced_database(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "identity", lambda ctx: ctx)
    bundle = stored_bundle(tmp_path / "a", monkeypatch)
    identifier = bundle["analysis"]["analysis_id"]
    manifest = server.measurement_get_chromatogram_evidence(identifier, "owner")
    stored_bundle(tmp_path / "b", monkeypatch)
    with pytest.raises(ValueError, match="request a manifest"):
        server.measurement_read_chromatogram_evidence(
            identifier, "trace", "owner", manifest["evidence_sha256"]
        )
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path / "a"))
    database = tmp_path / "a" / "measure.sqlite3"
    replacement = tmp_path / "a" / "replacement.sqlite3"
    replacement.write_bytes(database.read_bytes())
    os.replace(replacement, database)
    with pytest.raises(ValueError, match="request a manifest"):
        server.measurement_read_chromatogram_evidence(
            identifier, "trace", "owner", manifest["evidence_sha256"]
        )
    assert server.measurement_get_chromatogram_evidence(identifier, "owner") == manifest


def test_database_replacement_during_capture_is_not_bound_to_new_store(tmp_path, monkeypatch):
    bundle = stored_bundle(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "identity", lambda ctx: ctx)
    identifier = bundle["analysis"]["analysis_id"]
    capture = evidence.capture_evidence

    def replace_during_capture(document):
        snapshot = capture(document)
        database = tmp_path / "measure.sqlite3"
        replacement = tmp_path / "replacement.sqlite3"
        with sqlite3.connect(replacement) as connection:
            connection.execute("CREATE TABLE replacement_marker (id INTEGER)")
        os.replace(replacement, database)
        return snapshot

    monkeypatch.setattr(evidence, "capture_evidence", replace_during_capture)
    with pytest.raises(ValueError, match="Database identity changed"):
        server.measurement_get_chromatogram_evidence(identifier, "owner")
    assert len(server._EVIDENCE_SNAPSHOTS) == 0


@pytest.mark.r_backend
def test_real_installed_stdio_evidence_report_survives_restart(tmp_path):
    environment = dict(
        os.environ,
        MEASURE_MCP_DATA_DIR=str(tmp_path / "store"),
        MEASURE_MCP_R=os.environ.get("MEASURE_MCP_TEST_R", "R"),
        MEASURE_MCP_IDENTITY="local",
    )
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "measure_service"], env=environment
    )

    async def exercise():
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                for name in (
                    "measurement_get_chromatogram_evidence",
                    "measurement_read_chromatogram_evidence",
                ):
                    assert tools[name].annotations.readOnlyHint is True
                    assert tools[name].annotations.idempotentHint is True
                    assert "output_dir" not in tools[name].inputSchema["properties"]

                async def call(name, arguments):
                    response = await client.call_tool(name, arguments)
                    assert not response.isError, response.content
                    return response.structuredContent

                axis = [index / 100 for index in range(501)]
                signal = [
                    0.15
                    + 0.03 * x
                    + 2 * math.exp(-0.5 * ((x - 1.5) / 0.17) ** 2)
                    + 1.4 * math.exp(-0.5 * ((x - 3.5) / 0.23) ** 2)
                    + 0.04 * math.sin(80 * x)
                    for x in axis
                ]
                dataset = await call(
                    "measurement_register_dataset",
                    {
                        "dataset": {
                            "title": "Constructed real-backend report specimen",
                            "traces": [
                                {
                                    "trace_id": "uv",
                                    "sample_id": "specimen",
                                    "injection_id": "inj",
                                    "channel": "UV 254 nm",
                                    "separation": "LC",
                                    "detector": "UV",
                                    "axis_unit": "min",
                                    "signal_unit": "mAU",
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
                        "dataset_id": dataset["dataset_id"],
                        "trace_id": "uv",
                        "method": {
                            "baseline": "als",
                            "smoothing": "gaussian",
                            "smoothing_sigma": 2,
                            "min_prominence": 0.3,
                        },
                    },
                )
                assert analysis["summary"]["peak_count"] == 2
                stored_peaks = await call(
                    "measurement_read_analysis_data",
                    {"analysis_id": analysis["analysis_id"], "kind": "peaks"},
                )
                bundle = await collect_chromatogram_evidence(
                    client, analysis["analysis_id"], page_size=97
                )
                assert bundle["point_count"] == len(axis)
                assert [p["raw_signal"] for p in bundle["points"]] == signal
                assert [p["axis"] for p in bundle["points"]] == axis
                assert any(p["raw_signal"] != p["processed_signal"] for p in bundle["points"])
                assert bundle["peak_count"] == 2
                assert bundle["peaks"] == stored_peaks["data"]
                assert all("prominence" not in p for p in bundle["peaks"])
                assert bundle["availability"]["calibration"] == "not_linked"
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                restored = await collect_chromatogram_evidence(
                    client, analysis["analysis_id"], page_size=128
                )
                assert restored == bundle
                response = await client.call_tool(
                    "measurement_get_chromatogram_evidence", {"analysis_id": "missing"}
                )
                assert response.isError
        return bundle

    bundle = asyncio.run(exercise())
    write_report(bundle, tmp_path / "report")
    exported = json.loads((tmp_path / "report/evidence.json").read_text())
    assert exported == bundle
    evidence.validate_evidence(exported)
    with (tmp_path / "report/trace.csv").open(newline="") as stream:
        trace_rows = list(csv.DictReader(stream))
    assert len(trace_rows) == 501
    assert all(row["axis_unit"] == "min" and row["signal_unit"] == "mAU" for row in trace_rows)
    with (tmp_path / "report/peaks.csv").open(newline="") as stream:
        peak_rows = list(csv.DictReader(stream))
    assert len(peak_rows) == 2
    assert [row["area_basis"] for row in peak_rows] == [
        peak.get("area_basis", "") for peak in bundle["peaks"]
    ]
    html = (tmp_path / "report/report.html").read_text()
    assert bundle["evidence_sha256"] in html
    assert "trace.csv" in html and "peaks.csv" in html and "evidence.json" in html
    assert (tmp_path / "report/chromatogram.svg").is_file()
