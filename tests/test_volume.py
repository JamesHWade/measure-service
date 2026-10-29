from collections import UserDict
from fractions import Fraction

import pytest

from measure_methods import (
    constant_solvent_use,
    linear_product_volume,
    linear_solvent_use,
    program_solvent_use,
)


def analytic_product_volume(duration, flow_start, flow_end, fraction_start, fraction_end):
    """Integrate the expanded quadratic polynomial term by term."""
    flow_slope = flow_end - flow_start
    fraction_slope = fraction_end - fraction_start
    constant = flow_start * fraction_start
    linear = flow_start * fraction_slope + flow_slope * fraction_start
    quadratic = flow_slope * fraction_slope
    return duration * (constant + linear / 2 + quadratic / 3)


def test_linear_product_volume_known_gradient_example():
    assert linear_product_volume(2, 1, 3, 0.2, 0.8) == 2.2


@pytest.mark.parametrize(
    ("duration", "flow_start", "flow_end", "fraction_start", "fraction_end"),
    [
        (3.5, 0.7, 4.2, 0.05, 0.9),
        (1.25, 9.0, 0.25, 0.8, 0.1),
        (7.0, 2.5, 2.5, 0.3, 0.7),
        (2.0, 0.0, 8.0, 0.25, 0.25),
        (4.0, 12.0, 0.0, 1.0, 0.0),
    ],
)
def test_linear_product_volume_matches_polynomial_integral(
    duration, flow_start, flow_end, fraction_start, fraction_end
):
    expected = analytic_product_volume(duration, flow_start, flow_end, fraction_start, fraction_end)
    actual = linear_product_volume(duration, flow_start, flow_end, fraction_start, fraction_end)
    assert actual == pytest.approx(expected, rel=1e-14)


def test_constant_zero_and_reversed_slopes():
    assert linear_product_volume(3, 2, 2, 0.25, 0.25) == 1.5
    assert linear_product_volume(0, 2, 4, 0.25, 0.75) == 0
    assert linear_product_volume(5, 0, 0, 1, 1) == 0

    forward = linear_product_volume(3, 1, 4, 0.1, 0.9)
    reversed_both = linear_product_volume(3, 4, 1, 0.9, 0.1)
    assert reversed_both == pytest.approx(forward)


def test_linear_product_volume_scales_with_duration_and_flow():
    base = linear_product_volume(2, 1, 3, 0.2, 0.8)
    scaled = linear_product_volume(6, 2, 6, 0.2, 0.8)
    assert scaled == pytest.approx(base * 6)


def test_linear_solvent_use_known_gradient_and_conservation():
    result = linear_solvent_use(
        2,
        1,
        3,
        {"A": 0.2, "B": 0.8},
        {"A": 0.8, "B": 0.2},
    )
    assert result == {"A": 2.2, "B": 1.8}
    assert sum(result.values()) == pytest.approx(2 * (1 + 3) / 2)


def test_linear_solvent_use_accepts_mapping_and_fraction_sum_tolerance():
    result = linear_solvent_use(
        1,
        2,
        2,
        UserDict({"A": 0.5000000004, "B": 0.5}),
        UserDict({"A": 0.5, "B": 0.5}),
    )
    assert result["A"] == pytest.approx(1.0)
    assert result["B"] == pytest.approx(1.0)


def test_constant_solvent_use_preserves_legacy_float_order():
    result = constant_solvent_use(0.1, 0.3, {"A": 0.7, "B": 0.3})
    linear_result = linear_solvent_use(
        0.1,
        0.3,
        0.3,
        {"A": 0.7, "B": 0.3},
        {"A": 0.7, "B": 0.3},
    )

    assert result == {"A": 0.021, "B": 0.009}
    assert result["A"] != linear_result["A"]


def test_constant_solvent_use_zero_duration_and_flow():
    mixture = {"A": 0.2, "B": 0.8}
    assert constant_solvent_use(0, 5, mixture) == {"A": 0.0, "B": 0.0}
    assert constant_solvent_use(5, 0, mixture) == {"A": 0.0, "B": 0.0}


