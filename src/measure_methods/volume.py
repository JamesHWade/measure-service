"""Numerical integration kernels for product and solvent volumes."""

from __future__ import annotations

import math
import sys
from collections.abc import Mapping, Sequence
from fractions import Fraction
from numbers import Real

_FRACTION_SUM_TOLERANCE = 1e-9


def _finite_real(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a finite real number")

    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc

    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if result == 0 and value != 0:
        raise ValueError(f"{name} is too small to represent as a float")
    return result


def _nonnegative(name: str, value: object) -> float:
    result = _finite_real(name, value)
    if result < 0:
        raise ValueError(f"{name} must be nonnegative")
    return result


def _fraction(name: str, value: object) -> float:
    result = _finite_real(name, value)
    if not 0 <= result <= 1:
        raise ValueError(f"{name} must be between zero and one")
    return result


def _is_subnormal(value: float) -> bool:
    return 0 < abs(value) < sys.float_info.min


def _exact_volume(
    duration: float,
    flow_start: float,
    flow_end: float,
    fraction_start: float,
    fraction_end: float,
) -> float:
    """Retry an unstable calculation with exact rational input values."""
    return _exact_volume_for_duration(
        Fraction.from_float(duration),
        flow_start,
        flow_end,
        fraction_start,
        fraction_end,
    )


def _exact_volume_for_duration(
    duration: Fraction,
    flow_start: float,
    flow_end: float,
    fraction_start: float,
    fraction_end: float,
) -> float:
    """Integrate exactly given a rational duration and binary float states."""
    return _float_from_exact_volume(
        _exact_volume_fraction_for_duration(
            duration,
            flow_start,
            flow_end,
            fraction_start,
            fraction_end,
        )
    )


def _exact_volume_fraction_for_duration(
    duration: Fraction,
    flow_start: float,
    flow_end: float,
    fraction_start: float,
    fraction_end: float,
) -> Fraction:
    """Return the exact segment integral over binary float inputs."""
    d = duration
    f0 = Fraction.from_float(flow_start)
    f1 = Fraction.from_float(flow_end)
    c0 = Fraction.from_float(fraction_start)
    c1 = Fraction.from_float(fraction_end)

    # Integrate (f0 + (f1-f0)t) * (c0 + (c1-c0)t) over t in [0, 1].
    average_product = (f0 * c0 + f1 * c1 + (f0 + f1) * (c0 + c1)) / 6
    return d * average_product


def _float_from_exact_volume(exact_result: Fraction) -> float:
    if exact_result == 0:
        return 0.0

    try:
        result = float(exact_result)
    except OverflowError as exc:
        raise ValueError("solvent use is too large to represent as a float") from exc

    if not math.isfinite(result):
        raise ValueError("solvent use is too large to represent as a float")
    if result == 0:
        raise ValueError("positive solvent use is too small to represent as a float")
    return result


def _linear_product_volume_evaluated(
    duration: float,
    flow_start: float,
    flow_end: float,
    fraction_start: float,
    fraction_end: float,
) -> tuple[float, bool, bool]:
    """Return the ordinary result plus instability and subnormal flags."""
    if duration == 0 or (flow_start == 0 and flow_end == 0):
        return 0.0, False, False
    if fraction_start == 0 and fraction_end == 0:
        return 0.0, False, False

    flow_mid_delta = (flow_end - flow_start) * 0.5
    fraction_mid_delta = (fraction_end - fraction_start) * 0.5
    flow_mid = flow_start + flow_mid_delta
    fraction_mid = fraction_start + fraction_mid_delta
    start_product = flow_start * fraction_start
    middle_product = flow_mid * fraction_mid
    end_product = flow_end * fraction_end
    start_term = start_product / 6.0
    middle_term = middle_product * (2.0 / 3.0)
    end_term = end_product / 6.0

    # Keep the normal path identical to the former service kernel. Retry only
    # when a zero or subnormal intermediate may lose a result scaled by duration.
    unstable = (
        (flow_start != flow_end and flow_mid_delta == 0)
        or (fraction_start != fraction_end and fraction_mid_delta == 0)
        or _is_subnormal(flow_mid_delta)
        or _is_subnormal(fraction_mid_delta)
        or ((flow_start > 0 or flow_end > 0) and flow_mid == 0)
        or ((fraction_start > 0 or fraction_end > 0) and fraction_mid == 0)
        or (flow_mid > 0 and fraction_mid > 0 and middle_product == 0)
        or (flow_start > 0 and fraction_start > 0 and start_product == 0)
        or (flow_end > 0 and fraction_end > 0 and end_product == 0)
        or any(
            _is_subnormal(value)
            for value in (
                flow_mid,
                fraction_mid,
                start_product,
                middle_product,
                end_product,
                start_term,
                middle_term,
                end_term,
            )
        )
    )

    try:
        average_product = math.fsum((start_term, middle_term, end_term))
        result = duration * average_product
    except (OverflowError, ValueError):
        unstable = True
        result = math.inf

    if not math.isfinite(result):
        unstable = True
    elif result == 0:
        unstable = True
    subnormal = _is_subnormal(result)
    if subnormal:
        # A subnormal final result is valid, but an exact retry avoids additional
        # rounding when an earlier intermediate was also near underflow.
        unstable = unstable or any(
            _is_subnormal(value) for value in (flow_start, flow_end, fraction_start, fraction_end)
        )

    return result, unstable, subnormal


def _linear_product_volume(
    duration: float,
    flow_start: float,
    flow_end: float,
    fraction_start: float,
    fraction_end: float,
) -> float:
    """Integrate the product of linearly varying flow and fraction."""
    result, unstable, _ = _linear_product_volume_evaluated(
        duration,
        flow_start,
        flow_end,
        fraction_start,
        fraction_end,
    )
    if unstable:
        return _exact_volume(
            duration,
            flow_start,
            flow_end,
            fraction_start,
            fraction_end,
        )
    return result


def _constant_product_volume(
    duration: float,
    flow: float,
    fraction: float,
) -> float:
    """Integrate a constant flow/fraction with the legacy operation order."""
    if duration == 0 or flow == 0 or fraction == 0:
        return 0.0

    flow_fraction = flow * fraction
    result = duration * flow_fraction
    unstable = (
        flow_fraction == 0
        or _is_subnormal(flow_fraction)
        or not math.isfinite(result)
        or result == 0
        or (
            _is_subnormal(result)
            and any(_is_subnormal(value) for value in (duration, flow, fraction))
        )
    )

    if not unstable:
        return result

    exact_result = (
        Fraction.from_float(duration) * Fraction.from_float(flow) * Fraction.from_float(fraction)
    )
    return _float_from_exact_volume(exact_result)


def linear_product_volume(
    duration_min: float,
    flow_start_ml_min: float,
    flow_end_ml_min: float,
    fraction_start: float,
    fraction_end: float,
) -> float:
    """Integrate solvent volume for one linearly varying solvent fraction.

    Flow varies linearly from ``flow_start_ml_min`` to ``flow_end_ml_min`` over
    ``duration_min``. The solvent fraction varies linearly from ``fraction_start``
    to ``fraction_end`` over the same segment. The result is in millilitres.

    All inputs must be finite real scalars. Booleans and strings are rejected;
    duration and flow must be nonnegative, and fractions must be in ``[0, 1]``.
    If the positive result cannot be represented as a finite, nonzero float,
    ``ValueError`` is raised.
    """
    duration = _nonnegative("duration_min", duration_min)
    flow_start = _nonnegative("flow_start_ml_min", flow_start_ml_min)
    flow_end = _nonnegative("flow_end_ml_min", flow_end_ml_min)
    start_fraction = _fraction("fraction_start", fraction_start)
    end_fraction = _fraction("fraction_end", fraction_end)

    return _linear_product_volume(
        duration,
        flow_start,
        flow_end,
        start_fraction,
        end_fraction,
    )


def _validated_fractions(name: str, fractions: Mapping[str, float]) -> dict[str, float]:
    if not isinstance(fractions, Mapping):
        raise TypeError(f"{name} must be a mapping of solvent names to fractions")
    if not fractions:
        raise ValueError(f"{name} must contain at least one solvent")

    result: dict[str, float] = {}
    for solvent, value in fractions.items():
        if not isinstance(solvent, str) or not solvent.strip():
            raise ValueError("solvent names must be nonempty strings")
        result[solvent] = _fraction(f"{name}[{solvent!r}]", value)

    try:
        total = math.fsum(result.values())
    except OverflowError as exc:
        raise ValueError(f"fractions in {name} must sum to one") from exc
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=_FRACTION_SUM_TOLERANCE):
        raise ValueError(f"fractions in {name} must sum to one within 1e-9")
    return result


