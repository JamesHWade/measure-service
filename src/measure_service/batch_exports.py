"""Owner/store-bound immutable byte captures for bounded snapshot transport."""

from __future__ import annotations

import base64
import threading
from collections import OrderedDict
from typing import Any

from . import batches
from .storage import db_path

MAX_EXPORT_BYTES = 128 * 1024 * 1024
MAX_EXPORTS = 4
MAX_EXPORT_PAGE = 48_000
_CAPTURE_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_CACHE_BYTES = 0


def _store_identity() -> tuple:
    path = db_path().resolve()
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError("Batch export store is unavailable") from exc
    return str(path), info.st_dev, info.st_ino


def export_batch(owner: str, batch_id: str, expected_revision: int, mode: str) -> dict[str, Any]:
    """Capture without R or durable writes; only one heavy capture runs per process."""
    from .batch_capture import capture_snapshot
    from .batch_snapshot import snapshot_manifest

    global _CACHE_BYTES
    owner = batches._owner(owner)
    if not _CAPTURE_LOCK.acquire(blocking=False):
        raise ValueError("A batch export capture is busy; retry after it finishes")
    try:
        identity = _store_identity()
        data = capture_snapshot(owner, batch_id, expected_revision, mode)
        manifest = snapshot_manifest(data)
        if len(data) > MAX_EXPORT_BYTES:
            raise ValueError("Snapshot exceeds the export cache limit")
        if _store_identity() != identity:
            raise ValueError("Store identity changed during capture; request another export")
        key = (*identity, owner, manifest["snapshot_sha256"])
        with _CACHE_LOCK:
            previous = _CACHE.pop(key, None)
            if previous is not None:
                _CACHE_BYTES -= len(previous)
            while _CACHE and (
                len(_CACHE) >= MAX_EXPORTS or _CACHE_BYTES + len(data) > MAX_EXPORT_BYTES
            ):
                _, evicted = _CACHE.popitem(last=False)
                _CACHE_BYTES -= len(evicted)
            _CACHE[key] = data
            _CACHE_BYTES += len(data)
        return manifest
    finally:
        _CAPTURE_LOCK.release()


def read_export(owner: str, snapshot_sha256: str, offset: int = 0, limit: int = 48_000) -> dict:
    owner = batches._owner(owner)
    if (
        not isinstance(snapshot_sha256, str)
        or len(snapshot_sha256) != 64
        or any(char not in "0123456789abcdef" for char in snapshot_sha256)
    ):
        raise ValueError("snapshot_sha256 must be a lowercase SHA-256 value")
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= MAX_EXPORT_PAGE
    ):
        raise ValueError("Use an integer byte offset and a limit from 1 through 48000")
    key = (*_store_identity(), owner, snapshot_sha256)
    with _CACHE_LOCK:
        data = _CACHE.get(key)
        if data is None:
            raise ValueError("Export snapshot was not found or is no longer cached; capture again")
        _CACHE.move_to_end(key)
    if offset > len(data):
        raise ValueError("Export byte offset is beyond the snapshot")
    chunk = data[offset : offset + limit]
    next_offset = offset + len(chunk)
    return {
        "profile": "measure-chromatogram-batch-snapshot/1",
        "snapshot_sha256": snapshot_sha256,
        "offset": offset,
        "count": len(data),
        "next_offset": next_offset if next_offset < len(data) else None,
        "data_base64": base64.b64encode(chunk).decode("ascii"),
    }
