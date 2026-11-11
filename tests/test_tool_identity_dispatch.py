"""Identity lookup is dispatched only for registered, enabled MCP tools."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from starlette.requests import Request

from measure_service import connect_hosting
from measure_service.server import identity
from measure_service.tool_server import ServiceMCP

ALICE = uuid.UUID("11111111-1111-4111-8111-111111111111")


def test_service_mcp_captures_publicly_registered_tool_names():
    mcp = ServiceMCP("identity-dispatch")
    mcp.add_tool(lambda: "ok", name="known")

    assert mcp.is_tool_enabled("known") is True
    assert mcp.is_tool_enabled("unknown") is False
    mcp.remove_tool("known")
    assert mcp.is_tool_enabled("known") is False


def test_lookup_runs_at_the_registered_tool_call_boundary(monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "connect")
    monkeypatch.setenv("MEASURE_MCP_TRUST_CONNECT_HEADERS", "1")
    monkeypatch.setenv("MEASURE_MCP_CONNECT_USER_CLAIM", "username")
    monkeypatch.setenv("CONNECT_SERVER", "https://connect.example/")
    monkeypatch.setenv("MEASURE_MCP_CONNECT_API_KEY", "synthetic-key")
    seen = []

    def connect_api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": [{"username": "alice", "guid": str(ALICE)}]})

    mcp = ServiceMCP("identity-dispatch")

    @mcp.tool()
    def current_owner(ctx: Context) -> str:
        return identity(ctx)

    result = {}

    async def app(scope, receive, send):
        request = Request(scope)
        context = Context(
            request_context=SimpleNamespace(request=request),
            fastmcp=mcp,
        )
        monkeypatch.setattr(mcp, "get_context", lambda: context)
        assert seen == [], "Middleware must not contact Connect before a tool call"
        result["tool_result"] = await mcp.call_tool("current_owner", {})

    adapter = connect_hosting.ConnectUsernameIdentity(
        app,
        transport=httpx.MockTransport(connect_api),
    )
    asyncio.run(
        adapter(
            {
                "type": "http",
                "headers": [
                    (b"rstudio-connect-credentials", json.dumps({"user": "alice"}).encode())
                ],
            },
            None,
            None,
        )
    )

    assert len(seen) == 1
    assert result["tool_result"][0][0].text == "connect:" + str(ALICE)


def test_duplicate_failed_tool_calls_share_one_request_local_lookup(monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "connect")
    monkeypatch.setenv("MEASURE_MCP_TRUST_CONNECT_HEADERS", "1")
    monkeypatch.setenv("MEASURE_MCP_CONNECT_USER_CLAIM", "username")
    monkeypatch.setenv("CONNECT_SERVER", "https://connect.example/")
    monkeypatch.setenv("MEASURE_MCP_CONNECT_API_KEY", "synthetic-key")
    seen = []

    def connect_api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503)

    mcp = ServiceMCP("identity-dispatch")

    @mcp.tool()
    def current_owner(ctx: Context) -> str:
        return identity(ctx)

    async def app(scope, receive, send):
        request = Request(scope)
        context = Context(request_context=SimpleNamespace(request=request), fastmcp=mcp)
        monkeypatch.setattr(mcp, "get_context", lambda: context)
        for _ in range(2):
            with pytest.raises(ToolError, match="could not be verified"):
                await mcp.call_tool("current_owner", {})

    adapter = connect_hosting.ConnectUsernameIdentity(
        app,
        transport=httpx.MockTransport(connect_api),
    )
    asyncio.run(
        adapter(
            {
                "type": "http",
                "headers": [
                    (b"rstudio-connect-credentials", json.dumps({"user": "alice"}).encode())
                ],
            },
            None,
            None,
        )
    )

    assert len(seen) == 1


def test_unknown_or_disabled_tool_names_do_not_resolve_identity(monkeypatch):
    class SelectiveServiceMCP(ServiceMCP):
        def is_tool_enabled(self, name: str) -> bool:
            return name != "disabled"

    mcp = SelectiveServiceMCP("identity-dispatch")
    mcp.add_tool(lambda: "selected", name="selected")
    mcp.add_tool(lambda: "disabled", name="disabled")
    request = Request({"type": "http", "headers": []})
    context = SimpleNamespace(request_context=SimpleNamespace(request=request))
    monkeypatch.setattr(mcp, "get_context", lambda: context)
    calls = []

    async def resolve(actual_request):
        calls.append(actual_request)

    monkeypatch.setattr(connect_hosting, "resolve_request_identity", resolve)

    async def call_tools():
        await mcp.call_tool("selected", {})
        with pytest.raises(ToolError, match="not available"):
            await mcp.call_tool("disabled", {})
        with pytest.raises(ToolError):
            await mcp.call_tool("unknown", {})

    asyncio.run(call_tools())

    assert calls == [request]


def test_stdio_tools_keep_the_local_identity_without_resolver_calls(monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "local")
    mcp = ServiceMCP("identity-dispatch")

    @mcp.tool()
    def current_owner(ctx: Context) -> str:
        return identity(ctx)

    context = Context(
        request_context=SimpleNamespace(request=None),
        fastmcp=mcp,
    )
    monkeypatch.setattr(mcp, "get_context", lambda: context)

    async def fail_if_called(request):
        pytest.fail("stdio identity must not enter the Connect resolver")

    monkeypatch.setattr(connect_hosting, "resolve_request_identity", fail_if_called)
    result = asyncio.run(mcp.call_tool("current_owner", {}))

    assert result[0][0].text == "local"
