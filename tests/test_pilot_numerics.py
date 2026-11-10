"""Bounded synthetic numerical qualification over the public MCP client surface."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from measure_service.pilot_numerics import PROFILE, PilotCheckError, qualify_numerics

_READ_TOOLS = {
    "measurement_capabilities",
    "measurement_inspect_dataset",
    "measurement_read_source",
    "measurement_get_analysis",
    "measurement_read_analysis_data",
}
_CHECK_KEYS = {
    "numerical_capabilities",
    "source_text_retained",
    "source_digest_retained",
    "analysis_provenance_bound",
    "distribution_log10_mw_axis",
    "chromatogram_one_peak",
    "chromatogram_center",
    "chromatogram_area",
    "sec_mn",
    "sec_mw",
    "sec_dispersity",
    "distribution_integral",
    "distribution_cumulative_endpoints",
}


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _canonical_sha256(value: dict[str, Any]) -> str:
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _reference_distribution_axis(text: str) -> list[float]:
    rows = list(csv.DictReader(text.splitlines()))
    log_masses = [6 - 0.4 * float(row["time"]) + 0.03 * float(row["time"]) ** 2 for row in rows]
    minimum, maximum = min(log_masses), max(log_masses)
    return [minimum + index * (maximum - minimum) / 100 for index in range(101)]


def _moments(text: str) -> tuple[float, float]:
    rows = list(csv.DictReader(text.splitlines()))
    xs = [float(row["time"]) for row in rows]
    ys = [float(row["response"]) for row in rows]
    widths = [0.0] * len(xs)
    for index, (left, right) in enumerate(zip(xs, xs[1:])):
        widths[index] += (right - left) / 2
        widths[index + 1] += (right - left) / 2
    weights = [signal * width for signal, width in zip(ys, widths, strict=True)]
    masses = [10 ** (6 - 0.4 * x + 0.03 * x * x) for x in xs]
    total = sum(weights)
    mn = total / sum(w / m for w, m in zip(weights, masses, strict=True))
    mw = sum(w * m for w, m in zip(weights, masses, strict=True)) / total
    return mn, mw


def _tool_result(value: dict[str, Any] | None, *, is_error: bool = False):
    return SimpleNamespace(isError=is_error, structuredContent=value)


class FakeClient:
    """A transport fake implementing the small public tool contract under test."""

    def __init__(self, corruption: str | None = None):
        self.corruption = corruption
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.source_text = ""
        self.dataset_id = "dataset-pilot"
        self.input_sha256 = hashlib.sha256(b"canonical synthetic dataset").hexdigest()
        self.source_sha256 = ""
        self.analysis_ids: dict[str, str] = {}
        self.backend = {
            "r_version": "4.5.1",
            "packages": {
                "measure": "0.1.0.9000",
                "measure.sec": "0.1.0.9000",
                "recipes": "1.3.1",
            },
            "installed_r_code_md5": {"measure": "a" * 32, "measure.sec": "b" * 32},
            "sec_calibrated_curve_support": True,
            "warnings": ["fake backend warning retained in private evidence"],
        }

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        self.calls.append((name, arguments))
        if name == "measurement_capabilities":
            capabilities = {
                "techniques": {"generic_1d_trace": True, "relative_sec": True},
                "backend": self.backend,
            }
            if self.corruption == "missing_capability":
                capabilities["techniques"]["relative_sec"] = False
            if self.corruption == "capability_error":
                return _tool_result(None, is_error=True)
            if self.corruption == "missing_structured":
                return _tool_result(None)
            if self.corruption == "bad_error_flag":
                return SimpleNamespace(isError=None, structuredContent=capabilities)
            if self.corruption == "missing_fingerprint":
                self.backend["installed_r_code_md5"]["measure"] = None
            return _tool_result(capabilities)

        if name == "measurement_register_csv":
            self.source_text = arguments["csv_text"]
            self.source_sha256 = _sha256(self.source_text)
            digest = self.source_sha256
            if self.corruption == "registration_digest":
                digest = "0" * 64
            return _tool_result(
                {
                    "dataset_id": self.dataset_id,
                    "input_sha256": self.input_sha256,
                    "source_sha256": digest,
                    "point_count": 101,
                }
            )

        if name == "measurement_inspect_dataset":
            registration = next(
                args
                for tool, args in reversed(self.calls[:-1])
                if tool == "measurement_register_csv"
            )
            metadata = registration["metadata"]
            trace_info = metadata | {
                "point_count": 101,
                "axis_range": [1, 3],
                "signal_range": [0.0, 1.0],
            }
            digest = self.source_sha256
            if self.corruption == "inspection_digest":
                digest = "f" * 64
            return _tool_result(
                {
                    "dataset_id": self.dataset_id,
                    "input_sha256": self.input_sha256,
                    "title": "Synthetic pilot SEC trace",
                    "traces": [trace_info],
                    "source": {
                        "kind": "delimited_text",
                        "source_sha256": digest,
                        "mapping": {
                            "delimiter": ",",
                            "axis_column": "time",
                            "signal_columns": [
                                {"column": "response", "trace_id": metadata["trace_id"]}
                            ],
                        },
                    },
                }
            )

        if name == "measurement_read_source":
            offset, limit = arguments["offset"], arguments["limit"]
            part = self.source_text[offset : offset + limit]
            digest = self.source_sha256
            if self.corruption == "source_page_digest":
                digest = "0" * 64
            if self.corruption == "source_page_text" and offset == 0:
                part = "X" + part[1:]
            return _tool_result(
                {
                    "dataset_id": self.dataset_id,
                    "kind": "delimited_text",
                    "source_sha256": digest,
                    "mapping": {
                        "delimiter": ",",
                        "axis_column": "time",
                        "signal_columns": [{"column": "response", "trace_id": "pilot-trace-001"}],
                    },
                    "offset": offset,
                    "count": len(self.source_text),
                    "next_offset": offset + limit
                    if offset + limit < len(self.source_text)
                    else None,
                    "text": part,
                }
            )

        if name == "measurement_analyze_chromatogram":
            self.analysis_ids["chromatogram"] = "analysis-chrom"
            self.methods = getattr(self, "methods", {})
            self.methods["analysis-chrom"] = {
                "baseline": "none",
                "baseline_lambda": 1e6,
                "baseline_p": 0.01,
                "smoothing": "none",
                "smoothing_sigma": 1.0,
                "min_height": 0.0,
                "min_prominence": 0.0,
                "min_distance": 0.0,
                "integration_baseline": "local",
            }
            return _tool_result(
                {
                    "analysis_id": "analysis-chrom",
                    "dataset_id": self.dataset_id,
                    "trace_id": "pilot-trace-001",
                    "kind": "chromatogram",
                }
            )

        if name == "measurement_analyze_sec":
            self.analysis_ids["sec"] = "analysis-sec"
            self.methods = getattr(self, "methods", {})
            self.methods["analysis-sec"] = arguments["method"] | {
                "extrapolation": "none",
                "integration_range": None,
            }
            return _tool_result(
                {
                    "analysis_id": "analysis-sec",
                    "dataset_id": self.dataset_id,
                    "trace_id": "pilot-trace-001",
                    "kind": "sec",
                }
            )

        if name == "measurement_read_analysis_data":
            analysis_id, kind = arguments["analysis_id"], arguments["kind"]
            if kind == "peaks":
                area = math.sqrt(2 * math.pi) * 0.16
                if self.corruption == "peak_area_tolerance":
                    area *= 1.01
                return _tool_result(
                    {
                        "analysis_id": analysis_id,
                        "kind": kind,
                        "offset": 0,
                        "count": 1,
                        "next_offset": None,
                        "data": [{"peak_id": 1, "location": 2.0, "area": area}],
                    }
                )
            if kind == "distribution":
                axis = _reference_distribution_axis(self.source_text)
                differential = [1.0] * 101
                if self.corruption == "distribution_tolerance":
                    differential = [1.01] * 101
                if self.corruption == "distribution_negative_density":
                    differential[50] = -0.25
                cumulative = [index / 100 for index in range(101)]
                if self.corruption == "distribution_cdf_interior":
                    cumulative[50] = -0.25
                if self.corruption == "distribution_axis_alias":
                    axis = [value + 100 for value in axis]
                density = [value / (axis[-1] - axis[0]) for value in differential]
                return _tool_result(
                    {
                        "analysis_id": analysis_id,
                        "kind": kind,
                        "offset": 0,
                        "count": 101,
                        "next_offset": None,
                        "data": {
                            "log10_mw": axis,
                            "differential": density,
                            "cumulative": cumulative,
                        },
                    }
                )
            raise AssertionError(f"unexpected analysis data kind: {kind}")

        if name == "measurement_get_analysis":
            analysis_id = arguments["analysis_id"]
            kind = "chromatogram" if analysis_id == "analysis-chrom" else "sec"
            provenance = {
                "input_sha256": self.input_sha256,
                "source_sha256": self.source_sha256,
                "method_sha256": _canonical_sha256(self.methods[analysis_id]),
                "worker_sha256": "d" * 64,
                "packages": self.backend["packages"],
                "installed_r_code_md5": self.backend["installed_r_code_md5"],
                "r_version": self.backend["r_version"],
                "measure_revision": "/private/tmp/local-source",
                "measure_sec_revision": "https://example.invalid/source",
            }
            dataset_id, trace_id = self.dataset_id, "pilot-trace-001"
            if self.corruption == "analysis_alias" and kind == "sec":
                dataset_id = "dataset-other"
            if self.corruption == "analysis_trace_alias" and kind == "sec":
                trace_id = "other-trace"
            if kind == "chromatogram":
                summary = {"peak_count": 1}
            else:
                mn, mw = _moments(self.source_text)
                dispersity = mw / mn
                if self.corruption == "sec_mw_tolerance":
                    mw *= 1 + 2e-8
                if self.corruption == "sec_mn_nonfinite":
                    mn = float("nan")
                if self.corruption == "sec_mn_boolean":
                    mn = True
                if self.corruption == "sec_dispersity_tolerance":
                    dispersity *= 1 + 2e-8
                summary = {"mn_g_mol": mn, "mw_g_mol": mw, "dispersity": dispersity}
            method = self.methods[analysis_id]
            if self.corruption == "analysis_method_alias" and kind == "sec":
                method = method | {"fit_type": "linear"}
            if self.corruption == "analysis_method_digest_alias" and kind == "sec":
                provenance["method_sha256"] = "e" * 64
            return _tool_result(
                {
                    "analysis_id": analysis_id,
                    "dataset_id": dataset_id,
                    "trace_id": trace_id,
                    "kind": kind,
                    "method": method,
                    "summary": summary,
                    "warnings": ["synthetic warning retained in private evidence"],
                    "provenance": provenance,
                    "input_import_origin": {"url": "https://example.invalid/origin"},
                }
            )

        raise AssertionError(f"unexpected tool call: {name}")


def _run(client: Any) -> dict[str, Any]:
    return asyncio.run(qualify_numerics(client))


def test_qualify_numerics_returns_bounded_private_read_evidence():
    client = FakeClient()
    result = _run(client)

    assert set(result) == {
        "profile",
        "checks",
        "dataset_id",
        "trace_id",
        "analysis_ids",
        "records",
        "report",
    }
    assert result["profile"] == PROFILE
    assert set(result["checks"]) == _CHECK_KEYS
    assert all(value is True for value in result["checks"].values())
    assert result["trace_id"] == "pilot-trace-001"
    assert result["analysis_ids"] == {"chromatogram": "analysis-chrom", "sec": "analysis-sec"}
    assert all(record["tool"] in _READ_TOOLS for record in result["records"])
    assert all(set(record) == {"tool", "arguments", "response"} for record in result["records"])
    assert [name for name, _ in client.calls].count("measurement_register_csv") == 1
    assert [name for name, _ in client.calls].count("measurement_analyze_chromatogram") == 1
    assert [name for name, _ in client.calls].count("measurement_analyze_sec") == 1
    assert client.calls[0][0] == "measurement_capabilities"
    assert result["report"]["chromatogram"]["relative_area_error"] == pytest.approx(0)
    assert result["report"]["sec"]["relative_mn_error"] <= 1e-8
    assert result["report"]["sec"]["relative_mw_error"] <= 1e-8
    assert result["report"]["sec"]["relative_dispersity_error"] <= 1e-8
    assert result["report"]["distribution"]["integral"] == pytest.approx(1)
    getter_records = [
        record["response"]
        for record in result["records"]
        if record["tool"] == "measurement_get_analysis"
    ]
    assert all(record["warnings"] for record in getter_records)
    assert getter_records[0]["provenance"]["measure_revision"] == "/private/tmp/local-source"
    assert getter_records[0]["provenance"]["measure_sec_revision"] == (
        "https://example.invalid/source"
    )
    assert getter_records[0]["input_import_origin"]["url"] == "https://example.invalid/origin"


@pytest.mark.parametrize(
    "corruption",
    ["registration_digest", "inspection_digest", "source_page_digest", "source_page_text"],
)
def test_source_digest_or_text_corruption_fails(corruption):
    client = FakeClient(corruption)
    with pytest.raises(
        PilotCheckError, match="^Pilot numerical qualification could not be completed$"
    ):
        _run(client)
    assert not any(
        name in {"measurement_analyze_chromatogram", "measurement_analyze_sec"}
        for name, _ in client.calls
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "analysis_alias",
        "analysis_trace_alias",
        "peak_area_tolerance",
        "sec_mw_tolerance",
        "sec_mn_nonfinite",
        "sec_mn_boolean",
        "sec_dispersity_tolerance",
        "distribution_tolerance",
        "distribution_negative_density",
        "distribution_cdf_interior",
        "distribution_axis_alias",
        "analysis_method_alias",
        "analysis_method_digest_alias",
    ],
)
def test_analysis_aliases_nonfinite_values_and_tolerance_fail(corruption):
    with pytest.raises(
        PilotCheckError, match="^Pilot numerical qualification could not be completed$"
    ):
        _run(FakeClient(corruption))


def test_missing_numerical_capability_fails_before_any_mutation():
    client = FakeClient("missing_capability")
    with pytest.raises(PilotCheckError, match="^Required numerical capabilities are unavailable$"):
        _run(client)
    assert [name for name, _ in client.calls] == ["measurement_capabilities"]


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("capability_error", "Pilot numerical qualification call failed"),
        ("missing_structured", "Pilot numerical qualification call failed"),
        ("bad_error_flag", "Pilot numerical qualification call failed"),
        ("missing_fingerprint", "Required numerical capabilities are unavailable"),
    ],
)
def test_invalid_capability_call_fails_safely_before_mutation(corruption, message):
    client = FakeClient(corruption)
    with pytest.raises(PilotCheckError, match=f"^{message}$"):
        _run(client)
    assert [name for name, _ in client.calls] == ["measurement_capabilities"]


@pytest.mark.r_backend
def test_qualify_numerics_over_real_stdio(tmp_path):
    r_path = Path("/opt/homebrew/opt/r/bin/R")
    r_library = Path("/tmp/measure-design-lineage-r-library-20261109")
    if not r_path.exists():
        pytest.skip("the assigned R runtime is unavailable")
    env = dict(
        os.environ,
        MEASURE_MCP_R=str(r_path),
        R_LIBS_USER=str(r_library),
        MEASURE_MCP_DATA_DIR=str(tmp_path),
        MEASURE_MCP_IDENTITY="local",
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "measure_service"],
        env=env,
    )

    async def exercise():
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                return await qualify_numerics(client)

    result = asyncio.run(exercise())
    assert result["profile"] == PROFILE
    assert all(result["checks"].values())
    assert result["report"]["chromatogram"]["observed_peak_center"] == pytest.approx(2)
    assert result["report"]["chromatogram"]["relative_area_error"] <= 0.001
    assert result["report"]["sec"]["relative_mn_error"] <= 1e-8
    assert result["report"]["sec"]["relative_mw_error"] <= 1e-8
    assert result["report"]["distribution"]["integral"] == pytest.approx(1, rel=1e-5)
    assert all(record["tool"] in _READ_TOOLS for record in result["records"])
