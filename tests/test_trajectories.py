"""Hand-calculated contracts for pure campaign-success trajectories."""

import json
import math

import pytest

from measure_methods.trajectories import (
    SuccessObservation,
    aggregate_success_trajectories,
    summarize_campaign_success,
)


def obs(sequence, role, satisfied, volume=None):
    return SuccessObservation(sequence, role, satisfied, volume)


def test_multi_low_fidelity_unknown_then_confirmed_success_keeps_event_accounting():
    result = summarize_campaign_success(
        [
            obs(1, "high", False, 1.2),
            obs(2, "low", True, 99),
            obs(3, "low", False, 10),
            obs(4, "high", None, 1.0),
            obs(5, "high", True, 2.0),
        ],
        budget=4,
        low_fidelity_cost=0.25,
    )

    assert result["observation_count"] == 5
    assert result["high_fidelity_observation_count"] == 3
    assert result["low_fidelity_observation_count"] == 2
    assert result["unavailable_high_fidelity_indices"] == [2]
    assert result["first_possible_success_high_fidelity_index"] == 2
    assert result["success_within_budget"] is True
    assert result["first_confirmed_success"] == {
        "high_fidelity_index": 3,
        "sequence": 5,
        "high_fidelity_evaluation_count": 3,
        "low_fidelity_evaluation_count": 2,
        "evaluation_cost_proxy": 3.5,
        "evaluation_cost_proxy_status": "known",
        "high_fidelity_program_volume_ml": pytest.approx(4.2),
        "program_volume_status": "known",
        "lower_bound_high_fidelity_index": 2,
        "upper_bound_high_fidelity_index": 3,
    }
    assert [point["success_state"] for point in result["trajectory"]] == [
        "observed_no_success",
        "unknown",
        "confirmed_success",
        "confirmed_success",
    ]
    assert [point["observed_depth"] for point in result["trajectory"]] == [1, 2, 3, 3]
    json.dumps(result, allow_nan=False)


def test_low_fidelity_success_does_not_confirm_and_incomplete_hf_history_is_unknown():
    result = summarize_campaign_success(
        [obs(1, "low", True, 7), obs(2, "high", False, 1.5)],
        budget=3,
    )

    assert result["first_confirmed_success"] is None
    assert result["first_possible_success_high_fidelity_index"] == 2
    assert result["success_within_budget"] is False
    assert [point["success_state"] for point in result["trajectory"]] == [
        "observed_no_success",
        "unknown",
        "unknown",
    ]
    assert result["trajectory"][1]["observed_depth"] == 1


def test_missing_hf_volume_and_undeclared_low_cost_remain_unknown_not_zero():
    result = summarize_campaign_success(
        [
            obs(1, "high", False, None),
            obs(2, "low", False, 100),
            obs(3, "high", True, 3.0),
        ],
        budget=2,
    )

    event = result["first_confirmed_success"]
    assert event["high_fidelity_index"] == 2
    assert event["evaluation_cost_proxy"] is None
    assert event["evaluation_cost_proxy_status"] == "unknown_declared_low_fidelity_cost_missing"
    assert event["high_fidelity_program_volume_ml"] is None
    assert event["program_volume_status"] == "unknown_high_fidelity_program_volume_missing"


def test_success_after_budget_is_retained_but_not_counted_by_budget():
    result = summarize_campaign_success(
        [obs(1, "high", False, 1), obs(2, "low", True, 2), obs(3, "high", True, 3)],
        budget=1,
        low_fidelity_cost=0.5,
    )

    assert result["first_confirmed_success"]["high_fidelity_index"] == 2
    assert result["first_confirmed_success"]["sequence"] == 3
    assert result["first_confirmed_success"]["evaluation_cost_proxy"] == 2.5
    assert result["first_confirmed_success"]["high_fidelity_program_volume_ml"] == 4.0
    assert result["first_possible_success_high_fidelity_index"] == 2
    assert result["success_within_budget"] is False
    assert result["trajectory"] == [
        {
            "high_fidelity_budget": 1,
            "success_state": "observed_no_success",
            "observed_depth": 1,
            "unknown": False,
        }
    ]


