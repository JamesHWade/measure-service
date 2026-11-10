"""Exercise the installed ASGI boundary over a real local socket, without R."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest


@pytest.fixture
def running_service(tmp_path):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    store = tmp_path / "store"
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
            "--no-access-log",
        ],
        env=dict(
            os.environ,
            MEASURE_MCP_DATA_DIR=str(store),
            MEASURE_MCP_R=str(tmp_path / "unavailable-R"),
            MEASURE_MCP_IDENTITY="connect",
            MEASURE_MCP_TRUST_CONNECT_HEADERS="1",
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=tmp_path,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None, "Service exited before accepting connections"
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            raise AssertionError("Service did not start")
        yield port, store
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _headers():
    return {
        "RStudio-Connect-Credentials": json.dumps({"user": str(uuid.uuid4())}),
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def _create_request(key):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "measurement_create_experiment",
            "arguments": {
                "spec": {"title": "Synthetic operational check", "source": "manual"},
                "idempotency_key": key,
            },
        },
    }


def test_socket_health_and_chunked_oversize_reject_before_mutation(running_service):
    port, store = running_service

    async def exercise():
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers=_headers(), timeout=10, trust_env=False
        ) as client:
            live = await client.get("/health/live")
            assert live.status_code == 200 and live.json() == {"status": "live"}
            ready = await client.get("/health/ready")
            assert ready.status_code == 503 and ready.json() == {"status": "not_ready"}
            assert not store.exists(), "Readiness must not create the store"

            # A valid call is kept intact apart from request metadata padding.
            # A dispatch bug would create an experiment and a retry receipt.
            oversized = _create_request("oversized-must-not-commit")
            oversized["params"]["_meta"] = {"padding": "x" * 8_000_000}
            content = json.dumps(oversized).encode()

            async def chunks():
                for offset in range(0, len(content), 65536):
                    yield content[offset : offset + 65536]

            rejected = await client.post("/mcp", content=chunks())
            assert rejected.status_code == 413
            assert not store.exists(), "Rejected upload reached a storage-writing tool"

            response = await client.post("/mcp", json=_create_request("accepted"))
            assert response.status_code == 200
            assert response.json()["result"]["isError"] is False
            ready = await client.get("/health/ready")
            assert ready.status_code == 200 and ready.json() == {"status": "ready"}
            head = await client.head("/health/ready")
            assert head.status_code == 200 and head.content == b""
            marker = store / "INCOMPLETE"
            marker.touch()
            try:
                assert (await client.get("/health/ready")).status_code == 503
            finally:
                marker.unlink()

    asyncio.run(exercise())


def test_socket_slow_upload_capacity_keeps_health_live_and_releases_on_disconnect(running_service):
    port, store = running_service

    async def exercise():
        streams = []
        try:
            for _ in range(16):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                streams.append((reader, writer))
                writer.write(
                    b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Length: 1000000\r\nContent-Type: application/json\r\n\r\n{"
                )
                await writer.drain()
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", headers=_headers(), timeout=2, trust_env=False
            ) as client:
                deadline = time.monotonic() + 3
                while True:
                    probe = await client.post("/capacity-probe", content=b"")
                    if probe.status_code == 503:
                        break
                    assert probe.status_code == 404
                    assert time.monotonic() < deadline, "Uploads did not reserve capacity"
                    await asyncio.sleep(0.02)
                response = await client.post("/mcp", json=_create_request("at-capacity"))
                assert response.status_code == 503
                assert response.headers["retry-after"]
                assert (await client.get("/health/live")).status_code == 200
                assert not store.exists()
        finally:
            for _, writer in streams:
                writer.close()
            await asyncio.gather(*(writer.wait_closed() for _, writer in streams))
        # Disconnect is asynchronous at the network boundary. Retry only the
        # same idempotent request until the released slot becomes observable.
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers=_headers(), timeout=2, trust_env=False
        ) as client:
            deadline = time.monotonic() + 3
            while True:
                response = await client.post("/mcp", json=_create_request("after-disconnect"))
                if response.status_code != 503:
                    break
                assert time.monotonic() < deadline, "Capacity leaked after upload disconnect"
                await asyncio.sleep(0.02)
            assert response.status_code == 200
            assert response.json()["result"]["isError"] is False

    asyncio.run(exercise())
