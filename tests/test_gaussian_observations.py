"""Contract tests for the ideal Gaussian centroid observation producer."""

import json
import math

import pytest

from measure_methods.gaussian_observations import (
    RESOLUTION_DEFINITION,
    GaussianAnalyte,
    simulate_gaussian_centroids,
)


def _analyte(analyte_id, retention_time_min, fwhm_min=1.0):
    return GaussianAnalyte(
        analyte_id=analyte_id,
        retention_status="eluted",
        retention_time_min=retention_time_min,
        fwhm_min=fwhm_min,
    )


def _simulate(analytes, duration=10.0):
    return simulate_gaussian_centroids(
        analytes=analytes,
        acquisition_duration_min=duration,
    )


def test_known_gaussian_centroid_resolutions_and_per_analyte_minima():
    result = _simulate(
        [
            _analyte("A", 2.0, 1.0),
            _analyte("B", 4.0, 1.0),
            _analyte("C", 8.0, 2.0),
        ]
    )

    analytes = {item["analyte_id"]: item for item in result["analytes"]}
    pairs = {tuple(item["analyte_ids"]): item for item in result["pairs"]}
    assert result["profile"] == "ideal-gaussian-centroid/1"
    assert result["resolution_definition"] == RESOLUTION_DEFINITION
    assert result["fwhm_per_sigma"] == 2.3548
    assert pairs[("A", "B")]["resolution"] == pytest.approx(1.1774)
    assert pairs[("B", "C")]["resolution"] == pytest.approx(1.5698666666666667)
    assert pairs[("A", "B")]["resolution_status"] == "observed"
    assert analytes["A"]["resolution"] == pytest.approx(1.1774)
    assert analytes["B"]["resolution"] == pytest.approx(1.1774)
    assert analytes["C"]["resolution"] == pytest.approx(1.5698666666666667)
    assert all(item["resolution_status"] == "observed" for item in analytes.values())


def test_tied_centroids_have_exact_zero_resolution_and_id_tie_breaking():
    result = _simulate([_analyte("zeta", 3.0), _analyte("alpha", 3.0)])

    assert result["pairs"] == [
        {
            "analyte_ids": ["alpha", "zeta"],
            "resolution_status": "observed",
            "resolution": 0.0,
            "right_censor_bound_min": None,
        }
    ]
    assert [item["analyte_id"] for item in result["analytes"]] == ["alpha", "zeta"]
    assert all(item["resolution"] == 0.0 for item in result["analytes"])


def test_input_permutation_does_not_change_canonical_outputs():
    analytes = [_analyte("z", 7.0, 2.0), _analyte("a", 2.0), _analyte("m", 4.0)]

    assert _simulate(analytes) == _simulate(list(reversed(analytes)))


def test_singleton_is_unsupported_and_censored_analytes_keep_run_bound():
    result = _simulate(
        [
            _analyte("only", 2.0),
            GaussianAnalyte("later", "not_eluted", None, 1.5),
        ],
        duration=5.0,
    )
    analytes = {item["analyte_id"]: item for item in result["analytes"]}

    assert result["pairs"] == []
    assert analytes["only"]["resolution_status"] == "unsupported"
    assert analytes["only"]["resolution"] is None
    assert analytes["only"]["right_censor_bound_min"] is None
    assert analytes["only"]["resolution_reason"] == "no_adjacent_finite_centroid"
    assert analytes["later"]["resolution_status"] == "censored"
    assert analytes["later"]["resolution"] is None
    assert analytes["later"]["right_censor_bound_min"] == 5.0
    assert (
        analytes["later"]["resolution_reason"] == "retention_not_eluted_within_acquisition_duration"
    )


def test_all_censored_cohort_is_preserved_without_fabricated_pairs():
    result = _simulate(
        [
            GaussianAnalyte("B", "not_eluted", None, 1.0),
            GaussianAnalyte("A", "not_eluted", None, 2.0),
        ],
        duration=4.0,
    )

    assert [item["analyte_id"] for item in result["analytes"]] == ["A", "B"]
    assert all(item["resolution_status"] == "censored" for item in result["analytes"])
    assert all(item["right_censor_bound_min"] == 4.0 for item in result["analytes"])
    assert result["pairs"] == []


def test_centroid_at_acquisition_boundary_is_included():
    result = _simulate([_analyte("A", 2.0), _analyte("B", 5.0)], duration=5.0)

    assert result["analytes"][1]["retention_time_min"] == 5.0
    assert result["pairs"][0]["resolution_status"] == "observed"


def test_pair_resolution_underflow_from_positive_separation_is_rejected():
    with pytest.raises(OverflowError, match="underflowed to zero resolution"):
        _simulate(
            [
                _analyte("A", 0.0, 1e308),
                _analyte("B", math.ulp(0.0), 1e308),
            ],
            duration=1.0,
        )


