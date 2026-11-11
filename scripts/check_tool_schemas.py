"""Check registered MCP tool schemas against Draft 2020-12 and the catalog."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator

from measure_service.tool_catalog import (
    CANONICAL_SCHEMA_PROFILE,
    FUNCTION_HOST_SCHEMA_PROFILE,
    TOOL_GROUP_BY_NAME,
    schema_for_profile,
    validate_tool_registry,
)


def _schema_notices(schema: Any, path: str = "$") -> list[str]:
    notices: list[str] = []
    if isinstance(schema, Mapping):
        if "oneOf" in schema:
            notices.append(f"{path}: intentional oneOf construct")
        if "prefixItems" in schema:
            notices.append(f"{path}: intentional prefixItems tuple")
        if schema.get("type") == "array" and isinstance(schema.get("maxItems"), int):
            if schema["maxItems"] > 16_000:
                notices.append(f"{path}: large numeric array maximum {schema['maxItems']}")
        for key in ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas"):
            value = schema.get(key)
            if isinstance(value, Mapping):
                for name, child in value.items():
                    notices.extend(_schema_notices(child, f"{path}.{key}.{name}"))
        for key in (
            "additionalProperties",
            "items",
            "contains",
            "not",
            "if",
            "then",
            "else",
            "propertyNames",
            "unevaluatedProperties",
            "unevaluatedItems",
        ):
            child = schema.get(key)
            if isinstance(child, (Mapping, list)):
                notices.extend(_schema_notices(child, f"{path}.{key}"))
        for key in (
            "allOf",
            "anyOf",
            "oneOf",
            "prefixItems",
        ):
            children = schema.get(key)
            if isinstance(children, list):
                for index, child in enumerate(children):
                    notices.extend(_schema_notices(child, f"{path}.{key}[{index}]"))
    elif isinstance(schema, list):
        for index, child in enumerate(schema):
            notices.extend(_schema_notices(child, f"{path}[{index}]"))
    return notices


def _nonstandard_numeric_keywords(schema: Any, path: str = "$") -> list[str]:
    issues: list[str] = []
    if isinstance(schema, Mapping):
        for keyword in ("gt", "ge", "lt", "le"):
            if keyword in schema:
                issues.append(f"{path}.{keyword}")
        for key in ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas"):
            value = schema.get(key)
            if isinstance(value, Mapping):
                for name, child in value.items():
                    issues.extend(_nonstandard_numeric_keywords(child, f"{path}.{key}.{name}"))
        for key in (
            "additionalProperties",
            "items",
            "contains",
            "not",
            "if",
            "then",
            "else",
            "propertyNames",
            "unevaluatedProperties",
            "unevaluatedItems",
            "allOf",
            "anyOf",
            "oneOf",
            "prefixItems",
        ):
            child = schema.get(key)
            if isinstance(child, (Mapping, list)):
                issues.extend(_nonstandard_numeric_keywords(child, f"{path}.{key}"))
    elif isinstance(schema, list):
        for index, child in enumerate(schema):
            issues.extend(_nonstandard_numeric_keywords(child, f"{path}[{index}]"))
    return issues


async def _registered_tools():
    # Import only after argument parsing so --help does not initialize the MCP app.
    from measure_service.server import mcp

    return await mcp.registered_tools()


async def _check(profile: str) -> int:
    tools = await _registered_tools()
    validate_tool_registry(tool.name for tool in tools)

    errors: list[str] = []
    notices: list[str] = []
    keyword_counts: Counter[str] = Counter()
    for tool in tools:
        if tool.name not in TOOL_GROUP_BY_NAME:
            errors.append(f"{tool.name}: no registered tool group")
        for label, original in (("input", tool.inputSchema), ("output", tool.outputSchema)):
            if original is None:
                continue
            projected = schema_for_profile(original, profile)
            try:
                Draft202012Validator.check_schema(projected)
            except Exception as exc:  # jsonschema errors carry a detailed failing path.
                errors.append(f"{tool.name} {label} schema: {exc.message}")
            for location in _nonstandard_numeric_keywords(projected):
                errors.append(
                    f"{tool.name} {label} schema has a nonstandard numeric keyword at {location}"
                )
            current_notices = _schema_notices(projected)
            notices.extend(f"{tool.name} {label} {notice}" for notice in current_notices)
            for notice in current_notices:
                if "oneOf" in notice:
                    keyword_counts["oneOf"] += 1
                elif "prefixItems" in notice:
                    keyword_counts["prefixItems"] += 1
                elif "large numeric array" in notice:
                    keyword_counts["large numeric arrays"] += 1

    for notice in notices:
        print(f"NOTICE {notice}")
    if errors:
        for error in errors:
            print(f"ERROR {error}")
        return 1
    summary = ", ".join(f"{count} {name}" for name, count in sorted(keyword_counts.items()))
    details = f"; notices: {summary}" if summary else ""
    print(
        f"Validated Draft 2020-12 schemas and exact catalog coverage for {len(tools)} tools ({profile}){details}"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=(CANONICAL_SCHEMA_PROFILE, FUNCTION_HOST_SCHEMA_PROFILE),
        default=CANONICAL_SCHEMA_PROFILE,
        help="schema view to validate (canonical by default)",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_check(args.profile)))


if __name__ == "__main__":
    main()
