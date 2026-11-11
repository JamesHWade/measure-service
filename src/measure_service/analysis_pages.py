"""Compact response pages and warning bounds for stored analysis results."""

from __future__ import annotations

import json
import math
from typing import Any

MAX_PAGE = 1_000
MAX_RESPONSE_BYTES = 48_000
MAX_BACKEND_WARNINGS = 32
MAX_WARNING_UTF8_BYTES = 2_000

_WARNING_TRUNCATION_MARKER = " [warning text truncated]"


def _compact_json_bytes(value: Any, description: str) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"{description} is not valid finite JSON data") from exc


def _validate_json_value(value: Any, description: str) -> None:
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise ValueError(f"{description} must contain only finite numbers")
    if type(value) is list:
        for item in value:
            _validate_json_value(item, description)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError(f"{description} object keys must be strings")
        for item in value.values():
            _validate_json_value(item, description)
        return
    raise ValueError(f"{description} contains an unsupported JSON value")


def _analysis_rows(result: dict[str, Any], kind: str) -> tuple[int, list | dict]:
    if type(kind) is not str or kind not in {"peaks", "processed_trace", "distribution"}:
        raise ValueError("kind must be 'peaks', 'processed_trace' or 'distribution'")
    if kind not in result:
        raise ValueError("This analysis does not contain the requested data")
    data = result[kind]
    if kind == "peaks":
        if type(data) is not list:
            raise ValueError("Stored peak data must be a list of objects")
        for row in data:
            if type(row) is not dict:
                raise ValueError("Stored peak data must be a list of objects")
            _validate_json_value(row, "Stored peak data")
            _compact_json_bytes(row, "Stored peak row")
        return len(data), data

    expected = {
        "processed_trace": {"axis", "signal"},
        "distribution": {"log10_mw", "differential", "cumulative"},
    }[kind]
    if type(data) is not dict or set(data) != expected:
        raise ValueError(f"Stored {kind} data have an unsupported shape")
    if any(type(values) is not list for values in data.values()):
        raise ValueError(f"Stored {kind} arrays must be lists")
    lengths = {len(values) for values in data.values()}
    if len(lengths) != 1:
        raise ValueError(f"Stored {kind} arrays have inconsistent lengths")
    for values in data.values():
        if any(type(value) not in {int, float} for value in values):
            raise ValueError(f"Stored {kind} arrays must contain finite numbers")
        if any(type(value) is float and not math.isfinite(value) for value in values):
            raise ValueError(f"Stored {kind} arrays must contain finite numbers")
    return next(iter(lengths)), data


def page_analysis_data(
    analysis_id: str,
    kind: str,
    result: dict[str, Any],
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Return one canonical-JSON-bounded page without dropping or duplicating rows."""
    if type(analysis_id) is not str:
        raise ValueError("analysis_id must be a string")
    if type(result) is not dict:
        raise ValueError("Stored analysis result must be an object")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative plain integer")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"limit must be a plain integer from 1 to {MAX_PAGE}")

    count, data = _analysis_rows(result, kind)
    available = max(0, count - offset)
    row_limit = min(limit, available)

    def build_response(included: int) -> dict[str, Any]:
        end = offset + included
        next_offset = end if end < count else None
        if kind == "peaks":
            page: list | dict = data[offset:end]
        else:
            page = {key: values[offset:end] for key, values in data.items()}
        return {
            "analysis_id": analysis_id,
            "kind": kind,
            "offset": offset,
            "count": count,
            "next_offset": next_offset,
            "data": page,
        }

    empty_response = build_response(0)
    if len(_compact_json_bytes(empty_response, "Analysis page envelope")) > MAX_RESPONSE_BYTES:
        raise ValueError("Analysis page envelope exceeds the 48000-byte compact response limit")
    if row_limit == 0:
        return empty_response

    candidate = build_response(row_limit)
    if len(_compact_json_bytes(candidate, "Analysis page")) <= MAX_RESPONSE_BYTES:
        return candidate

    lower, upper = 1, row_limit - 1
    selected: dict[str, Any] | None = None
    while lower <= upper:
        included = (lower + upper) // 2
        candidate = build_response(included)
        if len(_compact_json_bytes(candidate, "Analysis page")) <= MAX_RESPONSE_BYTES:
            selected = candidate
            lower = included + 1
        else:
            upper = included - 1
    if selected is None:
        raise ValueError("A single analysis row exceeds the 48000-byte compact response limit")
    return selected


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    marker = _WARNING_TRUNCATION_MARKER
    marker_bytes = len(marker.encode("utf-8"))
    prefix_budget = maximum_bytes - marker_bytes
    prefix: list[str] = []
    used = 0
    for character in value:
        character_bytes = len(character.encode("utf-8"))
        if used + character_bytes > prefix_budget:
            break
        prefix.append(character)
        used += character_bytes
    return "".join(prefix) + marker


def bound_backend_warnings(warnings: Any) -> list[str]:
    """Normalize and cap backend warning count and UTF-8 size with visible markers."""
    if warnings is None:
        items: list[Any] = []
    elif isinstance(warnings, str):
        items = [warnings]
    elif type(warnings) is list:
        items = warnings
    else:
        raise ValueError("backend warnings must be a string, a list of strings, or null")
    if any(not isinstance(item, str) for item in items):
        raise ValueError("backend warnings must be a string, a list of strings, or null")

    if len(items) <= MAX_BACKEND_WARNINGS:
        return [_truncate_utf8(item, MAX_WARNING_UTF8_BYTES) for item in items]

    retained = [
        _truncate_utf8(item, MAX_WARNING_UTF8_BYTES) for item in items[: MAX_BACKEND_WARNINGS - 1]
    ]
    omitted = len(items) - len(retained)
    marker = f"{omitted} additional warning messages omitted"
    return [*retained, _truncate_utf8(marker, MAX_WARNING_UTF8_BYTES)]
