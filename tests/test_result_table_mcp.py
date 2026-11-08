"""Public MCP transport coverage for mapped result-table imports."""

from __future__ import annotations

import asyncio
import base64
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

from measure_service.result_table_client import collect_result_table_import


def _value(response: Any) -> dict[str, Any]:
    assert not response.isError, response.content
    assert isinstance(response.structuredContent, dict)
    return response.structuredContent


def _spec(rows: list[str], *, source_name: str = "reported.csv", constants=None) -> dict:
    return {
        "source_name": source_name,
        "csv_text": "attempt,revision,metric,status,unit,value,explanation\n"
        + "\n".join(rows)
        + "\n",
        "mapping": {
            "columns": {
                "attempt_id": "attempt",
                "expected_attempt_revision": "revision",
                "metric": "metric",
                "status": "status",
                "unit": "unit",
                "value": "value",
                "explanation": "explanation",
            },
            "constants": constants or {},
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


async def _create_attempt(client: ClientSession, key: str) -> str:
    experiment = _value(
        await client.call_tool(
            "measurement_create_experiment",
            {
                "spec": {"title": f"Public result-table fixture {key}", "source": "manual"},
                "idempotency_key": f"experiment-{key}",
            },
        )
    )
    attempt = _value(
        await client.call_tool(
            "measurement_record_attempt",
            {
                "experiment_id": experiment["experiment_id"],
                "spec": {"status": "completed"},
                "idempotency_key": f"attempt-{key}",
            },
        )
    )
    return attempt["attempt_id"]


def test_public_result_table_import_correction_restart_and_collector(tmp_path):
    store = tmp_path / "stdio-store"
    source_state: dict[str, Any] = {}

    async def exercise() -> None:
        async with stdio_client(_stdio_parameters(store)) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                for name in (
                    "measurement_preview_result_table",
                    "measurement_get_result_table_import",
                    "measurement_read_result_table_rows",
                    "measurement_read_result_table_source",
                ):
                    assert tools[name].annotations.readOnlyHint is True
                    assert tools[name].annotations.idempotentHint is True
                assert tools["measurement_import_result_table"].annotations.readOnlyHint is False
                assert tools["measurement_import_result_table"].annotations.idempotentHint is True

                capabilities = _value(
                    await client.call_tool("measurement_registry_capabilities", {})
                )
                assert capabilities["result_tables"]["source_bytes"] == 64_000
                assert capabilities["result_tables"]["outcomes_per_import"] == 50

                first = await _create_attempt(client, "first")
                second = await _create_attempt(client, "second")
                spec = _spec(
                    [
                        f"{first},1,area,observed,mAU,0,Original café",
                        f"{second},1,area,observed,mAU,2,Second result",
                    ]
                )
                preview = _value(
                    await client.call_tool(
                        "measurement_preview_result_table",
                        {"spec": spec, "offset": 0, "limit": 20},
                    )
                )
                assert preview["row_count"] == 2

                receipt = _value(
                    await client.call_tool(
                        "measurement_import_result_table",
                        {"spec": spec, "idempotency_key": "original"},
                    )
                )
                assert receipt["profile"] == "measure-result-table-import/1"
                assert receipt["row_count"] == 2
                source_bytes = spec["csv_text"].encode("utf-8")

                rows_page = _value(
                    await client.call_tool(
                        "measurement_read_result_table_rows",
                        {"import_id": receipt["import_id"], "offset": 0, "limit": 1},
                    )
                )
                rows_tail = _value(
                    await client.call_tool(
                        "measurement_read_result_table_rows",
                        {"import_id": receipt["import_id"], "offset": 1, "limit": 1},
                    )
                )
                assert rows_page["next_offset"] == 1
                assert rows_tail["next_offset"] is None
                assert [rows_page["rows"][0]["ordinal"], rows_tail["rows"][0]["ordinal"]] == [
                    0,
                    1,
                ]
                original_result_id = rows_page["rows"][0]["result_id"]

                full_source = _value(
                    await client.call_tool(
                        "measurement_read_result_table_source",
                        {
                            "import_id": receipt["import_id"],
                            "offset": 0,
                            "limit": 12_000,
                        },
                    )
                )
                assert base64.b64decode(full_source["content_base64"]) == source_bytes
                accented = source_bytes.index("é".encode("utf-8")) + 1
                byte_page = _value(
                    await client.call_tool(
                        "measurement_read_result_table_source",
                        {
                            "import_id": receipt["import_id"],
                            "offset": accented,
                            "limit": 1,
                        },
                    )
                )
                assert (
                    base64.b64decode(byte_page["content_base64"])
                    == source_bytes[accented : accented + 1]
                )

                revised = _value(
                    await client.call_tool(
                        "measurement_revise_attempt",
                        {
                            "attempt_id": first,
                            "expected_revision": 1,
                            "spec": {"status": "completed"},
                            "reason": "Corrected reported attempt metadata",
                            "idempotency_key": "revise-first",
                        },
                    )
                )
                assert revised["revision"] == 2
                correction = _spec(
                    [f"{first},2,area,observed,mAU,3,Corrected integration"],
                    source_name="correction.csv",
                    constants={"supersedes_result_id": original_result_id},
                )
                corrected = _value(
                    await client.call_tool(
                        "measurement_import_result_table",
                        {"spec": correction, "idempotency_key": "correction"},
                    )
                )
                assert corrected["import_id"] != receipt["import_id"]

                assert (
                    _value(
                        await client.call_tool(
                            "measurement_get_result_table_import",
                            {"import_id": receipt["import_id"]},
                        )
                    )
                    == receipt
                )
                assert _value(
                    await client.call_tool(
                        "measurement_read_result_table_rows",
                        {"import_id": receipt["import_id"], "offset": 0, "limit": 20},
                    )
                ) == {
                    **{
                        "profile": "measure-result-table-rows/1",
                        "import_id": receipt["import_id"],
                        "input_sha256": receipt["input_sha256"],
                        "offset": 0,
                        "limit": 20,
                        "total_count": 2,
                        "next_offset": None,
                        "rows": rows_page["rows"] + rows_tail["rows"],
                    }
                }
                assert (
                    _value(
                        await client.call_tool(
                            "measurement_read_result_table_source",
                            {
                                "import_id": receipt["import_id"],
                                "offset": 0,
                                "limit": 12_000,
                            },
                        )
                    )
                    == full_source
                )
                source_state.update({"receipt": receipt, "spec": spec})

        async with stdio_client(_stdio_parameters(store)) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                replay = _value(
                    await client.call_tool(
                        "measurement_import_result_table",
                        {"spec": source_state["spec"], "idempotency_key": "original"},
                    )
                )
                assert replay == source_state["receipt"]
                collected = await collect_result_table_import(
                    client, source_state["receipt"]["import_id"]
                )
                assert collected["receipt"] == source_state["receipt"]
                assert collected["csv_text"] == source_state["spec"]["csv_text"]
                assert [row["ordinal"] for row in collected["rows"]] == [0, 1]

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


def test_public_result_table_http_ownership_atomicity_and_strict_inputs(tmp_path):
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
        raise AssertionError("result-table HTTP server did not start")

    async def exercise() -> None:
        await wait_for_server()
        owner, other = (
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        )
        async with _http_session(port, owner) as owner_client:
            async with _http_session(port, other) as other_client:
                owner_attempt = await _create_attempt(owner_client, "owner")
                other_attempt = await _create_attempt(other_client, "other")
                owner_spec = _spec([f"{owner_attempt},1,area,observed,mAU,2,Owner result"])
                mixed_spec = _spec(
                    [
                        f"{owner_attempt},1,area,observed,mAU,2,Owner result",
                        f"{other_attempt},1,area,observed,mAU,4,Other result",
                    ]
                )

                capabilities = _value(
                    await other_client.call_tool("measurement_registry_capabilities", {})
                )
                assert capabilities["result_tables"]["atomicity"].startswith("all_attempts")
                preview = _value(
                    await other_client.call_tool(
                        "measurement_preview_result_table", {"spec": mixed_spec}
                    )
                )
                assert preview["row_count"] == 2

                assert (
                    await owner_client.call_tool(
                        "measurement_import_result_table",
                        {"spec": mixed_spec, "idempotency_key": "atomic"},
                    )
                ).isError
                good = _value(
                    await owner_client.call_tool(
                        "measurement_import_result_table",
                        {"spec": owner_spec, "idempotency_key": "atomic"},
                    )
                )
                assert good["row_count"] == 1
                assert (
                    await other_client.call_tool(
                        "measurement_import_result_table",
                        {"spec": owner_spec, "idempotency_key": "foreign"},
                    )
                ).isError
                assert (
                    await other_client.call_tool(
                        "measurement_get_result_table_import",
                        {"import_id": good["import_id"]},
                    )
                ).isError
                assert (
                    _value(
                        await owner_client.call_tool(
                            "measurement_get_result_table_import",
                            {"import_id": good["import_id"]},
                        )
                    )
                    == good
                )
                assert (
                    await other_client.call_tool(
                        "measurement_preview_result_table",
                        {"spec": mixed_spec, "offset": True},
                    )
                ).isError
                assert (
                    await other_client.call_tool(
                        "measurement_read_result_table_rows",
                        {"import_id": "not-an-import", "offset": True},
                    )
                ).isError
                assert (
                    await owner_client.call_tool(
                        "measurement_revise_attempt",
                        {
                            "attempt_id": owner_attempt,
                            "expected_revision": True,
                            "spec": {"status": "completed"},
                            "reason": "strict boolean rejection",
                            "idempotency_key": "strict-bool",
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
