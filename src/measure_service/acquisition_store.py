"""Owner-scoped persistence for validated native acquisition bundles."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from . import analysis_runtime
from .acquisition_models import (
    MAX_SUMMARY_BYTES,
    PROFILE,
    acquisition_digest,
    validate_files,
    validate_summary,
)
from .storage import canonical, db_path, now, registry_connection


def _owner(owner: Any) -> str:
    if type(owner) is not str or not owner.strip() or len(owner) > 200:
        raise ValueError("owner must be nonempty text")
    return owner


def _record_id(acquisition_id: Any) -> str:
    if type(acquisition_id) is not str or not acquisition_id.strip() or len(acquisition_id) > 100:
        raise ValueError("acquisition_id must be nonempty text")
    return acquisition_id


def _decode_json(serialized: Any, label: str, maximum: int) -> Any:
    if type(serialized) is not str:
        raise ValueError(f"Stored acquisition {label} exceeds its byte limit")
    try:
        serialized_bytes = serialized.encode("utf-8")
    except UnicodeError as error:
        raise ValueError(f"Stored acquisition {label} is not UTF-8") from error
    if len(serialized_bytes) > maximum:
        raise ValueError(f"Stored acquisition {label} exceeds its byte limit")
    try:
        value = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Stored acquisition {label} is malformed") from error
    try:
        if canonical(value) != serialized:
            raise ValueError(f"Stored acquisition {label} is not canonically encoded")
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError(f"Stored acquisition {label} is malformed") from error
    return value


def _response(
    acquisition_id: str, digest: str, created_at: str, inspection: dict[str, Any]
) -> dict[str, Any]:
    """Build the one public inspection contract shared by parent layers."""
    return {
        "profile": PROFILE,
        "acquisition_id": acquisition_id,
        "input_sha256": digest,
        "created_at": created_at,
        "inspection": inspection,
    }


def _files_for_connection(
    con: sqlite3.Connection, owner: str, acquisition_id: str, digest: str | None = None
) -> dict[str, str]:
    rows = con.execute(
        """SELECT f.name, f.content, a.digest
           FROM acquisition_files AS f
           JOIN acquisitions AS a ON a.id=f.acquisition_id
           WHERE f.owner=? AND f.acquisition_id=? AND a.owner=?
           ORDER BY f.name""",
        (owner, acquisition_id, owner),
    ).fetchall()
    if not rows:
        raise ValueError("Acquisition was not found or is not accessible")
    files = {row[0]: row[1] for row in rows}
    try:
        checked = validate_files(files)
    except ValueError as error:
        raise ValueError("Stored acquisition members are malformed") from error
    stored_digest = rows[0][2]
    if any(row[2] != stored_digest for row in rows) or acquisition_digest(checked) != stored_digest:
        raise ValueError("Stored acquisition members do not match their digest")
    if digest is not None and stored_digest != digest:
        raise ValueError("Stored acquisition digest is inconsistent")
    return checked


def _response_from_row(con: sqlite3.Connection, owner: str, row: sqlite3.Row) -> dict[str, Any]:
    inspection = _decode_json(row["summary"], "summary", MAX_SUMMARY_BYTES)
    if not isinstance(inspection, dict):
        raise ValueError("Stored acquisition summary is malformed")
    _files_for_connection(con, owner, row["id"], row["digest"])
    return _response(row["id"], row["digest"], row["created_at"], inspection)


def _existing_digest(owner: str, digest: str) -> dict[str, Any] | None:
    """Read a successful retry receipt without creating a missing store."""

    database = db_path(create=False)
    if not database.exists():
        return None
    try:
        with registry_connection() as con:
            try:
                row = con.execute(
                    "SELECT * FROM acquisitions WHERE owner=? AND digest=?",
                    (owner, digest),
                ).fetchone()
            except sqlite3.OperationalError as error:
                if "no such table" in str(error).lower():
                    return None
                raise
            if row is None:
                return None
            return _response_from_row(con, owner, row)
    except ValueError as error:
        if "Store is not initialized" in str(error):
            return None
        raise


def register_acquisition(
    owner: str,
    files: Mapping[str, str],
    *,
    worker: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate, inspect and persist one immutable native acquisition.

    The worker runs before the SQLite write, so malformed input or a failed
    scientific inspection leaves no partial acquisition.  A repeated
    owner/member digest returns the original immutable record, making a client
    retry safe without exposing an idempotency namespace to this layer.
    """

    principal = _owner(owner)
    normalized = validate_files(files)
    digest = acquisition_digest(normalized)
    existing = _existing_digest(principal, digest)
    if existing is not None:
        return existing
    execute = analysis_runtime.run_worker if worker is None else worker
    result = execute({"operation": "inspect_acquisition", "files": dict(normalized)})
    inspection = validate_summary(result)
    serialized = canonical(inspection)
    created_at = now()
    acquisition_id = str(uuid.uuid4())

    with registry_connection(write=True) as con:
        existing = con.execute(
            "SELECT * FROM acquisitions WHERE owner=? AND digest=?", (principal, digest)
        ).fetchone()
        if existing is not None:
            return _response_from_row(con, principal, existing)
        con.execute(
            "INSERT INTO acquisitions VALUES (?, ?, ?, ?, ?)",
            (acquisition_id, principal, digest, serialized, created_at),
        )
        con.executemany(
            "INSERT INTO acquisition_files VALUES (?, ?, ?, ?)",
            [(acquisition_id, principal, name, value) for name, value in normalized.items()],
        )
        row = con.execute("SELECT * FROM acquisitions WHERE id=?", (acquisition_id,)).fetchone()
        if row is None:  # pragma: no cover - SQLite would fail the transaction first.
            raise ValueError("Acquisition could not be stored")
        return _response(acquisition_id, digest, created_at, inspection)


def get_acquisition(owner: str, acquisition_id: str) -> dict[str, Any]:
    """Return the bounded inspection summary for an owned acquisition."""

    principal = _owner(owner)
    record_id = _record_id(acquisition_id)
    with registry_connection() as con:
        try:
            row = con.execute(
                "SELECT * FROM acquisitions WHERE id=? AND owner=?",
                (record_id, principal),
            ).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                raise ValueError("Acquisition was not found or is not accessible") from None
            raise
        if row is None:
            raise ValueError("Acquisition was not found or is not accessible")
        return _response_from_row(con, principal, row)


def acquisition_files(owner: str, acquisition_id: str) -> dict[str, str]:
    """Return exact native member text for an owned acquisition.

    This is intentionally internal: extract/report layers can use it, while
    ordinary lookup responses never expose raw arrays or member contents.
    """

    principal = _owner(owner)
    record_id = _record_id(acquisition_id)
    with registry_connection() as con:
        try:
            row = con.execute(
                "SELECT digest FROM acquisitions WHERE id=? AND owner=?",
                (record_id, principal),
            ).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                raise ValueError("Acquisition was not found or is not accessible") from None
            raise
        if row is None:
            raise ValueError("Acquisition was not found or is not accessible")
        return _files_for_connection(con, principal, record_id, row[0])


__all__ = [
    "acquisition_files",
    "get_acquisition",
    "register_acquisition",
]
