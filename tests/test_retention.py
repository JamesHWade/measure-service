"""Numerical checks for ideal linear-gradient retention references."""

import json
import math

import pytest

from measure_methods.retention import (
    RetentionParameters,
    predict_retention_time,
    retention_factor,
)


def _predict(parameters, **overrides):
    values = {
        "parameters": parameters,
        "gradient_times_min": [0.0, 10.0],
        "gradient_fractions": [0.2, 0.2],
        "flow_rate_ml_min": 1.0,
        "column_length_m": 1.0,
        "effective_area_m2": 1e-6,
        "acquisition_duration_min": 10.0,
        "post_column_volume_ml": 0.0,
        "absolute_tolerance_min": 1e-7,
        "max_evaluations": 100_000,
    }
    values.update(overrides)
    return predict_retention_time(**values)


def _reference_log_k(parameters, composition):
    if parameters.model == "linear":
        return parameters.ln_k0 - parameters.S1 * composition
    if parameters.model == "quadratic":
        return (
            parameters.ln_k0
            - parameters.S1 * composition
            - parameters.S2 * composition * composition
        )
    return (
        parameters.ln_k0
        + 2.0 * math.log1p(parameters.S2 * composition)
        - parameters.S1 * composition / (1.0 + parameters.S2 * composition)
    )


def _reference_integral(parameters, left_phi, right_phi, end_min, *, intervals=4096):
    """Composite Simpson reference for a single linear inlet ramp."""
    if end_min == 0:
        return 0.0
    step = end_min / intervals
    weighted_sum = 0.0
    for index in range(intervals + 1):
        time_min = index * step
        fraction = left_phi + (right_phi - left_phi) * time_min / 3.0
        integrand = math.exp(-_reference_log_k(parameters, fraction))
        weight = 1 if index in (0, intervals) else 4 if index % 2 else 2
        weighted_sum += weight * integrand
    return weighted_sum * step / 3.0


def _reference_root(parameters, *, target=1.0, left_phi=0.05, right_phi=0.85):
    low, high = 0.0, 2.0
    for _ in range(50):
        middle = (low + high) / 2.0
        value = _reference_integral(parameters, left_phi, right_phi, middle)
        if value < target:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def test_retention_factor_matches_all_three_declared_laws():
    linear = RetentionParameters("linear", math.log(3.0), 2.0)
    quadratic = RetentionParameters("quadratic", math.log(2.0), 1.0, 2.0)
    neue_kuss = RetentionParameters("neue-kuss", math.log(2.0), 1.0, 2.0)

    assert retention_factor(parameters=linear, composition=0.25) == pytest.approx(
        3.0 * math.exp(-0.5)
    )
    assert retention_factor(parameters=quadratic, composition=0.5) == pytest.approx(
        2.0 * math.exp(-1.0)
    )
    assert retention_factor(parameters=neue_kuss, composition=0.5) == pytest.approx(
        8.0 * math.exp(-0.25)
    )


def test_isocratic_shortcut_matches_dead_time_times_one_plus_k():
    parameters = RetentionParameters("linear", math.log(3.0), 0.0)
    result = _predict(
        parameters,
        gradient_fractions=[0.2, 0.2],
        acquisition_duration_min=10.0,
        post_column_volume_ml=0.5,
    )

    assert result["profile"] == "ideal-lc-retention-zeta/1"
    assert result["status"] == "eluted"
    assert result["dead_time_min"] == pytest.approx(1.0)
    assert result["post_column_transit_min"] == pytest.approx(0.5)
    assert result["retention_zeta_min"] == pytest.approx(3.0)
    assert result["retention_time_min"] == pytest.approx(1.0 * (1.0 + 3.0) + 0.5)
    assert result["evaluation_count"] == 1


