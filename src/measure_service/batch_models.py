"""Strict declarations used by the durable chromatogram batch registry.

The models in this module describe a request to process already-owned
datasets.  They deliberately contain no arrays and make no claim about
sample identity or internal-standard assignment: those facts remain the
caller's declaration and the immutable source records remain authoritative.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from .analysis_models import ChromatogramMethod, StrictModel

SCHEMA_VERSION = "1.0"
PROFILE = "measure-chromatogram-batch/1"
MAX_BATCH_ITEMS = 32
MAX_BATCH_POINTS = 500_000
MAX_MANIFEST_BYTES = 256 * 1024
MAX_INTERNAL_STANDARD_REFERENCES = 16
MAX_INTERNAL_STANDARD_REFERENCE_BYTES = 2_000

BatchRole = Literal["unknown", "blank", "standard", "qc", "reference"]


class BatchInput(StrictModel):
    """One caller-selected owned dataset/trace and its declarations."""

    dataset_id: str = Field(min_length=1, max_length=100)
    trace_id: str = Field(min_length=1, max_length=100)
    # ``unknown`` is explicit: omission must not silently turn a caller's
    # missing declaration into an asserted sample role.
    role: BatchRole
    internal_standard_references: list[str] = Field(
        default_factory=list,
        max_length=MAX_INTERNAL_STANDARD_REFERENCES,
        description=(
            "Caller-declared labels only; these labels do not establish an internal-standard assignment."
        ),
    )

    @field_validator("dataset_id", "trace_id")
    @classmethod
    def identifiers_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dataset_id and trace_id must be nonblank")
        return value

    @field_validator("internal_standard_references")
    @classmethod
    def validate_internal_standard_references(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("internal_standard_references must be unique")
        for label in value:
            if not label.strip():
                raise ValueError("internal_standard_references labels must be nonblank")
            if len(label.encode("utf-8")) > 200:
                raise ValueError("internal_standard_references labels are too long")
        if (
            sum(len(label.encode("utf-8")) for label in value)
            > MAX_INTERNAL_STANDARD_REFERENCE_BYTES
        ):
            raise ValueError("internal_standard_references exceed their byte limit")
        return value


class BatchSpec(StrictModel):
    """Versioned, bounded chromatogram batch declaration."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    profile: Literal["measure-chromatogram-batch/1"] = PROFILE
    title: str = Field(min_length=1, max_length=200)
    method: ChromatogramMethod
    inputs: list[BatchInput] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)

    @field_validator("title")
    @classmethod
    def title_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Batch title must be nonblank")
        return value

    @model_validator(mode="after")
    def validate_inputs(self) -> BatchSpec:
        selections = [(item.dataset_id, item.trace_id) for item in self.inputs]
        if len(set(selections)) != len(selections):
            raise ValueError("Batch inputs must select each dataset/trace only once")
        return self