def test_constant_solvent_use_stable_fallback_for_tiny_products():
    result = constant_solvent_use(
        1e308,
        1e-308,
        {"trace": 1e-308, "bulk": 1.0},
    )
    assert result["trace"] == pytest.approx(1e-308, rel=1e-14)
    assert result["bulk"] == pytest.approx(1.0, rel=1e-14)


@pytest.mark.parametrize(
    ("duration", "flow", "mixture"),
    [
        (2, 1.7e308, {"A": 1}),
        (1e-308, 1e-308, {"A": 1}),
    ],
)
def test_constant_solvent_use_unrepresentable_results_raise(duration, flow, mixture):
    with pytest.raises(ValueError):
        constant_solvent_use(duration, flow, mixture)


def test_constant_solvent_use_reuses_scalar_and_mixture_validation():
    with pytest.raises(TypeError):
        constant_solvent_use(True, 1, {"A": 1})
    with pytest.raises(ValueError):
        constant_solvent_use(1, 1, {"A": 0.5})


@pytest.mark.parametrize(
    ("args", "exception"),
    [
        (("1", 1, 1, 0, 1), TypeError),
        ((True, 1, 1, 0, 1), TypeError),
        ((1, False, 1, 0, 1), TypeError),
        ((1, 1, 1, "0.5", 1), TypeError),
        ((float("nan"), 1, 1, 0, 1), ValueError),
        ((1, 1, float("inf"), 0, 1), ValueError),
        ((Fraction(1, 10**400), 1, 1, 0, 1), ValueError),
        ((-1, 1, 1, 0, 1), ValueError),
        ((1, -1, 1, 0, 1), ValueError),
        ((1, 1, 1, -0.01, 1), ValueError),
        ((1, 1, 1, 0, 1.01), ValueError),
    ],
)
def test_linear_product_volume_rejects_invalid_inputs(args, exception):
    with pytest.raises(exception):
        linear_product_volume(*args)


@pytest.mark.parametrize(
    ("fractions_start", "fractions_end", "exception"),
    [
        ({}, {}, ValueError),
        ({"A": 1}, {"B": 1}, ValueError),
        ({"": 1}, {"": 1}, ValueError),
        ({"   ": 1}, {"   ": 1}, ValueError),
        ({1: 1}, {1: 1}, ValueError),
        ({"A": -0.1, "B": 1.1}, {"A": 0.5, "B": 0.5}, ValueError),
        ({"A": 0.7, "B": 0.2}, {"A": 0.5, "B": 0.5}, ValueError),
        ({"A": True}, {"A": 1}, TypeError),
        ([("A", 1)], {"A": 1}, TypeError),
    ],
)
def test_linear_solvent_use_rejects_invalid_mixtures(fractions_start, fractions_end, exception):
    with pytest.raises(exception):
        linear_solvent_use(1, 1, 1, fractions_start, fractions_end)


def test_linear_solvent_use_rejects_nonfinite_fraction_and_mixture_sum():
    with pytest.raises(ValueError):
        linear_solvent_use(1, 1, 1, {"A": float("inf")}, {"A": 1})
    with pytest.raises(ValueError):
        linear_solvent_use(1, 1, 1, {"A": 0.4, "B": 0.4}, {"A": 0.5, "B": 0.5})


def test_extreme_intermediate_underflow_uses_stable_fallback():
    result = linear_product_volume(1e308, 1e-308, 1e-308, 1e-308, 1e-308)
    assert result == pytest.approx(1e-308, rel=1e-14)


@pytest.mark.parametrize(
    ("args",),
    [
        ((2, 1.7e308, 1.7e308, 1, 1),),
        ((1e-308, 1e-308, 1e-308, 1, 1),),
    ],
)
def test_unrepresentable_final_results_raise(args):
    with pytest.raises(ValueError):
        linear_product_volume(*args)


