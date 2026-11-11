"""Shared bounded analytical inputs for preview and batch processing."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TRACES = 32
MAX_POINTS = 50_000
MAX_DATASET_POINTS = 150_000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TraceMetadata(StrictModel):
    trace_id: str = Field(min_length=1, max_length=100)
    sample_id: str = Field(min_length=1, max_length=100)
    injection_id: str = Field(min_length=1, max_length=100)
    channel: str = Field(min_length=1, max_length=100)
    separation: Literal["SEC", "LC", "GC", "CE", "other"]
    detector: str = Field(min_length=1, max_length=100)
    axis_unit: str = Field(min_length=1, max_length=40)
    signal_unit: str = Field(min_length=1, max_length=40)
    sample_type: Literal["unknown", "blank", "standard", "qc", "reference"] = "unknown"
    batch_id: str | None = Field(default=None, max_length=100)
    run_order: int | None = Field(default=None, ge=0)


class Trace(TraceMetadata):
    axis: list[float] = Field(
        min_length=3,
        max_length=MAX_POINTS,
        description="Finite, strictly increasing elution times or volumes in axis_unit.",
    )
    signal: list[float] = Field(
        min_length=3,
        max_length=MAX_POINTS,
        description="Finite detector response values in signal_unit; same length as axis.",
    )

    @model_validator(mode="after")
    def validate_trace(self) -> Trace:
        if len(self.axis) != len(self.signal):
            raise ValueError("axis and signal must have the same number of points")
        if any(not math.isfinite(v) for v in self.axis + self.signal):
            raise ValueError("axis and signal must contain only finite numbers")
        if any(b <= a for a, b in zip(self.axis, self.axis[1:])):
            raise ValueError("axis values must be strictly increasing and unique")
        return self


class CSVChannel(StrictModel):
    column: str = Field(
        min_length=1, max_length=200, description="Name of the numeric signal column in the table."
    )
    metadata: TraceMetadata


class CSVGroup(StrictModel):
    values: list[str] = Field(
        min_length=1, max_length=4, description="Group values in the same order as group_columns."
    )
    metadata: TraceMetadata


class Dataset(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    traces: list[Trace] = Field(min_length=1, max_length=MAX_TRACES)

    @model_validator(mode="after")
    def validate_dataset(self) -> Dataset:
        if sum(len(t.axis) for t in self.traces) > MAX_DATASET_POINTS:
            raise ValueError("dataset exceeds the total point limit")
        if len({t.trace_id for t in self.traces}) != len(self.traces):
            raise ValueError("trace_id values must be unique within a dataset")
        injection_samples: dict[str, tuple] = {}
        injection_channels: set[tuple[str, str]] = set()
        for trace in self.traces:
            details = (
                trace.sample_id,
                trace.sample_type,
                trace.batch_id,
                trace.run_order,
                trace.separation,
            )
            if (
                trace.injection_id in injection_samples
                and injection_samples[trace.injection_id] != details
            ):
                raise ValueError(
                    "traces of an injection must share sample, type, batch, run order and separation"
                )
            injection_samples[trace.injection_id] = details
            key = (trace.injection_id, trace.channel)
            if key in injection_channels:
                raise ValueError("each injection/channel combination must be unique")
            injection_channels.add(key)
        return self


class ChromatogramMethod(StrictModel):
    baseline: Literal["none", "als"] = Field(
        default="none",
        description=(
            "Preprocess the signal with no ALS correction or asymmetric least squares. "
            "`none` leaves acquired background in the signal; this is separate from "
            "`integration_baseline`, which controls area calculation."
        ),
    )
    baseline_lambda: float = Field(
        default=1e6,
        gt=0,
        le=1e12,
        description=(
            "Positive ALS penalty on second differences of the sample-index signal; larger values "
            "generally produce smoother baselines. Its effect depends on the sampling grid and "
            "signal, peak, and noise pattern, so the default 1e6 is not a universal setting."
        ),
    )
    baseline_p: float = Field(
        default=0.01,
        gt=0,
        lt=1,
        description=(
            "Unitless ALS weight for residuals above the fitted baseline; residuals below it "
            "receive weight 1 - p. Smaller values downweight above-baseline peaks more strongly. "
            "The default 0.01 is not an instrument-independent recommendation."
        ),
    )
    smoothing: Literal["none", "gaussian"] = Field(
        default="none",
        description=(
            "Leave the signal unsmoothed or apply Gaussian smoothing on the sample-index grid. "
            "The width is set by smoothing_sigma in sample points, not axis units."
        ),
    )
    smoothing_sigma: float = Field(
        default=1,
        gt=0,
        le=100,
        description="Gaussian kernel standard deviation in sample points, not axis units.",
    )
    min_height: float = Field(
        default=0,
        ge=0,
        description=(
            "Minimum detected apex value after preprocessing, in signal_unit. Zero disables "
            "height filtering; with baseline='none', the value still includes acquired background."
        ),
    )
    min_prominence: float = Field(
        default=0,
        ge=0,
        description=(
            "Minimum peak prominence for prominence detection, in signal_unit. Zero disables "
            "prominence filtering; prominence describes contrast to surrounding signal and is "
            "distinct from integration area."
        ),
    )
    min_distance: float = Field(
        default=0,
        ge=0,
        description=(
            "Minimum separation between retained peak locations in axis_unit. Zero disables "
            "distance filtering; when positive, the prominence detector keeps stronger nearby "
            "candidates first."
        ),
    )
    integration_baseline: Literal["local", "none", "global"] = Field(
        default="local",
        description=(
            "Observed-signal area baseline: local subtracts a line between the integration bounds; "
            "none integrates to zero and includes background; global subtracts the minimum in the "
            "peak range. Negative corrected integrand values are clipped to zero."
        ),
    )
