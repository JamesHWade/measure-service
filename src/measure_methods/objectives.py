"""Pure-Python objective normalization and hierarchical scalarization.

The normalization and hierarchy follow the method described in BoTier's
``AuxiliaryObjective`` and ``HierarchyScalarizer``. This module implements
the equations directly with the Python standard library; it does not depend
on BoTier, PyTorch, NumPy, or the measure service. The versioned references are:

* https://github.com/fsk-lab/botier/blob/v1.1.0/src/botier/auxiliary_objective.py
* https://github.com/fsk-lab/botier/blob/v1.1.0/src/botier/hierarchy_scalarizer.py
* https://github.com/fsk-lab/botier/blob/v1.1.0/src/botier/utils.py
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from typing import Literal

Direction = Literal["maximize", "minimize"]


def _finite_float(name: str, value: object) -> float:
    """Convert one real scalar without silently losing its finite value."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a real number (bool is not accepted)")

    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be representable as a finite float") from exc

    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if result == 0.0 and value != 0:
        raise ValueError(f"{name} is too small to represent as a nonzero float")
    return result


def _normalization_range(
    direction: Direction,
    lower_bound: float,
    upper_bound: float,
    threshold: float,
) -> tuple[float, float, float, float]:
    """Return worst, best, threshold distance and normalized best value."""
    if direction == "maximize":
        worst, best = lower_bound, upper_bound
    else:
        worst, best = upper_bound, lower_bound

    distance = threshold - worst
    span = best - worst
    if not math.isfinite(distance) or distance == 0.0:
        raise ValueError("threshold-to-worst distance is not representable as a finite float")
    if not math.isfinite(span):
        raise ValueError("bound range is not representable as a finite float")

    normalized_best = span / distance
    if not math.isfinite(normalized_best) or normalized_best <= 0.0:
        raise ValueError("normalized bound range is not representable as a finite positive float")
    return worst, best, distance, normalized_best


@dataclass(frozen=True)
class Criterion:
    """One bounded objective and its satisfaction threshold.

    ``lower_bound`` and ``upper_bound`` are inclusive clipping limits. The
    threshold must be strictly better than the worst bound and may equal the
    best bound. The threshold is normalized to 1 by :func:`hierarchical_score`.
    """

    name: str
    direction: Direction
    lower_bound: Real
    upper_bound: Real
    threshold: Real

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("criterion name must be a string")
        if not self.name.strip():
            raise ValueError("criterion name must not be empty")
        if self.direction not in ("maximize", "minimize"):
            raise ValueError("direction must be 'maximize' or 'minimize'")

        lower_bound = _finite_float("lower_bound", self.lower_bound)
        upper_bound = _finite_float("upper_bound", self.upper_bound)
        threshold = _finite_float("threshold", self.threshold)
        if lower_bound >= upper_bound:
            raise ValueError("lower_bound must be less than upper_bound")

        if self.direction == "maximize":
            threshold_is_valid = lower_bound < threshold <= upper_bound
        else:
            threshold_is_valid = lower_bound <= threshold < upper_bound
        if not threshold_is_valid:
            raise ValueError(
                "threshold must be strictly better than the worst bound and within bounds"
            )

        _normalization_range(self.direction, lower_bound, upper_bound, threshold)
        object.__setattr__(self, "lower_bound", lower_bound)
        object.__setattr__(self, "upper_bound", upper_bound)
        object.__setattr__(self, "threshold", threshold)


def _scaled_difference(smoothing: float, difference: float) -> float:
    """Multiply nonnegative finite values, allowing positive overflow to inf."""
    # IEEE float multiplication yields inf for overflow and zero for
    # underflow. Both are useful limits for the exponentials below.
    return smoothing * difference


def _smoothmin(value: float, threshold: float, smoothing: float) -> float:
    """Return the stable exponential-weighted mean of two finite values."""
    low = min(value, threshold)
    high = max(value, threshold)
    difference = high - low

    # After shifting by ``low``, the low-side weight is exactly one and the
    # high-side weight is exp(-k * difference). This is algebraically the
    # weighted mean in BoTier's utils.py without forming large exponentials.
    high_weight_ratio = math.exp(-_scaled_difference(smoothing, difference))
    high_weight = high_weight_ratio / (1.0 + high_weight_ratio)
    result = low + difference * high_weight
    if not math.isfinite(result):
        raise ValueError("smooth minimum is not representable as a finite float")
    return result


def _logistic(value: float, threshold: float, smoothing: float) -> float:
    """Return a logistic gate without evaluating an overflowing exponential."""
    exponent = smoothing * (value - threshold)
    if exponent >= 0.0:
        return 1.0 / (1.0 + math.exp(-exponent))
    positive_tail = math.exp(exponent)
    return positive_tail / (1.0 + positive_tail)