def linear_solvent_use(
    duration_min: float,
    flow_start_ml_min: float,
    flow_end_ml_min: float,
    fractions_start: Mapping[str, float],
    fractions_end: Mapping[str, float],
) -> dict[str, float]:
    """Integrate use for each solvent in a linearly varying mixture.

    Duration is in minutes, flow is in millilitres per minute, and the returned
    mapping contains millilitres by solvent. Every endpoint mixture must contain
    the same nonempty solvent names, each fraction must be in ``[0, 1]``, and
    fractions at each endpoint must sum to one within an absolute tolerance of
    ``1e-9``. Mapping insertion order is retained in the returned dictionary.

    All numeric inputs must be finite real scalars; booleans and strings are
    rejected. Duration and flow must be nonnegative. ``ValueError`` is raised if
    an input is out of range or a positive result cannot be represented as a
    finite, nonzero float.
    """
    duration = _nonnegative("duration_min", duration_min)
    flow_start = _nonnegative("flow_start_ml_min", flow_start_ml_min)
    flow_end = _nonnegative("flow_end_ml_min", flow_end_ml_min)
    start_fractions = _validated_fractions("fractions_start", fractions_start)
    end_fractions = _validated_fractions("fractions_end", fractions_end)

    if start_fractions.keys() != end_fractions.keys():
        raise ValueError("fractions_start and fractions_end must have the same solvents")

    return {
        solvent: _linear_product_volume(
            duration,
            flow_start,
            flow_end,
            start_fractions[solvent],
            end_fractions[solvent],
        )
        for solvent in start_fractions
    }