def test_linear_ramp_matches_closed_form_integral_and_root():
    parameters = RetentionParameters("linear", math.log(2.0), 1.2)
    phi0, phi1, duration = 0.1, 0.9, 10.0
    slope = (phi1 - phi0) / duration
    intercept = math.exp(-parameters.ln_k0 + parameters.S1 * phi0)
    exponent_slope = parameters.S1 * slope
    expected_zeta = math.log1p(exponent_slope / intercept) / exponent_slope

    result = _predict(
        parameters,
        gradient_times_min=[0.0, duration],
        gradient_fractions=[phi0, phi1],
        acquisition_duration_min=duration,
    )

    assert result["status"] == "eluted"
    assert result["retention_zeta_min"] == pytest.approx(expected_zeta, rel=0, abs=1e-7)
    assert result["retention_time_min"] == pytest.approx(expected_zeta + 1.0, rel=0, abs=1e-7)


def test_piecewise_linear_gradient_splits_integral_at_program_knot():
    parameters = RetentionParameters("linear", 0.0, 1.0)
    t0 = 3.0
    first_slope = (0.5 - 0.1) / 2.0
    first_integral = math.exp(0.1) * math.expm1(first_slope * 2.0) / first_slope
    second_slope = (0.9 - 0.5) / 8.0
    second_intercept = math.exp(0.5)
    remaining = t0 - first_integral
    expected_zeta = 2.0 + math.log1p(remaining * second_slope / second_intercept) / second_slope

    result = _predict(
        parameters,
        gradient_times_min=[0.0, 2.0, 10.0],
        gradient_fractions=[0.1, 0.5, 0.9],
        column_length_m=3.0,
    )

    assert result["dead_time_min"] == pytest.approx(t0)
    assert result["retention_zeta_min"] == pytest.approx(expected_zeta, rel=0, abs=1e-7)


@pytest.mark.parametrize(
    "parameters",
    [
        RetentionParameters("quadratic", 0.4, 2.0, 0.7),
        RetentionParameters("neue-kuss", 0.4, 2.0, 0.7),
    ],
)
def test_quadratic_and_neue_kuss_match_independent_composite_simpson(parameters):
    expected_zeta = _reference_root(parameters)
    result = _predict(
        parameters,
        gradient_times_min=[0.0, 3.0],
        gradient_fractions=[0.05, 0.85],
        acquisition_duration_min=3.0,
    )

    assert result["status"] == "eluted"
    assert result["retention_zeta_min"] == pytest.approx(expected_zeta, rel=0, abs=2e-7)


def test_tighter_tolerance_converges_and_reports_bounded_root_error():
    parameters = RetentionParameters("quadratic", 0.3, 1.7, 0.6)
    coarse = _predict(
        parameters,
        gradient_fractions=[0.05, 0.9],
        acquisition_duration_min=10.0,
        absolute_tolerance_min=1e-4,
    )
    fine = _predict(
        parameters,
        gradient_fractions=[0.05, 0.9],
        acquisition_duration_min=10.0,
        absolute_tolerance_min=1e-7,
    )

    assert abs(coarse["retention_zeta_min"] - fine["retention_zeta_min"]) <= 1e-4
    assert fine["numerical_error_estimate_min"] <= 1e-7
    assert fine["evaluation_count"] <= 100_000


def test_tiny_gradient_approaches_the_isocratic_solution():
    parameters = RetentionParameters("linear", math.log(1.8), 1e-12)
    result = _predict(
        parameters,
        gradient_times_min=[0.0, 10.0],
        gradient_fractions=[0.2, 0.200000000001],
        acquisition_duration_min=10.0,
    )
    expected_zeta = math.exp(parameters.ln_k0 - parameters.S1 * 0.2)

    assert result["status"] == "eluted"
    assert result["retention_zeta_min"] == pytest.approx(expected_zeta, rel=0, abs=1e-7)


