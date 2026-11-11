"""FastMCP adapter that resolves Connect identity only for service tool calls."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import AnyFunction, ContentBlock, Icon, TextContent, Tool, ToolAnnotations

from . import connect_hosting, tool_catalog


class ServiceMCP(FastMCP):
    """Resolve a fresh HTTP principal only before a registered enabled tool.

    All current service tools call ``identity(ctx)``. ``is_tool_enabled`` is
    the selection seam for a later catalog policy; by default every registered
    tool is enabled. Tool names are captured through FastMCP's public
    ``add_tool`` API so protocol discovery and unknown calls can be skipped
    without inspecting request bodies or SDK internals.
    """

    def __init__(
        self,
        *args: Any,
        selected_groups: frozenset[str] | None = None,
        schema_profile: str = tool_catalog.CANONICAL_SCHEMA_PROFILE,
        **kwargs: Any,
    ) -> None:
        if selected_groups is not None and not selected_groups.issubset(tool_catalog.TOOL_GROUPS):
            raise ValueError("Selected tool groups must be known group names")
        if schema_profile not in (
            tool_catalog.CANONICAL_SCHEMA_PROFILE,
            tool_catalog.FUNCTION_HOST_SCHEMA_PROFILE,
        ):
            raise ValueError("Unknown MCP tool schema profile")
        self._selected_groups = selected_groups
        self._schema_profile = schema_profile
        initial_tools = kwargs.get("tools") or ()
        self._registered_tool_names = {tool.name for tool in initial_tools}
        super().__init__(*args, **kwargs)

    def add_tool(
        self,
        fn: AnyFunction,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> None:
        super().add_tool(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=structured_output,
        )
        self._registered_tool_names.add(name or fn.__name__)

    def is_tool_enabled(self, name: str) -> bool:
        """Return whether this registered tool needs a request identity.

        Override this method when a selected tool catalog needs to distinguish
        enabled owner-scoped tools from other registered tools.
        """
        return name in self._registered_tool_names and (
            self._selected_groups is None or tool_catalog.enabled(name, self._selected_groups)
        )

    def validate_tool_catalog(self) -> None:
        """Verify the production registration against the versioned catalog."""
        tool_catalog.validate_tool_registry(self._registered_tool_names)

    def catalog_configuration(self) -> dict[str, Any]:
        """Describe the frozen deployment selection without account information."""
        selected = self._selected_groups
        return {
            "version": tool_catalog.TOOL_CATALOG_VERSION,
            "groups": [
                group for group in tool_catalog.TOOL_GROUPS if selected is None or group in selected
            ],
            "schema_profile": self._schema_profile,
            "enabled_tools": sum(
                self.is_tool_enabled(name) for name in self._registered_tool_names
            ),
        }

    async def registered_tools(self) -> list[Tool]:
        """Return the unfiltered canonical SDK catalog for documentation checks."""
        return await super().list_tools()

    async def list_tools(self) -> list[Tool]:
        """List selected groups with copied metadata and advertised schemas."""
        tools = await self.registered_tools()
        if self._selected_groups is None:
            return tools
        selected = []
        for tool in tools:
            if not self.is_tool_enabled(tool.name):
                continue
            decorated = tool_catalog.decorate_tool(tool)
            update = {
                "inputSchema": tool_catalog.schema_for_profile(
                    tool.inputSchema, self._schema_profile
                )
            }
            if tool.outputSchema is not None:
                update["outputSchema"] = tool_catalog.schema_for_profile(
                    tool.outputSchema, self._schema_profile
                )
            selected.append(decorated.model_copy(update=update))
        return selected

    def remove_tool(self, name: str) -> None:
        """Keep the public-name set aligned when a tool is removed."""
        super().remove_tool(name)
        self._registered_tool_names.discard(name)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        """Resolve the request principal before an enabled service tool runs."""
        if name in self._registered_tool_names:
            if not self.is_tool_enabled(name):
                raise ToolError("Tool is not available in this deployment")
            context = self.get_context()
            request_context = getattr(context, "request_context", None)
            request = getattr(request_context, "request", None)
            if request is not None:
                await connect_hosting.resolve_request_identity(request)
        result = await super().call_tool(name, arguments)
        if name == "measurement_read_analysis_data":
            return _compact_analysis_page(result)
        return result


def _compact_analysis_page(result: Any) -> Any:
    """Compact only an equivalent sole JSON text rendering of a structured page."""
    if not isinstance(result, tuple) or len(result) != 2:
        return result
    content, structured = result
    if not isinstance(structured, dict) or not isinstance(content, list):
        return result
    if len(content) != 1 or not isinstance(content[0], TextContent):
        return result

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON field")
            value[key] = item
        return value

    def compact(value):
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )

    try:
        text = compact(structured)
        if compact(json.loads(content[0].text, object_pairs_hook=unique_object)) != text:
            return result
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return result
    return [content[0].model_copy(update={"text": text})], structured