def constant_solvent_use(
    duration_min: float,
    flow_ml_min: float,
    solvent_fractions: Mapping[str, float],
) -> dict[str, float]:
    """Integrate solvent use for constant flow and composition.

    Duration is in minutes, flow is in millilitres per minute, and the returned
    mapping contains millilitres by solvent. ``solvent_fractions`` must contain
    nonempty solvent names, fractions in ``[0, 1]``, and sum to one within an
    absolute tolerance of ``1e-9``.

    All numeric inputs must be finite real scalars; booleans and strings are
    rejected. Duration and flow must be nonnegative. The normal path evaluates
    ``duration * (flow * fraction)`` in that order to preserve prior results.
    If a positive result cannot be represented as a finite, nonzero float,
    ``ValueError`` is raised.
    """
    duration = _nonnegative("duration_min", duration_min)
    flow = _nonnegative("flow_ml_min", flow_ml_min)
    fractions = _validated_fractions("solvent_fractions", solvent_fractions)

    return {
        solvent: _constant_product_volume(duration, flow, fraction)
        for solvent, fraction in fractions.items()
    }


def program_solvent_use(
    times_min: Sequence[float],
    flows_ml_min: Sequence[float],
    solvent_fractions: Sequence[Mapping[str, float]],
) -> dict[str, float]:
    """Integrate each solvent over a complete piecewise-linear method program.

    Each sample gives a time, total flow, and solvent mixture at one method
    boundary. Times may have any finite origin, but must be strictly increasing.
    All three sequences must have the same length of at least two. Every sample
    must declare the same nonempty solvent names and a mixture summing to one
    within the same absolute tolerance used by :func:`linear_solvent_use`.

    The function applies the same segment kernel as :func:`linear_solvent_use` to
    each adjacent pair and uses accurate floating-point summation for ordinary
    per-solvent totals. If any segment underflows or produces a subnormal result,
    it accumulates exact segment integrals before rounding the total. It returns
    millilitres by solvent and raises ``ValueError`` if the final total cannot be
    represented or any input is invalid.
    """
    sequences = (
        ("times_min", times_min),
        ("flows_ml_min", flows_ml_min),
        ("solvent_fractions", solvent_fractions),
    )
    for name, sequence in sequences:
        if isinstance(sequence, (str, bytes)) or not isinstance(sequence, Sequence):
            raise TypeError(f"{name} must be a sequence")

    length = len(times_min)
    if length < 2:
        raise ValueError("a program must contain at least two samples")
    if len(flows_ml_min) != length or len(solvent_fractions) != length:
        raise ValueError("times, flows, and solvent fractions must have equal lengths")

    times = [_finite_real(f"times_min[{i}]", value) for i, value in enumerate(times_min)]
    flows = [_nonnegative(f"flows_ml_min[{i}]", value) for i, value in enumerate(flows_ml_min)]
    fractions = [
        _validated_fractions(f"solvent_fractions[{i}]", point)
        for i, point in enumerate(solvent_fractions)
    ]

    solvents = fractions[0]
    solvent_names = solvents.keys()
    for i, point in enumerate(fractions[1:], start=1):
        if point.keys() != solvent_names:
            raise ValueError(f"solvent_fractions[{i}] must use the first sample's solvents")

    for i in range(1, length):
        if times[i] <= times[i - 1]:
            raise ValueError("times_min must be strictly increasing")

    segment_use: dict[str, list[float]] = {solvent: [] for solvent in solvents}
    durations: list[float | Fraction] = []
    needs_exact_aggregation = False
    for i in range(length - 1):
        duration = times[i + 1] - times[i]
        if math.isfinite(duration):
            durations.append(duration)
            for solvent in solvents:
                value, unstable, subnormal = _linear_product_volume_evaluated(
                    duration,
                    flows[i],
                    flows[i + 1],
                    fractions[i][solvent],
                    fractions[i + 1][solvent],
                )
                segment_use[solvent].append(value)
                needs_exact_aggregation = needs_exact_aggregation or unstable or subnormal
        else:
            # Both endpoints are finite, but their difference may exceed the
            # float range. Keep the exact positive duration for this rare case.
            exact_duration = Fraction.from_float(times[i + 1]) - Fraction.from_float(times[i])
            durations.append(exact_duration)
            needs_exact_aggregation = True

    if needs_exact_aggregation:
        exact_totals = {solvent: Fraction(0) for solvent in solvents}
        for i, duration in enumerate(durations):
            exact_duration = (
                duration if isinstance(duration, Fraction) else Fraction.from_float(duration)
            )
            for solvent in solvents:
                exact_totals[solvent] += _exact_volume_fraction_for_duration(
                    exact_duration,
                    flows[i],
                    flows[i + 1],
                    fractions[i][solvent],
                    fractions[i + 1][solvent],
                )
        return {solvent: _float_from_exact_volume(total) for solvent, total in exact_totals.items()}

    result: dict[str, float] = {}
    for solvent, values in segment_use.items():
        try:
            total = math.fsum(values)
        except OverflowError as exc:
            raise ValueError("solvent use total is too large to represent as a float") from exc
        if not math.isfinite(total):
            raise ValueError("solvent use total is too large to represent as a float")
        result[solvent] = total
    return result