def test_log_scaled_integral_handles_inverse_factor_above_float_range():
    parameters = RetentionParameters("linear", -600.0, 200.0)
    result = _predict(
        parameters,
        gradient_times_min=[0.0, 10.0],
        gradient_fractions=[0.0, 1.0],
        acquisition_duration_min=10.0,
        absolute_tolerance_min=1e-7,
    )

    assert result["status"] == "eluted"
    assert math.isfinite(result["retention_time_min"])
    assert result["retention_zeta_min"] <= 1e-7


def test_dead_time_scales_with_column_geometry_and_post_volume_shifts_only_time():
    parameters = RetentionParameters("linear", 0.0, 0.0)
    first = _predict(parameters, acquisition_duration_min=10.0)
    doubled = _predict(
        parameters,
        column_length_m=2.0,
        acquisition_duration_min=10.0,
    )
    transported = _predict(
        parameters,
        acquisition_duration_min=10.0,
        post_column_volume_ml=0.5,
    )

    assert first["dead_time_min"] == pytest.approx(1.0)
    assert doubled["dead_time_min"] == pytest.approx(2.0)
    assert doubled["retention_time_min"] == pytest.approx(2.0 * first["retention_time_min"])
    assert transported["retention_zeta_min"] == pytest.approx(first["retention_zeta_min"])
    assert transported["retention_time_min"] - first["retention_time_min"] == pytest.approx(0.5)


def test_isocratic_censoring_and_endpoint_are_inclusive():
    parameters = RetentionParameters("linear", 0.0, 0.0)
    endpoint = _predict(
        parameters,
        gradient_times_min=[0.0, 2.0],
        gradient_fractions=[0.3, 0.3],
        acquisition_duration_min=2.0,
    )
    censored = _predict(
        parameters,
        gradient_times_min=[0.0, 1.9],
        gradient_fractions=[0.3, 0.3],
        acquisition_duration_min=1.9,
    )

    assert endpoint["status"] == "eluted"
    assert endpoint["retention_time_min"] == 2.0
    assert censored["status"] == "not_eluted"
    assert censored["retention_time_min"] is None
    assert censored["retention_zeta_min"] is None
    assert censored["right_censor_bound_min"] == 1.9


def test_short_window_before_dead_time_is_known_not_eluted():
    result = _predict(
        RetentionParameters("linear", 0.0, 0.0),
        gradient_times_min=[0.0, 0.5],
        gradient_fractions=[0.2, 0.2],
        acquisition_duration_min=0.5,
    )

    assert result["status"] == "not_eluted"
    assert result["retention_time_min"] is None
    assert result["right_censor_bound_min"] == 0.5
    assert result["evaluation_count"] == 0


def test_post_column_volume_can_move_an_otherwise_eluted_peak_past_censor_boundary():
    parameters = RetentionParameters("linear", 0.0, 0.0)
    result = _predict(
        parameters,
        gradient_times_min=[0.0, 2.5],
        gradient_fractions=[0.2, 0.2],
        acquisition_duration_min=2.5,
        post_column_volume_ml=0.6,
    )

    assert result["status"] == "not_eluted"
    assert result["right_censor_bound_min"] == 2.5
    assert result["post_column_transit_min"] == pytest.approx(0.6)


def test_evaluation_budget_exhaustion_raises_instead_of_claiming_censoring():
    with pytest.raises(RuntimeError, match="max_evaluations"):
        _predict(
            RetentionParameters("linear", 0.2, 1.0),
            gradient_times_min=[0.0, 10.0],
            gradient_fractions=[0.1, 0.9],
            max_evaluations=2,
        )


def test_model_domains_and_exponential_underflow_or_overflow_are_explicit():
    with pytest.raises(ValueError, match="S2 must be None"):
        RetentionParameters("linear", 1.0, 1.0, 0.0)
    with pytest.raises(ValueError, match="S2 is required"):
        RetentionParameters("quadratic", 1.0, 1.0)
    with pytest.raises(ValueError, match="nonnegative"):
        RetentionParameters("neue-kuss", 1.0, 1.0, -0.1)
    # Source fitting bounds are not imposed on this mathematical kernel.
    assert retention_factor(
        parameters=RetentionParameters("linear", -4.0, 0.0), composition=0.2
    ) == pytest.approx(math.exp(-4.0))
    with pytest.raises(TypeError):
        RetentionParameters("linear", True, 1.0)
    with pytest.raises(OverflowError, match="overflows"):
        retention_factor(parameters=RetentionParameters("linear", 1000.0, 0.0), composition=0.0)
    with pytest.raises(OverflowError, match="underflows"):
        retention_factor(parameters=RetentionParameters("linear", -1000.0, 0.0), composition=0.0)