def test_program_solvent_use_integrates_multiple_segments():
    times = [10, 12, 15]
    flows = [1, 3, 2]
    mixtures = [
        {"A": 0.2, "B": 0.8},
        {"A": 0.8, "B": 0.2},
        {"A": 0.6, "B": 0.4},
    ]
    result = program_solvent_use(times, flows, mixtures)

    expected = {
        "A": sum(
            analytic_product_volume(
                times[i + 1] - times[i],
                flows[i],
                flows[i + 1],
                mixtures[i]["A"],
                mixtures[i + 1]["A"],
            )
            for i in range(len(times) - 1)
        ),
        "B": sum(
            analytic_product_volume(
                times[i + 1] - times[i],
                flows[i],
                flows[i + 1],
                mixtures[i]["B"],
                mixtures[i + 1]["B"],
            )
            for i in range(len(times) - 1)
        ),
    }
    assert result == pytest.approx(expected)
    assert result["A"] + result["B"] == pytest.approx(
        sum(
            (times[i + 1] - times[i]) * (flows[i] + flows[i + 1]) / 2 for i in range(len(times) - 1)
        )
    )


def test_program_solvent_use_known_gradient_example():
    result = program_solvent_use(
        [0, 2],
        [1, 3],
        [{"A": 0.2, "B": 0.8}, {"A": 0.8, "B": 0.2}],
    )
    assert result == {"A": 2.2, "B": 1.8}


def test_program_solvent_use_is_independent_of_finite_time_origin():
    flows = [2, 5, 3]
    mixtures = [{"A": 0.3, "B": 0.7}, {"A": 0.6, "B": 0.4}, {"A": 1, "B": 0}]
    baseline = program_solvent_use([0, 1.5, 4], flows, mixtures)
    shifted = program_solvent_use([100, 101.5, 104], flows, mixtures)
    assert shifted == pytest.approx(baseline)


def test_program_solvent_use_handles_finite_times_with_unrepresentable_difference():
    result = program_solvent_use(
        [-1.7e308, 1.7e308],
        [1e-308, 1e-308],
        [{"A": 1}, {"A": 1}],
    )
    assert result["A"] == pytest.approx(3.4)


def test_program_solvent_use_accumulates_underflowed_segments_exactly():
    result = program_solvent_use(
        [0, 1, 2],
        [5e-324, 5e-324, 5e-324],
        [{"A": 0.5, "B": 0.5}] * 3,
    )
    assert result == {"A": 5e-324, "B": 5e-324}


def test_program_solvent_use_aggregates_subnormal_segments_before_rounding():
    fractions = {"A": 0.7, "B": 0.3}
    one_segment = linear_product_volume(1, 5e-324, 5e-324, 0.7, 0.7)
    result = program_solvent_use(
        [0, 1, 2],
        [5e-324, 5e-324, 5e-324],
        [fractions] * 3,
    )

    assert one_segment == 5e-324
    assert result == {"A": 5e-324, "B": 5e-324}


@pytest.mark.parametrize(
    ("times", "flows", "mixtures", "exception"),
    [
        ([0], [1], [{"A": 1}], ValueError),
        ([0, 1], [1], [{"A": 1}, {"A": 1}], ValueError),
        ([0, 1], [1, 1], [{"A": 1}], ValueError),
        ([0, 1], [1, 1], [{"A": 1}, {"B": 1}], ValueError),
        ([0, 1], [1, 1], [{"A": 1}, {"A": 0.8, "B": 0.2}], ValueError),
        ([0, 0], [1, 1], [{"A": 1}, {"A": 1}], ValueError),
        ([1, 0], [1, 1], [{"A": 1}, {"A": 1}], ValueError),
        ([0, float("nan")], [1, 1], [{"A": 1}, {"A": 1}], ValueError),
        ([0, 1], [1, -1], [{"A": 1}, {"A": 1}], ValueError),
        ([0, 1], [1, 1], [{"A": "1"}, {"A": 1}], TypeError),
    ],
)
def test_program_solvent_use_rejects_invalid_programs(times, flows, mixtures, exception):
    with pytest.raises(exception):
        program_solvent_use(times, flows, mixtures)


def test_program_solvent_use_rejects_nonrepresentable_total():
    with pytest.raises(ValueError):
        program_solvent_use(
            [0, 1.5, 3],
            [1e308, 1e308, 1e308],
            [{"A": 1}, {"A": 1}, {"A": 1}],
        )
