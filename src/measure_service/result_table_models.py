"""Immutable value objects for mapped analytical outcome tables."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from .experiments import RecordModel, ResultSpec
from .storage import canonical

TABLE_REQUIRED_FIELDS = frozenset(
    {"attempt_id", "expected_attempt_revision", "metric", "status", "unit"}
)
TABLE_RESULT_FIELDS = frozenset(ResultSpec.model_fields)
TABLE_FIELDS = TABLE_REQUIRED_FIELDS | TABLE_RESULT_FIELDS
TABLE_NUMERIC_FIELDS = frozenset({"value", "lower_bound", "upper_bound"})
TABLE_STRING_FIELDS = frozenset(
    TABLE_RESULT_FIELDS - TABLE_NUMERIC_FIELDS - {"status"}
) | frozenset({"attempt_id", "metric", "status", "unit"})
ResultTableField = Literal[
    "attempt_id",
    "expected_attempt_revision",
    "metric",
    "status",
    "unit",
    "analyte_reference",
    "value",
    "lower_bound",
    "upper_bound",
    "analysis_id",
    "summary_field",
    "supersedes_result_id",
    "explanation",
]
ResultTableConstant = StrictStr | StrictInt | StrictFloat | None


class ResultTableMapping(RecordModel):
    """Field bindings and native constants used by a result-table parser."""

    columns: dict[ResultTableField, StrictStr]
    constants: dict[ResultTableField, ResultTableConstant] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mapping(self) -> ResultTableMapping:
        if any(not header for header in self.columns.values()):
            raise ValueError("Column bindings must use nonempty headers")
        if len(set(self.columns.values())) != len(self.columns):
            raise ValueError("Result table column bindings must be one-to-one")
        if set(self.columns) & set(self.constants):
            raise ValueError("Result table columns and constants must be disjoint")
        try:
            encoded = canonical(self.model_dump(mode="json")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("Result table mapping is not canonical JSON") from exc
        if len(encoded) > 8_000:
            raise ValueError("Result table mapping exceeds its byte limit")
        return self


class ResultTableSpec(BaseModel):
    """A caller-declared CSV source and its exact field mapping."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source_name: StrictStr = Field(min_length=1, max_length=200)
    csv_text: StrictStr = Field(max_length=64_000)
    mapping: ResultTableMapping
    delimiter: Literal[",", ";", "\t"] = ","

    @model_validator(mode="after")
    def validate_source_name(self) -> ResultTableSpec:
        if not self.source_name.strip():
            raise ValueError("Result table source_name must not be blank")
        return self


@dataclass(frozen=True, slots=True)
class ResultTableRow:
    """One normalized outcome row with its source location and identity."""

    ordinal: int
    record_number: int
    line_start: int
    line_end: int
    attempt_id: str
    expected_attempt_revision: int
    result_json: str
    row_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "record_number": self.record_number,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "attempt_id": self.attempt_id,
            "expected_attempt_revision": self.expected_attempt_revision,
            "result": json.loads(self.result_json),
        }

    def result_spec(self) -> ResultSpec:
        return ResultSpec.model_validate(json.loads(self.result_json))


@dataclass(frozen=True, slots=True)
class ResultTablePlan:
    """Complete immutable normalized result-table parse."""

    source_name: str
    source_bytes: bytes
    delimiter: str
    mapping_json: str
    headers: tuple[str, ...]
    unmapped_columns: tuple[str, ...]
    rows: tuple[ResultTableRow, ...]
    source_sha256: str
    mapping_sha256: str
    input_sha256: str

    def metadata(self) -> dict[str, Any]:
        status_counts: dict[str, int] = {}
        attempts: set[str] = set()
        for row in self.rows:
            attempts.add(row.attempt_id)
            status = row.result_spec().status
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "profile": "measure-result-table-plan/1",
            "schema_version": "1.0",
            "source_name": self.source_name,
            "delimiter": self.delimiter,
            "source_bytes": len(self.source_bytes),
            "source_sha256": self.source_sha256,
            "mapping": json.loads(self.mapping_json),
            "mapping_sha256": self.mapping_sha256,
            "headers": list(self.headers),
            "unmapped_columns": list(self.unmapped_columns),
            "row_count": len(self.rows),
            "attempt_count": len(attempts),
            "status_counts": status_counts,
        }


__all__ = [
    "ResultTableMapping",
    "ResultTablePlan",
    "ResultTableRow",
    "ResultTableSpec",
]
