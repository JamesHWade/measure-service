"""An actual MCP process dies after reservation; replay/recovery stay explicit.

The injected crash uses an already-expired lease to avoid waiting 150 seconds.
No R computation is claimed by this transport/state test.
"""

import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BOOTSTRAP = """
import json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from measure_service import batches, server
original = batches.reserve_run
batches._lease_expiry = lambda: (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
def crash_after_reservation(*args, **kwargs):
    record, created = original(*args, **kwargs)
    if created:
        Path(sys.argv[1]).write_text(json.dumps({'run_id': record['run_id']}))
        os._exit(73)
    return record, created
batches.reserve_run = crash_after_reservation
server.main()
"""


def test_public_mcp_process_crash_replay_and_explicit_recovery(tmp_path):
    marker = tmp_path / "committed-reservation.json"
    environment = dict(
        os.environ,
        MEASURE_MCP_DATA_DIR=str(tmp_path / "store"),
        MEASURE_MCP_R=str(tmp_path / "unavailable-R"),
        MEASURE_MCP_IDENTITY="local",
    )
    declared = {}

    async def call(client, name, arguments):
        response = await client.call_tool(name, arguments)
        assert not response.isError, response.content
        result = response.structuredContent
        assert isinstance(result, dict)
        assert "execution_token" not in json.dumps(result)
        return result

    async def interrupted_client():
        parameters = StdioServerParameters(
            command=sys.executable, args=["-c", BOOTSTRAP, str(marker)], env=environment
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                dataset = await call(
                    client,
                    "measurement_register_dataset",
                    {
                        "dataset": {
                            "title": "constructed crash fixture",
                            "traces": [
                                {
                                    "trace_id": "uv",
                                    "sample_id": "sample",
                                    "injection_id": "inj",
                                    "channel": "UV",
                                    "separation": "LC",
                                    "detector": "UV",
                                    "axis_unit": "min",
                                    "signal_unit": "mAU",
                                    "axis": [0, 1, 2],
                                    "signal": [0, 1, 0],
                                }
                            ],
                        }
                    },
                )
                batch = await call(
                    client,
                    "measurement_create_chromatogram_batch",
                    {
                        "batch": {
                            "title": "crash",
                            "method": {},
                            "inputs": [
                                {
                                    "dataset_id": dataset["dataset_id"],
                                    "trace_id": "uv",
                                    "role": "unknown",
                                }
                            ],
                        },
                        "idempotency_key": "create",
                    },
                )
                declared.update(batch)
                await call(
                    client,
                    "measurement_run_chromatogram_batch",
                    {
                        "batch_id": batch["batch_id"],
                        "idempotency_key": "interrupted-run",
                        "expected_revision": batch["revision"],
                        "max_items": 2,
                    },
                )

    with pytest.raises(Exception):
        asyncio.run(interrupted_client())
    assert marker.is_file(), "The crash must happen after a durable reservation"
    run_id = json.loads(marker.read_text())["run_id"]

    async def resumed_client():
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "measure_service"], env=environment
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                identifier = declared["batch_id"]
                same_request = {
                    "batch_id": identifier,
                    "idempotency_key": "interrupted-run",
                    "expected_revision": declared["revision"],
                    "max_items": 2,
                }
                replay = await call(client, "measurement_run_chromatogram_batch", same_request)
                assert replay["run_id"] == run_id and replay["status"] == "running"
                attempts = await call(
                    client,
                    "measurement_read_chromatogram_batch_attempts",
                    {"batch_id": identifier, "ordinal": 0},
                )
                assert attempts["total"] == 1 and attempts["attempts"][0]["analysis_id"] is None
                batch = await call(
                    client, "measurement_get_chromatogram_batch", {"batch_id": identifier}
                )
                recovered = await call(
                    client,
                    "measurement_recover_chromatogram_batch",
                    {
                        "batch_id": identifier,
                        "idempotency_key": "recover",
                        "expected_revision": batch["revision"],
                    },
                )
                assert (
                    recovered["counts"]["interrupted"] == 1 and recovered["counts"]["pending"] == 0
                )
                retried = await call(
                    client,
                    "measurement_retry_chromatogram_batch_items",
                    {
                        "batch_id": identifier,
                        "ordinals": [0],
                        "idempotency_key": "retry",
                        "expected_revision": recovered["revision"],
                    },
                )
                failed = await call(
                    client,
                    "measurement_run_chromatogram_batch",
                    {
                        "batch_id": identifier,
                        "idempotency_key": "new-run",
                        "expected_revision": retried["revision"],
                        "max_items": 1,
                    },
                )
                assert failed["status"] == "failed"
                old = await call(
                    client, "measurement_get_chromatogram_batch_run", {"run_id": run_id}
                )
                assert old["status"] == "interrupted" and old["items"][0]["item_state"] == "failed"
                attempts = await call(
                    client,
                    "measurement_read_chromatogram_batch_attempts",
                    {"batch_id": identifier, "ordinal": 0},
                )
                assert [a["state"] for a in attempts["attempts"]] == ["interrupted", "failed"]
                assert all(a["analysis_id"] is None for a in attempts["attempts"])
                events = await call(
                    client,
                    "measurement_read_chromatogram_batch_control_events",
                    {"batch_id": identifier},
                )
                assert [e["kind"] for e in events["events"]] == [
                    "recover_expired",
                    "retry_selected",
                ]

    asyncio.run(resumed_client())
