"""Boundary examples for observed analytical acceptance."""

import json
from dataclasses import FrozenInstanceError

import pytest

from measure_methods.observations import (
    AnalyteObservation,
    assess_analytical_observation,
)


def _observation(
    analyte_id="a",
    *,
    assignment="confirmed",
    retention_status="observed",
    retention_time_min=2.0,
    resolution_status="observed",
    resolution=1.5,
):
    return AnalyteObservation(
        analyte_id=analyte_id,
        assignment=assignment,
        retention_status=retention_status,
        retention_time_min=retention_time_min,
        resolution_status=resolution_status,
        resolution=resolution,
    )


def _assess(required, observations, **overrides):
    values = {
        "required_analytes": required,
        "observations": observations,
        "acquisition_duration_min": 5.0,
        "max_acquisition_duration_min": 5.0,
        "resolution_threshold": 1.5,
        "runtime_preference_min": 5.0,
    }
    values.update(overrides)
    return assess_analytical_observation(**values)


def test_thresholds_and_acquisition_endpoint_are_inclusive():
    result = _assess(["a"], [_observation(retention_time_min=5.0, resolution=1.5)])

    assert result["profile"] == "moses-observed-analytical/1"
    assert result["analytical_acceptance"] == "accepted"
    assert result["feasible"] is True
    assert result["runtime_preference_satisfied"] is True
    assert result["detected_count"] == 1
    assert result["unknown_count"] == 0
    assert result["confirmed_absent_count"] == 0
    assert result["per_analyte"][0]["detection"] is True
    assert result["per_analyte"][0]["resolution_satisfied"] is True
    assert result["per_analyte"][0]["acceptance_state"] == "accepted"


def test_confirmed_detection_with_missing_resolution_is_unknown_not_absent():
    result = _assess(
        ["singleton"],
        [
            _observation(
                "singleton",
                resolution_status="unsupported",
                resolution=None,
            )
        ],
    )

    row = result["per_analyte"][0]
    assert row["detection"] is True
    assert row["resolution_satisfied"] is None
    assert row["acceptance_state"] == "unknown"
    assert result["analytical_acceptance"] == "unknown"
    assert result["detected_count"] == 1
    assert result["unknown_count"] == 0
    assert result["confirmed_absent_count"] == 0


def test_confirmed_absence_dominates_missing_evidence_in_cohort():
    result = _assess(
        ["absent", "missing"],
        [
            _observation(
                "absent",
                retention_status="not_detected",
                retention_time_min=None,
                resolution_status="missing",
                resolution=None,
            )
        ],
    )

    assert [row["analyte_id"] for row in result["per_analyte"]] == ["absent", "missing"]
    assert [row["detection"] for row in result["per_analyte"]] == [False, None]
    assert [row["acceptance_state"] for row in result["per_analyte"]] == [
        "not_accepted",
        "unknown",
    ]
    assert result["analytical_acceptance"] == "not_accepted"
    assert result["detected_count"] == 0
    assert result["unknown_count"] == 1
    assert result["confirmed_absent_count"] == 1


def test_known_resolution_failure_dominates_unknown_detection():
    result = _assess(
        ["a"],
        [
            _observation(
                retention_status="failed",
                retention_time_min=None,
                resolution=0.0,
            )
        ],
    )

    row = result["per_analyte"][0]
    assert row["detection"] is None
    assert row["resolution_satisfied"] is False
    assert row["acceptance_state"] == "not_accepted"
    assert result["analytical_acceptance"] == "not_accepted"


@pytest.mark.parametrize("status", ["censored", "missing", "failed"])
def test_unavailable_confirmed_retention_evidence_is_unknown(status):
    result = _assess(
        ["a"],
        [
            _observation(
                retention_status=status,
                retention_time_min=None,
                resolution_status="observed",
                resolution=2.0,
            )
        ],
    )

    assert result["per_analyte"][0]["detection"] is None
    assert result["per_analyte"][0]["resolution_satisfied"] is True
    assert result["analytical_acceptance"] == "unknown"


@pytest.mark.parametrize("status", ["unsupported", "censored", "missing", "failed"])
def test_unavailable_confirmed_resolution_evidence_is_unknown(status):
    result = _assess(
        ["a"],
        [
            _observation(
                resolution_status=status,
                resolution=None,
            )
        ],
    )

    assert result["per_analyte"][0]["detection"] is True
    assert result["per_analyte"][0]["resolution_satisfied"] is None
    assert result["analytical_acceptance"] == "unknown"


def test_out_of_window_observation_is_negative_and_rules_remain_separate():
    out_of_window = _assess(
        ["a"],
        [_observation(retention_time_min=5.01, resolution=2.0)],
    )
    over_budget_but_analytically_acceptable = _assess(
        ["a"],
        [_observation(retention_time_min=5.0, resolution=1.5)],
        acquisition_duration_min=6.0,
        max_acquisition_duration_min=5.0,
        runtime_preference_min=5.0,
    )

    assert out_of_window["per_analyte"][0]["detection"] is False
    assert out_of_window["analytical_acceptance"] == "not_accepted"
    assert over_budget_but_analytically_acceptable["feasible"] is False
    assert over_budget_but_analytically_acceptable["runtime_preference_satisfied"] is False
    assert over_budget_but_analytically_acceptable["analytical_acceptance"] == "accepted"


