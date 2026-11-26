"""Check repository-local Markdown/Quarto links and complete JSON examples."""

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jsonschema import validate

from measure_service.server import mcp


def _strip_code_blocks(text: str) -> str:
    return re.sub(r"^```.*?^```", "", text, flags=re.MULTILINE | re.DOTALL)


def check() -> None:
    root = Path(__file__).resolve().parents[1]
    documents = [root / "README.md", root / "CONTRIBUTING.md", root / "AGENTS.md"]
    index_qmd = root / "index.qmd"
    if index_qmd.is_file():
        documents.append(index_qmd)
    documents.extend(sorted((root / "docs").glob("*.md")))
    user_guide = root / "user_guide"
    if user_guide.is_dir():
        documents.extend(sorted(user_guide.glob("*.qmd")))
        documents.extend(sorted(user_guide.glob("*.md")))

    links = examples = 0
    tools = {tool.name: tool for tool in asyncio.run(mcp.registered_tools())}
    for document in documents:
        text = document.read_text(encoding="utf-8")
        prose = _strip_code_blocks(text)
        for destination in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", prose):
            url = urlsplit(destination)
            if url.scheme or url.netloc or not url.path:
                continue
            if document.parent == user_guide and url.path.startswith("../reference/mcp/"):
                continue
            if document == index_qmd and url.path.startswith("reference/mcp/"):
                continue
            if document == index_qmd and url.path.startswith("user-guide/"):
                target = (user_guide / unquote(url.path.removeprefix("user-guide/"))).resolve()
            else:
                target = (document.parent / unquote(url.path)).resolve()
            if not target.is_file():
                raise SystemExit(f"Broken link in {document.relative_to(root)}: {destination}")
            links += 1

        blocks = re.findall(r"^```json\s*\n(.*?)^```", text, flags=re.MULTILINE | re.DOTALL)
        for block in blocks:
            value = json.loads(block)
            if document.name in {"inputs.md", "inputs.qmd"} and "csv_text" in value:
                if "channels" in value:
                    validate(value, tools["measurement_register_wide_csv"].inputSchema)
                elif "group_columns" in value:
                    validate(value, tools["measurement_register_long_csv"].inputSchema)
                else:
                    validate(value, tools["measurement_register_csv"].inputSchema)
            elif document.name in {"methods.md", "methods.qmd"} and "declaration" in value:
                validate(value, tools["measurement_validate_method"].inputSchema)
            examples += 1

    print(
        f"Checked {len(documents)} Markdown/Quarto files, {links} local links and {examples} JSON examples"
    )


if __name__ == "__main__":
    check()
