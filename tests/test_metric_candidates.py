"""Independent examples for the pure chromatographic metric kernel."""

import pytest

from measure_methods.metric_candidates import chromatography_metric_candidates


def _metrics(**overrides):
    values = {
        "num_compounds": 2,
        "retention_times": [1.0, 3.0],
        "peak_resolutions": [1.0, 2.0],
        "fwhms": [2.3548, 2.3548],
        "run_time_min": 4.0,
        "resolution_threshold": 1.5,
    }
    values.update(overrides)
    return chromatography_metric_candidates(**values)


def test_finite_retention_and_resolution_counts_keep_distinct_meanings():
    result = _metrics(
        retention_times=[1.0, float("inf")],
        peak_resolutions=[float("inf"), float("nan")],
        fwhms=[float("nan"), float("inf")],
    )

    assert result["candidates"]["reported_num_peaks_vs_finite_retention_time_count"] == 1
    assert result["candidates"]["reported_num_peaks_vs_finite_resolution_count"] == 0
    assert result["candidates"]["reported_critical_vs_min_finite_resolution"] == 0.0
    assert result["arrays"]["retention_times"] == {"finite": 1, "positive_infinity": 1}
    assert result["arrays"]["peak_resolutions"] == {"positive_infinity": 1, "nan": 1}
    assert result["pair_counts"]["finite_rt_without_finite_resolution"] == 1


def test_tied_times_have_zero_resolution_under_the_gaussian_convention():
    result = _metrics(
        retention_times=[2.0, 2.0],
        peak_resolutions=[0.0, 0.0],
        fwhms=[2.3548, 2.3548],
        run_time_min=2.0,
    )

    assert result["candidates"]["reported_critical_vs_sigma_pair_adjacent_min"] == 0.0
    assert result["pair_counts"]["duplicate_retention_time_pairs"] == 1
    assert result["pair_counts"]["zero_resolution_adjacent_pairs"] == 1
    assert result["pair_array_comparison"] == {
        "compared": 2,
        "mismatches": 0,
        "max_abs_error": 0.0,
    }
    assert result["candidates"]["reported_num_peaks_vs_finite_rt_within_runtime_inclusive"] == 2
    assert result["candidates"]["reported_num_peaks_vs_finite_rt_within_runtime_exclusive"] == 0


def test_fwhm_conversion_uses_2_3548_and_candidate_does_not_read_summary():
    result = _metrics(
        retention_times=[1.0, 3.0],
        peak_resolutions=[0.5, 0.5],
        fwhms=[2.3548, 2.3548],
    )

    # Each supplied FWHM corresponds to sigma=1, so delta=2 over 2*(1+1) gives Rs=0.5.
    assert result["candidates"]["reported_critical_vs_sigma_pair_adjacent_min"] == pytest.approx(
        0.5
    )
    assert result["candidates"]["reported_critical_vs_sigma_pair_all_min"] == pytest.approx(0.5)
    assert result["candidates"][
        "reported_critical_vs_fwhm_1_1774delta_over_width_sum_adjacent_min"
    ] == pytest.approx(0.5)
    assert result["pair_array_comparison"]["compared"] == 2
    assert "reported" not in result


def test_no_finite_values_use_zero_fallback_but_absent_and_empty_arrays_are_unknown():
    present = _metrics(
        retention_times=[float("inf"), float("inf")],
        peak_resolutions=[float("nan"), float("inf")],
        fwhms=[float("nan"), float("inf")],
    )
    absent = _metrics(retention_times=None, peak_resolutions=None, fwhms=None)
    empty = _metrics(retention_times=[], peak_resolutions=[], fwhms=[])

    assert present["candidates"]["reported_critical_vs_min_finite_resolution"] == 0.0
    assert present["candidates"]["reported_num_peaks_vs_finite_retention_time_count"] == 0
    for result in (absent, empty):
        assert result["candidates"]["reported_critical_vs_min_finite_resolution"] is None
        assert result["candidates"]["reported_num_peaks_vs_finite_retention_time_count"] is None
        assert result["arrays"]["retention_times"] == {"array_unavailable": 1}


def test_missing_middle_width_keeps_all_pair_candidate_but_blocks_adjacent_pairs():
    result = _metrics(
        num_compounds=3,
        retention_times=[1.0, 2.0, 3.0],
        peak_resolutions=[1.0, None, 1.0],
        fwhms=[1.0, None, 1.0],
    )

    assert result["candidates"]["reported_critical_vs_sigma_pair_all_min"] == pytest.approx(
        2.3548 / 2.0
    )
    assert result["candidates"]["reported_critical_vs_sigma_pair_adjacent_min"] is None
    assert result["pair_counts"]["source_style_all_pairs"] == 1
    assert result["pair_counts"]["source_style_adjacent_pairs"] == 0
    assert (
        result["pair_counts"][
            "observations_with_finite_rt_pairs_but_no_positive_finite_width_adjacent_pair"
        ]
        == 1
    )


def test_zero_width_keeps_known_peak_in_adjacency_and_does_not_bridge():
    result = _metrics(
        num_compounds=3,
        retention_times=[1.0, 2.0, 3.0],
        peak_resolutions=[1.0, 1.0, 1.0],
        fwhms=[1.0, 0.0, 1.0],
    )

    assert result["arrays"]["fwhms"] == {"finite": 3}
    assert result["pair_counts"]["source_style_all_pairs"] == 1
    assert result["pair_counts"]["source_style_adjacent_pairs"] == 0
    assert result["candidates"]["reported_critical_vs_sigma_pair_all_min"] == pytest.approx(
        2.3548 / 2.0
    )
    assert result["candidates"]["reported_critical_vs_sigma_pair_adjacent_min"] is None


def test_pair_array_error_tracks_subtolerance_difference():
    epsilon = 5e-13
    result = _metrics(
        peak_resolutions=[0.5 + epsilon, 0.5 + epsilon],
    )

    assert result["pair_array_comparison"]["compared"] == 2
    assert result["pair_array_comparison"]["mismatches"] == 0
    assert result["pair_array_comparison"]["max_abs_error"] == pytest.approx(
        epsilon, rel=0.001, abs=0
    )


def test_negative_measurements_and_boolean_cohort_are_rejected():
    with pytest.raises(ValueError, match=r"retention_times\[0\].*nonnegative"):
        _metrics(retention_times=[-1.0, 2.0])
    with pytest.raises(ValueError, match=r"fwhms\[1\].*nonnegative"):
        _metrics(fwhms=[1.0, -1.0])
    with pytest.raises(TypeError, match="num_compounds"):
        _metrics(num_compounds=True)


def test_declared_empty_cohort_is_not_a_missing_nonempty_experiment():
    with pytest.raises(ValueError, match="num_compounds must be positive"):
        _metrics(num_compounds=0, retention_times=[], peak_resolutions=[], fwhms=[])
