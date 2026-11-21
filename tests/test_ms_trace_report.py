"""Portable LC-MS trace report and bounded evidence collection contracts."""

import asyncio
import csv
import json
from types import SimpleNamespace

import pytest

from measure_service.ms_trace_report import (
    collect_xic_evidence,
    export_xic_report,
    read_xic_evidence,
    render_xic_report,
    write_xic_report,
)
from measure_service.storage import sha256


def synthetic_bundle():
    manifest = {
        "profile": "measure-ms-trace/1",
        "trace_id": "trace-lcms-1",
        "acquisition_id": "acq-1",
        "input_sha256": "b" * 64,
        "created_at": "2026-11-20T20:00:00+00:00",
        "processing": {
            "operation": "xic",
            "aggregation": "centroid_sum",
            "stream_id": "ms1",
            "polarity": "positive",
            "ms_level": 1,
            "mz_range": [100.0, 100.2],
            "time_unit": "min",
            "value_unit": "counts",
            "warnings": ["The producer retained one source warning."],
        },
        "provenance": {"source_sha256": "c" * 64, "backend": "synthetic"},
        "warnings": ["The producer retained one source warning."],
        "point_count": 4,
        "source_row_count": 3,
    }
    manifest["points"] = [
        {
            "scan_id": "scan-0",
            "scan_index": 1,
            "source_scan_id": "raw-0",
            "retention_time": 0.0,
            "value": 10.0,
            "status": "calculated",
            "selected_points": 1,
            "saturation_unknown": False,
        },
        {
            "scan_id": "scan-1",
            "scan_index": 2,
            "source_scan_id": "raw-1",
            "retention_time": 1.0,
            "value": None,
            "status": "missing_intensity",
            "selected_points": 0,
            "saturation_unknown": False,
        },
        {
            "scan_id": "scan-2",
            "scan_index": 3,
            "source_scan_id": "raw-2",
            "retention_time": 2.0,
            "value": 25.0,
            "status": "calculated",
            "selected_points": 2,
            "saturation_unknown": False,
        },
        {
            "scan_id": "scan-3",
            "scan_index": 4,
            "source_scan_id": "raw-3",
            "retention_time": 3.0,
            "value": 12.0,
            "status": "calculated",
            "selected_points": 1,
            "saturation_unknown": True,
        },
    ]
    manifest["source_rows"] = [
        {
            "scan_id": "scan-0",
            "point_index": 1,
            "source_row": 10,
            "mz": 100.1,
            "intensity": 10.0,
            "saturated": False,
        },
        {
            "scan_id": "scan-2",
            "point_index": 1,
            "source_row": 20,
            "mz": 100.0,
            "intensity": 15.0,
            "saturated": False,
        },
        {
            "scan_id": "scan-2",
            "point_index": 2,
            "source_row": 21,
            "mz": 100.2,
            "intensity": 10.0,
            "saturated": False,
        },
    ]
    manifest["evidence_sha256"] = sha256(manifest)
    return manifest


def test_render_retains_digest_warnings_links_and_safe_markup():
    artifacts = render_xic_report(synthetic_bundle())
    assert set(artifacts) == {
        "report.html",
        "xic.svg",
        "evidence.json",
        "xic.csv",
        "source-rows.csv",
    }
    html = artifacts["report.html"].decode()
    svg = artifacts["xic.svg"].decode()
    assert "The producer retained one source warning." in html
    assert 'href="#scan-2"' in html
    assert "#source-row-1" in html
    assert "<script" not in html.lower()
    assert 'href="http' not in html
    assert "leaves a gap" in html
    assert "gaps" in svg
    assert svg.count('class="trace"') == 2
    assert "Retention time (min)" in svg
    assert "Intensity (counts)" in svg


