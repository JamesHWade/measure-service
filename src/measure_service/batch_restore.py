"""Restore a self-contained chromatogram batch archive into a new local store.

Restoration is a local operator action.  It validates the complete archive
before reserving the destination, imports only datasets and ordinary analyses,
and creates a new pending batch.  Archived runs, attempts, control events,
execution tokens and idempotency keys remain evidence in the retained ZIP;
they are never copied into the active store.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import analysis_runtime, batches, import_origins
from .batch_models import BatchSpec
from .storage import canonical, now

SNAPSHOT_PROFILE = "measure-chromatogram-batch-snapshot/1"
SNAPSHOT_SCHEMA_VERSION = "1.0"
RESTORE_PROFILE = import_origins.RESTORE_PROFILE
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_SNAPSHOT_BYTES + 256 * 1024
INCOMPLETE_NAME = "INCOMPLETE"
ARCHIVE_NAME = "source-snapshot.zip"
RECEIPT_NAME = "restore-receipt.json"


@dataclass(frozen=True)
class _RestorePlan:
    snapshot_bytes: bytes
    archive_bytes: bytes
    snapshot: dict[str, Any]
    snapshot_sha256: str
    source_batch_id: str
    dataset_map: dict[str, str]
    analysis_map: dict[str, str]
    datasets: tuple[dict[str, Any], ...]
    analyses: tuple[dict[str, Any], ...]
    batch_spec: BatchSpec


def _text(value: Any, label: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be nonempty text")
    return value


def _safe_regular_bytes(path: Path, maximum: int, label: str) -> bytes:
    descriptor: int | None = None
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} was not found") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular non-symlink file")
    if info.st_size > maximum:
        raise ValueError(f"{label} exceeds its byte limit")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        opened = os.fstat(descriptor)
        stable_metadata = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > maximum
            or any(getattr(opened, key) != getattr(info, key) for key in stable_metadata)
        ):
            raise ValueError(f"{label} must be a bounded regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            data = handle.read(maximum + 1)
            final = os.fstat(handle.fileno())
        if (
            len(data) != opened.st_size
            or len(data) > maximum
            or any(getattr(final, key) != getattr(opened, key) for key in stable_metadata)
        ):
            raise ValueError(f"{label} changed while it was being read")
        return data
    except FileNotFoundError as exc:
        raise ValueError(f"{label} was not found") from exc
    except OSError as exc:
        raise ValueError(f"{label} could not be read") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _prepare_plan(
    snapshot_bytes: bytes, archive_bytes: bytes, snapshot: dict[str, Any]
) -> _RestorePlan:
    if snapshot.get("profile") != SNAPSHOT_PROFILE:
        raise ValueError("Unsupported chromatogram batch snapshot profile")
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("Unsupported chromatogram batch snapshot schema version")
    if snapshot.get("mode") != "self_contained":
        raise ValueError("Only self-contained snapshots can be restored")
    snapshot_sha = hashlib.sha256(snapshot_bytes).hexdigest()
    batch = snapshot.get("batch")
    if not isinstance(batch, dict):
        raise ValueError("Snapshot batch declaration is missing")
    source_batch_id = _text(batch.get("batch_id"), "batch_id")
    datasets_raw = snapshot.get("datasets")
    if not isinstance(datasets_raw, list):
        raise ValueError("Snapshot datasets must be a flat array")
    datasets = tuple(datasets_raw)
    if any(not isinstance(entry, dict) for entry in datasets):
        raise ValueError("Snapshot datasets must be objects")
    datasets_by_id = {_text(entry["dataset_id"], "dataset_id"): entry for entry in datasets}
    if len(datasets_by_id) != len(datasets):
        raise ValueError("Snapshot dataset IDs must be unique")
    dataset_map = {source_id: str(uuid.uuid4()) for source_id in datasets_by_id}
    analyses_raw = snapshot.get("analyses")
    if not isinstance(analyses_raw, list):
        raise ValueError("Snapshot analyses must be a flat array")
    analyses = tuple(analyses_raw)
    if any(not isinstance(entry, dict) for entry in analyses):
        raise ValueError("Snapshot analyses must be objects")
    analyses_by_id = {_text(entry["analysis_id"], "analysis_id"): entry for entry in analyses}
    if len(analyses_by_id) != len(analyses):
        raise ValueError("Snapshot analysis IDs must be unique")
    analysis_map = {source_id: str(uuid.uuid4()) for source_id in analyses_by_id}
    items = snapshot.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("Snapshot items must be a nonempty flat array")
    input_rows = []
    completed_analysis_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Snapshot batch items must be objects")
        dataset_id = item.get("dataset_id")
        if dataset_id not in dataset_map:
            raise ValueError("Snapshot item references an unavailable dataset")
        analysis_id = item.get("analysis_id")
        if item.get("state") == "running" or item.get("attempt_state") == "running":
            raise ValueError("Snapshots with running work cannot be restored")
        if item.get("state") == "completed":
            if analysis_id not in analyses_by_id:
                raise ValueError("Completed snapshot item lacks its analysis")
            completed_analysis_ids.add(analysis_id)
        elif analysis_id is not None:
            raise ValueError("Only completed snapshot items may link an analysis")
        input_rows.append(
            {
                "dataset_id": dataset_map[dataset_id],
                "trace_id": _text(item.get("trace_id"), "trace_id"),
                "role": item.get("role"),
                "internal_standard_references": item.get("internal_standard_references", []),
            }
        )
    if completed_analysis_ids != set(analyses_by_id):
        raise ValueError("Snapshot analyses are not exactly linked to completed items")
    method = batch.get("method")
    if not isinstance(method, dict):
        raise ValueError("Snapshot batch method is malformed")
    try:
        batch_spec = BatchSpec.model_validate(
            {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "profile": batch.get("profile"),
                "title": batch.get("title"),
                "method": method,
                "inputs": input_rows,
            }
        )
    except Exception as exc:
        raise ValueError("Snapshot batch declaration cannot create a pending batch") from exc
    return _RestorePlan(
        snapshot_bytes=snapshot_bytes,
        archive_bytes=archive_bytes,
        snapshot=snapshot,
        snapshot_sha256=snapshot_sha,
        source_batch_id=source_batch_id,
        dataset_map=dataset_map,
        analysis_map=analysis_map,
        datasets=datasets,
        analyses=analyses,
        batch_spec=batch_spec,
    )


def _destination_is_new(destination: Path) -> None:
    try:
        destination.lstat()
    except FileNotFoundError:
        return
    raise ValueError("Restore destination already exists")


def _origin_for(
    plan: _RestorePlan,
    source_record_id: str,
    local_record_id: str,
    record_kind: str,
    preserved_content_sha256: str,
    original_record_sha256: str,
    imported_at: str,
    import_id: str,
) -> dict[str, Any]:
    return {
        "profile": import_origins.PROFILE,
        "import_id": import_id,
        "snapshot_sha256": plan.snapshot_sha256,
        "source_batch_id": plan.source_batch_id,
        "source_record_id": source_record_id,
        "local_record_id": local_record_id,
        "record_kind": record_kind,
        "imported_at": imported_at,
        "original_record_sha256": original_record_sha256,
        "preserved_content_sha256": preserved_content_sha256,
    }


def _write_exclusive(path: Path, data: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _restore_transaction(plan: _RestorePlan, destination: Path) -> dict[str, Any]:
    from . import storage

    con = storage._connect_at(destination / "measure.sqlite3")
    con.row_factory = sqlite3.Row
    import_id = str(uuid.uuid4())
    restored_at = now()
    origins: list[dict[str, Any]] = []
    try:
        con.execute("BEGIN IMMEDIATE")
        import_origins.ensure_import_schema(con)
        for entry in plan.datasets:
            local_id = plan.dataset_map[entry["dataset_id"]]
            payload = entry["payload"]
            con.execute(
                "INSERT INTO datasets(id, owner, digest, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    local_id,
                    "local",
                    entry["input_sha256"],
                    canonical(payload),
                    entry["created_at"],
                ),
            )
            source = entry["source"]
            if source is not None:
                con.execute(
                    "INSERT INTO dataset_sources(dataset_id, kind, digest, content, mapping) VALUES (?, ?, ?, ?, ?)",
                    (
                        local_id,
                        source["kind"],
                        source["source_sha256"],
                        source["content"],
                        canonical(source["mapping"]),
                    ),
                )
            origins.append(
                _origin_for(
                    plan,
                    entry["dataset_id"],
                    local_id,
                    "dataset",
                    entry["input_sha256"],
                    import_origins.dataset_core_sha256(
                        entry["dataset_id"],
                        entry["input_sha256"],
                        entry["created_at"],
                        entry["payload"],
                        entry["source"],
                    ),
                    restored_at,
                    import_id,
                )
            )
        for entry in plan.analyses:
            local_id = plan.analysis_map[entry["analysis_id"]]
            record = {
                key: entry[key]
                for key in (
                    "analysis_id",
                    "dataset_id",
                    "trace_id",
                    "kind",
                    "method",
                    "result",
                    "provenance",
                    "created_at",
                )
            } | {
                "analysis_id": local_id,
                "dataset_id": plan.dataset_map[entry["dataset_id"]],
            }
            analysis_runtime.insert_analysis(con, "local", record)
            origins.append(
                _origin_for(
                    plan,
                    entry["analysis_id"],
                    local_id,
                    "analysis",
                    entry["result_sha256"],
                    import_origins.analysis_core_sha256(
                        entry["analysis_id"],
                        entry["dataset_id"],
                        entry["trace_id"],
                        entry["kind"],
                        entry["method"],
                        entry["result"],
                        entry["provenance"],
                        entry["created_at"],
                    ),
                    restored_at,
                    import_id,
                )
            )
        batch_result = batches._create_batch_record(con, owner="local", spec=plan.batch_spec)
        new_batch_id = batch_result.get("batch_id") if isinstance(batch_result, dict) else None
        if not isinstance(new_batch_id, str) or not new_batch_id:
            raise ValueError("Batch creation helper returned a malformed batch ID")
        receipt = {
            "profile": RESTORE_PROFILE,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_sha256": plan.snapshot_sha256,
            "source_batch_id": plan.source_batch_id,
            "new_batch_id": new_batch_id,
            "import_id": import_id,
            "restored_at": restored_at,
            "dataset_map": plan.dataset_map,
            "analysis_map": plan.analysis_map,
            "limits": {
                "max_snapshot_bytes": MAX_SNAPSHOT_BYTES,
                "max_archive_bytes": MAX_ARCHIVE_BYTES,
            },
            "no_execution_performed": True,
        }
        import_origins.record_import(con, "local", receipt, origins)
        con.commit()
        return receipt
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def restore_bundle(source: Path, destination: Path) -> dict[str, Any]:
    """Restore a validated self-contained archive into an exclusively new store."""
    source = Path(source)
    destination = Path(destination)

    # Validate and fully decode the source before touching the destination.
    from . import batch_bundle, batch_snapshot

    archive_bytes = _safe_regular_bytes(source, MAX_ARCHIVE_BYTES, "Snapshot archive")
    snapshot_bytes = batch_bundle.unpack_bundle(archive_bytes)
    if len(snapshot_bytes) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Snapshot exceeds its byte limit")
    snapshot = batch_snapshot.decode_snapshot(snapshot_bytes)
    plan = _prepare_plan(snapshot_bytes, archive_bytes, snapshot)
    _destination_is_new(destination)
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValueError("Restore destination already exists") from exc
    marker = destination / INCOMPLETE_NAME
    try:
        _write_exclusive(marker, b"restore in progress\n")
        _write_exclusive(destination / ARCHIVE_NAME, plan.archive_bytes)
        receipt = _restore_transaction(plan, destination)
        _write_exclusive(
            destination / RECEIPT_NAME,
            (canonical(receipt) + "\n").encode("utf-8"),
        )
        marker.unlink()
        return receipt
    except BaseException:
        # Keep the destination and marker as honest evidence of an incomplete
        # operator action.  Never recursively delete a path that may now be
        # owned or populated by a concurrent caller.
        raise


def main(argv: list[str] | None = None) -> int:
    """Run the local operator restore without changing service configuration."""
    parser = argparse.ArgumentParser(
        description="Restore a self-contained Measure chromatogram batch bundle"
    )
    parser.add_argument("source", type=Path, help="bundle ZIP to validate and restore")
    parser.add_argument(
        "--destination",
        required=True,
        type=Path,
        help="new directory to create for the restored local store",
    )
    arguments = parser.parse_args(argv)
    receipt = restore_bundle(arguments.source, arguments.destination)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI smoke test
    raise SystemExit(main())


__all__ = ["main", "restore_bundle"]
