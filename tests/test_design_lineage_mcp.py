"""Public MCP qualification for immutable experiment-design supersession."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


def _value(response: Any) -> dict[str, Any]:
    assert not response.isError, response.content
    assert isinstance(response.structuredContent, dict)
    return response.structuredContent


def _method_declaration(flow: int = 1000) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "time_unit": "s",
        "flow_unit": "uL/min",
        "solvents": ["A", "B"],
        "program": {
            "kind": "gradient_absolute",
            "points": [
                {"time": 0, "state": {"flow": flow, "solvent_fractions": {"A": 0.9, "B": 0.1}}},
                {
                    "time": 120,
                    "state": {"flow": flow, "solvent_fractions": {"A": 0.1, "B": 0.9}},
                },
            ],
        },
    }


def _stdio_parameters(store: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "measure_service"],
        env=dict(
            os.environ,
            MEASURE_MCP_DATA_DIR=str(store),
            MEASURE_MCP_R=str(store / "unavailable-R"),
            MEASURE_MCP_IDENTITY="local",
        ),
    )


async def _create_experiment(
    client: ClientSession, key: str, *, title: str, method_id: str | None = None
) -> dict[str, Any]:
    spec: dict[str, Any] = {"title": title, "source": "manual"}
    if method_id is not None:
        spec["executable_method_id"] = method_id
    return _value(
        await client.call_tool(
            "measurement_create_experiment",
            {"spec": spec, "idempotency_key": f"experiment-{key}"},
        )
    )


async def _create_attempt(client: ClientSession, experiment_id: str, key: str) -> dict[str, Any]:
    return _value(
        await client.call_tool(
            "measurement_record_attempt",
            {
                "experiment_id": experiment_id,
                "spec": {"status": "completed"},
                "idempotency_key": f"attempt-{key}",
            },
        )
    )


def test_stdio_supersession_preserves_old_design_and_replays_after_restart(tmp_path):
    store = tmp_path / "stdio-store"
    state: dict[str, Any] = {}

    async def exercise() -> None:
        async with stdio_client(_stdio_parameters(store)) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert tools["measurement_supersede_experiment"].annotations.readOnlyHint is False
                assert tools["measurement_supersede_experiment"].annotations.idempotentHint is True
                assert (
                    tools["measurement_get_experiment_design_lineage"].annotations.readOnlyHint
                    is True
                )
                assert (
                    tools["measurement_get_experiment_design_lineage"].annotations.idempotentHint
                    is True
                )

                old = await _create_experiment(client, "old", title="Original design")
                old_snapshot = _value(
                    await client.call_tool(
                        "measurement_get_experiment", {"experiment_id": old["experiment_id"]}
                    )
                )
                old_attempt = await _create_attempt(client, old["experiment_id"], "old")
                replacement_spec = {
                    "title": "Replacement design",
                    "source": "manual",
                    "description": "Changed declared design",
                }
                supersede_arguments = {
                    "experiment_id": old["experiment_id"],
                    "expected_input_sha256": old["input_sha256"],
                    "spec": replacement_spec,
                    "reason": "Updated intended conditions",
                    "idempotency_key": "supersede-old",
                }
                receipt = _value(
                    await client.call_tool("measurement_supersede_experiment", supersede_arguments)
                )
                entry = receipt["entry"]
                assert receipt["profile"] == "measure-experiment-design-supersession/1"
                assert entry["revision"] == 2
                assert entry["supersedes_experiment_id"] == old["experiment_id"]
                assert entry["experiment_input_sha256"]
                new_id = entry["experiment_id"]
                new_snapshot = _value(
                    await client.call_tool("measurement_get_experiment", {"experiment_id": new_id})
                )
                assert new_snapshot["spec"]["title"] == "Replacement design"

                late_attempt = await _create_attempt(client, old["experiment_id"], "late-old")
                late_revision = _value(
                    await client.call_tool(
                        "measurement_revise_attempt",
                        {
                            "attempt_id": late_attempt["attempt_id"],
                            "expected_revision": 1,
                            "spec": {"status": "completed", "explanation": "Late correction"},
                            "reason": "Corrected late historical attempt",
                            "idempotency_key": "late-old-revision",
                        },
                    )
                )
                assert late_revision["revision"] == 2
                assert (
                    _value(
                        await client.call_tool(
                            "measurement_get_experiment",
                            {"experiment_id": old["experiment_id"]},
                        )
                    )
                    == old_snapshot
                )
                assert (
                    _value(
                        await client.call_tool(
                            "measurement_get_attempt",
                            {"attempt_id": old_attempt["attempt_id"]},
                        )
                    )["attempt_id"]
                    == old_attempt["attempt_id"]
                )

                lineage = _value(
                    await client.call_tool(
                        "measurement_get_experiment_design_lineage",
                        {"experiment_id": old["experiment_id"], "offset": 0, "limit": 10},
                    )
                )
                assert lineage["selected_experiment_id"] == old["experiment_id"]
                assert lineage["current_experiment_id"] == new_id
                assert lineage["current_revision"] == 2
                assert [entry["revision"] for entry in lineage["entries"]] == [1, 2]
                assert lineage["entries"][0]["experiment_id"] == old["experiment_id"]
                assert (
                    _value(
                        await client.call_tool(
                            "measurement_get_experiment_design_lineage",
                            {"experiment_id": new_id, "offset": 0, "limit": 10},
                        )
                    )["selected_experiment_id"]
                    == new_id
                )
                state.update(
                    {
                        "old": old_snapshot,
                        "receipt": receipt,
                        "lineage": lineage,
                        "supersede_arguments": supersede_arguments,
                        "old_id": old["experiment_id"],
                    }
                )

        async with stdio_client(_stdio_parameters(store)) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                replay = _value(
                    await client.call_tool(
                        "measurement_supersede_experiment", state["supersede_arguments"]
                    )
                )
                assert replay == state["receipt"]
                assert (
                    _value(
                        await client.call_tool(
                            "measurement_get_experiment", {"experiment_id": state["old_id"]}
                        )
                    )
                    == state["old"]
                )
                assert (
                    _value(
                        await client.call_tool(
                            "measurement_get_experiment_design_lineage",
                            {"experiment_id": state["old_id"], "offset": 0, "limit": 10},
                        )
                    )
                    == state["lineage"]
                )

    asyncio.run(exercise())


def _available_port() -> int:
    with socket.socket() as selected:
        selected.bind(("127.0.0.1", 0))
        return selected.getsockname()[1]


@asynccontextmanager
async def _http_session(port: int, user: str):
    headers = {"RStudio-Connect-Credentials": json.dumps({"user": user})}
    async with httpx.AsyncClient(headers=headers, timeout=30, trust_env=False) as http:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as client:
                await client.initialize()
                yield client


def test_http_lineage_ownership_mixed_reference_rollback_and_strict_inputs(tmp_path):
    port = _available_port()
    environment = dict(
        os.environ,
        MEASURE_MCP_DATA_DIR=str(tmp_path / "http-store"),
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

    async def wait_for_server() -> None:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                assert process.poll() is None, process.stderr.read().decode()
                await asyncio.sleep(0.1)
        raise AssertionError("Lineage HTTP server did not start")

    async def exercise() -> None:
        await wait_for_server()
        owner = "11111111-1111-4111-8111-111111111111"
        other = "22222222-2222-4222-8222-222222222222"
        async with _http_session(port, owner) as owner_client:
            async with _http_session(port, other) as other_client:
                owner_method = _value(
                    await owner_client.call_tool(
                        "measurement_register_method",
                        {
                            "title": "Owner method",
                            "declaration": _method_declaration(1000),
                            "idempotency_key": "owner-method",
                        },
                    )
                )
                foreign_method = _value(
                    await other_client.call_tool(
                        "measurement_register_method",
                        {
                            "title": "Foreign method",
                            "declaration": _method_declaration(1100),
                            "idempotency_key": "foreign-method",
                        },
                    )
                )
                old = await _create_experiment(
                    owner_client,
                    "owner-old",
                    title="Owner original",
                    method_id=owner_method["method_id"],
                )
                bad_spec = {
                    "title": "Rejected foreign reference",
                    "source": "manual",
                    "executable_method_id": foreign_method["method_id"],
                }
                good_spec = {
                    "title": "Owner replacement",
                    "source": "manual",
                    "executable_method_id": owner_method["method_id"],
                }
                bad_arguments = {
                    "experiment_id": old["experiment_id"],
                    "expected_input_sha256": old["input_sha256"],
                    "spec": bad_spec,
                    "reason": "Foreign method must not cross owners",
                    "idempotency_key": "same-supersession-key",
                }
                assert (
                    await owner_client.call_tool("measurement_supersede_experiment", bad_arguments)
                ).isError
                good_arguments = {**bad_arguments, "spec": good_spec}
                replacement = _value(
                    await owner_client.call_tool("measurement_supersede_experiment", good_arguments)
                )
                current_id = replacement["entry"]["experiment_id"]
                current_digest = replacement["entry"]["experiment_input_sha256"]

                assert (
                    await other_client.call_tool(
                        "measurement_get_experiment_design_lineage",
                        {"experiment_id": old["experiment_id"]},
                    )
                ).isError
                assert (
                    await other_client.call_tool(
                        "measurement_supersede_experiment",
                        {
                            "experiment_id": current_id,
                            "expected_input_sha256": current_digest,
                            "spec": {
                                "title": "Foreign attempt",
                                "source": "manual",
                                "executable_method_id": foreign_method["method_id"],
                            },
                            "reason": "Foreign lineage must not be writable",
                            "idempotency_key": "foreign-supersession",
                        },
                    )
                ).isError

                for arguments in (
                    {"experiment_id": old["experiment_id"], "offset": True},
                    {"experiment_id": old["experiment_id"], "limit": True},
                ):
                    assert (
                        await owner_client.call_tool(
                            "measurement_get_experiment_design_lineage", arguments
                        )
                    ).isError
                assert (
                    await owner_client.call_tool(
                        "measurement_supersede_experiment",
                        {
                            **good_arguments,
                            "expected_input_sha256": True,
                            "idempotency_key": "strict-bool-digest",
                        },
                    )
                ).isError
                assert (
                    await owner_client.call_tool(
                        "measurement_supersede_experiment",
                        {
                            **good_arguments,
                            "expected_input_sha256": "0" * 63,
                            "idempotency_key": "strict-format-digest",
                        },
                    )
                ).isError
                assert (
                    await owner_client.call_tool(
                        "measurement_get_experiment_design_lineage",
                        {"experiment_id": old["experiment_id"] + "\x00"},
                    )
                ).isError
                assert (
                    await owner_client.call_tool(
                        "measurement_supersede_experiment",
                        {
                            **good_arguments,
                            "reason": "contains\x00nul",
                            "idempotency_key": "nul-reason",
                        },
                    )
                ).isError

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
