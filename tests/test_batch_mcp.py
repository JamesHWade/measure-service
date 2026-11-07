"""Public batch schemas, durable failures and two-owner HTTP boundaries."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _trace():
    return {
        "trace_id": "uv",
        "sample_id": "constructed",
        "injection_id": "injection",
        "channel": "UV 254 nm",
        "separation": "LC",
        "detector": "UV",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "axis": [0, 1, 2],
        "signal": [0, 1, 0],
    }


def _value(response):
    assert not response.isError, response.content
    assert response.structuredContent is not None
    assert "execution_token" not in json.dumps(response.structuredContent)
    return response.structuredContent


def test_public_batch_failure_replay_and_ownership_over_http(tmp_path):
    with socket.socket() as selected:
        selected.bind(("127.0.0.1", 0))
        port = selected.getsockname()[1]
    environment = dict(
        os.environ,
        MEASURE_MCP_DATA_DIR=str(tmp_path / "new-store"),
        MEASURE_MCP_R=str(tmp_path / "unavailable-R"),
        MEASURE_MCP_IDENTITY="connect",
        MEASURE_MCP_TRUST_CONNECT_HEADERS="1",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "measure_service.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    async def exercise():
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                assert process.poll() is None, process.stderr.read().decode()
                await asyncio.sleep(0.1)
        else:
            raise AssertionError("Batch HTTP test server did not start")
        owner, other = str(uuid.uuid4()), str(uuid.uuid4())

        async def call(user, name, arguments):
            headers = (
                {} if user is None else {"RStudio-Connect-Credentials": json.dumps({"user": user})}
            )
            async with httpx.AsyncClient(headers=headers, timeout=30, trust_env=False) as http:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp", http_client=http
                ) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        return await client.call_tool(name, arguments)

        assert (await call(None, "measurement_registry_capabilities", {})).isError
        caps = _value(await call(owner, "measurement_registry_capabilities", {}))
        assert caps["chromatogram_batches"]["r_workers_per_process"] == 2
        assert caps["chromatogram_batches"]["portable_source_bundle_restore"] is True
        assert caps["chromatogram_batches"]["historical_execution_authority_restore"] is False
        dataset = _value(
            await call(
                owner,
                "measurement_register_dataset",
                {
                    "dataset": {"title": "owned input", "traces": [_trace()]},
                },
            )
        )
        declaration = {
            "batch": {
                "title": "Declared batch",
                "method": {},
                "inputs": [
                    {
                        "dataset_id": dataset["dataset_id"],
                        "trace_id": "uv",
                        "role": "reference",
                    }
                ],
            },
            "idempotency_key": "create",
        }
        assert (await call(other, "measurement_create_chromatogram_batch", declaration)).isError
        created = _value(await call(owner, "measurement_create_chromatogram_batch", declaration))
        assert (
            _value(await call(owner, "measurement_create_chromatogram_batch", declaration))
            == created
        )
        batch_id = created["batch_id"]
        run_args = {
            "batch_id": batch_id,
            "idempotency_key": "run",
            "expected_revision": created["revision"],
            "max_items": 2,
        }
        assert (
            await call(
                owner, "measurement_run_chromatogram_batch", dict(run_args, expected_revision=True)
            )
        ).isError
        run = _value(await call(owner, "measurement_run_chromatogram_batch", run_args))
        assert run["selected_count"] == 1
        assert run["status"] == "failed"
        replay = _value(await call(owner, "measurement_run_chromatogram_batch", run_args))
        assert replay == run
        assert (
            _value(
                await call(
                    owner,
                    "measurement_get_chromatogram_batch_run",
                    {
                        "run_id": run["run_id"],
                    },
                )
            )
            == run
        )
        attempts = _value(
            await call(
                owner,
                "measurement_read_chromatogram_batch_attempts",
                {
                    "batch_id": batch_id,
                    "ordinal": 0,
                },
            )
        )
        assert attempts["count"] == 1
        assert attempts["attempts"][0]["state"] == "failed"
        assert attempts["attempts"][0]["runtime"]["runtime_status"] == "unknown"
        assert attempts["attempts"][0]["analysis_id"] is None
        assert len(attempts["attempts"][0]["execution_context"]["worker_sha256"]) == 64
        current = _value(
            await call(
                owner,
                "measurement_get_chromatogram_batch",
                {
                    "batch_id": batch_id,
                },
            )
        )
        assert current["counts"]["failed"] == 1
        forbidden = [
            ("measurement_get_chromatogram_batch", {"batch_id": batch_id}),
            ("measurement_read_chromatogram_batch_items", {"batch_id": batch_id}),
            ("measurement_read_chromatogram_batch_control_events", {"batch_id": batch_id}),
            ("measurement_read_chromatogram_batch_attempts", {"batch_id": batch_id, "ordinal": 0}),
            ("measurement_get_chromatogram_batch_run", {"run_id": run["run_id"]}),
            ("measurement_run_chromatogram_batch", dict(run_args, idempotency_key="borrow-run")),
            (
                "measurement_cancel_chromatogram_batch",
                {
                    "batch_id": batch_id,
                    "idempotency_key": "borrow-cancel",
                    "expected_revision": current["revision"],
                },
            ),
            (
                "measurement_recover_chromatogram_batch",
                {
                    "batch_id": batch_id,
                    "idempotency_key": "borrow-recover",
                    "expected_revision": current["revision"],
                },
            ),
            (
                "measurement_retry_chromatogram_batch_items",
                {
                    "batch_id": batch_id,
                    "ordinals": [0],
                    "idempotency_key": "borrow-retry",
                    "expected_revision": current["revision"],
                },
            ),
        ]
        for name, arguments in forbidden:
            assert (await call(other, name, arguments)).isError, name
        retried = _value(
            await call(
                owner,
                "measurement_retry_chromatogram_batch_items",
                {
                    "batch_id": batch_id,
                    "ordinals": [0],
                    "idempotency_key": "retry",
                    "expected_revision": current["revision"],
                },
            )
        )
        assert retried["counts"]["pending"] == 1
        cancelled = _value(
            await call(
                owner,
                "measurement_cancel_chromatogram_batch",
                {
                    "batch_id": batch_id,
                    "idempotency_key": "cancel",
                    "expected_revision": retried["revision"],
                },
            )
        )
        assert cancelled["counts"]["cancelled"] == 1
        assert cancelled["status"] == "cancelled"
        events = _value(
            await call(
                owner,
                "measurement_read_chromatogram_batch_control_events",
                {
                    "batch_id": batch_id,
                },
            )
        )
        assert [event["kind"] for event in events["events"]] == [
            "retry_selected",
            "cancel_requested",
        ]
        assert events["events"][1]["items"][0]["to_state"] == "cancelled"
        assert (
            _value(
                await call(
                    owner,
                    "measurement_read_chromatogram_batch_attempts",
                    {
                        "batch_id": batch_id,
                        "ordinal": 0,
                    },
                )
            )["count"]
            == 1
        )

    try:
        asyncio.run(exercise())
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stderr is not None:
            process.stderr.close()