def test_predict_rejects_bad_gradient_shapes_units_and_numeric_types():
    parameters = RetentionParameters("linear", 0.0, 0.0)
    with pytest.raises(ValueError, match="start at zero"):
        _predict(parameters, gradient_times_min=[1.0, 10.0])
    with pytest.raises(ValueError, match="strictly increasing"):
        _predict(
            parameters,
            gradient_times_min=[0.0, 10.0, 10.0],
            gradient_fractions=[0.2, 0.3, 0.4],
        )
    with pytest.raises(ValueError, match="final gradient time"):
        _predict(parameters, gradient_times_min=[0.0, 9.0])
    with pytest.raises(ValueError, match="equal lengths"):
        _predict(parameters, gradient_fractions=[0.2, 0.3, 0.4])
    with pytest.raises(ValueError, match="between zero and one"):
        _predict(parameters, gradient_fractions=[0.2, 1.1])
    with pytest.raises(TypeError, match="flow_rate_ml_min"):
        _predict(parameters, flow_rate_ml_min="1.0")
    too_many = [index / 128 for index in range(129)]
    with pytest.raises(ValueError, match="between 2 and 128"):
        _predict(
            parameters,
            gradient_times_min=list(map(float, range(129))),
            gradient_fractions=too_many,
            acquisition_duration_min=128.0,
        )


def test_prediction_result_is_json_safe():
    result = _predict(
        RetentionParameters("neue-kuss", 0.2, 1.4, 0.3),
        gradient_times_min=[0.0, 4.0],
        gradient_fractions=[0.05, 0.8],
        acquisition_duration_min=4.0,
    )

    json.dumps(result, allow_nan=False)


def test_neue_kuss_down_ramp_splits_at_an_interior_extremum():
    parameters = RetentionParameters("neue-kuss", 0.4, 2.0, 0.7)
    expected_zeta = _reference_root(parameters, left_phi=0.9, right_phi=0.05)
    result = _predict(
        parameters,
        gradient_times_min=[0.0, 3.0],
        gradient_fractions=[0.9, 0.05],
        acquisition_duration_min=3.0,
    )

    assert result["status"] == "eluted"
    assert result["retention_zeta_min"] == pytest.approx(expected_zeta, rel=0, abs=2e-7)


def test_large_acquisition_window_preserves_small_isocratic_retention_time():
    result = _predict(
        RetentionParameters("linear", 0.1, 0.0),
        gradient_times_min=[0.0, 1e12],
        gradient_fractions=[0.1, 0.1],
        acquisition_duration_min=1e12,
    )
    expected = math.fsum((1.0, math.exp(0.1)))
    absolute_error = abs(result["retention_time_min"] - expected)

    assert result["status"] == "eluted"
    assert result["retention_time_min"] == pytest.approx(expected, rel=0, abs=1e-7)
    assert absolute_error <= result["numerical_error_estimate_min"]


def test_large_acquisition_window_preserves_small_ramp_retention_time():
    duration = 1e12
    phi0, phi1 = 0.1, 0.9
    slope = (phi1 - phi0) / duration
    result = _predict(
        RetentionParameters("linear", 0.0, 1.0),
        gradient_times_min=[0.0, duration],
        gradient_fractions=[phi0, phi1],
        acquisition_duration_min=duration,
    )
    expected_zeta = math.log1p(slope * math.exp(-phi0)) / slope
    expected = math.fsum((1.0, expected_zeta))
    absolute_error = abs(result["retention_time_min"] - expected)

    assert result["status"] == "eluted"
    assert result["retention_time_min"] == pytest.approx(expected, rel=0, abs=1e-7)
    assert absolute_error <= result["numerical_error_estimate_min"]
    assert result["numerical_error_estimate_min"] <= 1e-7


