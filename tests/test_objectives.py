import json
import math
from dataclasses import FrozenInstanceError
from fractions import Fraction
from pathlib import Path

import pytest

from measure_methods.objectives import Criterion, hierarchical_score


def test_single_maximization_criterion_has_known_worst_threshold_and_best_scores():
    criterion = Criterion("yield", "maximize", 0, 2, 1)

    worst = hierarchical_score({"yield": 0}, [criterion], smoothing=math.log(3))
    at_threshold = hierarchical_score({"yield": 1}, [criterion], smoothing=math.log(3))
    above_threshold = hierarchical_score({"yield": 2}, [criterion], smoothing=math.log(3))

    assert worst["score"] == pytest.approx(0)
    assert worst["criteria"][0]["normalized_value"] == 0
    assert worst["criteria"][0]["gate"] == pytest.approx(0.25)
    assert worst["final_contribution"] == pytest.approx(-0.25)

    assert at_threshold["score"] == pytest.approx(1)
    assert at_threshold["criteria"][0]["normalized_value"] == 1
    assert at_threshold["criteria"][0]["gate"] == pytest.approx(0.5)
    assert at_threshold["criteria"][0]["contribution"] == pytest.approx(1)

    assert above_threshold["score"] == pytest.approx(2)
    assert above_threshold["criteria"][0]["normalized_value"] == 2
    assert above_threshold["criteria"][0]["gate"] == pytest.approx(0.75)
    assert above_threshold["criteria"][0]["contribution"] == pytest.approx(1.25)
    assert above_threshold["final_contribution"] == pytest.approx(0.75)
    assert above_threshold["final_rule"] == "excess"


def test_final_rule_value_preserves_the_complete_normalized_final_objective():
    result = hierarchical_score(
        {"yield": 2},
        [Criterion("yield", "maximize", 0, 2, 1)],
        smoothing=math.log(3),
        final_rule="value",
    )

    assert result["final_rule"] == "value"
    assert result["final_contribution"] == pytest.approx(1.5)
    assert result["score"] == pytest.approx(2.75)


def test_minimization_maps_lower_is_better_to_the_same_normalized_scale():
    criterion = Criterion("error", "minimize", 2, 10, 6)

    worst = hierarchical_score({"error": 10}, [criterion], smoothing=math.log(3))
    at_threshold = hierarchical_score({"error": 6}, [criterion], smoothing=math.log(3))
    best = hierarchical_score({"error": 2}, [criterion], smoothing=math.log(3))

    assert worst["criteria"][0]["normalized_value"] == 0
    assert worst["score"] == pytest.approx(0)
    assert at_threshold["criteria"][0]["normalized_value"] == 1
    assert at_threshold["score"] == pytest.approx(1)
    assert best["criteria"][0]["normalized_value"] == 2
    assert best["score"] == pytest.approx(2)


def test_hierarchy_weights_later_objectives_by_prior_gates_and_selects_final_objective():
    criteria = [
        Criterion("primary", "maximize", 0, 2, 1),
        Criterion("secondary", "maximize", 0, 2, 1),
    ]
    values = {"primary": 0, "secondary": 2}

    default_final = hierarchical_score(values, criteria, smoothing=math.log(3))
    selected_final = hierarchical_score(
        values,
        criteria,
        smoothing=math.log(3),
        final_objective="secondary",
    )

    assert default_final["final_objective"] == "primary"
    assert default_final["criteria"][0]["prefix_gate"] == pytest.approx(1)
    assert default_final["criteria"][1]["prefix_gate"] == pytest.approx(0.25)
    assert default_final["criteria"][0]["contribution"] == pytest.approx(0.25)
    assert default_final["criteria"][1]["contribution"] == pytest.approx(0.3125)
    assert default_final["final_gate"] == pytest.approx(0.1875)
    assert default_final["final_contribution"] == pytest.approx(-0.1875)
    assert default_final["score"] == pytest.approx(0.375)

    assert selected_final["final_objective"] == "secondary"
    assert selected_final["final_contribution"] == pytest.approx(0.1875)
    assert selected_final["score"] == pytest.approx(0.75)

    whole_final = hierarchical_score(
        values,
        criteria,
        smoothing=math.log(3),
        final_objective="primary",
        final_rule="value",
    )
    assert whole_final["final_rule"] == "value"
    assert whole_final["final_contribution"] == pytest.approx(0)
    assert whole_final["score"] == pytest.approx(0.5625)


