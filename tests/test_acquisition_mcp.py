"""Check the group and public schema at the MCP boundary."""

import asyncio

from mcp.shared.memory import create_connected_server_and_client_session

from measure_service import server


def test_acquisition_tools_use_the_authenticated_principal(monkeypatch):
    monkeypatch.setattr(server.mcp, "_selected_groups", frozenset({"ms"}))
    received = []

    def register(owner, files):
        received.append((owner, files))
        return {"acquisition_id": "native-1"}

    monkeypatch.setattr(server.acquisition_store, "register_acquisition", register)
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "local")
    files = {f"file-{i}": "native member" for i in range(13)}

    async def exercise():
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            public = tools["measurement_register_acquisition"]
            assert set(public.inputSchema["properties"]) == {"files"}
            assert public.annotations.idempotentHint
            assert tools["measurement_get_acquisition"].annotations.readOnlyHint
            invalid = await client.call_tool("measurement_register_acquisition", {"files": {}})
            assert invalid.isError
            result = await client.call_tool("measurement_register_acquisition", {"files": files})
            assert not result.isError
            assert result.structuredContent == {"acquisition_id": "native-1"}

    asyncio.run(exercise())
    assert received == [("local", files)]
