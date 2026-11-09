"""Bound canonical JSON trees before serializers allocate their output."""

from __future__ import annotations

import math
from typing import Any, Callable

MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000
MAX_JSON_STRING_CHARS = 64_000
MAX_JSON_STRING_BYTES = 64_000
MAX_JSON_NUMBER_CHARS = 1_000
MAX_JSON_INTEGER_BITS = MAX_JSON_NUMBER_CHARS * 4


def _check_json_string(value: str, label: str, fail: Callable[[str], ValueError]) -> int:
    if len(value) > MAX_JSON_STRING_CHARS:
        raise fail(f"{label} contains a string beyond the JSON string bound")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise fail(f"{label} contains invalid Unicode text") from error
    if encoded_length > MAX_JSON_STRING_BYTES:
        raise fail(f"{label} contains a string beyond the JSON byte bound")
    # ensure_ascii=False still escapes quotes, backslashes, controls and the
    # six short JSON control escapes. Count those bytes without materializing
    # an escaped copy of the string.
    escaped_extra = 0
    for character in value:
        if character in ('"', "\\") or character in "\b\t\n\f\r":
            escaped_extra += 1
        elif ord(character) < 0x20:
            escaped_extra += 5
    return encoded_length + escaped_extra + 2


def preflight_json(
    value: Any,
    label: str,
    *,
    max_bytes: int,
    fail: Callable[[str], ValueError],
) -> None:
    """Reject an overbudget/non-JSON tree before canonical serialization.

    ``fail`` supplies the caller's bounded error type and message prefix. The
    walk is iterative and tracks the exact compact ``ensure_ascii=False`` JSON
    UTF-8 size, including escaped control characters and JSON punctuation.
    """

    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 1
    document_bytes = 0

    def charge(size: int) -> None:
        nonlocal document_bytes
        document_bytes += size
        if document_bytes > max_bytes:
            raise fail(f"{label} exceeds the {max_bytes}-byte canonical JSON limit")

    while stack:
        current, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise fail(f"{label} exceeds the JSON nesting bound")
        if type(current) is dict:
            if nodes + len(current) > MAX_JSON_NODES:
                raise fail(f"{label} exceeds the JSON node bound")
            charge(2 + max(0, len(current) - 1))
            for key, child in current.items():
                if type(key) is not str:
                    raise fail(f"{label} contains a non-text JSON object key")
                charge(_check_json_string(key, label, fail) + 1)
                nodes += 1
                stack.append((child, depth + 1))
        elif type(current) is list:
            if nodes + len(current) > MAX_JSON_NODES:
                raise fail(f"{label} exceeds the JSON node bound")
            charge(2 + max(0, len(current) - 1))
            for child in reversed(current):
                nodes += 1
                stack.append((child, depth + 1))
        elif type(current) is str:
            charge(_check_json_string(current, label, fail))
        elif current is None:
            charge(4)
        elif type(current) is bool:
            charge(4 if current else 5)
        elif type(current) is int:
            if current.bit_length() > MAX_JSON_INTEGER_BITS:
                raise fail(f"{label} contains an oversized JSON integer")
            number_bytes = len(str(current))
            if number_bytes > MAX_JSON_NUMBER_CHARS:
                raise fail(f"{label} contains an oversized JSON integer")
            charge(number_bytes)
        elif type(current) is float:
            if not math.isfinite(current):
                raise fail(f"{label} contains a non-finite JSON number")
            charge(len(repr(current)))
        else:
            raise fail(f"{label} contains a value that is not canonical JSON")


__all__ = [
    "MAX_JSON_DEPTH",
    "MAX_JSON_INTEGER_BITS",
    "MAX_JSON_NODES",
    "MAX_JSON_NUMBER_CHARS",
    "MAX_JSON_STRING_BYTES",
    "MAX_JSON_STRING_CHARS",
    "preflight_json",
]