def test_value_mapping_order_does_not_change_criterion_order_or_score():
    criteria = [
        Criterion("first", "maximize", 0, 2, 1),
        Criterion("second", "minimize", 2, 10, 6),
    ]
    ordered = hierarchical_score({"first": 2, "second": 6}, criteria, smoothing=math.log(3))
    reversed_mapping = hierarchical_score(
        {"second": 6, "first": 2}, criteria, smoothing=math.log(3)
    )

    assert [item["name"] for item in reversed_mapping["criteria"]] == ["first", "second"]
    assert reversed_mapping == ordered


@pytest.mark.parametrize(
    ("raw_value", "expected_clamped", "expected_normalized", "expected_score"),
    [(-100, 0, 0, 0), (100, 2, 2, 2)],
)
def test_values_outside_bounds_are_clamped_before_normalization(
    raw_value, expected_clamped, expected_normalized, expected_score
):
    result = hierarchical_score(
        {"yield": raw_value},
        [Criterion("yield", "maximize", 0, 2, 1)],
        smoothing=math.log(3),
    )

    assert result["criteria"][0]["raw_value"] == raw_value
    assert result["criteria"][0]["clamped_value"] == expected_clamped
    assert result["criteria"][0]["normalized_value"] == expected_normalized
    assert result["score"] == pytest.approx(expected_score)


def test_result_exposes_json_safe_decomposition_and_criterion_is_immutable():
    criterion = Criterion("yield", "maximize", 0, 2, 1)
    result = hierarchical_score({"yield": 2}, [criterion], smoothing=math.log(3))

    json.dumps(result, allow_nan=False)
    assert result["criteria"][0]["threshold"] == 1.0
    assert result["criteria"][0]["normalized_best"] == 2
    with pytest.raises(FrozenInstanceError):
        criterion.threshold = 0.5


@pytest.mark.parametrize(
    ("smoothing", "exception"),
    [
        (True, TypeError),
        ("1", TypeError),
        (0, ValueError),
        (-1, ValueError),
        (math.inf, ValueError),
        (math.nan, ValueError),
    ],
)
def test_invalid_smoothing_is_rejected(smoothing, exception):
    with pytest.raises(exception, match="smoothing"):
        hierarchical_score(
            {"yield": 1}, [Criterion("yield", "maximize", 0, 2, 1)], smoothing=smoothing
        )


@pytest.mark.parametrize(
    ("args", "exception", "message"),
    [
        (("", "maximize", 0, 1, 0.5), ValueError, "name"),
        ((1, "maximize", 0, 1, 0.5), TypeError, "name"),
        (("x", "increase", 0, 1, 0.5), ValueError, "direction"),
        (("x", "maximize", True, 1, 0.5), TypeError, "lower_bound"),
        (("x", "maximize", 0, 1, math.inf), ValueError, "threshold"),
        (("x", "maximize", 0, 1, 0), ValueError, "threshold"),
        (("x", "maximize", 0, 1, 1.1), ValueError, "threshold"),
        (("x", "minimize", 0, 1, 1), ValueError, "threshold"),
        (("x", "maximize", 1, 1, 1), ValueError, "lower_bound"),
        (("x", "maximize", 0, 1e308, 1e-308), ValueError, "normalized bound range"),
        (("x", "maximize", -1.7e308, 1.7e308, 0), ValueError, "bound range"),
    ],
)
def test_criterion_rejects_invalid_or_unrepresentable_parameters(args, exception, message):
    with pytest.raises(exception, match=message):
        Criterion(*args)


@pytest.mark.parametrize(
    ("value", "exception"),
    [
        (True, TypeError),
        ("1", TypeError),
        (math.inf, ValueError),
        (math.nan, ValueError),
        (Fraction(1, 10**400), ValueError),
    ],
)
def test_objective_values_must_be_finite_representable_real_numbers(value, exception):
    with pytest.raises(exception, match=r"values\['yield'\]"):
        hierarchical_score({"yield": value}, [Criterion("yield", "maximize", 0, 2, 1)])


