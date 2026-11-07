"""One fixed analytical backend and record format for preview and batch callers."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sqlite3
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from .analysis_models import ChromatogramMethod, Trace
from .storage import canonical, now, sha256

ROOT = Path(__file__).resolve().parent
PROCESSING_PROFILE = "measure-chromatogram/1"
MAX_R_WORKERS = 2
_R_WORKER_SLOTS = threading.BoundedSemaphore(MAX_R_WORKERS)


class _WorkerResult(dict[str, Any]):
    """Carry execution provenance in memory without adding scientific result fields."""

    def __init__(self, result: dict[str, Any], worker_sha256: str) -> None:
        super().__init__(result)
        self.worker_sha256 = worker_sha256


class _WorkerSlot:
    """One process-local R permit, reserved before claiming a durable batch item."""

    def __init__(self) -> None:
        self._active = True
        self._lock = threading.Lock()

    def run(self, payload: dict, *, worker_path: Path | None = None) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise ValueError("An R worker permit is already executing")
        try:
            if not self._active:
                raise ValueError("The R worker permit was released")
            return _execute_worker(payload, worker_path=worker_path)
        finally:
            self._lock.release()

    def release(self) -> None:
        with self._lock:
            if self._active:
                self._active = False
                _R_WORKER_SLOTS.release()


def acquire_worker_slots(maximum: int = 1) -> list[_WorkerSlot]:
    """Take available permits without starting workers or waiting in a queue."""
    if type(maximum) is not int or not 1 <= maximum <= MAX_R_WORKERS:
        raise ValueError("Request one or two R worker permits")
    slots = []
    for _ in range(maximum):
        if not _R_WORKER_SLOTS.acquire(blocking=False):
            break
        slots.append(_WorkerSlot())
    return slots


def execution_context() -> dict[str, Any]:
    """Record installed service/worker evidence without claiming an observed R build."""
    try:
        version = importlib.metadata.version("measure-service")
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {
        "processing_profile": PROCESSING_PROFILE,
        "service_version": version,
        "analysis_runtime_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "analysis_models_sha256": hashlib.sha256(
            (ROOT / "analysis_models.py").read_bytes()
        ).hexdigest(),
        "worker_sha256": hashlib.sha256((ROOT / "worker.R").read_bytes()).hexdigest(),
        "r_runtime_evidence": "unknown_until_success",
    }


def run_worker(payload: dict, *, worker_path: Path | None = None) -> dict[str, Any]:
    slots = acquire_worker_slots()
    if not slots:
        raise ValueError("R worker capacity is busy; retry after current analyses finish")
    try:
        return slots[0].run(payload, worker_path=worker_path)
    finally:
        slots[0].release()


def _execute_worker(payload: dict, *, worker_path: Path | None = None) -> dict[str, Any]:
    command = os.environ.get("MEASURE_MCP_R", "R")
    path = ROOT / "worker.R" if worker_path is None else worker_path
    worker_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        process = subprocess.run(
            [command, "--vanilla", "--slave", "-f", str(path)],
            input=canonical(payload),
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("The R analysis worker could not start or timed out") from exc
    if process.returncode != 0:
        raise ValueError("The R analysis worker failed; check the service environment")
    if hashlib.sha256(path.read_bytes()).hexdigest() != worker_digest:
        raise ValueError("The R worker source changed during analysis; no result was retained")
    try:
        output = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("The R analysis worker returned an invalid response") from exc
    if not output.get("ok"):
        raise ValueError("Analysis failed: " + str(output.get("error", "unknown worker error")))
    warnings = output.get("warnings", [])
    if isinstance(warnings, str):
        warnings = [warnings]
    result = output["result"]
    result["warnings"] = warnings
    return _WorkerResult(result, worker_digest)


def process_chromatogram(
    trace: dict,
    method: dict,
    *,
    worker: Callable[[dict], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply the same validated method, backend and spacing warning to each trace."""
    data = Trace.model_validate(trace).model_dump(mode="json")
    spec = ChromatogramMethod.model_validate(method).model_dump(mode="json")
    execute = run_worker if worker is None else worker
    result = execute({"operation": "chromatogram", "trace": data, "method": spec})
    if spec["baseline"] == "als" or spec["smoothing"] == "gaussian":
        steps = [b - a for a, b in zip(data["axis"], data["axis"][1:])]
        if max(steps) - min(steps) > 1e-6 * max(steps):
            result["warnings"].append(
                "ALS baseline and Gaussian smoothing use sample indices; spacing is irregular, so their physical width varies along the axis."
            )
    return result


def make_analysis_record(
    dataset_id: str,
    trace_id: str,
    kind: str,
    method: dict,
    result: dict,
    input_sha256: str,
    source_sha256: str | None,
    *,
    worker_path: Path | None = None,
) -> dict[str, Any]:
    path = ROOT / "worker.R" if worker_path is None else worker_path
    provenance = {
        "input_sha256": input_sha256,
        "method_sha256": sha256(method),
        "worker_sha256": (
            result.worker_sha256
            if isinstance(result, _WorkerResult)
            else hashlib.sha256(path.read_bytes()).hexdigest()
        ),
        "source_sha256": source_sha256,
        "packages": result.get("packages"),
        "installed_r_code_md5": result.get("installed_r_code_md5"),
        "r_version": result.get("r_version"),
        "measure_revision": os.environ.get("MEASURE_MCP_MEASURE_REVISION"),
        "measure_sec_revision": os.environ.get("MEASURE_MCP_SEC_REVISION"),
    }
    return {
        "analysis_id": str(uuid.uuid4()),
        "dataset_id": dataset_id,
        "trace_id": trace_id,
        "kind": kind,
        "method": method,
        "result": result,
        "provenance": provenance,
        "created_at": now(),
    }


def insert_analysis(con: sqlite3.Connection, owner: str, record: dict[str, Any]) -> None:
    """Insert within the caller's transaction, including batch completion fencing."""
    con.execute(
        "INSERT INTO analyses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record["analysis_id"],
            owner,
            record["dataset_id"],
            record["trace_id"],
            record["kind"],
            canonical(record["method"]),
            canonical(record["result"]),
            canonical(record["provenance"]),
            record["created_at"],
        ),
    )


def compact_analysis(record: dict[str, Any]) -> dict[str, Any]:
    result = record["result"]
    return {
        "analysis_id": record["analysis_id"],
        "dataset_id": record["dataset_id"],
        "trace_id": record["trace_id"],
        "kind": record["kind"],
        "status": "completed_with_warnings" if result.get("warnings") else "completed",
        "qualification": "experimental",
        "summary": result.get("summary", {"peak_count": result.get("peak_count")}),
        "diagnostics": result.get("diagnostics"),
        "warnings": result.get("warnings", []),
        "notes": result.get("notes", []),
        "provenance": record["provenance"],
        "created_at": record["created_at"],
        "artifact": True,
    }
