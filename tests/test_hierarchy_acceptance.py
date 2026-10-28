"""A smooth scalar cutoff cannot stand in for analytical acceptance."""

from measure_methods.moses_objective import moses_observation_score


def test_score_above_two_can_fail_the_declared_resolution_threshold():
    result = moses_observation_score(
        profile="moses-botier-1.0.0-binary64/1",
        num_compounds=10,
        peak_resolutions=[1.49] * 10,
        run_time_min=1,
        max_run_time_min=20,
        resolution_threshold=1.5,
    )
    assert result["score"] > 2.6
    assert result["score_at_least_two"] is True
    assert result["analytical_thresholds_satisfied"] is False
    assert result["all_objective_thresholds_satisfied"] is False
