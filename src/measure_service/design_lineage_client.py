"""Collect and verify an immutable experiment-design lineage over public MCP pages."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .bounded_json import (
    MAX_JSON_DEPTH as MAX_JSON_DEPTH,
)
from .bounded_json import (
    MAX_JSON_INTEGER_BITS as MAX_JSON_INTEGER_BITS,
)
from .bounded_json import (
    MAX_JSON_NODES as MAX_JSON_NODES,
)
from .bounded_json import (
    MAX_JSON_STRING_BYTES as MAX_JSON_STRING_BYTES,
)
from .bounded_json import (
    MAX_JSON_STRING_CHARS as MAX_JSON_STRING_CHARS,
)
from .bounded_json import (
    preflight_json,
)
from .evidence_client import _call

LINEAGE_PROFILE = "measure-experiment-design-lineage/1"
ENTRY_PROFILE = "measure-experiment-design-version/1"
SCHEMA_VERSION = "1.0"
MAX_VERSIONS = 50
PAGE_LIMIT = 20
MAX_PAGE_BYTES = 64_000
MAX_ENTRY_BYTES = 3_000
MAX_AGGREGATE_BYTES = 160_000
MAX_ID_CHARS = 100
MAX_REASON_BYTES = 2_000
MAX_TIMESTAMP_CHARS = 100
_HEX = frozenset("0123456789abcdef")
_ENTRY_PAYLOAD_FIELDS = frozenset(
    {
        "profile",
        "schema_version",
        "root_experiment_id",
        "revision",
        "experiment_id",
        "experiment_input_sha256",
        "supersedes_experiment_id",
        "supersedes_entry_sha256",
        "reason",
        "created_at",
    }
)
_ENTRY_FIELDS = _ENTRY_PAYLOAD_FIELDS | {"entry_sha256"}
_PAGE_FIELDS = frozenset(
    {
        "profile",
        "schema_version",
        "root_experiment_id",
        "selected_experiment_id",
        "selected_revision",
        "current_experiment_id",
        "current_revision",
        "current_entry_sha256",
        "count",
        "offset",
        "limit",
        "next_offset",
        "entries",
    }
)
_STABLE_PAGE_FIELDS = _PAGE_FIELDS - {"offset", "next_offset", "entries"}


def _fail(message: str) -> ValueError:
    return ValueError(f"Experiment design lineage collection failed: {message}")


def _preflight_json(value: Any, label: str, *, max_bytes: int) -> None:
    preflight_json(value, label, max_bytes=max_bytes, fail=_fail)


def _canonical(value: Any, label: str, *, max_bytes: int) -> tuple[str, int]:
    _preflight_json(value, label, max_bytes=max_bytes)
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded_length = len(rendered.encode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as error:
        raise _fail(f"{label} is not canonical JSON: {error}") from error
    if encoded_length > max_bytes:
        raise _fail(f"{label} exceeds its {max_bytes}-byte canonical JSON limit")
    return rendered, encoded_length


def _sha256_json(value: Any, label: str, *, max_bytes: int) -> str:
    rendered, _ = _canonical(value, label, max_bytes=max_bytes)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(f"{label} must be an array")
    return value


def _strict_int(value: Any, label: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int:
        raise _fail(f"{label} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise _fail(f"{label} is outside its bounded range")
    return value


def _text(value: Any, label: str, *, max_chars: int, nonblank: bool = True) -> str:
    if type(value) is not str or (nonblank and not value):
        raise _fail(f"{label} must be text")
    if nonblank and not value.strip():
        raise _fail(f"{label} must not be whitespace-only")
    if len(value) > max_chars:
        raise _fail(f"{label} exceeds its character limit")
    if "\x00" in value:
        raise _fail(f"{label} contains a NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _fail(f"{label} contains an invalid Unicode surrogate") from error
    return value


def _identifier(value: Any, label: str) -> str:
    return _text(value, label, max_chars=MAX_ID_CHARS)


def _digest(value: Any, label: str) -> str:
    value = _text(value, label, max_chars=64)
    if len(value) != 64 or any(character not in _HEX for character in value):
        raise _fail(f"{label} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, label: str) -> str:
    value = _text(value, label, max_chars=MAX_TIMESTAMP_CHARS)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError) as error:
        raise _fail(f"{label} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _fail(f"{label} must include a timezone offset")
    return value


def _reason(value: Any) -> str:
    if type(value) is not str or not value or not value.strip():
        raise _fail("lineage entry reason must not be empty or whitespace-only")
    if "\x00" in value:
        raise _fail("lineage entry reason contains a NUL")
    try:
        byte_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise _fail("lineage entry reason contains an invalid Unicode surrogate") from error
    if byte_length > MAX_REASON_BYTES:
        raise _fail("lineage entry reason exceeds its UTF-8 byte limit")
    return value


def _validate_entry(value: Any, root_id: str) -> tuple[dict[str, Any], int]:
    _, entry_bytes = _canonical(value, "lineage entry", max_bytes=MAX_ENTRY_BYTES)
    entry = _object(value, "lineage entry")
    if set(entry) != _ENTRY_FIELDS:
        raise _fail("lineage entry fields are incomplete or unexpected")
    if entry["profile"] != ENTRY_PROFILE or entry["schema_version"] != SCHEMA_VERSION:
        raise _fail("lineage entry profile or schema version is invalid")
    if _identifier(entry["root_experiment_id"], "lineage entry root_experiment_id") != root_id:
        raise _fail("lineage entry root identity changed")
    _strict_int(entry["revision"], "lineage entry revision", minimum=1, maximum=MAX_VERSIONS)
    _identifier(entry["experiment_id"], "lineage entry experiment_id")
    _digest(entry["experiment_input_sha256"], "lineage entry experiment_input_sha256")
    supersedes_id = entry["supersedes_experiment_id"]
    supersedes_sha = entry["supersedes_entry_sha256"]
    if supersedes_id is not None:
        _identifier(supersedes_id, "lineage entry supersedes_experiment_id")
    if supersedes_sha is not None:
        _digest(supersedes_sha, "lineage entry supersedes_entry_sha256")
    if (supersedes_id is None) != (supersedes_sha is None):
        raise _fail("lineage entry predecessor fields must be both null or both present")
    _reason(entry["reason"])
    _timestamp(entry["created_at"], "lineage entry created_at")
    entry_sha = _digest(entry["entry_sha256"], "lineage entry entry_sha256")
    payload = {field: entry[field] for field in _ENTRY_PAYLOAD_FIELDS}
    if _sha256_json(payload, "lineage entry payload", max_bytes=MAX_ENTRY_BYTES) != entry_sha:
        raise _fail("lineage entry entry_sha256 does not match its canonical payload")
    return entry, entry_bytes


def _validate_page(
    value: Any, experiment_id: str, expected_offset: int
) -> tuple[dict[str, Any], list[Any]]:
    _canonical(value, "lineage page", max_bytes=MAX_PAGE_BYTES)
    page = _object(value, "lineage page")
    if set(page) != _PAGE_FIELDS:
        raise _fail("lineage page fields are incomplete or unexpected")
    if page["profile"] != LINEAGE_PROFILE or page["schema_version"] != SCHEMA_VERSION:
        raise _fail("lineage page profile or schema version is invalid")
    _identifier(page["root_experiment_id"], "lineage page root_experiment_id")
    if (
        _identifier(page["selected_experiment_id"], "lineage page selected_experiment_id")
        != experiment_id
    ):
        raise _fail("lineage page selected experiment differs from the requested experiment")
    _strict_int(
        page["selected_revision"], "lineage page selected_revision", minimum=1, maximum=MAX_VERSIONS
    )
    _identifier(page["current_experiment_id"], "lineage page current_experiment_id")
    _strict_int(
        page["current_revision"], "lineage page current_revision", minimum=1, maximum=MAX_VERSIONS
    )
    _digest(page["current_entry_sha256"], "lineage page current_entry_sha256")
    count = _strict_int(page["count"], "lineage page count", minimum=1, maximum=MAX_VERSIONS)
    if _strict_int(page["offset"], "lineage page offset") != expected_offset:
        raise _fail("lineage page offset is not contiguous")
    if (
        _strict_int(page["limit"], "lineage page limit", minimum=1, maximum=PAGE_LIMIT)
        != PAGE_LIMIT
    ):
        raise _fail("lineage page limit changed from the bounded request")
    entries = _array(page["entries"], "lineage page entries")
    remaining = count - expected_offset
    if remaining <= 0 or not entries or len(entries) > min(PAGE_LIMIT, remaining):
        raise _fail("lineage page contains an invalid number of entries")
    expected_next = expected_offset + len(entries)
    next_offset = page["next_offset"]
    if next_offset is not None:
        _strict_int(next_offset, "lineage page next_offset")
    if next_offset != (expected_next if expected_next < count else None):
        raise _fail("lineage page continuation is inconsistent")
    stable = {field: page[field] for field in _STABLE_PAGE_FIELDS}
    return stable, entries


def _validate_chain(
    entries: list[dict[str, Any]], stable: dict[str, Any], experiment_id: str
) -> None:
    if len(entries) != stable["count"]:
        raise _fail("collected lineage count does not match the page envelope")
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        expected_revision = index + 1
        if entry["revision"] != expected_revision:
            raise _fail("lineage revisions are not contiguous and oldest-first")
        if entry["experiment_id"] in seen_ids:
            raise _fail("lineage experiment IDs are duplicated")
        seen_ids.add(entry["experiment_id"])
        if index == 0:
            if entry["experiment_id"] != stable["root_experiment_id"]:
                raise _fail("lineage root entry does not identify the root experiment")
            if entry["supersedes_experiment_id"] is not None:
                raise _fail("lineage root entry unexpectedly has a predecessor")
            if entry["supersedes_entry_sha256"] is not None:
                raise _fail("lineage root entry unexpectedly has a predecessor digest")
            if entry["reason"] != "Initial immutable experiment":
                raise _fail("lineage root entry has an invalid initial reason")
        else:
            previous = entries[index - 1]
            if entry["supersedes_experiment_id"] != previous["experiment_id"]:
                raise _fail("lineage entry predecessor identity is not immediate")
            if entry["supersedes_entry_sha256"] != previous["entry_sha256"]:
                raise _fail("lineage entry predecessor digest is not immediate")
    selected = next((entry for entry in entries if entry["experiment_id"] == experiment_id), None)
    if selected is None or selected["revision"] != stable["selected_revision"]:
        raise _fail("selected experiment is not represented by its declared revision")
    current = entries[-1]
    if (
        current["experiment_id"] != stable["current_experiment_id"]
        or current["revision"] != stable["current_revision"]
        or current["entry_sha256"] != stable["current_entry_sha256"]
    ):
        raise _fail("lineage current head does not match the collected chain")


async def collect_experiment_design_lineage(client: Any, experiment_id: str) -> dict[str, Any]:
    """Collect the complete immutable design chain selected by ``experiment_id``."""

    experiment_id = _identifier(experiment_id, "experiment_id")
    offset = 0
    stable: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = []
    aggregate_bytes = 2
    count: int | None = None
    while count is None or offset < count:
        page = await _call(
            client,
            "measurement_get_experiment_design_lineage",
            {"experiment_id": experiment_id, "offset": offset, "limit": PAGE_LIMIT},
        )
        page_stable, page_entries = _validate_page(page, experiment_id, offset)
        if stable is None:
            stable = page_stable
            count = page_stable["count"]
        elif page_stable != stable:
            raise _fail("lineage envelope or current head changed between pages")
        for value in page_entries:
            entry, entry_bytes = _validate_entry(value, stable["root_experiment_id"])
            separator = 1 if entries else 0
            if aggregate_bytes + separator + entry_bytes > MAX_AGGREGATE_BYTES:
                raise _fail("collected lineage exceeds its aggregate canonical JSON limit")
            aggregate_bytes += separator + entry_bytes
            entries.append(entry)
        next_offset = page["next_offset"]
        offset = next_offset if next_offset is not None else offset + len(page_entries)
    if stable is None or count is None:
        raise _fail("lineage did not return a page")
    _validate_chain(entries, stable, experiment_id)
    normalized = {
        **stable,
        "offset": 0,
        "limit": count,
        "next_offset": None,
        "entries": entries,
    }
    _canonical(normalized, "collected lineage envelope", max_bytes=MAX_AGGREGATE_BYTES)
    return normalized


__all__ = ["collect_experiment_design_lineage"]
