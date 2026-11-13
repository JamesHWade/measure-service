"""MCP dispatcher progress and worker-slot retention for numerical tools."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from measure_service import analysis_runtime, server

_CHILD_BOOTSTRAP = r"""
import sys
import threading
import time
from pathlib import Path

from measure_service import analysis_runtime, server

started = Path(sys.argv[1])
release = Path(sys.argv[2])
calls = 0
calls_lock = threading.Lock()


def controlled_worker(payload, *, worker_path=None):
    global calls
    operation = payload["operation"]
    if operation == "chromatogram":
        trace = payload["trace"]
        if trace["trace_id"] == "slow":
            with calls_lock:
                calls += 1
                started.write_text(str(calls), encoding="utf-8")
            while not release.exists():
                time.sleep(0.01)
        return {
            "kind": "chromatogram",
            "peak_count": 1,
            "peaks": [{"peak_id": 1, "area": 3.0, "location": 1.0}],
            "processed_trace": {"axis": trace["axis"], "signal": trace["signal"]},
            "warnings": [],
            "notes": [],
        }
    if operation == "capabilities":
        return {
            "packages": {"measure": "synthetic", "recipes": "synthetic"},
            "sec_calibrated_curve_support": True,
        }
    if operation == "sec":
        return {"summary": {"mw_number_g_mol": 10.0}, "warnings": [], "notes": []}
    if operation == "quantitation":
        return {"concentration": 1.5, "warnings": [], "notes": []}
    raise AssertionError(f"unexpected operation: {operation}")


analysis_runtime._execute_worker = controlled_worker
import uvicorn

