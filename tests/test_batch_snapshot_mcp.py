"""Actual HTTP capture/collection under simulated two-owner proxy identities."""

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

from measure_service.batch_snapshot import decode_snapshot
from measure_service.batch_snapshot_client import collect_batch_snapshot

BOOTSTRAP = """
import sys,time
from pathlib import Path
from measure_service import batch_capture,server
import uvicorn
marker,release=Path(sys.argv[1]),Path(sys.argv[2])
original=batch_capture.capture_snapshot
def controlled(*args,**kwargs):
    marker.write_text('capturing')
    while not release.exists():time.sleep(.01)
    return original(*args,**kwargs)
batch_capture.capture_snapshot=controlled
uvicorn.run(server.app,host='127.0.0.1',port=int(sys.argv[3]),log_level='error')
"""


def test_public_snapshot_is_owned_bounded_r_free_and_keeps_transport_responsive(tmp_path):
    with socket.socket() as selected:
        selected.bind(("127.0.0.1", 0))
        port = selected.getsockname()[1]
    marker, release = tmp_path / "capturing", tmp_path / "release"
    env = dict(
        os.environ,
        MEASURE_MCP_DATA_DIR=str(tmp_path / "store"),
        MEASURE_MCP_R=str(tmp_path / "unavailable-R"),
        MEASURE_MCP_IDENTITY="connect",
        MEASURE_MCP_TRUST_CONNECT_HEADERS="1",
    )
    process = subprocess.Popen(
        [sys.executable, "-c", BOOTSTRAP, str(marker), str(release), str(port)],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    async def exercise():
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                assert process.poll() is None, "Test server exited"
                await asyncio.sleep(0.05)
        else:
            raise AssertionError("Snapshot test server did not start")
        owner, other = str(uuid.uuid4()), str(uuid.uuid4())

        async def call(user, name, args):
            headers = {"RStudio-Connect-Credentials": json.dumps({"user": user})}
            async with httpx.AsyncClient(headers=headers, timeout=30, trust_env=False) as http:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp", http_client=http
                ) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        return await client.call_tool(name, args)

        def value(response):
            assert not response.isError, response.content
            assert isinstance(response.structuredContent, dict)
            return response.structuredContent

        traces = [
            {
                "trace_id": f"uv-{index}",
                "sample_id": "sample",
                "injection_id": "inj",
                "channel": f"UV-{index}",
                "separation": "LC",
                "detector": "UV",
                "axis_unit": "min",
                "signal_unit": "mAU",
                "axis": [0, 1, 2],
                "signal": [0, index + 1, 0],
            }
            for index in range(2)
        ]
        dataset = value(
            await call(
                owner,
                "measurement_register_dataset",
                {"dataset": {"title": "HTTP snapshot fixture", "traces": traces}},
            )
        )
        batch = value(
            await call(
                owner,
                "measurement_create_chromatogram_batch",
                {
                    "batch": {
                        "title": "Unexecuted owned declaration",
                        "method": {},
                        "inputs": [
                            {
                                "dataset_id": dataset["dataset_id"],
                                "trace_id": "uv-0",
                                "role": "reference",
                            }
                        ],
                    },
                    "idempotency_key": "create",
                },
            )
        )
        args = {
            "batch_id": batch["batch_id"],
            "expected_revision": batch["revision"],
            "mode": "self_contained",
        }
        assert (
            await call(owner, "measurement_get_analytical_import", {"import_id": "missing"})
        ).isError
        assert (
            await call(
                owner, "measurement_export_chromatogram_batch", dict(args, expected_revision=True)
            )
        ).isError
        assert (
            await call(owner, "measurement_export_chromatogram_batch", dict(args, mode="guessed"))
        ).isError
        assert not marker.exists()
        pending = asyncio.create_task(call(owner, "measurement_export_chromatogram_batch", args))
        try:

            async def started():
                while not marker.exists():
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(started(), 5)
            current = value(
                await asyncio.wait_for(
                    call(
                        owner, "measurement_get_chromatogram_batch", {"batch_id": batch["batch_id"]}
                    ),
                    2,
                )
            )
            assert current == batch
            busy = await asyncio.wait_for(
                call(owner, "measurement_export_chromatogram_batch", args), 2
            )
            assert busy.isError
            assert not pending.done()
        finally:
            release.write_text("ready")
        manifest = value(await asyncio.wait_for(pending, 10))
        assert (
            manifest["item_count"] == manifest["dataset_count"] == 1
            and manifest["analysis_count"] == 0
        )
        read_args = {"snapshot_sha256": manifest["snapshot_sha256"], "offset": 0, "limit": 100}
        assert (await call(other, "measurement_read_chromatogram_batch_export", read_args)).isError
        assert (
            await call(
                owner, "measurement_read_chromatogram_batch_export", dict(read_args, limit=True)
            )
        ).isError
        assert (await call(other, "measurement_export_chromatogram_batch", args)).isError

        class Remote:
            async def call_tool(self, name, arguments):
                return await call(owner, name, arguments)

        data = await collect_batch_snapshot(
            Remote(), batch["batch_id"], batch["revision"], "self_contained", page_size=1000
        )
        doc = decode_snapshot(data)
        assert len(doc["datasets"][0]["payload"]["traces"]) == 2
        assert doc["datasets"][0]["point_count"] == 6
        assert (
            doc["batch"]["revision"] == batch["revision"] and doc["batch"]["counts"]["pending"] == 1
        )
        assert doc["analyses"] == doc["attempts"] == doc["runs"] == []
        assert (
            value(
                await call(
                    owner, "measurement_get_chromatogram_batch", {"batch_id": batch["batch_id"]}
                )
            )
            == batch
        )

    try:
        asyncio.run(exercise())
    finally:
        release.write_text("ready")
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()
