"""Pure, bounded parser for mapped analytical outcome tables."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .experiments import ResultSpec
from .result_table_models import (
    TABLE_FIELDS,
    TABLE_NUMERIC_FIELDS,
    TABLE_REQUIRED_FIELDS,
    TABLE_STRING_FIELDS,
    ResultTablePlan,
    ResultTableRow,
    ResultTableSpec,
)
from .storage import canonical, sha256

MAX_SOURCE_BYTES = 64_000
MAX_DATA_RECORDS = 50
MAX_MAPPING_BYTES = 8_000
MAX_PLAN_BYTES = 48_000
MAX_PREVIEW_PAGE = 20
_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_REVISION_RE = re.compile(r"[1-9][0-9]*\Z")


def _fail(message: str) -> None:
    raise ValueError(message)


def _source_bytes(spec: ResultTableSpec) -> bytes:
    if len(spec.csv_text) > MAX_SOURCE_BYTES:
        _fail("Result table source exceeds its byte limit")
    try:
        source = spec.csv_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Result table source is not valid UTF-8") from exc
    if b"\x00" in source:
        _fail("Result table source must not contain NUL bytes")
    if len(source) > MAX_SOURCE_BYTES:
        _fail("Result table source exceeds its byte limit")
    return source


def _validate_mapping(spec: ResultTableSpec) -> str:
    mapping = spec.mapping.model_dump(mode="json")
    encoded = canonical(mapping).encode("utf-8")
    if len(encoded) > MAX_MAPPING_BYTES:
        _fail("Result table mapping exceeds its byte limit")
    fields = set(mapping["columns"]) | set(mapping["constants"])
    if not TABLE_REQUIRED_FIELDS <= fields:
        missing = ", ".join(sorted(TABLE_REQUIRED_FIELDS - fields))
        _fail(f"Result table mapping is missing required fields: {missing}")
    if len(set(mapping["columns"].values())) != len(mapping["columns"]):
        _fail("Result table column bindings must be one-to-one")
    return canonical(mapping)


def _validate_headers(headers: list[str]) -> tuple[str, ...]:
    if not 1 <= len(headers) <= 64:
        _fail("Result table must contain 1 to 64 columns")
    for header in headers:
        if not header or len(header) > 200:
            _fail("Result table headers must be nonempty and at most 200 characters")
    if len(set(headers)) != len(headers):
        _fail("Result table headers must be unique")
    return tuple(headers)


def _parse_numeric(text: str, field_name: str) -> int | float:
    if len(text) > 100 or not _NUMBER_RE.fullmatch(text):
        _fail(f"Invalid numeric value for {field_name}")
    try:
        decimal_value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid numeric value for {field_name}") from exc
    if "." not in text and "e" not in text.lower():
        try:
            value: int | float = int(text)
        except ValueError as exc:
            raise ValueError(f"Invalid numeric value for {field_name}") from exc
    else:
        value = float(text)
    if isinstance(value, float) and (
        not math.isfinite(value) or (decimal_value != 0 and value == 0)
    ):
        _fail(f"Numeric value for {field_name} is not finite or underflows binary64")
    return value


def _constant_value(field_name: str, value: Any) -> Any:
    if value is None:
        if field_name in TABLE_REQUIRED_FIELDS:
            _fail(f"Required result table field {field_name!r} cannot be null")
        return None
    if field_name == "expected_attempt_revision":
        if type(value) is not int or value < 1:
            _fail("expected_attempt_revision must be a positive integer")
        return value
    if field_name in TABLE_NUMERIC_FIELDS:
        if type(value) not in {int, float} or isinstance(value, bool):
            _fail(f"Constant for {field_name!r} must be numeric")
        if isinstance(value, float) and not math.isfinite(value):
            _fail(f"Constant for {field_name!r} must be finite")
        return value
    if field_name in TABLE_STRING_FIELDS and type(value) is not str:
        _fail(f"Constant for {field_name!r} must be a string")
    if field_name == "attempt_id" and (not value.strip() or len(value) > 100):
        _fail("attempt_id must contain 1 to 100 characters")
    return value


def _cell_value(field_name: str, text: str) -> Any:
    if text == "":
        if field_name in TABLE_REQUIRED_FIELDS:
            _fail(f"Required result table field {field_name!r} cannot be empty")
        return None
    if field_name == "expected_attempt_revision":
        if len(text) > 100 or not _REVISION_RE.fullmatch(text):
            _fail("expected_attempt_revision must be a positive decimal integer")
        return int(text)
    if field_name in TABLE_NUMERIC_FIELDS:
        return _parse_numeric(text, field_name)
    if field_name in TABLE_STRING_FIELDS:
        if field_name == "attempt_id" and (not text.strip() or len(text) > 100):
            _fail("attempt_id must contain 1 to 100 characters")
        return text
    _fail(f"Unknown result table field: {field_name}")


def _normalized_plan_bytes(plan: ResultTablePlan) -> bytes:
    rows = []
    for row in plan.rows:
        item = row.as_dict()
        item["row_sha256"] = row.row_sha256
        rows.append(item)
    return canonical(
        {"metadata": plan.metadata(), "input_sha256": plan.input_sha256, "rows": rows}
    ).encode("utf-8")


def parse_result_table(spec: ResultTableSpec) -> ResultTablePlan:
    """Parse and validate one bounded mapped CSV table without I/O or registry access."""
    if not isinstance(spec, ResultTableSpec):
        raise TypeError("parse_result_table requires a ResultTableSpec")
    source_bytes = _source_bytes(spec)
    mapping_json = _validate_mapping(spec)
    mapping = json.loads(mapping_json)
    parse_text = spec.csv_text[1:] if spec.csv_text.startswith("\ufeff") else spec.csv_text
    try:
        reader = csv.reader(
            io.StringIO(parse_text, newline=""), delimiter=spec.delimiter, strict=True
        )
        try:
            headers_raw = next(reader)
        except StopIteration as exc:
            raise ValueError("Result table must contain a header and data records") from exc
        headers = _validate_headers(headers_raw)
        header_index = {header: index for index, header in enumerate(headers)}
        columns = mapping["columns"]
        for field_name, header in columns.items():
            if header not in header_index:
                _fail(f"Mapped result table header {header!r} is not present")
        unmapped = tuple(header for header in headers if header not in set(columns.values()))
        rows: list[ResultTableRow] = []
        revisions: dict[str, int] = {}
        final_keys: set[tuple[str, str, str | None]] = set()
        previous_line = reader.line_num
        for values in reader:
            record_number = len(rows) + 2
            line_start = previous_line + 1
            line_end = reader.line_num
            previous_line = line_end
            if len(rows) >= MAX_DATA_RECORDS:
                _fail("Result table exceeds its record limit")
            if not values:
                _fail(f"Blank result table record at {record_number}")
            if len(values) != len(headers):
                _fail(f"Result table record {record_number} has the wrong number of fields")
            fields: dict[str, Any] = {}
            for field_name in TABLE_FIELDS:
                if field_name in columns:
                    value = _cell_value(field_name, values[header_index[columns[field_name]]])
                elif field_name in mapping["constants"]:
                    value = _constant_value(field_name, mapping["constants"][field_name])
                else:
                    continue
                if value is not None:
                    fields[field_name] = value
            if "attempt_id" not in fields or "expected_attempt_revision" not in fields:
                _fail(f"Result table record {record_number} is missing attempt identity")
            attempt_id = fields.pop("attempt_id")
            expected_revision = fields.pop("expected_attempt_revision")
            try:
                result = ResultSpec.model_validate(fields)
            except Exception as exc:
                raise ValueError(f"Invalid result table outcome at record {record_number}") from exc
            prior_revision = revisions.setdefault(attempt_id, expected_revision)
            if prior_revision != expected_revision:
                _fail(f"Result table attempt {attempt_id!r} has inconsistent revisions")
            final_key = (attempt_id, result.metric, result.analyte_reference)
            if final_key in final_keys:
                _fail("Result table contains a duplicate final outcome")
            final_keys.add(final_key)
            result_json = canonical(result.model_dump(mode="json"))
            row_dict = {
                "ordinal": len(rows),
                "record_number": record_number,
                "line_start": line_start,
                "line_end": line_end,
                "attempt_id": attempt_id,
                "expected_attempt_revision": expected_revision,
                "result": json.loads(result_json),
            }
            rows.append(
                ResultTableRow(
                    ordinal=len(rows),
                    record_number=record_number,
                    line_start=line_start,
                    line_end=line_end,
                    attempt_id=attempt_id,
                    expected_attempt_revision=expected_revision,
                    result_json=result_json,
                    row_sha256=sha256(row_dict),
                )
            )
    except csv.Error as exc:
        raise ValueError("Result table is not valid strict CSV") from exc
    if not rows:
        _fail("Result table must contain at least one data record")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    mapping_sha256 = sha256(mapping)
    provisional = ResultTablePlan(
        source_name=spec.source_name,
        source_bytes=source_bytes,
        delimiter=spec.delimiter,
        mapping_json=mapping_json,
        headers=headers,
        unmapped_columns=unmapped,
        rows=tuple(rows),
        source_sha256=source_sha256,
        mapping_sha256=mapping_sha256,
        input_sha256="",
    )
    input_sha256 = sha256(
        {"metadata": provisional.metadata(), "rows": [row.as_dict() for row in provisional.rows]}
    )
    plan = ResultTablePlan(
        source_name=provisional.source_name,
        source_bytes=provisional.source_bytes,
        delimiter=provisional.delimiter,
        mapping_json=provisional.mapping_json,
        headers=provisional.headers,
        unmapped_columns=provisional.unmapped_columns,
        rows=provisional.rows,
        source_sha256=provisional.source_sha256,
        mapping_sha256=provisional.mapping_sha256,
        input_sha256=input_sha256,
    )
    if len(_normalized_plan_bytes(plan)) > MAX_PLAN_BYTES:
        _fail("Normalized result table plan exceeds its byte limit")
    return plan


def preview_result_table(spec: ResultTableSpec, offset: int = 0, limit: int = 10) -> dict[str, Any]:
    """Return a bounded pure preview page from a parsed result table."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("Result table offset must be a nonnegative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PREVIEW_PAGE:
        raise ValueError("Result table preview limit must be between 1 and 20")
    plan = parse_result_table(spec)
    if offset > len(plan.rows):
        raise ValueError("Result table offset exceeds its row count")
    rows = []
    for row in plan.rows[offset : offset + limit]:
        item = row.as_dict()
        item["row_sha256"] = row.row_sha256
        rows.append(item)
    next_offset = offset + limit if offset + limit < len(plan.rows) else None
    return {
        **plan.metadata(),
        "input_sha256": plan.input_sha256,
        "offset": offset,
        "limit": limit,
        "total_count": len(plan.rows),
        "next_offset": next_offset,
        "rows": rows,
    }


__all__ = [
    "MAX_DATA_RECORDS",
    "MAX_MAPPING_BYTES",
    "MAX_PLAN_BYTES",
    "MAX_PREVIEW_PAGE",
    "MAX_SOURCE_BYTES",
    "parse_result_table",
    "preview_result_table",
]
