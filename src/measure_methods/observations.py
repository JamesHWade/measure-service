"""Acceptance rules for caller-declared analytical observations.

This kernel evaluates an explicitly declared analyte cohort. It applies the
caller-provided assignments and measurement statuses; it does not qualify or
certify their producer, provenance, fidelity, storage, or physical meaning.
Retention times and acquisition durations are in minutes.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from typing import Any, Literal

_ASSIGNMENTS = ("confirmed", "ambiguous", "unassigned")
_RETENTION_STATUSES = (
    "observed",
    "not_detected",
    "non_eluting",
    "censored",
    "missing",
    "failed",
)
_RESOLUTION_STATUSES = ("observed", "unsupported", "censored", "missing", "failed")


def _finite_nonnegative(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a finite nonnegative real number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if number < 0:
        raise ValueError(f"{name} must be nonnegative")
    return number


def _validate_analyte_id(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("analyte_id must be a string")
    if not value.strip():
        raise ValueError("analyte_id must be a nonempty string")
    return value


def _diagnostic(code: str, **details: object) -> dict[str, object]:
    result: dict[str, object] = {"code": code}
    result.update(details)
    return result


def _acceptance_state(detection: bool | None, resolution: bool | None) -> str:
    if detection is False or resolution is False:
        return "not_accepted"
    if detection is True and resolution is True:
        return "accepted"
    return "unknown"


@dataclass(frozen=True, slots=True)
class AnalyteObservation:
    """One caller-assigned observation and its explicit measurement statuses.

    An ``observed`` retention time or resolution must have a finite,
    nonnegative numeric value. Other statuses must omit that value. Assignment
    is caller-declared and is not verified by this record or the acceptance
    kernel.
    """

    analyte_id: str
    assignment: Literal["confirmed", "ambiguous", "unassigned"]
    retention_status: Literal[
        "observed", "not_detected", "non_eluting", "censored", "missing", "failed"
    ]
    retention_time_min: Real | None = None
    resolution_status: Literal["observed", "unsupported", "censored", "missing", "failed"] = (
        "missing"
    )
    resolution: Real | None = None

    def __post_init__(self) -> None:
        _validate_analyte_id(self.analyte_id)
        if self.assignment not in _ASSIGNMENTS:
            raise ValueError("assignment must be confirmed, ambiguous, or unassigned")
        if self.retention_status not in _RETENTION_STATUSES:
            raise ValueError("retention_status is unsupported")
        if self.resolution_status not in _RESOLUTION_STATUSES:
            raise ValueError("resolution_status is unsupported")

        if self.retention_status == "observed":
            if self.retention_time_min is None:
                raise ValueError("observed retention requires retention_time_min")
            object.__setattr__(
                self,
                "retention_time_min",
                _finite_nonnegative("retention_time_min", self.retention_time_min),
            )
        elif self.retention_time_min is not None:
            raise ValueError(
                "retention_time_min must be omitted unless retention_status is observed"
            )

        if self.resolution_status == "observed":
            if self.resolution is None:
                raise ValueError("observed resolution requires a resolution value")
            object.__setattr__(
                self, "resolution", _finite_nonnegative("resolution", self.resolution)
            )
        elif self.resolution is not None:
            raise ValueError("resolution must be omitted unless resolution_status is observed")


def assess_analytical_observation(
    *,
    required_analytes: Sequence[str],
    observations: Sequence[AnalyteObservation],
    acquisition_duration_min: Real,
    max_acquisition_duration_min: Real,
    resolution_threshold: Real = 1.5,
    runtime_preference_min: Real = 5,
) -> dict[str, Any]:
    """Assess detection and resolution for a declared required analyte cohort.

    Detection requires a confirmed assignment and either a retention time at
    or before ``acquisition_duration_min``, or explicit confirmed absence
    (``not_detected`` or ``non_eluting``). Ambiguous assignments and missing,
    failed, or censored evidence remain unknown. Resolution is assessed only
    for confirmed assignments with an observed finite value, using an
    inclusive threshold. Known failure dominates unknown in each conjunction.

    Feasibility and the runtime preference are reported independently from
    analytical acceptance. Missing required rows are included in declared
    order as unknown. Duplicate IDs and rows outside the required cohort are
    rejected. This pure kernel does not validate how assignment or measurement
    evidence was produced. ``detected_count``, ``unknown_count`` and
    ``confirmed_absent_count`` partition the detection values; ``unknown_count``
    does not count rows whose detection is known but resolution is unavailable.
    ``confirmed_absent_count`` includes confirmed retention times after the
    declared acquisition window, which are negative for this acceptance test.
    """
    if isinstance(required_analytes, (str, bytes, bytearray, Mapping)) or not isinstance(
        required_analytes, Sequence
    ):
        raise TypeError("required_analytes must be a sequence of strings")
    required = [_validate_analyte_id(item) for item in required_analytes]
    if not required:
        raise ValueError("required_analytes must contain at least one analyte")
    if len(set(required)) != len(required):
        raise ValueError("required_analytes must not contain duplicate IDs")

    if isinstance(observations, (str, bytes, bytearray, Mapping)) or not isinstance(
        observations, Sequence
    ):
        raise TypeError("observations must be a sequence of AnalyteObservation values")
    observations_by_id: dict[str, AnalyteObservation] = {}
    for observation in observations:
        if not isinstance(observation, AnalyteObservation):
            raise TypeError("observations must contain only AnalyteObservation values")
        if observation.analyte_id in observations_by_id:
            raise ValueError("observations must not contain duplicate analyte IDs")
        observations_by_id[observation.analyte_id] = observation
    if set(observations_by_id) - set(required):
        raise ValueError("observations must belong to the declared required analyte cohort")

    duration = _finite_nonnegative("acquisition_duration_min", acquisition_duration_min)
    max_duration = _finite_nonnegative("max_acquisition_duration_min", max_acquisition_duration_min)
    threshold = _finite_nonnegative("resolution_threshold", resolution_threshold)
    runtime_preference = _finite_nonnegative("runtime_preference_min", runtime_preference_min)
    feasible = duration <= max_duration
    runtime_preference_satisfied = duration <= runtime_preference

    rows: list[dict[str, object]] = []
    top_diagnostics: list[dict[str, object]] = []
    if not feasible:
        top_diagnostics.append(
            _diagnostic(
                "acquisition_duration_exceeds_maximum",
                acquisition_duration_min=duration,
                max_acquisition_duration_min=max_duration,
            )
        )
    if not runtime_preference_satisfied:
        top_diagnostics.append(
            _diagnostic(
                "runtime_preference_not_satisfied",
                acquisition_duration_min=duration,
                runtime_preference_min=runtime_preference,
            )
        )

    for analyte_id in required:
        observation = observations_by_id.get(analyte_id)
        row_diagnostics: list[dict[str, object]] = []
        if observation is None:
            assignment = None
            retention_status = "missing"
            retention_time = None
            resolution_status = "missing"
            resolution = None
            detection: bool | None = None
            resolution_satisfied: bool | None = None
            row_diagnostics.append(_diagnostic("required_observation_missing"))
        else:
            assignment = observation.assignment
            retention_status = observation.retention_status
            retention_time = observation.retention_time_min
            resolution_status = observation.resolution_status
            resolution = observation.resolution

            if assignment != "confirmed":
                detection = None
                row_diagnostics.append(
                    _diagnostic("assignment_not_confirmed", assignment=assignment)
                )
            elif retention_status == "observed":
                assert retention_time is not None
                detection = retention_time <= duration
                if not detection:
                    row_diagnostics.append(
                        _diagnostic(
                            "retention_time_after_acquisition_window",
                            retention_time_min=retention_time,
                            acquisition_duration_min=duration,
                        )
                    )
            elif retention_status in ("not_detected", "non_eluting"):
                detection = False
                row_diagnostics.append(_diagnostic(f"confirmed_{retention_status}"))
            else:
                detection = None
                row_diagnostics.append(_diagnostic(f"retention_evidence_{retention_status}"))

            if assignment != "confirmed":
                resolution_satisfied = None
                if resolution_status == "observed":
                    row_diagnostics.append(_diagnostic("resolution_assignment_not_confirmed"))
            elif resolution_status == "observed":
                assert resolution is not None
                resolution_satisfied = resolution >= threshold
                if not resolution_satisfied:
                    row_diagnostics.append(
                        _diagnostic(
                            "resolution_below_threshold",
                            resolution=resolution,
                            resolution_threshold=threshold,
                        )
                    )
            else:
                resolution_satisfied = None
                row_diagnostics.append(_diagnostic(f"resolution_evidence_{resolution_status}"))

        acceptance_state = _acceptance_state(detection, resolution_satisfied)
        rows.append(
            {
                "analyte_id": analyte_id,
                "assignment": assignment,
                "retention_status": retention_status,
                "retention_time_min": retention_time,
                "detection": detection,
                "resolution_status": resolution_status,
                "resolution": resolution,
                "resolution_satisfied": resolution_satisfied,
                "acceptance_state": acceptance_state,
                "diagnostics": row_diagnostics,
            }
        )
        top_diagnostics.extend(
            _diagnostic(
                item["code"],
                analyte_id=analyte_id,
                **{k: v for k, v in item.items() if k != "code"},
            )
            for item in row_diagnostics
        )

    acceptance_states = [str(row["acceptance_state"]) for row in rows]
    if "not_accepted" in acceptance_states:
        analytical_acceptance = "not_accepted"
    elif all(state == "accepted" for state in acceptance_states):
        analytical_acceptance = "accepted"
    else:
        analytical_acceptance = "unknown"

    detection_states = [row["detection"] for row in rows]
    return {
        "profile": "moses-observed-analytical/1",
        "analytical_acceptance": analytical_acceptance,
        "feasible": feasible,
        "runtime_preference_satisfied": runtime_preference_satisfied,
        "required_analyte_count": len(required),
        "detected_count": sum(state is True for state in detection_states),
        "unknown_count": sum(state is None for state in detection_states),
        "confirmed_absent_count": sum(state is False for state in detection_states),
        "per_analyte": rows,
        "diagnostics": top_diagnostics,
    }
