"""Selected discovery and dispatch share one frozen deployment policy."""

import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from measure_service import connect_hosting, server, tool_catalog
from measure_service.tool_server import ServiceMCP, _compact_analysis_page


def test_selection_is_frozen_and_disabled_calls_skip_identity_and_handler(monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_TOOL_GROUPS", '["traces"]')
    mcp = ServiceMCP("selected", selected_groups=tool_catalog.configured_groups())
    called = []
    mcp.add_tool(lambda: "allowed", name="measurement_get_analysis", meta={"retained": True})
    mcp.add_tool(lambda: called.append("disabled"), name="measurement_create_experiment")
    monkeypatch.setenv("MEASURE_MCP_TOOL_GROUPS", '["experiments"]')
    request = SimpleNamespace(scope={})
    monkeypatch.setattr(
        mcp,
        "get_context",
        lambda: SimpleNamespace(request_context=SimpleNamespace(request=request)),
    )
    lookups = []

    async def resolve(actual):
        lookups.append(actual)

    monkeypatch.setattr(connect_hosting, "resolve_request_identity", resolve)

    async def run():
        tools = await mcp.list_tools()
        assert [tool.name for tool in tools] == ["measurement_get_analysis"]
        assert tools[0].meta == {"retained": True, tool_catalog.TOOL_GROUP_META_KEY: "traces"}
        originals = await mcp.registered_tools()
        assert originals[0].meta == {"retained": True}
        with pytest.raises(ToolError, match="not available"):
            await mcp.call_tool("measurement_create_experiment", {})
        with pytest.raises(ToolError):
            await mcp.call_tool("unregistered", {})
        assert lookups == called == []
        await mcp.call_tool("measurement_get_analysis", {})
        assert lookups == [request]

    asyncio.run(run())


def test_production_mcp_filters_tools_and_enforces_runtime_trace_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "local")
    monkeypatch.setattr(
        server.mcp, "_selected_groups", frozenset({"discovery", "traces", "evidence"})
    )
    monkeypatch.setattr(server.mcp, "_schema_profile", tool_catalog.FUNCTION_HOST_SCHEMA_PROFILE)

    async def run():
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 17
            assert all(
                tool.meta[tool_catalog.TOOL_GROUP_META_KEY] in {"discovery", "traces", "evidence"}
                for tool in tools
            )
            disabled = await client.call_tool("measurement_create_experiment", {})
            assert disabled.isError
            assert "not available" in disabled.content[0].text
            trace = {
                "trace_id": "t",
                "sample_id": "s",
                "injection_id": "i",
                "channel": "uv",
                "separation": "LC",
                "detector": "UV",
                "axis_unit": "min",
                "signal_unit": "mAU",
                "axis": list(range(50_001)),
                "signal": [0.0] * 50_001,
            }
            rejected = await client.call_tool(
                "measurement_register_dataset", {"dataset": {"title": "bounded", "traces": [trace]}}
            )
            assert rejected.isError
            assert "50000" in rejected.content[0].text or "50,000" in rejected.content[0].text
            assert len(await server.mcp.registered_tools()) == 76

    asyncio.run(run())
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("MEASURE_MCP_TOOL_GROUPS", '["PRIVATE_SENTINEL"]'),
        ("MEASURE_MCP_TOOL_SCHEMA_PROFILE", "PRIVATE_SENTINEL"),
    ],
)
def test_invalid_deployment_selection_fails_startup_without_values(tmp_path, setting, value):
    environment = dict(
        os.environ, MEASURE_MCP_IDENTITY="local", MEASURE_MCP_DATA_DIR=str(tmp_path / "store")
    )
    environment.pop("PYTHONPATH", None)
    environment[setting] = value
    result = subprocess.run(
        [sys.executable, "-c", "import measure_service.server"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert setting in result.stderr
    assert "PRIVATE_SENTINEL" not in result.stderr
    assert not (tmp_path / "store").exists()


def test_compact_page_text_preserves_structure_and_text_metadata():
    structured = {"data": ["測定", 1.0, True]}
    content = [
        TextContent(type="text", text=json.dumps(structured, indent=2), _meta={"retained": True})
    ]
    compact, actual = _compact_analysis_page((content, structured))
    assert actual is structured
    assert compact[0].text == json.dumps(
        structured, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert compact[0].meta == {"retained": True}
    assert "\n" in content[0].text


@pytest.mark.parametrize("text", ['{"value":true}', '{"value":0,"value":1}', "not JSON"])
def test_non_equivalent_or_ambiguous_text_is_preserved(text):
    original = ([TextContent(type="text", text=text)], {"value": 1})
    assert _compact_analysis_page(original) is original


def test_production_analysis_page_text_matches_its_byte_bounded_structure(monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "local")
    peaks = [
        {
            "peak_id": index + 1,
            "location": float(index),
            "height": 1.0,
            "left_base": index - 0.5,
            "right_base": index + 0.5,
            "area": 0.1,
            "area_basis": "observed_signal",
        }
        for index in range(1000)
    ]
    monkeypatch.setattr(
        server, "analysis_for", lambda owner, identifier: {"result": {"peaks": peaks}}
    )

    async def run():
        collected = []
        offset = 0
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
            while offset is not None:
                response = await client.call_tool(
                    "measurement_read_analysis_data",
                    {
                        "analysis_id": "constructed",
                        "kind": "peaks",
                        "offset": offset,
                        "limit": 1000,
                    },
                )
                assert not response.isError
                assert len(response.content) == 1
                assert len(response.content[0].text.encode("utf-8")) <= 48_000
                assert json.loads(response.content[0].text) == response.structuredContent
                page = response.structuredContent
                assert page["data"] == peaks[offset : offset + len(page["data"])]
                collected.extend(page["data"])
                offset = page["next_offset"]
        assert collected == peaks

    asyncio.run(run())


def test_host_projection_rejects_unreviewed_tuple_lengths():
    schema = {
        "type": "array",
        "prefixItems": [{"type": "number"}] * 2,
        "minItems": 1,
        "maxItems": 3,
    }
    with pytest.raises(tool_catalog.UnsupportedSchemaProfileError, match="tuple"):
        tool_catalog.schema_for_profile(schema, tool_catalog.FUNCTION_HOST_SCHEMA_PROFILE)


def test_host_projection_does_not_overwrite_an_existing_union_constraint():
    schema = {
        "$defs": {
            tag: {
                "type": "object",
                "required": ["kind"],
                "properties": {"kind": {"type": "string", "const": tag}},
            }
            for tag in ("a", "b")
        },
        "oneOf": [{"$ref": "#/$defs/a"}, {"$ref": "#/$defs/b"}],
        "discriminator": {"propertyName": "kind"},
        "anyOf": [{"type": "null"}],
    }
    with pytest.raises(tool_catalog.UnsupportedSchemaProfileError, match="oneOf"):
        tool_catalog.schema_for_profile(schema, tool_catalog.FUNCTION_HOST_SCHEMA_PROFILE)