def test_cohort_keeps_missing_campaigns_in_full_planned_denominator():
    complete = summarize_campaign_success(
        [obs(1, "high", True, 2), obs(2, "high", False, 1)], budget=2
    )
    incomplete = summarize_campaign_success([obs(1, "high", False, 1)], budget=2)

    result = aggregate_success_trajectories([complete, incomplete], planned_count=3, budget=2)
    assert result == [
        {
            "high_fidelity_budget": 1,
            "cohort_size": 3,
            "recorded_campaign_count": 2,
            "observed_at_budget_count": 2,
            "success_count": 1,
            "unknown_success_count": 1,
            "observed_no_success_count": 1,
            "success_fraction": pytest.approx(1 / 3),
            "success_fraction_upper_bound": pytest.approx(2 / 3),
        },
        {
            "high_fidelity_budget": 2,
            "cohort_size": 3,
            "recorded_campaign_count": 2,
            "observed_at_budget_count": 1,
            "success_count": 1,
            "unknown_success_count": 2,
            "observed_no_success_count": 0,
            "success_fraction": pytest.approx(1 / 3),
            "success_fraction_upper_bound": pytest.approx(1.0),
        },
    ]
    json.dumps(result, allow_nan=False)


def test_all_known_no_success_has_zero_width_success_fraction_bounds():
    summaries = [
        summarize_campaign_success([obs(1, "high", False, 1), obs(2, "high", False, 1)], budget=1),
        summarize_campaign_success([obs(1, "high", False, 1)], budget=1),
    ]
    result = aggregate_success_trajectories(summaries, planned_count=2, budget=1)
    assert result[0]["success_count"] == 0
    assert result[0]["unknown_success_count"] == 0
    assert result[0]["observed_no_success_count"] == 2
    assert result[0]["success_fraction"] == 0
    assert result[0]["success_fraction_upper_bound"] == 0


def test_invalid_counts_summaries_and_numeric_inputs_are_rejected():
    with pytest.raises(ValueError, match="at least one"):
        summarize_campaign_success([], budget=2)
    with pytest.raises(ValueError, match="positive integer"):
        summarize_campaign_success([obs(1, "high", False)], budget=True)
    with pytest.raises(ValueError, match="contiguous"):
        summarize_campaign_success([obs(2, "high", True)], budget=2)
    with pytest.raises(ValueError, match="boolean or null"):
        summarize_campaign_success([obs(1, "high", 1)], budget=1)
    with pytest.raises(ValueError, match="finite nonnegative"):
        summarize_campaign_success([obs(1, "high", False, math.inf)], budget=1)
    with pytest.raises(ValueError, match="finite nonnegative"):
        summarize_campaign_success([obs(1, "high", False)], budget=1, low_fidelity_cost=False)
    summary = summarize_campaign_success([obs(1, "high", False)], budget=1)
    with pytest.raises(ValueError, match="cannot exceed planned_count"):
        aggregate_success_trajectories([summary, summary], planned_count=1, budget=1)


def test_aggregate_rejects_summary_with_inconsistent_budget_state():
    summary = summarize_campaign_success([obs(1, "high", False)], budget=2)
    summary["trajectory"][1]["success_state"] = "observed_no_success"
    with pytest.raises(ValueError, match="success_state is inconsistent"):
        aggregate_success_trajectories([summary], planned_count=1, budget=2)


def test_observed_zero_volume_and_zero_declared_simulation_cost_remain_zero():
    result = summarize_campaign_success(
        [obs(1, "low", True, 0), obs(2, "high", True, 0)],
        budget=1,
        low_fidelity_cost=0,
    )
    event = result["first_confirmed_success"]
    assert event["high_fidelity_program_volume_ml"] == 0
    assert event["program_volume_status"] == "known"
    assert event["evaluation_cost_proxy"] == 1
    assert event["evaluation_cost_proxy_status"] == "known"
