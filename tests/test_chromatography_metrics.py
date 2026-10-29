"""Behavioral checks for the pure-Python archived chromatography metrics."""

import json

import pytest

from measure_methods.chromatography import (
    gaussian_pairwise_resolutions,
    summarize_chromatography_observation,
)


def summarize(**overrides):
    arguments = {
        "num_compounds": 4,
        "retention_times": [1.0, 2.0, 3.0, float("inf")],
        "peak_resolutions": [1.5, 1.49, 2.0, float("nan")],
        "fwhms": [0.2, 0.3, 0.4, float("nan")],
        "areas": [5.0, 6.0, 7.0, float("nan")],
        "run_time": 2.0,
        "retention_time_unit": "min",
        "fwhm_unit": "min",
        "run_time_unit": "min",
        "area_unit": "archive-reported",
    }
    arguments.update(overrides)
    return summarize_chromatography_observation(**arguments)


def test_reference_finite_resolution_metrics_keep_threshold_boundary():
    result = summarize()

    assert result["reference_resolution_metrics"] == {
        "status": "available",
        "num_peaks": 3,
        "num_resolved_peaks": 2,
        "critical_resolution": 1.49,
        "critical_resolution_status": "minimum_finite_resolution",
        "resolution_threshold": 1.5,
    }
    assert result["time_window_elution_metrics"]["num_eluted_within_run_time"] == 2
    assert (
        result["time_window_elution_metrics"]["comparison"]
        == "retention_time <= run_time (inclusive)"
    )


def test_nonfinite_values_and_absent_elements_remain_distinct_and_json_safe():
    result = summarize(
        retention_times=[1.0, float("inf"), float("nan"), None],
        peak_resolutions=[1.5, float("inf"), 1.6, None],
        fwhms=[0.2, float("nan"), None, float("inf")],
        areas=[5.0, float("nan"), None, float("inf")],
    )

    measurements = result["measurements"]
    assert measurements["retention_times"]["values"] == [
        {"value": 1.0, "status": "finite"},
        {"value": None, "status": "non_eluting"},
        {"value": None, "status": "unavailable"},
        {"value": None, "status": "missing"},
    ]
    assert measurements["peak_resolutions"]["values"][1] == {
        "value": None,
        "status": "positive_infinity",
    }
    assert measurements["fwhms"]["values"][2] == {"value": None, "status": "missing"}
    assert result["reference_resolution_metrics"]["num_peaks"] == 2
    assert result["time_window_elution_metrics"]["num_eluted_within_run_time"] == 1
    assert any(
        item["code"] == "finite_resolution_without_finite_retention_time"
        and item["analyte_index"] == 2
        for item in result["diagnostics"]
    )
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("missing", [None, []])
def test_missing_resolution_array_is_not_reported_as_zero_observation(missing):
    result = summarize(peak_resolutions=missing)

    assert result["measurements"]["peak_resolutions"] == {
        "availability": "array_unavailable",
        "values": None,
    }
    assert result["reference_resolution_metrics"] == {
        "status": "unavailable",
        "num_peaks": None,
        "num_resolved_peaks": None,
        "critical_resolution": None,
        "critical_resolution_status": "unavailable",
        "resolution_threshold": 1.5,
    }
    assert any(
        item["code"] == "peak_resolutions_array_unavailable" for item in result["diagnostics"]
    )


def test_missing_retention_array_keeps_resolution_metrics_but_elution_unavailable():
    result = summarize(retention_times=None)

    assert result["reference_resolution_metrics"]["num_peaks"] == 3
    assert result["time_window_elution_metrics"]["status"] == "unavailable"
    assert result["time_window_elution_metrics"]["num_eluted_within_run_time"] is None
    assert any(
        item["code"] == "finite_resolutions_without_retention_time_array"
        for item in result["diagnostics"]
    )


def test_all_nan_resolutions_keep_source_zero_default_explicitly_labeled():
    result = summarize(peak_resolutions=[float("nan")] * 4)

    metrics = result["reference_resolution_metrics"]
    assert metrics["status"] == "no_finite_resolution_values"
    assert metrics["num_peaks"] == 0
    assert metrics["num_resolved_peaks"] == 0
    assert metrics["critical_resolution"] == 0.0
    assert metrics["critical_resolution_status"] == "source_default_no_finite_values"
    assert result["measurements"]["peak_resolutions"]["values"][0] == {
        "value": None,
        "status": "unavailable",
    }


