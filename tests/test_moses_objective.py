"""The source score profile is explicit, independent of optimizer state."""

import json
import math

import pytest

from measure_methods.moses_objective import (
    MOSES_SCORE_PROFILES,
    moses_observation_score,
    moses_summary_score,
)


def score(**kwargs):
    inputs = dict(
        profile="moses-botier-1.0.0-binary64/1",
        num_compounds=2,
        peak_resolutions=[3, 3],
        run_time_min=10,
        max_run_time_min=20,
    )
    inputs.update(kwargs)
    return moses_observation_score(**inputs)


def test_source_count_bound_is_ten_even_for_smaller_complete_mixtures():
    result = score()
    missing = result["criteria"][0]
    assert missing["normalized_value"] == pytest.approx(10 / 9.5)
    assert result["detected_count"] == 2
    assert result["critical_resolution"] == 3
    assert result["analytical_thresholds_satisfied"] is True
    assert result["all_objective_thresholds_satisfied"] is False


def test_published_and_excess_profiles_remain_distinct_when_all_tiers_are_satisfied():
    released = score(run_time_min=1)
    excess = score(run_time_min=1, profile="moses-botier-1.1.0-binary64/1")
    assert released["score"] - excess["score"] == pytest.approx(1, abs=0.01)
    assert released["profile"] != excess["profile"]


def test_target_index_zero_scores_only_that_analyte_and_uses_target_count_bound():
    result = score(
        objective="SinglePeak-Resolution", compound_idx=0, peak_resolutions=[3, math.nan]
    )
    assert result["scoring_compound_count"] == 1
    assert result["detected_count"] == 1
    assert result["critical_resolution"] == 3
    assert result["criteria"][0]["normalized_value"] == 2
    assert result["analytical_thresholds_satisfied"] is True


def test_unavailable_array_and_present_no_detection_are_distinct():
    assert score(peak_resolutions=[]) == {
        "profile": "moses-botier-1.0.0-binary64/1",
        "status": "unavailable",
        "score": None,
        "reason": "resolution_array_unavailable",
    }
    absent = score(peak_resolutions=[math.nan, None])
    assert absent["status"] == "scored"
    assert absent["detected_count"] == 0
    assert absent["critical_resolution"] == 0
    assert absent["analytical_thresholds_satisfied"] is False
    json.dumps(absent, allow_nan=False)


def test_resolved_count_objective_applies_inclusive_resolution_cutoff():
    result = score(objective="NumPeaks-NumResPeaks", peak_resolutions=[1.5, 1.49])
    assert result["detected_count"] == 2
    assert result["resolved_count"] == 1
    assert result["analytical_thresholds_satisfied"] is False
    assert result["criteria"][1]["name"] == "unresolved_analytes"


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(profile="latest"),
        dict(num_compounds=True),
        dict(compound_idx=0),
        dict(objective="SinglePeak-Resolution"),
        dict(objective="SinglePeak-Resolution", compound_idx=True),
        dict(peak_resolutions=""),
        dict(peak_resolutions=b""),
        dict(peak_resolutions=[True, 3]),
        dict(peak_resolutions=[3]),
        dict(count_threshold=10),
        dict(resolution_threshold=0),
        dict(max_run_time_min=5),
        dict(run_time_min=math.inf),
    ],
)
def test_ambiguous_or_undefined_profiles_cannot_silently_score(kwargs):
    with pytest.raises((TypeError, ValueError)):
        score(**kwargs)


def test_all_supported_profiles_are_named_and_json_serializable():
    for name in MOSES_SCORE_PROFILES:
        result = score(profile=name)
        assert result["profile"] == name
        json.dumps(result, allow_nan=False)


def test_summary_inputs_match_array_math_only_when_the_supplied_metrics_match():
    raw = score()
    reported = moses_summary_score(
        profile=raw["profile"],
        num_compounds=2,
        detected_count=2,
        critical_resolution=3,
        run_time_min=10,
        max_run_time_min=20,
    )
    assert reported["score"] == raw["score"]
    assert reported["input_policy"] == "serialized_summary_hypothesis"
    assert raw["input_policy"] == "raw_resolution_array"
    changed = moses_summary_score(
        profile=raw["profile"],
        num_compounds=2,
        detected_count=2,
        critical_resolution=0.1,
        run_time_min=10,
        max_run_time_min=20,
    )
    assert changed["score"] != raw["score"]


def test_summary_count_mode_requires_declared_resolved_count():
    inputs = dict(
        profile="moses-botier-1.0.0-binary64/1",
        num_compounds=2,
        detected_count=2,
        critical_resolution=1.5,
        run_time_min=10,
        max_run_time_min=20,
        objective="NumPeaks-NumResPeaks",
    )
    with pytest.raises(ValueError, match="resolved_count"):
        moses_summary_score(**inputs)
    result = moses_summary_score(**inputs, resolved_count=1)
    assert (
        result["score"]
        == score(objective=inputs["objective"], peak_resolutions=[1.5, 1.49])["score"]
    )
    with pytest.raises(ValueError, match="cannot exceed"):
        moses_summary_score(**{**inputs, "detected_count": 1}, resolved_count=2)


@pytest.mark.parametrize("detected", [True, -1, 3, 1.5])
def test_summary_count_must_be_an_integer_within_the_mixture(detected):
    with pytest.raises(ValueError, match="detected_count"):
        moses_summary_score(
            profile="moses-botier-1.0.0-binary64/1",
            num_compounds=2,
            detected_count=detected,
            critical_resolution=1.5,
            run_time_min=10,
            max_run_time_min=20,
        )


def test_invalid_configuration_is_rejected_even_when_measurement_is_unavailable():
    with pytest.raises(ValueError, match="profile"):
        score(profile=[], peak_resolutions=None)
    with pytest.raises(ValueError, match="threshold"):
        score(peak_resolutions=None, resolution_threshold=0)
    with pytest.raises(ValueError, match="representable"):
        score(peak_resolutions=[10**1000, 3])
