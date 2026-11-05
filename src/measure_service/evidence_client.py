"""Collect a complete owned chromatogram through bounded MCP evidence pages."""

from __future__ import annotations

from typing import Any

from . import analysis_evidence
from .storage import canonical


async def _call(client: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = await client.call_tool(name, arguments)
    if response.isError or not isinstance(response.structuredContent, dict):
        detail = next(
            (
                item.text
                for item in getattr(response, "content", [])
                if getattr(item, "type", None) == "text"
            ),
            "",
        )
        suffix = f": {detail[:1000]}" if detail else ""
        raise ValueError(f"{name} did not return a successful structured evidence response{suffix}")
    return response.structuredContent


async def collect_chromatogram_evidence(
    client: Any, analysis_id: str, *, page_size: int = 1000
) -> dict[str, Any]:
    """Fetch all exact pages and reject inconsistent identities or incomplete data.

    ``client`` is an initialized MCP ClientSession. This reads stored evidence;
    it does not analyze another trace, run an optimizer, or write report files.
    """
    if type(page_size) is not int or not 1 <= page_size <= analysis_evidence.MAX_PAGE:
        raise ValueError("page_size must be an integer between 1 and 1000")
    manifest = await _call(
        client, "measurement_get_chromatogram_evidence", {"analysis_id": analysis_id}
    )
    if len(canonical(manifest).encode("utf-8")) > analysis_evidence.MAX_BUNDLE_BYTES:
        raise ValueError("Evidence manifest exceeds the document byte limit")
    if (
        manifest.get("profile") != analysis_evidence.PROFILE
        or not isinstance(manifest.get("analysis"), dict)
        or manifest["analysis"].get("analysis_id") != analysis_id
        or not isinstance(manifest.get("evidence_sha256"), str)
        or len(manifest["evidence_sha256"]) != 64
    ):
        raise ValueError("Evidence manifest does not identify the requested analysis")
    collected: dict[str, list[dict[str, Any]]] = {}
    collected_bytes = len(canonical(manifest).encode("utf-8"))
    for kind, field, maximum in (
        ("trace", "point_count", analysis_evidence.MAX_POINTS),
        ("peaks", "peak_count", analysis_evidence.MAX_PEAKS),
    ):
        count = manifest.get(field)
        if type(count) is not int or not 0 <= count <= maximum:
            raise ValueError("Evidence manifest has an invalid planned row count")
        rows: list[dict[str, Any]] = []
        offset = 0
        while offset < count:
            page = await _call(
                client,
                "measurement_read_chromatogram_evidence",
                {
                    "analysis_id": analysis_id,
                    "kind": kind,
                    "evidence_sha256": manifest["evidence_sha256"],
                    "offset": offset,
                    "limit": page_size,
                },
            )
            if len(canonical(page).encode("utf-8")) > analysis_evidence.MAX_PAGE_BYTES:
                raise ValueError("Evidence page exceeds the declared byte limit")
            data = page.get("data")
            if (
                set(page)
                != {
                    "profile",
                    "analysis_id",
                    "evidence_sha256",
                    "kind",
                    "offset",
                    "count",
                    "next_offset",
                    "data",
                }
                or page.get("profile") != manifest["profile"]
                or page.get("analysis_id") != analysis_id
                or page.get("evidence_sha256") != manifest["evidence_sha256"]
                or type(page.get("offset")) is not int
                or page["offset"] != offset
                or type(page.get("count")) is not int
                or page["count"] != count
                or page.get("kind") != kind
                or not isinstance(data, list)
                or not data
                or len(data) > min(page_size, count - offset)
            ):
                raise ValueError("Evidence page identity, position or row count changed")
            next_offset = offset + len(data)
            expected_next = next_offset if next_offset < count else None
            if page.get("next_offset") != expected_next or (
                expected_next is not None and type(page.get("next_offset")) is not int
            ):
                raise ValueError("Evidence page continuation is inconsistent")
            collected_bytes += len(canonical(data).encode("utf-8"))
            # Page JSON adds repeated array framing. Bound collection separately;
            # assembly enforces the exact canonical whole-document size.
            if collected_bytes > 2 * analysis_evidence.MAX_BUNDLE_BYTES:
                raise ValueError("Collected evidence exceeds the document byte limit")
            rows.extend(data)
            offset = next_offset
        collected[kind] = rows
    return analysis_evidence.assemble_evidence(manifest, collected["trace"], collected["peaks"])
