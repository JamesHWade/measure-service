"""Continuous ideal-retention references for linear LC inlet gradients.

The ``ideal-lc-retention-zeta/1`` profile solves
``integral(0, zeta, 1 / k(phi(z))) dz = t0`` for ideal plug flow, where
``t0 = column_length * effective_area / flow_rate``. It returns
``zeta + t0 + post_column_volume / flow_rate``. The result is a continuous
reference, not the upstream simulator's discrete Gaussian-mask result and not
a qualification of a physical instrument or fitted retention law.

Flow is constant. Gradient times and retention times use minutes, flow and
post-column volume use mL/min and mL, column length uses m, and effective area
uses m^2. The gradient is piecewise linear in inlet solvent fraction. This
kernel does not model pre-injection volume, gradient delay, peak width, or
plate height. Retention coefficients are not constrained to upstream fitting
bounds; only their numeric domains are validated.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from numbers import Real
from typing import Any, Literal

_MAX_GRADIENT_POINTS = 128
_DEFAULT_MAX_EVALUATIONS = 100_000
_MAX_ADAPTIVE_DEPTH = 60
_LOG_MIN_SUBNORMAL = math.log(math.ulp(0.0))
_LOG_MAX_FLOAT = math.log(float.fromhex("0x1.fffffffffffffp+1023"))
_ROUND_OFF_GUARD_ULPS = 64.0


def _finite_real(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a finite real number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _finite_nonnegative(name: str, value: object) -> float:
    number = _finite_real(name, value)
    if number < 0:
        raise ValueError(f"{name} must be nonnegative")
    return number


def _finite_positive(name: str, value: object) -> float:
    number = _finite_real(name, value)
    if number <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return number


@dataclass(frozen=True, slots=True)
class RetentionParameters:
    """Natural-log coefficients for one of the three source retention laws.

    ``ln_k0`` is the natural logarithm of the retention factor at zero
    composition. ``S1`` and optional ``S2`` use the positive solvent-strength
    convention documented by the selected model. Fitting bounds are not
    imposed here.
    """

    model: Literal["linear", "quadratic", "neue-kuss"]
    ln_k0: Real
    S1: Real
    S2: Real | None = None

    def __post_init__(self) -> None:
        if self.model not in ("linear", "quadratic", "neue-kuss"):
            raise ValueError("model must be linear, quadratic, or neue-kuss")
        ln_k0 = _finite_real("ln_k0", self.ln_k0)
        s1 = _finite_nonnegative("S1", self.S1)
        object.__setattr__(self, "ln_k0", ln_k0)
        object.__setattr__(self, "S1", s1)
        if self.model == "linear":
            if self.S2 is not None:
                raise ValueError("S2 must be None for the linear model")
            return
        if self.S2 is None:
            raise ValueError(f"S2 is required for the {self.model} model")
        s2 = _finite_nonnegative("S2", self.S2)
        object.__setattr__(self, "S2", s2)


def _validate_parameters(parameters: object) -> RetentionParameters:
    if not isinstance(parameters, RetentionParameters):
        raise TypeError("parameters must be RetentionParameters")
    return parameters


def _log_retention_factor(parameters: RetentionParameters, composition: float) -> float:
    if parameters.model == "linear":
        log_k = parameters.ln_k0 - parameters.S1 * composition
    elif parameters.model == "quadratic":
        assert parameters.S2 is not None
        log_k = (
            parameters.ln_k0
            - parameters.S1 * composition
            - parameters.S2 * composition * composition
        )
    else:
        assert parameters.S2 is not None
        s2_phi = parameters.S2 * composition
        log_k = (
            parameters.ln_k0
            + 2.0 * math.log1p(s2_phi)
            - parameters.S1 * composition / (1.0 + s2_phi)
        )
    if not math.isfinite(log_k):
        raise OverflowError("retention law produced a nonfinite log retention factor")
    return log_k


def retention_factor(*, parameters: RetentionParameters, composition: Real) -> float:
    """Evaluate a positive, finite retention factor at fraction ``composition``.

    The composition must lie in [0, 1]. Exponential overflow or underflow is
    reported as ``OverflowError``; the function does not clamp ``k``.
    """
    model = _validate_parameters(parameters)
    phi = _finite_nonnegative("composition", composition)
    if phi > 1:
        raise ValueError("composition must be between zero and one")
    log_k = _log_retention_factor(model, phi)
    try:
        factor = math.exp(log_k)
    except OverflowError as error:
        raise OverflowError("retention factor overflows the supported numeric range") from error
    if factor == 0.0:
        raise OverflowError("retention factor underflows the supported numeric range")
    if not math.isfinite(factor):
        raise OverflowError("retention factor is not finite")
    return factor


def _number_sequence(name: str, values: object, *, minimum: int, maximum: int) -> list[float]:
    if isinstance(values, (str, bytes, bytearray, Mapping)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be a numeric sequence")
    if not minimum <= len(values) <= maximum:
        raise ValueError(f"{name} must contain between {minimum} and {maximum} values")
    return [_finite_real(f"{name}[{index}]", value) for index, value in enumerate(values)]


def _gradient_arrays(
    gradient_times_min: object,
    gradient_fractions: object,
    acquisition_duration_min: float,
) -> tuple[list[float], list[float]]:
    times = _number_sequence(
        "gradient_times_min",
        gradient_times_min,
        minimum=2,
        maximum=_MAX_GRADIENT_POINTS,
    )
    fractions = _number_sequence(
        "gradient_fractions",
        gradient_fractions,
        minimum=2,
        maximum=_MAX_GRADIENT_POINTS,
    )
    if len(times) != len(fractions):
        raise ValueError("gradient_times_min and gradient_fractions must have equal lengths")
    if times[0] != 0.0:
        raise ValueError("gradient_times_min must start at zero")
    if times[-1] != acquisition_duration_min:
        raise ValueError("the final gradient time must equal acquisition_duration_min")
    for index, (left, right) in enumerate(zip(times, times[1:])):
        if right <= left:
            raise ValueError(f"gradient_times_min must be strictly increasing at index {index + 1}")
    for index, fraction in enumerate(fractions):
        if not 0.0 <= fraction <= 1.0:
            raise ValueError(f"gradient_fractions[{index}] must be between zero and one")
    return times, fractions


def _nk_derivative_sign(parameters: RetentionParameters, composition: float) -> int:
    """Sign of the derivative of the log inverse-retention factor."""
    assert parameters.S2 is not None
    s2 = parameters.S2
    if s2 == 0.0:
        return (parameters.S1 > 0.0) - (parameters.S1 < 0.0)
    denominator = 1.0 + s2 * composition
    twice_s2_denominator = 2.0 * s2 * denominator
    if not math.isfinite(twice_s2_denominator):
        return -1
    return (parameters.S1 > twice_s2_denominator) - (parameters.S1 < twice_s2_denominator)


def _integration_grid(
    parameters: RetentionParameters,
    times: list[float],
    fractions: list[float],
) -> tuple[list[float], list[float]]:
    """Split NK gradient segments at their possible interior log-k extremum."""
    result_times = [times[0]]
    result_fractions = [fractions[0]]
    if parameters.model != "neue-kuss" or parameters.S2 == 0.0:
        return times, fractions

    assert parameters.S2 is not None
    for index, (left_time, right_time) in enumerate(zip(times, times[1:])):
        left_phi = fractions[index]
        right_phi = fractions[index + 1]
        left_sign = _nk_derivative_sign(parameters, left_phi)
        right_sign = _nk_derivative_sign(parameters, right_phi)
        if (left_sign > 0 and right_sign < 0) or (left_sign < 0 and right_sign > 0):
            denominator = 2.0 * parameters.S2
            critical_phi = (parameters.S1 / denominator - 1.0) / parameters.S2
            if min(left_phi, right_phi) < critical_phi < max(left_phi, right_phi):
                position = (critical_phi - left_phi) / (right_phi - left_phi)
                critical_time = left_time + position * (right_time - left_time)
                if left_time < critical_time < right_time:
                    result_times.append(critical_time)
                    result_fractions.append(critical_phi)
        result_times.append(right_time)
        result_fractions.append(right_phi)
    return result_times, result_fractions


def _log_add(left: float, right: float) -> float:
    if left == -math.inf:
        return right
    if right == -math.inf:
        return left
    high = max(left, right)
    low = min(left, right)
    return high + math.log1p(math.exp(low - high))


def _log_abs_difference(left: float, right: float) -> float:
    if left == right:
        return -math.inf
    high = max(left, right)
    low = min(left, right)
    difference = -math.expm1(low - high)
    return high + math.log(difference) if difference > 0 else -math.inf


class _EvaluationBudget:
    __slots__ = ("parameters", "times", "fractions", "max_evaluations", "cache")

    def __init__(
        self,
        parameters: RetentionParameters,
        times: list[float],
        fractions: list[float],
        max_evaluations: int,
    ) -> None:
        self.parameters = parameters
        self.times = times
        self.fractions = fractions
        self.max_evaluations = max_evaluations
        self.cache: dict[float, float] = {}

    @property
    def count(self) -> int:
        return len(self.cache)

    def log_inverse_factor(self, time_min: float) -> float:
        if time_min in self.cache:
            return self.cache[time_min]
        if self.count >= self.max_evaluations:
            raise RuntimeError("retention integration exceeded max_evaluations")
        index = min(bisect_right(self.times, time_min) - 1, len(self.times) - 2)
        if index < 0:
            index = 0
        left_time = self.times[index]
        right_time = self.times[index + 1]
        left_phi = self.fractions[index]
        right_phi = self.fractions[index + 1]
        ratio = (time_min - left_time) / (right_time - left_time)
        composition = left_phi + ratio * (right_phi - left_phi)
        log_value = -_log_retention_factor(self.parameters, composition)
        if not math.isfinite(log_value):
            raise OverflowError("inverse retention factor has a nonfinite logarithm")
        self.cache[time_min] = log_value
        return log_value


def _adaptive_simpson(
    left: float,
    right: float,
    *,
    scale: float,
    evaluator: _EvaluationBudget,
    log_absolute_tolerance_scaled: float,
) -> tuple[float, float]:
    """Return log integral and log Simpson error after scaling the integrand."""

    def scaled_value(time_min: float) -> float:
        delta = evaluator.log_inverse_factor(time_min) - scale
        if delta < _LOG_MIN_SUBNORMAL:
            return 0.0
        return math.exp(delta)

    def simpson(a: float, b: float, fa: float, fm: float, fb: float) -> float:
        return (b - a) * (fa + 4.0 * fm + fb) / 6.0

    middle = left + (right - left) / 2.0
    if middle == left or middle == right:
        raise RuntimeError("retention quadrature interval cannot be subdivided")
    fa = scaled_value(left)
    fm = scaled_value(middle)
    fb = scaled_value(right)
    whole = simpson(left, right, fa, fm, fb)

    def refine(
        a: float,
        b: float,
        f_a: float,
        f_m: float,
        f_b: float,
        estimate: float,
        log_tolerance: float,
        depth: int,
    ) -> tuple[float, float]:
        m = a + (b - a) / 2.0
        left_middle = a + (m - a) / 2.0
        right_middle = m + (b - m) / 2.0
        if left_middle in (a, m) or right_middle in (m, b):
            raise RuntimeError("retention quadrature reached time precision limit")
        f_left_middle = scaled_value(left_middle)
        f_right_middle = scaled_value(right_middle)
        left_estimate = simpson(a, m, f_a, f_left_middle, f_m)
        right_estimate = simpson(m, b, f_m, f_right_middle, f_b)
        refined = left_estimate + right_estimate
        difference = abs(refined - estimate)
        error = difference / 15.0
        log_error = math.log(error) if error > 0 else -math.inf
        if log_error <= log_tolerance:
            corrected = refined + (refined - estimate) / 15.0
            if corrected <= 0.0:
                corrected = refined
            log_value = math.log(corrected) if corrected > 0.0 else -math.inf
            return log_value, log_error
        if depth >= _MAX_ADAPTIVE_DEPTH:
            raise RuntimeError("retention quadrature did not meet its error tolerance")
        half_tolerance = log_tolerance - math.log(2.0)
        left_value, left_error = refine(
            a,
            m,
            f_a,
            f_left_middle,
            f_m,
            left_estimate,
            half_tolerance,
            depth + 1,
        )
        right_value, right_error = refine(
            m,
            b,
            f_m,
            f_right_middle,
            f_b,
            right_estimate,
            half_tolerance,
            depth + 1,
        )
        return _log_add(left_value, right_value), _log_add(left_error, right_error)

    return refine(left, right, fa, fm, fb, whole, log_absolute_tolerance_scaled, 0)


def _integrate_prefix(
    stop_min: float,
    *,
    parameters: RetentionParameters,
    times: list[float],
    fractions: list[float],
    log_dead_time: float,
    budget: _EvaluationBudget,
    log_absolute_tolerance_min: float,
) -> tuple[float, float]:
    """Return log(integral/t0) and log(error/t0) through ``stop_min``."""
    log_integral_ratio = -math.inf
    log_error_ratio = -math.inf
    interval_count = sum(left < stop_min for left in times[:-1])
    if interval_count == 0:
        return log_integral_ratio, log_error_ratio
    per_interval_tolerance = log_absolute_tolerance_min - math.log(interval_count)
    for index, (left, grid_right) in enumerate(zip(times, times[1:])):
        if left >= stop_min:
            break
        right = min(stop_min, grid_right)
        if right <= left:
            continue
        left_log_value = budget.log_inverse_factor(left)
        right_log_value = budget.log_inverse_factor(right)
        log_lower_bound_ratio = (
            min(left_log_value, right_log_value) + math.log(right - left) - log_dead_time
        )
        if log_lower_bound_ratio >= 0.0:
            # Each integration-grid interval is monotone in log(1/k). This
            # lower bound is enough to establish that the cumulative integral
            # has crossed t0, even when exp(log(1/k)) is outside float range.
            return log_lower_bound_ratio, -math.inf
        midpoint = left + (right - left) / 2.0
        scale = max(
            left_log_value,
            budget.log_inverse_factor(midpoint),
            right_log_value,
        )
        log_scaled_integral, log_scaled_error = _adaptive_simpson(
            left,
            right,
            scale=scale,
            evaluator=budget,
            log_absolute_tolerance_scaled=per_interval_tolerance - scale,
        )
        if log_scaled_integral != -math.inf:
            log_ratio = scale + log_scaled_integral - log_dead_time
            log_integral_ratio = _log_add(log_integral_ratio, log_ratio)
        if log_scaled_error != -math.inf:
            log_error = scale + log_scaled_error - log_dead_time
            log_error_ratio = _log_add(log_error_ratio, log_error)
    return log_integral_ratio, log_error_ratio


def _interval_state(log_value: float, log_error: float) -> Literal["below", "above", "uncertain"]:
    log_upper = _log_add(log_value, log_error)
    if log_upper < 0.0:
        return "below"
    if log_value > log_error:
        log_lower = log_value + math.log1p(-math.exp(log_error - log_value))
        if log_lower >= 0.0:
            return "above"
    return "uncertain"


def _roundoff_guard_ratio(parameters: RetentionParameters, budget: _EvaluationBudget) -> float:
    """Estimate a nonshrinking relative floor for binary64 law/integral work.

    This is a conservative guard for classification, not a formal quadrature
    certificate. Scaling it by coefficient and log-integrand magnitude makes
    cancellation-prone laws fail closed instead of gaining false precision.
    """
    s2 = 0.0 if parameters.S2 is None else abs(parameters.S2)
    log_magnitude = max((abs(value) for value in budget.cache.values()), default=0.0)
    try:
        scale = math.fsum((1.0, abs(parameters.ln_k0), parameters.S1, s2, log_magnitude))
    except OverflowError as error:
        raise RuntimeError("retention law rounding is numerically indeterminate") from error
    guard = _ROUND_OFF_GUARD_ULPS * math.ulp(1.0) * scale
    if not math.isfinite(guard) or guard >= 1.0:
        raise RuntimeError("retention law rounding is numerically indeterminate")
    return guard


def _apply_roundoff_guard(log_value: float, log_error: float, relative_guard: float) -> float:
    if log_value == -math.inf or relative_guard <= 0.0:
        return log_error
    log_floor = (
        math.inf if not math.isfinite(relative_guard) else log_value + math.log(relative_guard)
    )
    return _log_add(log_error, log_floor)


def _positive_from_log(name: str, log_value: float) -> float:
    if log_value > _LOG_MAX_FLOAT:
        raise OverflowError(f"{name} overflows the supported numeric range")
    if log_value < _LOG_MIN_SUBNORMAL:
        raise OverflowError(f"{name} underflows the supported numeric range")
    value = math.exp(log_value)
    if value <= 0.0 or not math.isfinite(value):
        raise OverflowError(f"{name} is not representable as a positive finite number")
    return value


def _fraction_to_float(name: str, value: Fraction, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except OverflowError as error:
        raise OverflowError(f"{name} overflows the supported numeric range") from error
    if not math.isfinite(result):
        raise OverflowError(f"{name} is not finite")
    if positive and result <= 0.0:
        raise OverflowError(f"{name} underflows the supported numeric range")
    return result


def _fraction_rounding_error(exact: Fraction, rounded: float) -> float:
    difference = abs(exact - Fraction.from_float(rounded))
    if difference == 0:
        return 0.0
    try:
        result = float(difference)
    except OverflowError:
        return math.inf
    return result if result > 0.0 else math.ulp(0.0)


def _nonnegative_fraction_to_float(name: str, value: Fraction) -> float:
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    result = _fraction_to_float(name, value)
    if value > 0 and result == 0.0:
        return math.ulp(0.0)
    return result


def _nonnegative_from_log(name: str, log_value: float) -> float:
    if log_value == -math.inf:
        return 0.0
    if log_value < _LOG_MIN_SUBNORMAL:
        return math.ulp(0.0)
    if log_value > _LOG_MAX_FLOAT:
        raise OverflowError(f"{name} overflows the supported numeric range")
    value = math.exp(log_value)
    if not math.isfinite(value):
        raise OverflowError(f"{name} is not finite")
    return value


def _roundoff_estimate(*values: float) -> float:
    return 4.0 * max(math.ulp(value) for value in values)


def _exact_log_retention_factor(
    parameters: RetentionParameters, composition: float
) -> Fraction | None:
    """Return an exact rational log-k where the selected law is rational."""
    phi = Fraction.from_float(composition)
    ln_k0 = Fraction.from_float(parameters.ln_k0)
    s1 = Fraction.from_float(parameters.S1)
    if parameters.model == "linear":
        return ln_k0 - s1 * phi
    if parameters.model == "quadratic":
        assert parameters.S2 is not None
        s2 = Fraction.from_float(parameters.S2)
        return ln_k0 - s1 * phi - s2 * phi * phi
    if parameters.S2 == 0.0:
        return ln_k0 - s1 * phi
    return None


def _float_down_from_fraction(value: Fraction) -> float:
    rounded = _fraction_to_float("retention_zeta_limit_min", value)
    if Fraction.from_float(rounded) > value:
        rounded = math.nextafter(rounded, 0.0)
    return rounded


def _fraction_log(value: Fraction) -> float:
    """Compute a finite log for a positive Fraction without converting it."""
    if value <= 0:
        raise ValueError("value must be positive")
    return math.log(value.numerator) - math.log(value.denominator)


def _endpoint_integral(
    stop_min: float,
    *,
    parameters: RetentionParameters,
    times: list[float],
    fractions: list[float],
    log_dead_time: float,
    budget: _EvaluationBudget,
    log_absolute_tolerance_min: float,
    omitted_tail: Fraction,
    relative_dead_time_error: float,
) -> tuple[Literal["below", "above"], float, float]:
    """Resolve endpoint status or raise; tolerance never changes the status."""
    log_omitted_tail_error = -math.inf
    if omitted_tail > 0:
        next_time = min(times[-1], math.nextafter(stop_min, math.inf))
        probes = [stop_min, next_time]
        knot_index = bisect_right(times, stop_min)
        if knot_index < len(times) - 1 and times[knot_index] <= next_time:
            probes.append(times[knot_index])
        log_max_integrand = max(budget.log_inverse_factor(time) for time in probes)
        log_omitted_tail_error = log_max_integrand + _fraction_log(omitted_tail) - log_dead_time
    log_geometry_error = (
        math.log(relative_dead_time_error) if relative_dead_time_error > 0.0 else -math.inf
    )

    log_tolerance = log_absolute_tolerance_min
    for _ in range(14):
        log_value, log_error = _integrate_prefix(
            stop_min,
            parameters=parameters,
            times=times,
            fractions=fractions,
            log_dead_time=log_dead_time,
            budget=budget,
            log_absolute_tolerance_min=log_tolerance,
        )
        log_error = _log_add(log_error, log_geometry_error)
        log_error = _apply_roundoff_guard(
            log_value, log_error, _roundoff_guard_ratio(parameters, budget)
        )
        state = _interval_state(log_value, log_error)
        if state == "above":
            return state, log_value, log_error
        log_error = _log_add(log_error, log_omitted_tail_error)
        state = _interval_state(log_value, log_error)
        if state == "below" or state == "above":
            return state, log_value, log_error
        log_tolerance -= math.log(16.0)
    raise RuntimeError("retention at the acquisition endpoint is numerically indeterminate")


def _result(
    *,
    status: Literal["eluted", "not_eluted"],
    duration: float,
    dead_time: float,
    post_transit: float,
    zeta: float | None,
    numerical_error: float,
    evaluation_count: int,
    diagnostics: list[dict[str, object]],
    absolute_tolerance_min: float,
    exact_dead_time: Fraction,
    exact_post_transit: Fraction,
    exact_zeta: Fraction | None = None,
    certified_endpoint: bool = False,
) -> dict[str, Any]:
    if status == "eluted":
        if zeta is None:
            raise ValueError("eluted result requires a retention zeta")
        if certified_endpoint:
            retention_time = duration
        else:
            retention_time = math.fsum((zeta, dead_time, post_transit))
        exact_retention_time = (
            (Fraction.from_float(zeta) if exact_zeta is None else exact_zeta)
            + exact_dead_time
            + exact_post_transit
        )
        if not certified_endpoint and exact_retention_time > Fraction.from_float(duration):
            raise RuntimeError("computed retention time exceeds the acquisition window")
        if not certified_endpoint and retention_time > duration:
            raise RuntimeError("rounded retention time exceeds the acquisition window")
        numerical_error = math.fsum((numerical_error, math.ulp(retention_time)))
        if numerical_error > absolute_tolerance_min:
            raise RuntimeError(
                "retention time cannot meet absolute_tolerance_min at float precision"
            )
        censor_bound = None
    else:
        retention_time = None
        zeta = None
        censor_bound = duration
    if retention_time is not None and not math.isfinite(retention_time):
        raise OverflowError("retention_time_min is not finite")
    return {
        "profile": "ideal-lc-retention-zeta/1",
        "status": status,
        "retention_time_min": retention_time,
        "dead_time_min": dead_time,
        "post_column_transit_min": post_transit,
        "retention_zeta_min": zeta,
        "right_censor_bound_min": censor_bound,
        "numerical_error_estimate_min": numerical_error,
        "evaluation_count": evaluation_count,
        "diagnostics": diagnostics,
    }


def predict_retention_time(
    *,
    parameters: RetentionParameters,
    gradient_times_min: Sequence[Real],
    gradient_fractions: Sequence[Real],
    flow_rate_ml_min: Real,
    column_length_m: Real,
    effective_area_m2: Real,
    acquisition_duration_min: Real,
    post_column_volume_ml: Real = 0.0,
    absolute_tolerance_min: Real = 1e-7,
    max_evaluations: int = _DEFAULT_MAX_EVALUATIONS,
) -> dict[str, Any]:
    """Predict an ideal retention time or report right-censoring at run end.

    ``gradient_times_min`` must start at zero and end exactly at the declared
    acquisition duration. Fractions are piecewise linear between these points.
    The solver integrates in log-scaled coordinates and bisects a monotone
    integral, with adaptive Simpson error estimates and a bounded evaluation
    cache. Numerical failures and evaluation-budget exhaustion raise rather
    than being reported as ``not_eluted``.
    """
    model = _validate_parameters(parameters)
    duration = _finite_positive("acquisition_duration_min", acquisition_duration_min)
    flow = _finite_positive("flow_rate_ml_min", flow_rate_ml_min)
    length = _finite_positive("column_length_m", column_length_m)
    area = _finite_positive("effective_area_m2", effective_area_m2)
    post_volume = _finite_nonnegative("post_column_volume_ml", post_column_volume_ml)
    absolute_tolerance = _finite_positive("absolute_tolerance_min", absolute_tolerance_min)
    if isinstance(max_evaluations, bool) or not isinstance(max_evaluations, int):
        raise TypeError("max_evaluations must be a positive integer")
    if max_evaluations < 1:
        raise ValueError("max_evaluations must be a positive integer")

    times, fractions = _gradient_arrays(gradient_times_min, gradient_fractions, duration)
    flow_exact = Fraction.from_float(flow)
    exact_dead_time = (
        Fraction.from_float(length) * Fraction.from_float(area) * 1_000_000 / flow_exact
    )
    exact_post_transit = Fraction.from_float(post_volume) / flow_exact
    dead_time = _fraction_to_float("dead_time_min", exact_dead_time, positive=True)
    post_transit = (
        0.0
        if post_volume == 0.0
        else _fraction_to_float("post_column_transit_min", exact_post_transit, positive=True)
    )
    geometry_error = math.fsum(
        (
            _fraction_rounding_error(exact_dead_time, dead_time),
            _fraction_rounding_error(exact_post_transit, post_transit),
        )
    )
    log_dead_time = math.log(dead_time)
    exact_zeta_limit = Fraction.from_float(duration) - exact_dead_time - exact_post_transit
    if exact_zeta_limit <= 0:
        return _result(
            status="not_eluted",
            duration=duration,
            dead_time=dead_time,
            post_transit=post_transit,
            zeta=None,
            numerical_error=geometry_error,
            evaluation_count=0,
            diagnostics=[{"code": "no_zeta_time_available_within_acquisition_window"}],
            absolute_tolerance_min=absolute_tolerance,
            exact_dead_time=exact_dead_time,
            exact_post_transit=exact_post_transit,
        )
    zeta_limit = _float_down_from_fraction(exact_zeta_limit)
    if zeta_limit <= 0.0:
        raise RuntimeError("positive acquisition zeta limit is below float time precision")
    omitted_tail = exact_zeta_limit - Fraction.from_float(zeta_limit)
    relative_dead_time_error = geometry_error / dead_time

    constant_retention_law = model.S1 == 0.0 and (model.model == "linear" or model.S2 == 0.0)
    if constant_retention_law or all(fraction == fractions[0] for fraction in fractions[1:]):
        log_k = _log_retention_factor(model, fractions[0])
        exact_log_k = _exact_log_retention_factor(model, fractions[0])
        if exact_log_k == 0:
            exact_zeta = exact_dead_time
            exact_retention_time = exact_dead_time * 2 + exact_post_transit
            if exact_retention_time > Fraction.from_float(duration):
                return _result(
                    status="not_eluted",
                    duration=duration,
                    dead_time=dead_time,
                    post_transit=post_transit,
                    zeta=None,
                    numerical_error=geometry_error,
                    evaluation_count=1,
                    diagnostics=[{"code": "retention_not_eluted_by_acquisition_window"}],
                    absolute_tolerance_min=absolute_tolerance,
                    exact_dead_time=exact_dead_time,
                    exact_post_transit=exact_post_transit,
                )
            certified_endpoint = exact_retention_time == Fraction.from_float(duration)
            return _result(
                status="eluted",
                duration=duration,
                dead_time=dead_time,
                post_transit=post_transit,
                zeta=dead_time,
                exact_zeta=exact_zeta,
                numerical_error=math.fsum(
                    (
                        geometry_error,
                        _fraction_rounding_error(exact_dead_time, dead_time),
                    )
                ),
                evaluation_count=1,
                diagnostics=[],
                absolute_tolerance_min=absolute_tolerance,
                exact_dead_time=exact_dead_time,
                exact_post_transit=exact_post_transit,
                certified_endpoint=certified_endpoint,
            )

        factor = _positive_from_log("retention_factor", log_k)
        # Bound rounded law evaluation and exp before using it to decide which
        # side of the acquisition endpoint the peak lies on.
        log_terms = [log_k, model.ln_k0, model.S1 * fractions[0]]
        if model.model == "quadratic":
            assert model.S2 is not None
            log_terms.append(model.S2 * fractions[0] * fractions[0])
        elif model.model == "neue-kuss":
            assert model.S2 is not None
            s2_phi = model.S2 * fractions[0]
            log_terms.extend(
                (
                    s2_phi,
                    2.0 * math.log1p(s2_phi),
                    model.S1 * fractions[0] / (1.0 + s2_phi),
                )
            )
        log_error = 8.0 * max(math.ulp(term) for term in log_terms if math.isfinite(term))
        factor_low = math.nextafter(
            _positive_from_log("retention_factor lower bound", log_k - log_error), 0.0
        )
        factor_high = math.nextafter(
            _positive_from_log("retention_factor upper bound", log_k + log_error), math.inf
        )
        if not math.isfinite(factor_high):
            raise RuntimeError(
                "isocratic retention at the acquisition endpoint is numerically indeterminate"
            )
        exact_zeta_low = exact_dead_time * Fraction.from_float(factor_low)
        exact_zeta = exact_dead_time * Fraction.from_float(factor)
        exact_zeta_high = exact_dead_time * Fraction.from_float(factor_high)
        exact_duration = Fraction.from_float(duration)
        retention_low = exact_zeta_low + exact_dead_time + exact_post_transit
        retention_center = exact_zeta + exact_dead_time + exact_post_transit
        retention_high = exact_zeta_high + exact_dead_time + exact_post_transit
        if retention_low > exact_duration:
            return _result(
                status="not_eluted",
                duration=duration,
                dead_time=dead_time,
                post_transit=post_transit,
                zeta=None,
                numerical_error=geometry_error,
                evaluation_count=1,
                diagnostics=[{"code": "retention_not_eluted_by_acquisition_window"}],
                absolute_tolerance_min=absolute_tolerance,
                exact_dead_time=exact_dead_time,
                exact_post_transit=exact_post_transit,
            )
        if retention_high > exact_duration:
            raise RuntimeError(
                "isocratic retention at the acquisition endpoint is numerically indeterminate"
            )
        zeta = _fraction_to_float("retention_zeta_min", exact_zeta, positive=True)
        law_uncertainty = _nonnegative_fraction_to_float(
            "retention_law_uncertainty_min",
            max(retention_center - retention_low, retention_high - retention_center),
        )
        return _result(
            status="eluted",
            duration=duration,
            dead_time=dead_time,
            post_transit=post_transit,
            zeta=zeta,
            exact_zeta=exact_zeta,
            numerical_error=math.fsum(
                (
                    geometry_error,
                    law_uncertainty,
                    _fraction_rounding_error(exact_zeta, zeta),
                )
            ),
            evaluation_count=1,
            diagnostics=[],
            absolute_tolerance_min=absolute_tolerance,
            exact_dead_time=exact_dead_time,
            exact_post_transit=exact_post_transit,
        )

    grid_times, grid_fractions = _integration_grid(model, times, fractions)
    budget = _EvaluationBudget(model, grid_times, grid_fractions, max_evaluations)
    log_atol = math.log(absolute_tolerance / 4.0)
    state, total_log, total_log_error = _endpoint_integral(
        zeta_limit,
        parameters=model,
        times=grid_times,
        fractions=grid_fractions,
        log_dead_time=log_dead_time,
        budget=budget,
        log_absolute_tolerance_min=log_atol,
        omitted_tail=omitted_tail,
        relative_dead_time_error=relative_dead_time_error,
    )
    if state == "below":
        error = (
            geometry_error
            if total_log_error == -math.inf
            else math.fsum(
                (
                    geometry_error,
                    _nonnegative_from_log(
                        "numerical_error_estimate_min", log_dead_time + total_log_error
                    ),
                )
            )
        )
        return _result(
            status="not_eluted",
            duration=duration,
            dead_time=dead_time,
            post_transit=post_transit,
            zeta=None,
            numerical_error=error,
            evaluation_count=budget.count,
            diagnostics=[{"code": "retention_not_eluted_by_acquisition_window"}],
            absolute_tolerance_min=absolute_tolerance,
            exact_dead_time=exact_dead_time,
            exact_post_transit=exact_post_transit,
        )

    left = 0.0
    right = zeta_limit
    root_width_target = absolute_tolerance / 4.0
    while right - left > root_width_target:
        midpoint = left + (right - left) / 2.0
        if midpoint == left or midpoint == right:
            raise RuntimeError("retention root reached time precision limit")
        midpoint_log_integrand = budget.log_inverse_factor(midpoint)
        root_log_tolerance = log_atol + midpoint_log_integrand - math.log(8.0)
        midpoint_state: Literal["below", "above", "uncertain"] = "uncertain"
        for _ in range(8):
            value, error = _integrate_prefix(
                midpoint,
                parameters=model,
                times=grid_times,
                fractions=grid_fractions,
                log_dead_time=log_dead_time,
                budget=budget,
                log_absolute_tolerance_min=root_log_tolerance,
            )
            if relative_dead_time_error > 0.0:
                error = _log_add(error, math.log(relative_dead_time_error))
            error = _apply_roundoff_guard(value, error, _roundoff_guard_ratio(model, budget))
            midpoint_state = _interval_state(value, error)
            if midpoint_state != "uncertain":
                break
            root_log_tolerance -= math.log(16.0)
        if midpoint_state == "below":
            left = midpoint
        elif midpoint_state == "above":
            right = midpoint
        else:
            raise RuntimeError("retention root could not be resolved within the evaluation budget")

    zeta = left + (right - left) / 2.0
    if zeta <= 0.0 or not math.isfinite(zeta):
        raise OverflowError("retention_zeta_min is not representable")
    error_estimate = (right - left) / 2.0
    return _result(
        status="eluted",
        duration=duration,
        dead_time=dead_time,
        post_transit=post_transit,
        zeta=zeta,
        numerical_error=math.fsum((error_estimate, geometry_error)),
        evaluation_count=budget.count,
        diagnostics=[],
        absolute_tolerance_min=absolute_tolerance,
        exact_dead_time=exact_dead_time,
        exact_post_transit=exact_post_transit,
        exact_zeta=Fraction.from_float(zeta),
    )
