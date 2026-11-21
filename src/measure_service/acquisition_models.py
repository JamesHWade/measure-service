"""Bounded input and summary contracts for native acquisitions.

The native acquisition profile is deliberately represented as text members at
the service boundary.  Parsing and scientific validation stay in the trusted
worker; this module only enforces the fixed member set, UTF-8 encoding and
byte budgets needed before dispatch or persistence.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .storage import canonical, sha256

PROFILE = "measure-acquisition/1"

# ``measure`` writes one manifest and twelve payloads.  A service caller may
# choose either CSV or TSV, but may not mix extensions or add arbitrary paths.
_PAYLOAD_STEMS = (
    "samples",
    "injections",
    "channels",
    "sources",
    "curves",
    "points",
    "streams",
    "scans",
    "scan_points",
    "arrays",
    "array_axes",
    "array_points",
)
CSV_MEMBER_NAMES = ("manifest.dcf",) + tuple(f"{stem}.csv" for stem in _PAYLOAD_STEMS)
TSV_MEMBER_NAMES = ("manifest.dcf",) + tuple(f"{stem}.tsv" for stem in _PAYLOAD_STEMS)
NATIVE_MEMBER_NAMES = frozenset(
    CSV_MEMBER_NAMES + tuple(name for name in TSV_MEMBER_NAMES if name != "manifest.dcf")
)

# Keep the service request bound explicit and below the HTTP/pilot budgets;
# each member is also bounded so one text value cannot consume the whole
# request budget by itself.
MAX_ACQUISITION_BYTES = 4_000_000
MAX_MEMBER_BYTES = 4_000_000
MAX_SUMMARY_BYTES = 64 * 1024
MAX_SUMMARY_ITEMS = 256


def acquisition_digest(files: Mapping[str, str]) -> str:
    """Return a deterministic digest for the exact native member texts.

    JSON canonicalization provides unambiguous member boundaries and sorted
    names.  It preserves line endings and Unicode code points in the text
    values before UTF-8 encoding; callers do not need to normalize or rewrite
    the native payload.
    """

    return sha256(dict(files))


def _member_names_for(files: Mapping[str, str]) -> tuple[str, ...]:
    names = set(files)
    csv_names = set(CSV_MEMBER_NAMES)
    tsv_names = set(TSV_MEMBER_NAMES)
    if names == csv_names:
        return CSV_MEMBER_NAMES
    if names == tsv_names:
        return TSV_MEMBER_NAMES
    unknown = sorted(name for name in names if name not in NATIVE_MEMBER_NAMES)
    if unknown:
        raise ValueError("Native acquisition contains unsupported member names")
    raise ValueError("Native acquisition must contain exactly one complete CSV or TSV member set")


def validate_files(files: Mapping[str, str]) -> dict[str, str]:
    """Validate and copy a complete fixed native bundle mapping.

    The returned dictionary contains the original text values without newline
    conversion.  Filesystem paths, bytes and archive containers are rejected by
    the fixed-key and strict-text checks.
    """

    if not isinstance(files, Mapping) or isinstance(files, (str, bytes, bytearray)):
        raise ValueError("files must be a mapping of fixed member names to UTF-8 text")
    if len(files) != len(CSV_MEMBER_NAMES):
        raise ValueError("Native acquisition must contain exactly 13 fixed members")
    if any(type(name) is not str for name in files):
        raise ValueError("Native acquisition member names must be text")
    names = _member_names_for(files)
    normalized: dict[str, str] = {}
    total = 0
    for name in names:
        value = files[name]
        if type(value) is not str:
            raise ValueError("Native acquisition members must be UTF-8 text")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("Native acquisition members must contain valid UTF-8 text") from error
        size = len(encoded)
        if size > MAX_MEMBER_BYTES:
            raise ValueError("A native acquisition member exceeds the byte limit")
        total += size
        if total > MAX_ACQUISITION_BYTES:
            raise ValueError("Native acquisition exceeds the total byte limit")
        normalized[name] = value
    return normalized


def validate_summary(summary: Any) -> dict[str, Any]:
    """Validate a worker inspection result before it enters SQLite."""

    if not isinstance(summary, Mapping):
        raise ValueError("The acquisition worker returned an invalid summary")
    try:
        serialized = canonical(dict(summary))
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as error:
        raise ValueError("The acquisition worker returned a non-JSON summary") from error
    try:
        encoded = serialized.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("The acquisition worker returned a non-UTF-8 summary") from error
    if len(encoded) > MAX_SUMMARY_BYTES:
        # Array discovery is an optional preview. An input that fit the original
        # summary limit must still register after adding the preview.
        metadata = summary.get("summary")
        if isinstance(metadata, dict) and "arrays" in metadata:
            trimmed = {key: value for key, value in metadata.items() if key != "arrays"}
            return validate_summary({**summary, "summary": trimmed})
        raise ValueError("The acquisition summary exceeds the byte limit")
    # A bounded object should also have bounded top-level fan-out even when its
    # values are compact.  The worker remains responsible for semantic checks.
    if len(summary) > MAX_SUMMARY_ITEMS:
        raise ValueError("The acquisition summary contains too many fields")
    return dict(summary)


__all__ = [
    "CSV_MEMBER_NAMES",
    "MAX_ACQUISITION_BYTES",
    "MAX_MEMBER_BYTES",
    "MAX_SUMMARY_BYTES",
    "NATIVE_MEMBER_NAMES",
    "PROFILE",
    "TSV_MEMBER_NAMES",
    "acquisition_digest",
    "validate_files",
    "validate_summary",
]