def hierarchical_score(
    values: Mapping[str, Real],
    criteria: Sequence[Criterion],
    *,
    smoothing: Real = 100.0,
    final_objective: str | None = None,
    final_rule: Literal["excess", "value"] = "excess",
) -> dict[str, object]:
    """Normalize and hierarchically scalarize a complete set of objectives.

    Criteria are evaluated in sequence order. Each is clipped to its bounds,
    oriented by ``direction``, then scaled so its worst value is 0 and its
    satisfaction threshold is 1. A smooth minimum and logistic satisfaction
    gate allocate score to each objective in turn; after every gate is met,
    the selected final objective contributes either its excess above 1
    (``"excess"``) or its complete normalized value (``"value"``). The
    excess rule matches the cited BoTier source; the value rule makes an
    published BoTier v1.0.0 formulation explicit for source comparisons.

    The result includes each gate, preceding-gate product, normalized value,
    and score contribution so callers can inspect why a score was produced.
    The returned mapping contains only JSON-safe strings, floats, lists, and
    dictionaries. Nonfinite inputs and computations that cannot be represented
    by finite floats raise ``ValueError`` rather than emitting NaN or infinity.
    """
    if not isinstance(values, Mapping):
        raise TypeError("values must be a mapping from criterion names to real numbers")
    if isinstance(criteria, (str, bytes)) or not isinstance(criteria, Sequence):
        raise TypeError("criteria must be a nonempty sequence of Criterion objects")
    if not criteria:
        raise ValueError("criteria must not be empty")

    names: list[str] = []
    for criterion in criteria:
        if not isinstance(criterion, Criterion):
            raise TypeError("every entry in criteria must be a Criterion")
        names.append(criterion.name)
    if len(set(names)) != len(names):
        raise ValueError("criterion names must be unique")

    expected_names = set(names)
    provided_names = set(values.keys())
    missing_names = expected_names - provided_names
    unknown_names = provided_names - expected_names
    if missing_names or unknown_names:
        parts = []
        if missing_names:
            parts.append(f"missing values for {sorted(missing_names)!r}")
        if unknown_names:
            parts.append(f"unknown value names {sorted(unknown_names, key=repr)!r}")
        raise ValueError("values must match criterion names exactly: " + "; ".join(parts))

    if final_objective is None:
        final_name = names[0]
    else:
        if not isinstance(final_objective, str):
            raise TypeError("final_objective must be a criterion name or None")
        if final_objective not in expected_names:
            raise ValueError(f"final_objective {final_objective!r} is not a criterion name")
        final_name = final_objective

    if not isinstance(final_rule, str):
        raise TypeError("final_rule must be a string")
    if final_rule not in ("excess", "value"):
        raise ValueError("final_rule must be 'excess' or 'value'")

    smoothing_value = _finite_float("smoothing", smoothing)
    if smoothing_value <= 0.0:
        raise ValueError("smoothing must be greater than zero")

    criterion_results: list[dict[str, object]] = []
    contributions: list[float] = []
    normalized_values: dict[str, float] = {}
    prefix_gate = 1.0

    for criterion in criteria:
        raw_value = _finite_float(f"values[{criterion.name!r}]", values[criterion.name])
        clamped_value = min(max(raw_value, criterion.lower_bound), criterion.upper_bound)
        worst, _, distance, normalized_best = _normalization_range(
            criterion.direction,
            criterion.lower_bound,
            criterion.upper_bound,
            criterion.threshold,
        )
        normalized_value = (clamped_value - worst) / distance
        if normalized_value == 0.0:
            normalized_value = 0.0
        if not math.isfinite(normalized_value) or not 0.0 <= normalized_value <= normalized_best:
            raise ValueError(f"normalized value for {criterion.name!r} is not representable")

        gate = _logistic(normalized_value, 1.0, smoothing_value)
        smooth_minimum = _smoothmin(normalized_value, 1.0, smoothing_value)
        contribution = smooth_minimum * prefix_gate
        if not math.isfinite(gate) or not math.isfinite(contribution):
            raise ValueError(f"score contribution for {criterion.name!r} is not representable")

        normalized_values[criterion.name] = normalized_value
        contributions.append(contribution)
        criterion_results.append(
            {
                "name": criterion.name,
                "raw_value": raw_value,
                "clamped_value": clamped_value,
                "normalized_value": normalized_value,
                "normalized_best": normalized_best,
                "threshold": 1.0,
                "gate": gate,
                "prefix_gate": prefix_gate,
                "contribution": contribution,
            }
        )
        prefix_gate *= gate

    final_value = normalized_values[final_name]
    final_factor = final_value - 1.0 if final_rule == "excess" else final_value
    final_contribution = prefix_gate * final_factor
    try:
        score = math.fsum((*contributions, final_contribution))
    except OverflowError as exc:
        raise ValueError("hierarchical score is not representable as a finite float") from exc
    if not math.isfinite(final_contribution) or not math.isfinite(score):
        raise ValueError("hierarchical score is not representable as a finite float")

    return {
        "score": score,
        "criteria": criterion_results,
        "final_objective": final_name,
        "final_rule": final_rule,
        "final_gate": prefix_gate,
        "final_contribution": final_contribution,
        "smoothing": smoothing_value,
    }
