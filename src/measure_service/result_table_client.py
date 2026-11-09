"""Collect and verify a complete result-table import through public MCP pages."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime
from typing import Any

from .bounded_json import (
    MAX_JSON_DEPTH as MAX_JSON_DEPTH,
)
from .bounded_json import (
    MAX_JSON_NODES as MAX_JSON_NODES,
)
from .bounded_json import (
    MAX_JSON_NUMBER_CHARS as MAX_JSON_NUMBER_CHARS,
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

MAX_SOURCE_BYTES = 64_000
MAX_ROWS = 50
MAX_SOURCE_PAGE = 12_000
MAX_ROW_PAGE = 20
MAX_IMPORT_ID = 100
MAX_DOCUMENT_BYTES = 64_000
MAX_SOURCE_NAME = 200
MAX_TIMESTAMP_CHARS = 100
MAX_MAPPING_BYTES = 8_000
_HEX_SHA256 = set("0123456789abcdef")
_KNOWN_OUTCOME_STATUSES = frozenset(
    {
        "observed",
        "not_detected",
        "below_limit",
        "censored",
        "missing",
        "failed",
        "infeasible",
    }
)
_PLAN_METADATA_FIELDS = {
    "profile",
    "schema_version",
    "source_name",
    "delimiter",
    "source_bytes",
    "source_sha256",
    "mapping",
    "mapping_sha256",
    "headers",
    "unmapped_columns",
    "row_count",
    "attempt_count",
    "status_counts",
}
_IMPORT_FIELDS = _PLAN_METADATA_FIELDS | {
    "import_id",
    "input_sha256",
    "created_at",
    "result_bindings_sha256",
}
_SOURCE_PAGE_FIELDS = {
    "profile",
    "import_id",
    "input_sha256",
    "source_sha256",
    "offset",
    "total_bytes",
    "next_offset",
    "encoding",
    "content_base64",
}
_ROW_PAGE_FIELDS = {
    "profile",
    "import_id",
    "input_sha256",
    "offset",
    "limit",
    "total_count",
    "next_offset",
    "rows",
}
_ROW_FIELDS = {
    "ordinal",
    "record_number",
    "line_start",
    "line_end",
    "attempt_id",
    "expected_attempt_revision",
    "result",
    "row_sha256",
    "result_id",
    "result_sha256",
}
_ROW_DIGEST_FIELDS = (
    "ordinal",
    "record_number",
    "line_start",
    "line_end",
    "attempt_id",
    "expected_attempt_revision",
    "result",
)


def _fail(message: str) -> ValueError:
    return ValueError(f"Result-table import collection failed: {message}")


def _preflight_json(value: Any, label: str, *, max_bytes: int = MAX_DOCUMENT_BYTES) -> None:
    preflight_json(value, label, max_bytes=max_bytes, fail=_fail)


def _canonical(
    value: Any, label: str = "JSON value", *, max_bytes: int = MAX_DOCUMENT_BYTES
) -> str:
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
        raise _fail(f"value is not canonical JSON: {error}") from error
    if encoded_length > max_bytes:
        raise _fail(f"{label} exceeds the {max_bytes}-byte canonical JSON limit")
    return rendered


def _sha256_json(value: Any, label: str = "JSON value") -> str:
    return hashlib.sha256(_canonical(value, label).encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise _fail(f"{label} must be an integer")
    return value


def _nonempty_text(value: Any, label: str) -> str:
    if type(value) is not str or not value:
        raise _fail(f"{label} must be non-empty text")
    return value


def _record_id(value: Any, label: str) -> str:
    value = _nonempty_text(value, label)
    if len(value) > MAX_IMPORT_ID:
        raise _fail(f"{label} exceeds the record ID length bound")
    return value


def _sha256_text(value: Any, label: str) -> str:
    value = _nonempty_text(value, label)
    if len(value) != 64 or any(character not in _HEX_SHA256 for character in value):
        raise _fail(f"{label} must be a lowercase SHA-256 digest")
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(f"{label} must be an array")
    return value


def _validate_import_receipt(receipt: Any, import_id: str) -> dict[str, Any]:
    _preflight_json(receipt, "import receipt")
    receipt = _object(receipt, "import receipt")
    if set(receipt) != _IMPORT_FIELDS:
        raise _fail("import receipt fields are incomplete or unexpected")
    if receipt["profile"] != "measure-result-table-import/1":
        raise _fail("import receipt has the wrong profile")
    if receipt["schema_version"] != "1.0":
        raise _fail("import receipt has the wrong schema version")
    if receipt["import_id"] != import_id:
        raise _fail("import receipt identifies a different import")
    _sha256_text(receipt["input_sha256"], "import receipt input_sha256")
    _sha256_text(receipt["source_sha256"], "import receipt source_sha256")
    _sha256_text(receipt["mapping_sha256"], "import receipt mapping_sha256")
    _sha256_text(receipt["result_bindings_sha256"], "import receipt result_bindings_sha256")
    source_name = _nonempty_text(receipt["source_name"], "import receipt source_name")
    if len(source_name) > MAX_SOURCE_NAME:
        raise _fail("import receipt source_name exceeds the source name length bound")
    delimiter = receipt["delimiter"]
    if delimiter not in (",", ";", "\t"):
        raise _fail("import receipt delimiter is unsupported")
    source_bytes = _strict_int(receipt["source_bytes"], "import receipt source_bytes")
    if not 1 <= source_bytes <= MAX_SOURCE_BYTES:
        raise _fail("import receipt source_bytes is outside the bounded source limit")
    headers = _array(receipt["headers"], "import receipt headers")
    if (
        not headers
        or len(headers) > 64
        or any(
            type(header) is not str or not header or len(header) > MAX_SOURCE_NAME
            for header in headers
        )
    ):
        raise _fail("import receipt headers are invalid")
    if len(set(headers)) != len(headers):
        raise _fail("import receipt headers are not unique and bounded")
    unmapped_columns = _array(receipt["unmapped_columns"], "import receipt unmapped_columns")
    if len(unmapped_columns) > 64:
        raise _fail("import receipt unmapped_columns exceeds the column count bound")
    if any(
        type(value) is not str or not value or len(value) > MAX_SOURCE_NAME
        for value in unmapped_columns
    ):
        raise _fail("import receipt unmapped_columns contains an invalid column")
    if len(set(unmapped_columns)) != len(unmapped_columns):
        raise _fail("import receipt unmapped_columns is not unique")
    row_count = _strict_int(receipt["row_count"], "import receipt row_count")
    attempt_count = _strict_int(receipt["attempt_count"], "import receipt attempt_count")
    if not 1 <= row_count <= MAX_ROWS or not 1 <= attempt_count <= row_count:
        raise _fail("import receipt row or attempt count is outside the bounded limit")
    status_counts = _object(receipt["status_counts"], "import receipt status_counts")
    if any(
        type(key) is not str or type(value) is not int or value <= 0
        for key, value in status_counts.items()
    ):
        raise _fail("import receipt status_counts must contain positive integers")
    if not set(status_counts) <= _KNOWN_OUTCOME_STATUSES:
        raise _fail("import receipt status_counts contains an unknown outcome status")
    if sum(status_counts.values()) != row_count:
        raise _fail("import receipt status_counts does not sum to row_count")
    mapping = _object(receipt["mapping"], "import receipt mapping")
    if set(mapping) != {"columns", "constants"}:
        raise _fail("import receipt mapping fields are incomplete or unexpected")
    columns = _object(mapping["columns"], "import receipt mapping columns")
    constants = _object(mapping["constants"], "import receipt mapping constants")
    if any(
        type(key) is not str or type(value) is not str or not value
        for key, value in columns.items()
    ):
        raise _fail("import receipt mapping columns are invalid")
    if any(type(key) is not str for key in constants):
        raise _fail("import receipt mapping constants are invalid")
    if any(value not in headers for value in columns.values()):
        raise _fail("import receipt mapping binds a column outside the declared headers")
    if any(value not in headers for value in unmapped_columns):
        raise _fail("import receipt unmapped_columns contains a header not in the receipt")
    _canonical(mapping, "import receipt mapping", max_bytes=MAX_MAPPING_BYTES)
    created_at = _nonempty_text(receipt["created_at"], "import receipt created_at")
    if len(created_at) > MAX_TIMESTAMP_CHARS:
        raise _fail("import receipt created_at exceeds the timestamp length bound")
    try:
        timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise _fail("import receipt created_at is not an ISO timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise _fail("import receipt created_at must include a timezone offset")
    _canonical(receipt, "import receipt")
    return receipt


def _decode_source_page(
    page: Any,
    *,
    import_id: str,
    input_sha256: str,
    source_sha256: str,
    offset: int,
    total_bytes: int,
    limit: int,
) -> bytes:
    _canonical(page, "source page")
    page = _object(page, "source page")
    if set(page) != _SOURCE_PAGE_FIELDS:
        raise _fail("source page fields are incomplete or unexpected")
    if page["profile"] != "measure-result-table-source/1":
        raise _fail("source page has the wrong profile")
    if page["import_id"] != import_id or page["input_sha256"] != input_sha256:
        raise _fail("source page identifies a different import")
    if page["source_sha256"] != source_sha256:
        raise _fail("source page identifies a different source digest")
    if _strict_int(page["offset"], "source page offset") != offset:
        raise _fail("source page offset is not contiguous")
    if _strict_int(page["total_bytes"], "source page total_bytes") != total_bytes:
        raise _fail("source page total_bytes changed")
    if page["encoding"] != "base64":
        raise _fail("source page encoding is not base64")
    encoded = page["content_base64"]
    if type(encoded) is not str or len(encoded) > 64_000:
        raise _fail("source page base64 content is invalid or too large")
    try:
        chunk = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise _fail("source page is not strict base64") from error
    if base64.b64encode(chunk).decode("ascii") != encoded:
        raise _fail("source page base64 is not canonical")
    remaining = total_bytes - offset
    if not chunk and remaining:
        raise _fail("source page is empty before the terminal offset")
    if len(chunk) > min(limit, remaining):
        raise _fail("source page exceeds its requested or remaining byte bound")
    end = offset + len(chunk)
    next_offset = page["next_offset"]
    expected_next = end if end < total_bytes else None
    if next_offset != expected_next or (next_offset is not None and type(next_offset) is not int):
        raise _fail("source page continuation is inconsistent")
    return chunk


async def _collect_source(
    client: Any, receipt: dict[str, Any], import_id: str, *, limit: int = MAX_SOURCE_PAGE
) -> bytes:
    total_bytes = receipt["source_bytes"]
    offset = 0
    pages = 0
    chunks: list[bytes] = []
    while offset < total_bytes or pages == 0:
        page = await _call(
            client,
            "measurement_read_result_table_source",
            {"import_id": import_id, "offset": offset, "limit": limit},
        )
        chunk = _decode_source_page(
            page,
            import_id=import_id,
            input_sha256=receipt["input_sha256"],
            source_sha256=receipt["source_sha256"],
            offset=offset,
            total_bytes=total_bytes,
            limit=limit,
        )
        chunks.append(chunk)
        offset += len(chunk)
        pages += 1
        if pages > total_bytes + 1:
            raise _fail("source page sequence exceeded its bounded page count")
        if total_bytes == 0:
            break
    source_bytes = b"".join(chunks)
    if len(source_bytes) != total_bytes or _sha256_bytes(source_bytes) != receipt["source_sha256"]:
        raise _fail("collected source bytes do not match the receipt digest or size")
    return source_bytes


def _validate_row(
    row: Any, ordinal: int, *, row_sha256s: set[str], result_ids: set[str]
) -> dict[str, Any]:
    _canonical(row, f"result-table row {ordinal}")
    row = _object(row, f"result-table row {ordinal}")
    if set(row) != _ROW_FIELDS:
        raise _fail(f"result-table row {ordinal} fields are incomplete or unexpected")
    if _strict_int(row["ordinal"], f"row {ordinal} ordinal") != ordinal:
        raise _fail("result-table rows are not in contiguous ordinal order")
    record_number = _strict_int(row["record_number"], f"row {ordinal} record_number")
    line_start = _strict_int(row["line_start"], f"row {ordinal} line_start")
    line_end = _strict_int(row["line_end"], f"row {ordinal} line_end")
    if record_number < 2 or line_start < 1 or line_end < line_start:
        raise _fail(f"result-table row {ordinal} physical location is invalid")
    _record_id(row["attempt_id"], f"row {ordinal} attempt_id")
    revision = _strict_int(row["expected_attempt_revision"], f"row {ordinal} revision")
    if revision < 1:
        raise _fail(f"row {ordinal} revision is not positive")
    _object(row["result"], f"row {ordinal} result")
    row_digest = _sha256_text(row["row_sha256"], f"row {ordinal} row_sha256")
    result_id = _record_id(row["result_id"], f"row {ordinal} result_id")
    _sha256_text(row["result_sha256"], f"row {ordinal} result_sha256")
    if row_digest in row_sha256s:
        raise _fail(f"row {ordinal} row_sha256 is duplicated")
    if result_id in result_ids:
        raise _fail(f"row {ordinal} result_id is duplicated")
    row_without_digest = {key: row[key] for key in _ROW_DIGEST_FIELDS}
    if _sha256_json(row_without_digest) != row_digest:
        raise _fail(f"row {ordinal} row_sha256 does not match the row contents")
    row_sha256s.add(row_digest)
    result_ids.add(result_id)
    return row


async def _collect_rows(
    client: Any, receipt: dict[str, Any], import_id: str, *, limit: int = MAX_ROW_PAGE
) -> list[dict[str, Any]]:
    total_count = receipt["row_count"]
    offset = 0
    pages = 0
    rows: list[dict[str, Any]] = []
    row_sha256s: set[str] = set()
    result_ids: set[str] = set()
    result_sha256s: set[str] = set()
    row_documents_bytes = 2  # The surrounding canonical JSON array.
    normalized_documents_bytes = 2
    while offset < total_count:
        page = await _call(
            client,
            "measurement_read_result_table_rows",
            {"import_id": import_id, "offset": offset, "limit": limit},
        )
        _canonical(page, "rows page")
        page = _object(page, "rows page")
        if set(page) != _ROW_PAGE_FIELDS:
            raise _fail("rows page fields are incomplete or unexpected")
        if page["profile"] != "measure-result-table-rows/1":
            raise _fail("rows page has the wrong profile")
        if page["import_id"] != import_id or page["input_sha256"] != receipt["input_sha256"]:
            raise _fail("rows page identifies a different import")
        if _strict_int(page["offset"], "rows page offset") != offset:
            raise _fail("rows page offset is not contiguous")
        if _strict_int(page["limit"], "rows page limit") != limit:
            raise _fail("rows page limit changed")
        if _strict_int(page["total_count"], "rows page total_count") != total_count:
            raise _fail("rows page total_count changed")
        page_rows = _array(page["rows"], "rows page rows")
        remaining = total_count - offset
        if not page_rows or len(page_rows) > min(limit, remaining):
            raise _fail("rows page has an invalid number of rows")
        for row in page_rows:
            validated_row = _validate_row(
                row, len(rows), row_sha256s=row_sha256s, result_ids=result_ids
            )
            if validated_row["result_sha256"] in result_sha256s:
                raise _fail("result-table result_sha256 is duplicated")
            result_sha256s.add(validated_row["result_sha256"])
            row_bytes = len(
                _canonical(validated_row, f"result-table row {len(rows)}").encode("utf-8")
            )
            normalized_row = {key: validated_row[key] for key in _ROW_DIGEST_FIELDS}
            normalized_bytes = len(
                _canonical(
                    normalized_row,
                    f"normalized result-table row {len(rows)}",
                ).encode("utf-8")
            )
            separator = 1 if rows else 0
            if row_documents_bytes + separator + row_bytes > MAX_DOCUMENT_BYTES:
                raise _fail("collected result-table rows exceed the canonical JSON byte limit")
            if normalized_documents_bytes + separator + normalized_bytes > MAX_DOCUMENT_BYTES:
                raise _fail(
                    "collected normalized result-table rows exceed the canonical JSON byte limit"
                )
            row_documents_bytes += separator + row_bytes
            normalized_documents_bytes += separator + normalized_bytes
            rows.append(validated_row)
        next_offset = page["next_offset"]
        expected_next = offset + len(page_rows)
        if next_offset != (expected_next if expected_next < total_count else None) or (
            next_offset is not None and type(next_offset) is not int
        ):
            raise _fail("rows page continuation is inconsistent")
        offset = expected_next
        pages += 1
        if pages > total_count:
            raise _fail("rows page sequence exceeded its bounded page count")
    if len(rows) != total_count:
        raise _fail("collected row count does not match the receipt")
    bindings = [
        {
            "ordinal": row["ordinal"],
            "row_sha256": row["row_sha256"],
            "result_id": row["result_id"],
            "result_sha256": row["result_sha256"],
        }
        for row in rows
    ]
    if _sha256_json(bindings) != receipt["result_bindings_sha256"]:
        raise _fail("result ID/digest bindings do not match the import receipt")
    return rows


def _make_plan(receipt: dict[str, Any], csv_text: str) -> Any:
    try:
        from .result_table_models import ResultTableMapping, ResultTableSpec
        from .result_table_parser import parse_result_table
    except ImportError as error:  # pragma: no cover - parser is a sibling component
        raise _fail("result-table parser component is unavailable") from error
    try:
        mapping = ResultTableMapping(**receipt["mapping"])
        spec = ResultTableSpec(
            source_name=receipt["source_name"],
            csv_text=csv_text,
            mapping=mapping,
            delimiter=receipt["delimiter"],
        )
        return parse_result_table(spec)
    except Exception as error:
        raise _fail(f"raw source could not be parsed into a normalized plan: {error}") from error


def _model_json(value: Any, label: str) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
    elif isinstance(value, dict):
        dumped = value
    else:
        raise _fail(f"{label} is not a JSON model")
    return _object(dumped, label)


def _row_attr(row: Any, name: str, label: str) -> Any:
    if isinstance(row, dict):
        if name not in row:
            raise _fail(f"{label} is missing {name!r}")
        return row[name]
    if not hasattr(row, name):
        raise _fail(f"{label} is missing {name!r}")
    return getattr(row, name)


def _verify_plan(
    receipt: dict[str, Any], source_bytes: bytes, rows: list[dict[str, Any]], plan: Any
) -> None:
    if getattr(plan, "source_bytes", None) != source_bytes:
        raise _fail("normalized plan source bytes differ from the public source pages")
    if getattr(plan, "source_sha256", None) != receipt["source_sha256"]:
        raise _fail("normalized plan source_sha256 differs from the import receipt")
    if getattr(plan, "mapping_sha256", None) != receipt["mapping_sha256"]:
        raise _fail("normalized plan mapping_sha256 differs from the import receipt")
    if getattr(plan, "input_sha256", None) != receipt["input_sha256"]:
        raise _fail("normalized plan input_sha256 differs from the import receipt")
    if (
        getattr(plan, "source_name", None) != receipt["source_name"]
        or getattr(plan, "delimiter", None) != receipt["delimiter"]
    ):
        raise _fail("normalized plan source identity differs from the import receipt")
    if tuple(getattr(plan, "headers", ())) != tuple(receipt["headers"]):
        raise _fail("normalized plan headers differ from the import receipt")
    if tuple(getattr(plan, "unmapped_columns", ())) != tuple(receipt["unmapped_columns"]):
        raise _fail("normalized plan unmapped columns differ from the import receipt")
    mapping_json = getattr(plan, "mapping_json", None)
    if mapping_json != _canonical(receipt["mapping"]):
        raise _fail("normalized plan mapping JSON is not canonical receipt mapping")
    metadata = plan.metadata()
    metadata = _object(metadata, "normalized plan metadata")
    expected_metadata = {key: receipt[key] for key in _PLAN_METADATA_FIELDS}
    expected_metadata["profile"] = "measure-result-table-plan/1"
    if _canonical(metadata) != _canonical(expected_metadata):
        raise _fail("normalized plan metadata does not match the import receipt")
    plan_rows = tuple(getattr(plan, "rows", ()))
    if len(plan_rows) != len(rows) or len(rows) != receipt["row_count"]:
        raise _fail("normalized plan row count does not match the public rows")
    for ordinal, (plan_row, public_row) in enumerate(zip(plan_rows, rows, strict=True)):
        for field in (
            "ordinal",
            "record_number",
            "line_start",
            "line_end",
            "attempt_id",
            "expected_attempt_revision",
            "result",
        ):
            if field == "result":
                result_spec = getattr(plan_row, "result_spec", None)
                if callable(result_spec):
                    expected = _model_json(result_spec(), f"normalized row {ordinal} result")
                else:
                    expected = _model_json(
                        _row_attr(plan_row, field, f"normalized row {ordinal}"),
                        f"normalized row {ordinal} result",
                    )
            else:
                expected = _row_attr(plan_row, field, f"normalized row {ordinal}")
            if _canonical(expected) != _canonical(public_row[field]):
                raise _fail(f"normalized row {ordinal} field {field!r} differs from the public row")
        plan_row_digest = _row_attr(plan_row, "row_sha256", f"normalized row {ordinal}")
        if plan_row_digest != public_row["row_sha256"]:
            raise _fail(f"normalized row {ordinal} row_sha256 differs from the public row")
        result_json = _row_attr(plan_row, "result_json", f"normalized row {ordinal}")
        if result_json != _canonical(public_row["result"]):
            raise _fail(f"normalized row {ordinal} result JSON is not canonical")


async def collect_result_table_import(client: Any, import_id: str) -> dict[str, Any]:
    """Collect one complete owned import using only public MCP read tools.

    The source text and row pages are independently verified against a fresh
    parser plan.  This does not read a local path, open SQLite, call R, or
    execute a write tool.
    """

    if type(import_id) is not str or not import_id.strip() or len(import_id) > MAX_IMPORT_ID:
        raise ValueError(f"import_id must be a nonempty ID of at most {MAX_IMPORT_ID} characters")
    receipt = _validate_import_receipt(
        await _call(client, "measurement_get_result_table_import", {"import_id": import_id}),
        import_id,
    )
    source_bytes = await _collect_source(client, receipt, import_id)
    try:
        csv_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _fail("source bytes are not valid UTF-8") from error
    rows = await _collect_rows(client, receipt, import_id)
    plan = _make_plan(receipt, csv_text)
    _verify_plan(receipt, source_bytes, rows, plan)
    return {"receipt": receipt, "csv_text": csv_text, "rows": rows}
