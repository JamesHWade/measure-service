"""Check repository-local Markdown links and complete JSON examples."""

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jsonschema import validate

from measure_service.server import mcp


def check() -> None:
    root = Path(__file__).resolve().parents[1]
    documents = [root / "README.md", root / "CONTRIBUTING.md", root / "AGENTS.md"]
    documents.extend(sorted((root / "docs").glob("*.md")))
    links = examples = 0
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for destination in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", text):
            url = urlsplit(destination)
            if url.scheme or url.netloc or not url.path:
                continue
            target = (document.parent / unquote(url.path)).resolve()
            if not target.is_file():
                raise SystemExit(f"Broken link in {document.relative_to(root)}: {destination}")
            links += 1
        blocks = re.findall(r"^```json\s*\n(.*?)^```", text, flags=re.MULTILINE | re.DOTALL)
        for block in blocks:
            value = json.loads(block)
            if document == root / "docs" / "inputs.md":
                validate(value, tools["measurement_register_csv"].inputSchema)
            examples += 1
    print(
        f"Checked {len(documents)} Markdown files, {links} local links and {examples} JSON examples"
    )


if __name__ == "__main__":
    check()
