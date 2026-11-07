"""HTTP concurrency proof for the public resumable chromatogram batch tools.

The child bootstrap replaces only the numerical processing seam with a
deterministic file-gated result.  Durable ownership, claims, cancellation,
replay and result retention remain the real service implementation.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_CHILD_BOOTSTRAP = r"""
import sys
import threading
import time
from pathlib import Path

from measure_service import analysis_runtime, server

marker = Path(sys.argv[1])
release = Path(sys.argv[2])
port = int(sys.argv[3])
started = 0
started_lock = threading.Lock()


def blocked_process(trace, method, *, worker):
    global started
    with started_lock:
        started += 1
        marker.write_text(str(started), encoding="utf-8")
    while not release.exists():
        time.sleep(0.01)
    return {
        "kind": "chromatogram",
        "peak_count": 0,
        "peaks": [],
        "warnings": [],
        "notes": [],
        "processed_trace": {"axis": trace["axis"], "signal": trace["signal"]},
    }


analysis_runtime.process_chromatogram = blocked_process
import uvicorn

uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="error")
"""


def _trace(index: int) -> dict:
    return {
        "trace_id": f"uv-{index}",
        "sample_id": f"sample-{index}",
        "injection_id": f"injection-{index}",
        "channel": "UV 254 nm",
        "separation": "LC",
        "detector": "UV",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "axis": [0, 1, 2, 3],
        "signal": [0, index + 1, 0, 0],
    }


def _value(response):
    assert not response.isError, response.content
    assert response.structuredContent is not None
    assert "execution_token" not in json.dumps(response.structuredContent)
    return response.structuredContent


def test_http_batch_run_does_not_block_progress_replay_or_cancel(tmp_path):
    with socket.socket() as selected:
        selected.bind(("127.0.0.1", 0))
        port = selected.getsockname()[1]

    marker = tmp_path / "worker-started.txt"
    release = tmp_path / "release-worker.txt"
    environment = dict(
        os.environ,
        MEASURE_MCP_DATA_DIR=str(tmp_path / "store"),
        MEASURE_MCP_R=str(tmp_path / "unavailable-R"),
        MEASURE_MCP_IDENTITY="connect",
        MEASURE_MCP_TRUST_CONNECT_HEADERS="1",
    )
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD_BOOTSTRAP, str(marker), str(release), str(port)],
        env=environment,
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    run_task: asyncio.Task | None = None

    async def wait_for_server() -> None:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                if process.poll() is not None:
                    stderr = "" if process.stderr is None else process.stderr.read().decode()
                    raise AssertionError(f"batch HTTP server exited during startup: {stderr}")
                await asyncio.sleep(0.05)
        raise AssertionError("batch HTTP concurrency server did not start")

    async def call(user: str, name: str, arguments: dict):
        headers = {"RStudio-Connect-Credentials": json.dumps({"user": user})}
        async with httpx.AsyncClient(headers=headers, timeout=15, trust_env=False) as http:
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    return await client.call_tool(name, arguments)

    async def exercise() -> None:
        nonlocal run_task
        await wait_for_server()
        owner, other = str(uuid.uuid4()), str(uuid.uuid4())
        dataset = _value(
            await call(
                owner,
                "measurement_register_dataset",
                {
                    "dataset": {
                        "title": "transport concurrency fixture",
                        "traces": [_trace(index) for index in range(3)],
                    }
                },
            )
        )
        created = _value(
            await call(
                owner,
                "measurement_create_chromatogram_batch",
                {
                    "batch": {
                        "title": "blocked transport batch",
                        "method": {},
                        "inputs": [
                            {
                                "dataset_id": dataset["dataset_id"],
                                "trace_id": f"uv-{index}",
                                "role": "unknown",
                            }
                            for index in range(3)
                        ],
                    },
                    "idempotency_key": "create",
                },
            )
        )
        run_args = {
            "batch_id": created["batch_id"],
            "idempotency_key": "blocked-run",
            "expected_revision": created["revision"],
            "max_items": 2,
        }

        run_task = asyncio.create_task(call(owner, "measurement_run_chromatogram_batch", run_args))
        try:
            await asyncio.wait_for(_wait_for_marker(marker, "2"), timeout=5)

            # These calls use independent HTTP/MCP sessions while both real
            # durable claims are blocked in the child worker.
            progress = _value(
                await asyncio.wait_for(
                    call(
                        owner,
                        "measurement_get_chromatogram_batch",
                        {"batch_id": created["batch_id"]},
                    ),
                    timeout=2,
                )
            )
            assert progress["counts"] == {
                "pending": 1,
                "running": 2,
                "completed": 0,
                "failed": 0,
                "interrupted": 0,
                "cancelled": 0,
            }
            assert progress["status"] == "active"

            replay = _value(
                await asyncio.wait_for(
                    call(owner, "measurement_run_chromatogram_batch", run_args), timeout=2
                )
            )
            assert replay["status"] == "running"
            assert replay["selected_count"] == 2

            items = _value(
                await asyncio.wait_for(
                    call(
                        owner,
                        "measurement_read_chromatogram_batch_items",
                        {"batch_id": created["batch_id"]},
                    ),
                    timeout=2,
                )
            )
            assert [item["state"] for item in items["items"]] == [
                "running",
                "running",
                "pending",
            ]

            assert (
                await asyncio.wait_for(
                    call(
                        other,
                        "measurement_get_chromatogram_batch",
                        {"batch_id": created["batch_id"]},
                    ),
                    timeout=2,
                )
            ).isError

            cancelled = _value(
                await asyncio.wait_for(
                    call(
                        owner,
                        "measurement_cancel_chromatogram_batch",
                        {
                            "batch_id": created["batch_id"],
                            "idempotency_key": "cancel-pending",
                            "expected_revision": progress["revision"],
                        },
                    ),
                    timeout=2,
                )
            )
            assert cancelled["counts"]["pending"] == 0
            assert cancelled["counts"]["cancelled"] == 1
            assert cancelled["counts"]["running"] == 2
            assert cancelled["status"] == "cancelling"
            assert marker.read_text(encoding="utf-8") == "2"

            after_cancel_replay = _value(
                await asyncio.wait_for(
                    call(owner, "measurement_run_chromatogram_batch", run_args), timeout=2
                )
            )
            assert after_cancel_replay["status"] == "running"
        finally:
            # Always unblock the child before waiting for or terminating it.
            release.write_text("release", encoding="utf-8")

        completed = _value(await asyncio.wait_for(run_task, timeout=5))
        assert completed["status"] == "completed"
        assert [item["state"] for item in completed["items"]] == ["completed", "completed"]
        analysis_ids = [item["analysis_id"] for item in completed["items"]]
        assert len(analysis_ids) == len(set(analysis_ids)) == 2
        assert marker.read_text(encoding="utf-8") == "2"

        final_batch = _value(
            await call(
                owner,
                "measurement_get_chromatogram_batch",
                {"batch_id": created["batch_id"]},
            )
        )
        assert final_batch["counts"]["completed"] == 2
        assert final_batch["counts"]["cancelled"] == 1
        assert final_batch["status"] == "cancelled"
        replay_after_completion = _value(
            await asyncio.wait_for(
                call(owner, "measurement_run_chromatogram_batch", run_args), timeout=2
            )
        )
        assert replay_after_completion == completed

        attempts = _value(
            await call(
                owner,
                "measurement_read_chromatogram_batch_attempts",
                {"batch_id": created["batch_id"], "ordinal": 0},
            )
        )
        assert attempts["total"] == 1
        assert attempts["attempts"][0]["state"] == "completed"

    try:
        asyncio.run(exercise())
    finally:
        release.write_text("release", encoding="utf-8")
        if run_task is not None and not run_task.done():
            run_task.cancel()
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