@pytest.mark.parametrize(
    ("length", "area", "duration"),
    [
        (2.0, 2**-24, 0.2384185791015625),
        (1.0, 2**-28, 0.007450580596923828),
    ],
)
def test_exact_binary_isocratic_endpoint_is_inclusive(length, area, duration):
    result = _predict(
        RetentionParameters("linear", 0.0, 0.0),
        gradient_times_min=[0.0, duration],
        gradient_fractions=[0.0, 0.0],
        column_length_m=length,
        effective_area_m2=area,
        acquisition_duration_min=duration,
    )

    assert result["status"] == "eluted"
    assert result["retention_time_min"] == duration
    assert result["retention_zeta_min"] == result["dead_time_min"]


def test_endpoint_decision_does_not_use_root_tolerance_as_elution_slack():
    result = _predict(
        RetentionParameters("linear", 0.2603950891151677, 1.0),
        gradient_times_min=[0.0, 2.0],
        gradient_fractions=[0.0, 1.0],
        acquisition_duration_min=2.0,
        absolute_tolerance_min=1e-7,
    )

    assert result["status"] == "not_eluted"
    assert result["retention_time_min"] is None
    assert result["right_censor_bound_min"] == 2.0


def test_isocratic_exp_rounding_cannot_make_a_late_exact_law_look_on_time():
    with pytest.raises(RuntimeError, match="numerically indeterminate"):
        _predict(
            RetentionParameters("linear", 1e-17, 0.0),
            gradient_times_min=[0.0, 0.2384185791015625],
            gradient_fractions=[0.0, 0.0],
            column_length_m=2.0,
            effective_area_m2=2**-24,
            acquisition_duration_min=0.2384185791015625,
        )


def test_nonconstant_tiny_law_does_not_invent_an_endpoint_classification():
    with pytest.raises(RuntimeError, match="numerically indeterminate"):
        _predict(
            RetentionParameters("linear", 1e-17, 1e-20),
            gradient_times_min=[0.0, 0.2384185791015625],
            gradient_fractions=[0.0, 1.0],
            column_length_m=2.0,
            effective_area_m2=2**-24,
            acquisition_duration_min=0.2384185791015625,
        )


def test_tiny_law_with_a_strictly_earlier_acquisition_end_remains_censored():
    result = _predict(
        RetentionParameters("linear", 1e-17, 1e-20),
        gradient_times_min=[0.0, 0.23841857199613514],
        gradient_fractions=[0.0, 1.0],
        column_length_m=2.0,
        effective_area_m2=2**-24,
        acquisition_duration_min=0.23841857199613514,
    )
    assert result["status"] == "not_eluted"
    assert result["retention_time_min"] is None


def test_ill_conditioned_law_coefficients_fail_closed_at_endpoint_classification():
    with pytest.raises(RuntimeError, match="numerically indeterminate"):
        _predict(
            RetentionParameters("linear", 1e15, 1e16),
            gradient_times_min=[0.0, 10.0],
            gradient_fractions=[0.1, 0.10000000000000002],
            acquisition_duration_min=10.0,
        )


def test_large_log_law_roundoff_is_not_treated_as_small_relative_integral_error():
    with pytest.raises(RuntimeError, match="numerically indeterminate"):
        _predict(
            RetentionParameters("linear", 1e18 + 128, 1e19),
            gradient_times_min=[0.0, 1.0],
            gradient_fractions=[0.1, math.nextafter(0.1, 0.0)],
            column_length_m=1.0,
            effective_area_m2=1e-46,
            acquisition_duration_min=1.0,
        )