def test_ambiguous_and_unassigned_rows_do_not_establish_detection_or_resolution():
    observations = [
        _observation("ambiguous", assignment="ambiguous", resolution=2.0),
        _observation(
            "unassigned",
            assignment="unassigned",
            retention_status="not_detected",
            retention_time_min=None,
            resolution=0.0,
        ),
    ]
    result = _assess(["ambiguous", "unassigned"], observations)

    assert [row["detection"] for row in result["per_analyte"]] == [None, None]
    assert [row["resolution_satisfied"] for row in result["per_analyte"]] == [None, None]
    assert result["analytical_acceptance"] == "unknown"
    assert result["unknown_count"] == 2
    assert result["confirmed_absent_count"] == 0


def test_missing_cohort_rows_keep_declared_order_and_observation_permutation():
    first = _observation("first", retention_status="non_eluting", retention_time_min=None)
    last = _observation("last", retention_time_min=3.0, resolution=1.7)
    expected = _assess(["first", "middle", "last"], [last, first])
    permuted = _assess(["first", "middle", "last"], [first, last])

    assert expected == permuted
    assert [row["analyte_id"] for row in expected["per_analyte"]] == [
        "first",
        "middle",
        "last",
    ]
    assert [row["detection"] for row in expected["per_analyte"]] == [False, None, True]
    assert expected["per_analyte"][1]["diagnostics"] == [{"code": "required_observation_missing"}]
    assert expected["analytical_acceptance"] == "not_accepted"


def test_required_target_subset_is_explicit_and_extra_rows_are_rejected():
    target_only = _assess(["target"], [_observation("target")])

    assert target_only["required_analyte_count"] == 1
    assert [row["analyte_id"] for row in target_only["per_analyte"]] == ["target"]
    with pytest.raises(ValueError, match="declared required analyte cohort"):
        _assess(["target"], [_observation("other"), _observation("target")])


def test_input_sequences_are_not_mutated_and_observation_is_frozen():
    required = ["a"]
    observations = [_observation("a")]
    original_observations = observations.copy()
    _assess(required, observations)

    assert required == ["a"]
    assert observations == original_observations
    with pytest.raises(FrozenInstanceError):
        observations[0].retention_time_min = 4.0


@pytest.mark.parametrize(
    ("field", "status", "value"),
    [
        ("retention", "observed", None),
        ("resolution", "observed", None),
        ("retention", "missing", 1.0),
        ("resolution", "missing", 1.0),
    ],
)
def test_status_and_value_must_agree(field, status, value):
    if field == "retention":
        kwargs = {"retention_status": status, "retention_time_min": value}
        if status == "observed":
            kwargs["resolution"] = 1.0
        with pytest.raises(ValueError):
            _observation(**kwargs)
    else:
        kwargs = {"resolution_status": status, "resolution": value}
        with pytest.raises(ValueError):
            _observation(**kwargs)


@pytest.mark.parametrize("value", [True, "2.0", float("nan"), float("inf"), -1.0])
def test_observed_numeric_values_reject_bool_strings_nonfinite_and_negative(value):
    with pytest.raises((TypeError, ValueError)):
        _observation(retention_time_min=value)


@pytest.mark.parametrize("value", [True, "1.5", float("nan"), float("inf"), -0.1])
def test_observed_resolution_rejects_bool_strings_nonfinite_and_negative(value):
    with pytest.raises((TypeError, ValueError)):
        _observation(resolution=value)


@pytest.mark.parametrize("value", [True, "5.0", float("nan"), float("inf"), -1.0])
def test_duration_thresholds_and_preference_require_finite_nonnegative_reals(value):
    with pytest.raises((TypeError, ValueError)):
        _assess(["a"], [_observation()], acquisition_duration_min=value)
    with pytest.raises((TypeError, ValueError)):
        _assess(["a"], [_observation()], max_acquisition_duration_min=value)
    with pytest.raises((TypeError, ValueError)):
        _assess(["a"], [_observation()], resolution_threshold=value)
    with pytest.raises((TypeError, ValueError)):
        _assess(["a"], [_observation()], runtime_preference_min=value)


def test_duplicate_ids_empty_required_cohort_and_invalid_rows_are_rejected():
    with pytest.raises(ValueError, match="duplicate IDs"):
        _assess(["a", "a"], [_observation()])
    with pytest.raises(ValueError, match="duplicate analyte IDs"):
        _assess(["a"], [_observation(), _observation()])
    with pytest.raises(ValueError, match="at least one analyte"):
        _assess([], [])
    with pytest.raises(TypeError, match="only AnalyteObservation"):
        _assess(["a"], [object()])


def test_result_contains_only_json_safe_values():
    result = _assess(["a", "missing"], [_observation()])

    serialized = json.dumps(result, allow_nan=False)
    assert '"analytical_acceptance": "unknown"' in serialized