def test_source_disagreement_is_reported_without_adjusting_either_count():
    result = summarize(
        retention_times=[1.0, float("inf"), 3.0, 4.0],
        peak_resolutions=[float("nan"), 1.5, 1.6, 1.7],
    )

    assert result["reference_resolution_metrics"]["num_peaks"] == 3
    assert result["time_window_elution_metrics"]["num_eluted_within_run_time"] == 1
    codes = [item["code"] for item in result["diagnostics"]]
    assert "finite_resolution_without_finite_retention_time" in codes
    assert codes.count("retention_time_after_run_time") == 2
    assert "eluted_within_run_time_without_finite_resolution" in codes


def test_gaussian_oracle_sorts_shuffled_analytes_and_reports_only_neighbors():
    result = gaussian_pairwise_resolutions(
        [3.0, 1.0, 2.0],
        [2.3548, 2.3548, 2.3548],
        time_unit="min",
        fwhm_unit="min",
    )

    assert result["status"] == "complete"
    assert [
        (pair["left_analyte_index"], pair["right_analyte_index"]) for pair in result["pairs"]
    ] == [
        (1, 2),
        (2, 0),
    ]
    assert [pair["resolution"] for pair in result["pairs"]] == [0.25, 0.25]
    assert all(
        pair["neighbor_scope"] == "adjacent_among_observed_finite_retention_times"
        for pair in result["pairs"]
    )


def test_gaussian_oracle_excludes_nonelution_and_does_not_bridge_missing_width():
    result = gaussian_pairwise_resolutions(
        [1.0, 2.0, 3.0, float("inf")],
        [2.3548, float("nan"), 2.3548, float("nan")],
        time_unit="min",
        fwhm_unit="min",
    )

    assert result["status"] == "partial"
    assert result["pairs"] == []
    assert {item["analyte_index"]: item["status"] for item in result["excluded_analytes"]} == {
        1: "fwhm_unavailable",
        3: "non_eluting",
    }
    assert any(item["code"] == "adjacent_pair_missing_fwhm" for item in result["diagnostics"])


def test_gaussian_oracle_qualifies_unknown_retention_time():
    result = gaussian_pairwise_resolutions(
        [1.0, float("nan"), 3.0],
        [2.3548, 2.3548, 2.3548],
        time_unit="min",
        fwhm_unit="min",
    )

    assert result["status"] == "partial"
    assert result["pairs"][0]["neighbor_scope"] == "adjacent_among_observed_finite_retention_times"
    assert any(
        item["code"] == "full_set_adjacency_unknown_due_to_missing_retention_time"
        for item in result["diagnostics"]
    )


def test_empty_gaussian_observation_is_not_an_infinite_resolution():
    result = gaussian_pairwise_resolutions([], [], time_unit="min", fwhm_unit="min")

    assert result["status"] == "no_analytes"
    assert result["pairs"] == []


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"num_compounds": True}, TypeError),
        ({"num_compounds": -1}, ValueError),
        ({"retention_times": [1.0]}, ValueError),
        ({"fwhms": [0.2, 0.3, 0.4]}, ValueError),
        ({"peak_resolutions": [1.0, True, 2.0, 3.0]}, TypeError),
        ({"run_time": True}, TypeError),
        ({"run_time": float("nan")}, ValueError),
        ({"resolution_threshold": True}, TypeError),
        ({"retention_time_unit": "min", "fwhm_unit": "s"}, ValueError),
        ({"fwhms": [0.2, -0.1, 0.4, 0.5]}, ValueError),
        ({"peak_resolutions": [1.0, -0.1, 2.0, 3.0]}, ValueError),
    ],
)
def test_invalid_observation_inputs_are_rejected(arguments, error):
    with pytest.raises(error):
        summarize(**arguments)


def test_gaussian_oracle_rejects_length_mismatch_boolean_and_nonpositive_width():
    args = {"time_unit": "min", "fwhm_unit": "min"}

    with pytest.raises(ValueError, match="equal lengths"):
        gaussian_pairwise_resolutions([1.0, 2.0], [1.0], **args)
    with pytest.raises(TypeError, match=r"retention_times\[0\]"):
        gaussian_pairwise_resolutions([True, 2.0], [1.0, 1.0], **args)
    with pytest.raises(ValueError, match="greater than zero"):
        gaussian_pairwise_resolutions([1.0, 2.0], [0.0, 1.0], **args)