def test_write_and_read_report_are_replayable_without_client(tmp_path):
    output = tmp_path / "report"
    files = write_xic_report(synthetic_bundle(), output)
    assert set(files) == {
        "report.html",
        "xic.svg",
        "evidence.json",
        "xic.csv",
        "source-rows.csv",
    }
    evidence = read_xic_evidence(output)
    assert evidence["evidence_sha256"] == synthetic_bundle()["evidence_sha256"]
    with output.joinpath("xic.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[1]["value"] == ""
    with pytest.raises(FileExistsError):
        write_xic_report(evidence, output)


class PagedClient:
    def __init__(self, bundle, *, mutate=None):
        self.bundle = bundle
        self.calls = []
        self.mutate = mutate

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "measurement_get_ms_trace":
            manifest = {
                key: value
                for key, value in self.bundle.items()
                if key not in {"points", "source_rows"}
            }
            response = SimpleNamespace(isError=False, structuredContent=manifest)
        else:
            rows = self.bundle[arguments["kind"]]
            offset = arguments["offset"]
            limit = arguments["limit"]
            data = rows[offset : offset + limit]
            response = SimpleNamespace(
                isError=False,
                structuredContent={
                    "profile": self.bundle["profile"],
                    "trace_id": self.bundle["trace_id"],
                    "evidence_sha256": self.bundle["evidence_sha256"],
                    "kind": arguments["kind"],
                    "offset": offset,
                    "count": len(data),
                    "next_offset": offset + len(data) if offset + len(data) < len(rows) else None,
                    "data": data,
                },
            )
        if self.mutate:
            self.mutate(name, response.structuredContent)
        return response


def test_collects_bounded_points_and_source_rows_by_digest():
    client = PagedClient(synthetic_bundle())
    bundle = asyncio.run(collect_xic_evidence(client, "trace-lcms-1", page_size=2))
    assert bundle["evidence_sha256"] == synthetic_bundle()["evidence_sha256"]
    assert [row["scan_id"] for row in bundle["points"]] == ["scan-0", "scan-1", "scan-2", "scan-3"]
    assert [row["source_row"] for row in bundle["source_rows"]] == [10, 20, 21]
    assert [name for name, _ in client.calls] == [
        "measurement_get_ms_trace",
        "measurement_read_ms_trace",
        "measurement_read_ms_trace",
        "measurement_read_ms_trace",
        "measurement_read_ms_trace",
    ]


def test_export_collects_and_writes_with_one_trace_id(tmp_path):
    result = asyncio.run(
        export_xic_report(PagedClient(synthetic_bundle()), "trace-lcms-1", tmp_path / "inline")
    )
    assert result["report.html"]["path"].endswith("/inline/report.html")


@pytest.mark.parametrize("page_size", [0, -1, 1_001, True, 1.5])
def test_invalid_page_size_is_rejected_before_mcp(page_size):
    class ForbiddenClient:
        async def call_tool(self, *_args, **_kwargs):
            raise AssertionError("invalid page size must not call MCP")

    with pytest.raises(ValueError, match="page_size"):
        asyncio.run(collect_xic_evidence(ForbiddenClient(), "trace", page_size=page_size))


def test_digest_and_page_identity_are_checked():
    bundle = synthetic_bundle()
    bundle["processing"]["display_name"] = '<img src=x onerror="alert(1)">'
    bundle["evidence_sha256"] = sha256(
        {key: value for key, value in bundle.items() if key != "evidence_sha256"}
    )
    artifacts = render_xic_report(bundle)
    assert "&lt;img src=x onerror=" in artifacts["report.html"].decode()
    assert "alert(1)" in artifacts["report.html"].decode()

    bad = synthetic_bundle()
    bad["points"][0]["value"] = 99.0
    with pytest.raises(ValueError, match="digest"):
        render_xic_report(bad)

    def mutate(name, page):
        if name == "measurement_read_ms_trace":
            page["evidence_sha256"] = "0" * 64

    with pytest.raises(ValueError):
        asyncio.run(
            collect_xic_evidence(PagedClient(synthetic_bundle(), mutate=mutate), "trace-lcms-1")
        )


def test_evidence_json_remains_valid_json():
    payload = json.loads(render_xic_report(synthetic_bundle())["evidence.json"])
    assert payload["processing"]["operation"] == "xic"


def test_unknown_response_unit_and_source_row_are_not_invented():
    bundle = synthetic_bundle()
    bundle["processing"]["value_unit"] = None
    bundle["source_rows"][0]["source_row"] = None
    bundle["evidence_sha256"] = sha256({k: v for k, v in bundle.items() if k != "evidence_sha256"})
    files = render_xic_report(bundle)
    assert "Intensity (unit unavailable)" in files["xic.svg"].decode()
    assert "centroid 1 (source row unknown)" in files["report.html"].decode()
    assert json.loads(files["evidence.json"])["processing"]["value_unit"] is None


def test_default_page_accepts_a_thousand_scan_rows():
    bundle = synthetic_bundle()
    first = bundle["points"][0]
    bundle["points"] = [
        {
            **first,
            "scan_id": f"scan-{i}",
            "scan_index": i + 1,
            "source_scan_id": None,
            "retention_time": float(i),
            "selected_points": 0,
            "status": "empty_window",
            "value": 0,
        }
        for i in range(1000)
    ]
    bundle["point_count"] = len(bundle["points"])
    bundle["source_rows"] = []
    bundle["source_row_count"] = 0
    bundle["evidence_sha256"] = sha256({k: v for k, v in bundle.items() if k != "evidence_sha256"})
    assert asyncio.run(collect_xic_evidence(PagedClient(bundle), bundle["trace_id"])) == bundle


@pytest.mark.parametrize("bounds", [["unknown", 101], [0, 101], [102, 101], [100], None])
def test_offline_report_checks_mz_bounds_before_rendering(bounds):
    bundle = synthetic_bundle()
    bundle["processing"]["mz_range"] = bounds
    bundle["evidence_sha256"] = sha256({k: v for k, v in bundle.items() if k != "evidence_sha256"})
    with pytest.raises(ValueError, match="m/z"):
        render_xic_report(bundle)
