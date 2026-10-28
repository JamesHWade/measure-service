"""Render the MCP catalog from registered schemas; no R or database is needed."""

import argparse
import asyncio
import json
from pathlib import Path

from measure_service.server import mcp


def format_reference(tools) -> str:
    lines = [
        "# MCP tool reference",
        "",
        "Generated from the registered tools by `python scripts/render_reference.py --write`.",
        "Do not edit the tables by hand. MCP `tools/list` supplies complete JSON schemas,",
        "including nested record models, defaults and validation limits. For a local JSON",
        "export, run `python scripts/render_reference.py --json`.",
        "",
        "All tools return structured JSON. Check `isError` before reading",
        "`structuredContent`. Tool annotations describe effects, not scientific validity.",
        "Registry writes use required retry keys. Dataset and analysis writes create",
        "a new record each time. No tool executes an instrument command.",
        "",
        f"The current catalog contains {len(tools)} tools.",
        "",
        "| Tool | Required arguments | Effect | Retry behavior |",
        "| --- | --- | --- | --- |",
    ]
    for tool in sorted(tools, key=lambda item: item.name):
        required = ", ".join(f"`{name}`" for name in tool.inputSchema.get("required", [])) or "—"
        effect = "read" if tool.annotations.readOnlyHint else "write"
        retry = "idempotent" if tool.annotations.idempotentHint else "creates new record"
        lines.append(f"| `{tool.name}` | {required} | {effect} | {retry} |")
    lines.extend(["", "## Tool descriptions", ""])
    for tool in sorted(tools, key=lambda item: item.name):
        lines.extend([f"### `{tool.name}`", "", tool.description.strip(), ""])
    lines.extend(
        [
            "## Related contracts",
            "",
            "- [Input layouts and limits](inputs.md)",
            "- [Numerical methods](analysis.md)",
            "- [Registry semantics](experiments.md)",
            "- [HTTP identity and storage](deployment.md)",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Update docs/tools.md")
    mode.add_argument("--check", action="store_true", help="Fail if docs/tools.md is stale")
    mode.add_argument("--json", action="store_true", help="Print complete registered schemas")
    args = parser.parse_args()
    tools = asyncio.run(mcp.list_tools())
    if args.json:
        print(json.dumps([tool.model_dump(mode="json") for tool in tools], indent=2))
        return
    rendered = format_reference(tools)
    target = Path(__file__).resolve().parents[1] / "docs" / "tools.md"
    if args.write:
        target.write_text(rendered, encoding="utf-8")
    elif args.check:
        if target.read_text(encoding="utf-8") != rendered:
            raise SystemExit(
                "docs/tools.md is stale; run python scripts/render_reference.py --write"
            )
        print("MCP reference matches the registered tools")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