def test_pair_resolution_overflow_is_rejected():
    with pytest.raises(ValueError, match="Gaussian resolution is not finite"):
        _simulate(
            [_analyte("A", 0.0, 1e-308), _analyte("B", 1e308, 1e-308)],
            duration=1e308,
        )


def test_positive_subnormal_width_that_scales_to_zero_is_rejected():
    with pytest.raises(ValueError, match="Gaussian resolution is not finite"):
        _simulate(
            [_analyte("A", 0.0, math.ulp(0.0)), _analyte("B", 1.0, math.ulp(0.0))],
            duration=1.0,
        )


def test_large_widths_keep_a_representable_ratio_despite_intermediate_overflow():
    largest = float.fromhex("0x1.fffffffffffffp+1023")
    from fractions import Fraction

    result = _simulate([_analyte("A", 0.0, largest), _analyte("B", 1.0, largest)], duration=1.0)
    expected = float(Fraction(2.3548) / (4 * Fraction(largest)))
    assert result["pairs"][0]["resolution"] == expected
    assert expected > 0


def test_tied_centroids_keep_zero_even_when_width_normalization_underflows():
    result = _simulate([_analyte("A", 0.0, math.ulp(0.0)), _analyte("B", 0.0, math.ulp(0.0))])
    assert result["pairs"][0]["resolution"] == 0


def test_output_is_json_safe_and_states_scope_limits():
    result = _simulate([_analyte("A", 1.0), _analyte("B", 3.0)])

    json.dumps(result, allow_nan=False)
    assert "adjacent" in result["scope"]["adjacency"]
    assert "synthetic" in result["scope"]["widths"]
    assert "not_trace_simulation" in result["scope"]["interpretation"]


def test_helper_conversion_cannot_silently_change_the_declared_profile(monkeypatch):
    from measure_methods import gaussian_observations

    original = gaussian_observations.gaussian_pairwise_resolutions

    def changed_conversion(*args, **kwargs):
        result = original(*args, **kwargs)
        result["fwhm_per_sigma"] = 2.5
        return result

    monkeypatch.setattr(gaussian_observations, "gaussian_pairwise_resolutions", changed_conversion)
    with pytest.raises(ValueError, match="conversion differs"):
        _simulate([_analyte("A", 1.0), _analyte("B", 3.0)])


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "analyte_id": " ",
            "retention_status": "eluted",
            "retention_time_min": 0.0,
            "fwhm_min": 1.0,
        },
        {
            "analyte_id": "A" * 101,
            "retention_status": "eluted",
            "retention_time_min": 0.0,
            "fwhm_min": 1.0,
        },
        {
            "analyte_id": "A",
            "retention_status": "missing",
            "retention_time_min": 0.0,
            "fwhm_min": 1.0,
        },
        {
            "analyte_id": "A",
            "retention_status": "eluted",
            "retention_time_min": None,
            "fwhm_min": 1.0,
        },
        {
            "analyte_id": "A",
            "retention_status": "not_eluted",
            "retention_time_min": 1.0,
            "fwhm_min": 1.0,
        },
        {
            "analyte_id": "A",
            "retention_status": "eluted",
            "retention_time_min": True,
            "fwhm_min": 1.0,
        },
        {
            "analyte_id": "A",
            "retention_status": "eluted",
            "retention_time_min": "1.0",
            "fwhm_min": 1.0,
        },
        {
            "analyte_id": "A",
            "retention_status": "eluted",
            "retention_time_min": -1.0,
            "fwhm_min": 1.0,
        },
        {
            "analyte_id": "A",
            "retention_status": "eluted",
            "retention_time_min": 0.0,
            "fwhm_min": 0.0,
        },
        {
            "analyte_id": "A",
            "retention_status": "eluted",
            "retention_time_min": 0.0,
            "fwhm_min": True,
        },
        {
            "analyte_id": "A",
            "retention_status": "eluted",
            "retention_time_min": 0.0,
            "fwhm_min": math.inf,
        },
    ],
)
def test_analyte_values_validate(kwargs):
    with pytest.raises((TypeError, ValueError)):
        GaussianAnalyte(**kwargs)


def test_cohort_and_acquisition_inputs_validate():
    one = _analyte("A", 0.0)
    with pytest.raises(ValueError, match="between 1 and 32"):
        _simulate([])
    with pytest.raises(ValueError, match="between 1 and 32"):
        _simulate([_analyte(str(index), 0.0) for index in range(33)])
    with pytest.raises(ValueError, match="unique"):
        _simulate([one, one])
    with pytest.raises(TypeError, match="sequence"):
        _simulate("not analytes")
    with pytest.raises(TypeError, match="GaussianAnalyte"):
        _simulate([{"analyte_id": "A"}])
    with pytest.raises(TypeError, match="acquisition_duration_min"):
        _simulate([one], duration=True)
    with pytest.raises(ValueError, match="greater than zero"):
        _simulate([one], duration=0.0)
    with pytest.raises(ValueError, match="exceeds acquisition_duration_min"):
        _simulate([_analyte("late", 2.0)], duration=1.0)
