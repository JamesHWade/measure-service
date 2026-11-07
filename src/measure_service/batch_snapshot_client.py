"""Collect exact immutable batch bytes without trusting page identity or lengths."""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Any

from .evidence_client import _call
from .storage import canonical


async def collect_batch_snapshot(
    client: Any, batch_id: str, expected_revision: int, mode: str, *, page_size: int = 48_000
) -> bytes:
    from .batch_snapshot import MAX_SNAPSHOT_BYTES, snapshot_manifest

    if type(batch_id) is not str or not batch_id.strip() or len(batch_id) > 200:
        raise ValueError("batch_id must be a nonempty ID of at most 200 characters")
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("expected_revision must be a positive integer")
    if mode not in ("references", "self_contained"):
        raise ValueError("mode must explicitly be references or self_contained")
    if type(page_size) is not int or not 1 <= page_size <= 48_000:
        raise ValueError("page_size must be an integer from 1 through 48000")
    manifest = await _call(
        client,
        "measurement_export_chromatogram_batch",
        {
            "batch_id": batch_id,
            "expected_revision": expected_revision,
            "mode": mode,
        },
    )
    fields = {
        "profile",
        "schema_version",
        "mode",
        "snapshot_sha256",
        "size_bytes",
        "batch_id",
        "batch_revision",
        "dataset_count",
        "analysis_count",
        "item_count",
    }
    if (
        set(manifest) != fields
        or manifest.get("profile") != "measure-chromatogram-batch-snapshot/1"
        or manifest.get("schema_version") != "1.0"
        or manifest.get("mode") != mode
        or manifest.get("batch_id") != batch_id
        or type(manifest.get("batch_revision")) is not int
        or manifest["batch_revision"] != expected_revision
        or type(manifest.get("size_bytes")) is not int
        or not 1 <= manifest["size_bytes"] <= MAX_SNAPSHOT_BYTES
        or len(canonical(manifest).encode("utf-8")) > 8192
    ):
        raise ValueError("Export manifest does not match the requested bounded snapshot")
    digest = manifest.get("snapshot_sha256")
    for field in ("dataset_count", "analysis_count", "item_count"):
        lower = 0 if field == "analysis_count" else 1
        if type(manifest.get(field)) is not int or not lower <= manifest[field] <= 32:
            raise ValueError("Export manifest has an invalid record count")
    if (
        manifest["analysis_count"] > manifest["item_count"]
        or manifest["dataset_count"] > manifest["item_count"]
    ):
        raise ValueError("Export manifest record counts do not match batch membership")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ValueError("Export manifest has an invalid snapshot digest")
    size = manifest["size_bytes"]
    collected = bytearray()
    while len(collected) < size:
        offset = len(collected)
        page = await _call(
            client,
            "measurement_read_chromatogram_batch_export",
            {
                "snapshot_sha256": digest,
                "offset": offset,
                "limit": page_size,
            },
        )
        if (
            set(page)
            != {"profile", "snapshot_sha256", "offset", "count", "next_offset", "data_base64"}
            or page.get("profile") != manifest["profile"]
            or page.get("snapshot_sha256") != digest
            or type(page.get("offset")) is not int
            or page["offset"] != offset
            or type(page.get("count")) is not int
            or page["count"] != size
            or not isinstance(page.get("data_base64"), str)
            or len(page["data_base64"]) > 64_000
            or len(canonical(page).encode("utf-8")) > 256 * 1024
        ):
            raise ValueError("Export page identity, position or size changed")
        try:
            chunk = base64.b64decode(page["data_base64"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Export page is not strict base64") from exc
        if (
            not chunk
            or len(chunk) > min(page_size, size - offset)
            or base64.b64encode(chunk).decode("ascii") != page["data_base64"]
        ):
            raise ValueError("Export page has an invalid byte chunk")
        end = offset + len(chunk)
        expected_next = end if end < size else None
        if page.get("next_offset") != expected_next or (
            expected_next is not None and type(page["next_offset"]) is not int
        ):
            raise ValueError("Export page continuation is inconsistent")
        collected.extend(chunk)
    data = bytes(collected)
    if hashlib.sha256(data).hexdigest() != digest or snapshot_manifest(data) != manifest:
        raise ValueError("Collected snapshot digest or manifest does not match")
    return data
