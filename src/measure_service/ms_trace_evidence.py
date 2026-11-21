"""Retain one core-derived XIC and serve its scan and centroid rows."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from collections import Counter
from typing import Any

from . import acquisition_store, analysis_runtime
from .storage import canonical, now, registry_connection, sha256

PROFILE = "measure-ms-trace/1"
MAX_PAGE = 1000
MAX_PAGE_BYTES = 256 * 1024
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
ROW_COUNTS = {"points": "point_count", "source_rows": "source_row_count"}


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _saved_manifest(row):
    text = row["manifest"]
    if _digest_text(text) != row["manifest_sha256"]:
        raise ValueError("Stored MS trace manifest failed its integrity check")
    return json.loads(text)


def _existing(con, owner, request_digest):
    row = con.execute(
        "SELECT manifest, manifest_sha256 FROM ms_traces WHERE owner=? AND request_sha256=?",
        (owner, request_digest),
    ).fetchone()
    return _saved_manifest(row) if row else None


def _validate_links(points, source_rows):
    scans = {point["scan_id"]: point["selected_points"] for point in points}
    if len(scans) != len(points):
        raise ValueError("Core returned duplicate scan IDs")
    counts = Counter()
    previous_index = {}
    for row in source_rows:
        scan = row["scan_id"]
        index = row["point_index"]
        if scan not in scans or type(index) is not int or index <= previous_index.get(scan, 0):
            raise ValueError("Core returned inconsistent centroid indices")
        previous_index[scan] = index
        counts[scan] += 1
    if any(n != counts[scan] for scan, n in scans.items()):
        raise ValueError("Core returned inconsistent selected centroid counts")


def extract_ms_trace(
    owner: str,
    acquisition_id: str,
    stream_id: str,
    polarity: str,
    ms_level: int,
    mz_min: float,
    mz_max: float,
) -> dict[str, Any]:
    if not isinstance(stream_id, str) or not stream_id or polarity not in ("positive", "negative"):
        raise ValueError("Select a declared MS stream and polarity")
    if type(ms_level) is not int or not 1 <= ms_level <= 2**31 - 1:
        raise ValueError("MS level must be a positive R integer")
    if (
        not all(type(x) in (int, float) and math.isfinite(x) for x in (mz_min, mz_max))
        or not 0 < mz_min <= mz_max
    ):
        raise ValueError("The m/z bounds must form a finite, positive, inclusive interval")
    files = acquisition_store.acquisition_files(owner, acquisition_id)
    input_digest = sha256(files)
    selectors = dict(
        stream_id=stream_id,
        polarity=polarity,
        ms_level=ms_level,
        mz_min=float(mz_min),
        mz_max=float(mz_max),
    )
    request_digest = sha256([PROFILE, acquisition_id, input_digest, selectors])
    # A successful acquisition may come from an older installation. Initialize
    # the additive tables once before looking for the original extraction.
    with registry_connection(write=True) as con:
        saved = _existing(con, owner, request_digest)
    if saved is not None:
        return saved
    result = analysis_runtime.run_worker(
        {"operation": "extract_ms_trace", "files": files, **selectors}
    )
    points, sources = result["points"], result["source_rows"]
    if not isinstance(points, list) or not isinstance(sources, list) or not points:
        raise ValueError("Expected one or more scan results and an array of centroid rows")
    _validate_links(points, sources)
    trace_id = str(uuid.uuid4())
    manifest = {
        "profile": PROFILE,
        "trace_id": trace_id,
        "acquisition_id": acquisition_id,
        "input_sha256": input_digest,
        "created_at": now(),
        "processing": result["processing"],
        "provenance": {
            "worker_sha256": result.worker_sha256,
            "packages": result["packages"],
            "installed_r_code_md5": result["installed_r_code_md5"],
            "r_version": result["r_version"],
        },
        "warnings": result["warnings"],
        "point_count": len(points),
        "source_row_count": len(sources),
    }
    encoded = canonical({**manifest, "points": points, "source_rows": sources}).encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise ValueError("Extracted MS trace exceeds the evidence byte limit")
    manifest["evidence_sha256"] = hashlib.sha256(encoded).hexdigest()
    manifest_text = canonical(manifest)
    if len(manifest_text.encode("utf-8")) > MAX_PAGE_BYTES:
        raise ValueError("Extracted MS trace manifest exceeds the page byte limit")
    with registry_connection(write=True) as con:
        saved = _existing(con, owner, request_digest)
        if saved is not None:
            return saved
        con.execute(
            "INSERT INTO ms_traces VALUES (?,?,?,?,?,?)",
            (
                trace_id,
                owner,
                acquisition_id,
                request_digest,
                manifest_text,
                _digest_text(manifest_text),
            ),
        )
        for kind, rows in (("points", points), ("source_rows", sources)):
            for ordinal, row in enumerate(rows):
                text = canonical(row)
                if len(text.encode("utf-8")) > MAX_PAGE_BYTES - 1024:
                    raise ValueError("A centroid or scan row exceeds the page byte limit")
                con.execute(
                    "INSERT INTO ms_trace_rows VALUES (?,?,?,?,?)",
                    (trace_id, kind, ordinal, text, _digest_text(text)),
                )
    return manifest


def _manifest(con, owner, trace_id):
    try:
        row = con.execute(
            "SELECT manifest, manifest_sha256 FROM ms_traces WHERE id=? AND owner=?",
            (trace_id, owner),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if str(exc) != "no such table: ms_traces":
            raise
        row = None
    if row is None:
        raise ValueError("MS trace was not found or is not accessible")
    return _saved_manifest(row)


def get_ms_trace(owner, trace_id):
    with registry_connection() as con:
        return _manifest(con, owner, trace_id)


def read_ms_trace(owner, trace_id, kind, evidence_sha256, offset=0, limit=100):
    if (
        kind not in ROW_COUNTS
        or type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= MAX_PAGE
    ):
        raise ValueError("Select a valid MS trace row kind, offset and page size")
    with registry_connection() as con:
        manifest = _manifest(con, owner, trace_id)
        if evidence_sha256 != manifest["evidence_sha256"]:
            raise ValueError("Evidence digest does not match the retained MS trace")
        total = manifest[ROW_COUNTS[kind]]
        if offset > total:
            raise ValueError("MS trace offset is past the end of the retained rows")
        rows = con.execute(
            "SELECT ordinal, payload, row_sha256 FROM ms_trace_rows "
            "WHERE trace_id=? AND kind=? AND ordinal>=? ORDER BY ordinal LIMIT ?",
            (trace_id, kind, offset, limit),
        )
        data = []
        # IDs and profile in the envelope have fixed sizes.
        used = 1024
        for ordinal, text, digest in rows:
            if ordinal != offset + len(data) or _digest_text(text) != digest:
                raise ValueError("Stored MS trace row failed its integrity check")
            used += len(text.encode("utf-8")) + 1
            if data and used > MAX_PAGE_BYTES:
                break
            data.append(json.loads(text))
        else:
            if len(data) != min(limit, total - offset):
                raise ValueError("Stored MS trace page has missing rows")
        end = offset + len(data)
        return {
            "profile": PROFILE,
            "trace_id": trace_id,
            "kind": kind,
            "evidence_sha256": evidence_sha256,
            "offset": offset,
            "count": len(data),
            "next_offset": end if end < total else None,
            "data": data,
        }