uvicorn.run(server.app, host="127.0.0.1", port=int(sys.argv[3]), log_level="error")
"""

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"


def _trace(trace_id: str, *, separation: str = "LC") -> dict:
    return {
        "trace_id": trace_id,
        "sample_id": f"sample-{trace_id}",
        "injection_id": f"injection-{trace_id}",
        "channel": "UV 254 nm",
        "separation": separation,
        "detector": "UV",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "axis": [0.0, 1.0, 2.0],
        "signal": [0.0, 3.0, 0.0],
    }


def _value(response):
    assert not response.isError, response.content
    assert response.structuredContent is not None
    return response.structuredContent


def test_numerical_mcp_call_keeps_http_dispatcher_responsive(tmp_path):
    with socket.socket() as selected:
        selected.bind(("127.0.0.1", 0))
        port = selected.getsockname()[1]

    marker = tmp_path / "worker-started.txt"
    release = tmp_path / "release-worker.txt"
    environment = dict(
        os.environ,
        MEASURE_MCP_DATA_DIR=str(tmp_path / "store"),
        MEASURE_MCP_IDENTITY="connect",
        MEASURE_MCP_TRUST_CONNECT_HEADERS="1",
        MEASURE_MCP_HTTP_ALLOWED_HOSTS='["127.0.0.1"]',
    )
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD_BOOTSTRAP, str(marker), str(release), str(port)],
        env=environment,
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    slow_call: asyncio.Task | None = None
    url = f"http://127.0.0.1:{port}/mcp"

    async def wait_for_server() -> None:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                if process.poll() is not None:
                    stderr = "" if process.stderr is None else process.stderr.read().decode()
                    raise AssertionError(f"numerical HTTP server exited during startup: {stderr}")
                await asyncio.sleep(0.05)
        raise AssertionError("numerical HTTP server did not start")

    async def call(user: str, name: str, arguments: dict):
        headers = {"RStudio-Connect-Credentials": json.dumps({"user": user})}
        async with httpx.AsyncClient(headers=headers, timeout=10, trust_env=False) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    return await client.call_tool(name, arguments)

    async def ping(user: str) -> None:
        headers = {"RStudio-Connect-Credentials": json.dumps({"user": user})}
        async with httpx.AsyncClient(headers=headers, timeout=5, trust_env=False) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    await client.send_ping()

    async def health() -> None:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as http:
            response = await http.get(f"http://127.0.0.1:{port}/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "live"}

    async def exercise() -> None:
        nonlocal slow_call
        await wait_for_server()
        dataset = _value(
            await call(
                ALICE,
                "measurement_register_dataset",
                {
                    "dataset": {
                        "title": "dispatcher responsiveness",
                        "traces": [_trace("fast"), _trace("slow"), _trace("sec", separation="SEC")],
                    }
                },
            )
        )

        capabilities = _value(await call(ALICE, "measurement_capabilities", {}))
        assert capabilities["techniques"]["generic_1d_trace"] is True

        fast = _value(
            await call(
                ALICE,
                "measurement_analyze_chromatogram",
                {"dataset_id": dataset["dataset_id"], "trace_id": "fast", "method": {}},
            )
        )
        sec = _value(
            await call(
                ALICE,
                "measurement_analyze_sec",
                {
                    "dataset_id": dataset["dataset_id"],
                    "trace_id": "sec",
                    "method": {
                        "standards": [
                            {"location": 0.0, "molecular_weight_g_mol": 100.0},
                            {"location": 2.0, "molecular_weight_g_mol": 10.0},
                        ],
                        "standard_description": "synthetic standards",
                        "baseline_corrected": True,
                        "signal_proportional_to_weight_concentration": True,
                    },
                },
            )
        )
        assert sec["kind"] == "sec"

        quantified = _value(
            await call(
                ALICE,
                "measurement_quantify_peak",
                {
                    "analysis_id": fast["analysis_id"],
                    "peak_id": 1,
                    "method": {
                        "analyte": "synthetic analyte",
                        "concentration_unit": "mg/L",
                        "standards": [
                            {"concentration": 1.0, "response": 2.0},
                            {"concentration": 2.0, "response": 4.0},
                            {"concentration": 3.0, "response": 6.0},
                        ],
                        "peak_assigned_to_analyte": True,
                        "standards_use_same_area_basis": True,
                    },
                },
            )
        )
        assert quantified["kind"] == "peak_quantitation"

        slow_call = asyncio.create_task(
            call(
                ALICE,
                "measurement_analyze_chromatogram",
                {"dataset_id": dataset["dataset_id"], "trace_id": "slow", "method": {}},
            )
        )
        await asyncio.wait_for(_wait_for_marker(marker, "1"), timeout=5)

        # A separate HTTP session must still make a stored-record read, MCP
        # heartbeat, and health request while the numerical child is blocked.
        read, _, _ = await asyncio.wait_for(
            asyncio.gather(
                call(ALICE, "measurement_get_analysis", {"analysis_id": fast["analysis_id"]}),
                ping(ALICE),
                health(),
            ),
            timeout=3,
        )
        assert _value(read)["analysis_id"] == fast["analysis_id"]

        release.write_text("release", encoding="utf-8")
        slow = _value(await asyncio.wait_for(slow_call, timeout=5))
        assert slow["kind"] == "chromatogram"
        assert marker.read_text(encoding="utf-8") == "1"

        # The owner was captured from this HTTP request before the thread hop;
        # the completed record remains invisible to a different principal.
        assert (
            _value(
                await call(ALICE, "measurement_get_analysis", {"analysis_id": slow["analysis_id"]})
            )["analysis_id"]
            == slow["analysis_id"]
        )
        assert (
            await call(BOB, "measurement_get_analysis", {"analysis_id": slow["analysis_id"]})
        ).isError

    try:
        asyncio.run(exercise())
    finally:
        release.write_text("release", encoding="utf-8")
        if slow_call is not None and not slow_call.done():
            slow_call.cancel()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stderr is not None:
            process.stderr.close()


async def _wait_for_marker(marker: Path, expected: str) -> None:
    while not marker.exists() or marker.read_text(encoding="utf-8") != expected:
        await asyncio.sleep(0.01)


async def _wait_for_one_free_slot() -> None:
    while True:
        slots = analysis_runtime.acquire_worker_slots(2)
        if len(slots) == 1:
            slots[0].release()
            return
        for slot in slots:
            slot.release()
        await asyncio.sleep(0.01)


def test_cancelled_chromatogram_does_not_release_worker_or_store_late_result(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(server, "identity", lambda _ctx: "owner")
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0

    def controlled_worker(payload, *, worker_path=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(timeout=5)
            finished.set()
        trace = payload["trace"]
        return {
            "kind": "chromatogram",
            "peak_count": 0,
            "peaks": [],
            "processed_trace": {"axis": trace["axis"], "signal": trace["signal"]},
            "warnings": [],
            "notes": [],
        }

    monkeypatch.setattr(analysis_runtime, "_execute_worker", controlled_worker)
    dataset = server.register_dataset(
        "owner", server.Dataset(title="cancellation", traces=[server.Trace(**_trace("uv"))])
    )
    reserved = analysis_runtime.acquire_worker_slots(1)
    assert len(reserved) == 1

    def analysis_count() -> int:
        with server.connect() as con:
            return con.execute(
                "SELECT count(*) FROM analyses WHERE owner=?", ("owner",)
            ).fetchone()[0]

    async def exercise() -> None:
        task = asyncio.create_task(
            server.measurement_analyze_chromatogram(
                dataset["dataset_id"], "uv", server.ChromatogramMethod(), None
            )
        )
        assert await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The asyncio waiter is gone, but the blocking worker still owns its
        # process permit until the thread returns.
        assert analysis_runtime.acquire_worker_slots(2) == []
        with pytest.raises(ValueError, match="capacity is busy"):
            await server.measurement_analyze_chromatogram(
                dataset["dataset_id"], "uv", server.ChromatogramMethod(), None
            )
        assert analysis_count() == 0
        release.set()
        assert await asyncio.wait_for(asyncio.to_thread(finished.wait), timeout=2)
        assert analysis_count() == 0
        await asyncio.wait_for(_wait_for_one_free_slot(), timeout=2)

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        for slot in reserved:
            slot.release()

    result = asyncio.run(
        server.measurement_analyze_chromatogram(
            dataset["dataset_id"], "uv", server.ChromatogramMethod(), None
        )
    )
    assert result["kind"] == "chromatogram"
    assert calls == 2
    assert analysis_count() == 1