def test_criteria_and_values_must_be_nonempty_exactly_named_and_unique():
    criterion = Criterion("yield", "maximize", 0, 2, 1)

    with pytest.raises(ValueError, match="must not be empty"):
        hierarchical_score({"yield": 1}, [])
    with pytest.raises(ValueError, match="unique"):
        hierarchical_score({"yield": 1}, [criterion, criterion])
    with pytest.raises(ValueError, match="missing values"):
        hierarchical_score({}, [criterion])
    with pytest.raises(ValueError, match="unknown value names"):
        hierarchical_score({"other": 1}, [criterion])
    with pytest.raises(ValueError, match="exactly"):
        hierarchical_score({"yield": 1, "extra": 2}, [criterion])


def test_final_objective_name_must_be_known():
    criteria = [Criterion("yield", "maximize", 0, 2, 1)]

    with pytest.raises(ValueError, match="not a criterion name"):
        hierarchical_score({"yield": 1}, criteria, final_objective="other")
    with pytest.raises(TypeError, match="final_objective"):
        hierarchical_score({"yield": 1}, criteria, final_objective=1)


@pytest.mark.parametrize("final_rule", ["absolute", "EXCESS"])
def test_final_rule_must_be_explicitly_supported(final_rule):
    with pytest.raises(ValueError, match="final_rule"):
        hierarchical_score(
            {"yield": 1},
            [Criterion("yield", "maximize", 0, 2, 1)],
            final_rule=final_rule,
        )


@pytest.mark.parametrize("final_rule", [1, None])
def test_final_rule_must_be_a_string(final_rule):
    with pytest.raises(TypeError, match="final_rule"):
        hierarchical_score(
            {"yield": 1},
            [Criterion("yield", "maximize", 0, 2, 1)],
            final_rule=final_rule,
        )


@pytest.mark.parametrize(
    ("criterion", "raw_value", "smoothing"),
    [
        (Criterion("large", "maximize", 1e308, 1.6e308, 1.2e308), 1.6e308, 1e308),
        (Criterion("large", "maximize", 1e308, 1.6e308, 1.2e308), 1.6e308, 1e-320),
    ],
)
def test_large_finite_values_and_extreme_smoothing_do_not_make_nan_scores(
    criterion, raw_value, smoothing
):
    result = hierarchical_score({"large": raw_value}, [criterion], smoothing=smoothing)

    assert result["criteria"][0]["normalized_value"] == pytest.approx(3)
    assert math.isfinite(result["criteria"][0]["gate"])
    assert math.isfinite(result["criteria"][0]["contribution"])
    assert math.isfinite(result["final_contribution"])
    assert math.isfinite(result["score"])
    json.dumps(result, allow_nan=False)


def test_independent_published_wheel_oracle_and_explicit_excess_formula():
    # Generated independently by executing the published BoTier 1.0.0 wheel;
    # its hashes and runtime are retained in the fixture. No BoTier dependency
    # or original archive data is needed to verify these equations.
    oracle = json.loads(
        (Path(__file__).parent / "fixtures" / "botier-synthetic-oracle.json").read_text()
    )
    assert oracle["case_count"] == len(oracle["cases"]) == 528
    names = ["primary", "secondary", "tertiary"]
    criteria = [Criterion(name, "maximize", 0, 5, 1) for name in names]
    for case in oracle["cases"]:
        values = dict(zip(names, case["values"], strict=True))
        for rule, expected in (
            ("value", "published_expected_score"),
            ("excess", "excess_expected_score"),
        ):
            result = hierarchical_score(values, criteria, smoothing=case["k"], final_rule=rule)
            assert result["score"] == pytest.approx(case[expected], abs=2e-14, rel=2e-14), case[
                "name"
            ]
            assert result["final_gate"] == pytest.approx(
                case["all_gate_product"], abs=2e-14, rel=2e-14
            )
            assert [c["gate"] for c in result["criteria"]] == pytest.approx(
                case["gates"], abs=2e-14, rel=2e-14
            )
            assert [c["contribution"] for c in result["criteria"]] == pytest.approx(
                case["capped_contributions"], abs=2e-14, rel=2e-14
            )
