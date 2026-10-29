"""Offline tests for the independent Gaussian-centroid qualification adapter."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from measure_methods.gaussian_observations import GaussianAnalyte, simulate_gaussian_centroids
from measure_service.benchmarks import gaussian_qualification as qualification


def _singleton_case():
    expected = {
        "profile": qualification.PRODUCER_PROFILE,
        "resolution_definition": qualification.RESOLUTION_DEFINITION,
        "fwhm_per_sigma": 2.3548,
        "acquisition_duration_min": "1",
        "analytes": [
            {
                "analyte_id": "a",
                "retention_status": "eluted",
                "retention_time_min": "0.5",
                "fwhm_min": "0.25",
                "resolution_status": "unsupported",
                "resolution": None,
                "right_censor_bound_min": None,
                "resolution_reason": "no_adjacent_finite_centroid",
            }
        ],
        "pairs": [],
        "scope": dict(qualification._SCOPE),
    }
    return {
        "case_id": "synthetic_singleton",
        "input_sha256": "a" * 64,
        "expected": expected,
    }


def _singleton_output():
    return simulate_gaussian_centroids(
        analytes=[
            GaussianAnalyte(
                analyte_id="a",
                retention_status="eluted",
                retention_time_min=0.5,
                fwhm_min=0.25,
            )
        ],
        acquisition_duration_min=1.0,
    )


def test_singleton_comparison_preserves_unsupported_null_resolution():
    result = qualification._compare_case_output(_singleton_case(), _singleton_output())

    assert result["case_passed"] is True
    assert result["analyte_ids_match"] is True
    assert result["pair_ids_match"] is True
    assert result["statuses_match"] is True
    assert result["null_semantics_match"] is True
    assert result["null_semantics_check_count"] == 3
    assert result["numeric_comparison_count"] == 4


def test_null_to_zero_resolution_is_reported_as_a_disagreement():
    observed = _singleton_output()
    observed["analytes"][0]["resolution"] = 0.0

    result = qualification._compare_case_output(_singleton_case(), observed)

    assert result["case_passed"] is False
    assert result["null_semantics_match"] is False
    assert result["issue_count"] == {"null_semantics_mismatch": 1}
    assert result["issues"] == [
        {"field_path": "analytes[a].resolution", "category": "null_semantics_mismatch"}
    ]


def test_truncated_cohort_keeps_oracle_coverage_denominators():
    observed = _singleton_output()
    observed["analytes"] = []
    case = _singleton_case()

    result = qualification._compare_case_output(case, observed)

    assert result["case_passed"] is False
    assert result["expected_analyte_count"] == 1
    assert result["observed_analyte_count"] == 0
    assert result["analyte_id_element_count"] == 1
    assert result["analyte_id_elements_match"] == 0
    assert result["status_field_count"] == 3
    assert result["status_field_match_count"] == 0
    assert result["null_semantics_check_count"] == 3
    assert result["null_semantics_match_count"] == 0
    assert result["null_semantics_match"] is False
    assert result["numeric_comparison_count"] == 4
    assert result["numeric_match_count"] == 2
    assert result["numeric_unmatched_count"] == 2


def test_omitted_nullable_key_does_not_match_explicit_null():
    observed = _singleton_output()
    del observed["analytes"][0]["right_censor_bound_min"]

    result = qualification._compare_case_output(_singleton_case(), observed)

    assert result["case_passed"] is False
    assert result["null_semantics_check_count"] == 3
    assert result["null_semantics_match_count"] == 2
    assert result["null_semantics_match"] is False
    assert result["issue_count"]["null_semantics_mismatch"] == 1


def test_missing_nonnull_nullable_value_counts_as_numeric_unmatched():
    observed = _singleton_output()
    del observed["analytes"][0]["retention_time_min"]

    result = qualification._compare_case_output(_singleton_case(), observed)

    assert result["case_passed"] is False
    assert result["null_semantics_match"] is False
    assert result["numeric_comparison_count"] == 4
    assert result["numeric_fields_evaluated"] == 4
    assert result["numeric_match_count"] == 3
    assert result["numeric_mismatch_count"] == 1
    assert result["numeric_unmatched_count"] == 1


def test_pair_identity_status_and_null_disagreements_are_separate():
    observed = simulate_gaussian_centroids(
        analytes=[
            GaussianAnalyte("a", "eluted", 0.0, 0.5),
            GaussianAnalyte("b", "eluted", 1.0, 0.75),
        ],
        acquisition_duration_min=2.0,
    )
    expected = deepcopy(observed)
    case = {
        "case_id": "synthetic_pair",
        "input_sha256": "c" * 64,
        "expected": expected,
    }
    assert qualification._compare_case_output(case, observed)["case_passed"] is True

    observed["pairs"][0]["analyte_ids"] = ["a", "c"]
    observed["pairs"][0]["resolution_status"] = "censored"
    observed["pairs"][0]["resolution"] = None
    result = qualification._compare_case_output(case, observed)

    assert result["case_passed"] is False
    assert result["pair_ids_match"] is False
    assert result["statuses_match"] is False
    assert result["null_semantics_match"] is False
    assert set(result["issue_count"]) == {
        "identity_mismatch",
        "numeric_mismatch",
        "null_semantics_mismatch",
        "status_mismatch",
    }


def test_tiny_positive_resolution_replaced_by_zero_fails_relative_only_guard(monkeypatch):
    analytes = [
        GaussianAnalyte("a", "eluted", 0.0, 1.0),
        GaussianAnalyte("b", "eluted", 1e-300, 1.0),
    ]
    expected = simulate_gaussian_centroids(analytes=analytes, acquisition_duration_min=1.0)
    assert expected["pairs"][0]["resolution"] > 0.0
    case = {
        "case_id": "synthetic_tiny_positive_resolution",
        "input_sha256": "e" * 64,
        "input": {
            "acquisition_duration_min": "1",
            "analytes": [
                {
                    "analyte_id": "a",
                    "retention_status": "eluted",
                    "retention_time_min": "0",
                    "fwhm_min": "1",
                },
                {
                    "analyte_id": "b",
                    "retention_status": "eluted",
                    "retention_time_min": "1e-300",
                    "fwhm_min": "1",
                },
            ],
        },
        "expected": expected,
    }

    def return_zero_resolution(**_kwargs):
        result = deepcopy(expected)
        result["pairs"][0]["resolution"] = 0.0
        return result

    monkeypatch.setattr(qualification, "simulate_gaussian_centroids", return_zero_resolution)
    comparison = qualification._run_numerical_case(case)

    assert comparison["case_passed"] is False
    assert comparison["issue_count"] == {"numeric_mismatch": 1}
    assert any(issue["field_path"].endswith(".resolution") for issue in comparison["issues"])


def test_expected_invalid_width_is_recorded_as_explicit_failure():
    case = {
        "case_id": "negative_width",
        "input_sha256": "b" * 64,
        "input": {
            "acquisition_duration_min": "1",
            "analytes": [
                {
                    "analyte_id": "a",
                    "retention_status": "eluted",
                    "retention_time_min": "0.5",
                    "fwhm_min": "-0.1",
                }
            ],
        },
        "expected_failure": {
            "category": "input_validation",
            "must_not_return_resolution": True,
        },
    }

    result = qualification._run_failure_case(case)

    assert result["case_passed"] is True
    assert result["explicit_failure_observed"] is True
    assert result["observed_exception_type"] == "ValueError"
    assert result["returned_any_result"] is False


def test_unrepresentable_resolution_requires_explicit_failure():
    case = {
        "case_id": "resolution_overflow_must_fail",
        "input_sha256": "d" * 64,
        "input": {
            "acquisition_duration_min": "1.7976931348623157e308",
            "analytes": [
                {
                    "analyte_id": "a",
                    "retention_status": "eluted",
                    "retention_time_min": "0",
                    "fwhm_min": "1e-308",
                },
                {
                    "analyte_id": "b",
                    "retention_status": "eluted",
                    "retention_time_min": "1e308",
                    "fwhm_min": "1e-308",
                },
            ],
        },
        "expected_failure": {
            "category": "numeric_overflow",
            "allowed_exception_types": ["OverflowError", "ValueError"],
            "must_not_return_resolution": True,
        },
    }

    result = qualification._run_failure_case(case)

    assert result["case_passed"] is True
    assert result["explicit_failure_observed"] is True
    assert result["observed_exception_type"] in {"ValueError", "OverflowError"}


def test_tampered_reference_aborts_before_creating_output(tmp_path):
    reference = tmp_path / "oracle.json"
    reference.write_text('{"profile":"tampered"}\n', encoding="utf-8")
    output = tmp_path / "qualification"

    with pytest.raises(ValueError, match="SHA256"):
        qualification.qualify_gaussian_producer(output, reference_json=reference)

    assert not output.exists()


def test_malformed_reference_aborts_even_when_its_digest_is_reviewed(tmp_path, monkeypatch):
    reference = tmp_path / "malformed.json"
    payload = b'{"profile":'
    reference.write_bytes(payload)
    monkeypatch.setattr(
        qualification,
        "EXPECTED_REFERENCE_SHA256",
        hashlib.sha256(payload).hexdigest(),
    )
    output = tmp_path / "qualification"

    with pytest.raises(ValueError, match="strict UTF-8 JSON"):
        qualification.qualify_gaussian_producer(output, reference_json=reference)

    assert not output.exists()


def test_missing_oracle_cases_abort_before_creating_output(tmp_path, monkeypatch):
    constant = float(qualification._FWHM_PER_SIGMA)
    numerator, denominator = constant.as_integer_ratio()
    document = {
        "schema_version": 1,
        "profile": "measure-service-gaussian-centroid-independent-reference/1",
        "producer_profile": qualification.PRODUCER_PROFILE,
        "resolution_definition": qualification.RESOLUTION_DEFINITION,
        "fwhm_per_sigma": qualification._FWHM_PER_SIGMA,
        "scope": {
            "cohort_range": [1, 32],
            "pair_rule": "synthetic",
            "resolution_rule": "synthetic",
            "fwhm_per_sigma_limitation": "synthetic",
            "singleton_rule": "synthetic",
            "censoring_rule": "synthetic",
            "tie_rule": "synthetic",
            "per_analyte_rule": "synthetic",
            "numeric_inputs": "synthetic",
            "nonclaims": [],
        },
        "arithmetic": {
            "engine": "mpmath arbitrary precision",
            "decimal_precision_digits": 70,
            "absolute_tolerance": "0",
            "relative_tolerance": "1e-12",
            "fwhm_per_sigma_binary64": {
                "decimal": repr(constant),
                "hex": constant.hex(),
                "exact_rational": [str(numerator), str(denominator)],
            },
        },
        "case_count": 0,
        "cohort_sizes_covered": list(range(1, 33)),
        "cases": [],
        "expected_failure_case_count": 0,
        "expected_failure_cases": [],
        "cross_case_checks": {
            "permutation_invariance": {"passed": True},
            "sixty_fold_time_scale_invariance": {"passed": True},
        },
    }
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    reference = tmp_path / "incomplete.json"
    reference.write_bytes(payload)
    monkeypatch.setattr(
        qualification,
        "EXPECTED_REFERENCE_SHA256",
        hashlib.sha256(payload).hexdigest(),
    )
    output = tmp_path / "qualification"

    with pytest.raises(ValueError, match="case count is incomplete"):
        qualification.qualify_gaussian_producer(output, reference_json=reference)

    assert not output.exists()


def test_reviewed_default_reference_writes_a_sealed_full_cohort_report(tmp_path):
    if not qualification.DEFAULT_REFERENCE_ORACLE.is_file():
        pytest.skip("reviewed oracle package data will be added by the integrating change")

    output = tmp_path / "qualification"
    report = qualification.qualify_gaussian_producer(output)
    complete = json.loads((output / "complete.json").read_text(encoding="utf-8"))
    file_entries = complete["files"]
    assert set(file_entries) == {"report.json", "report.md", "cases.csv"}
    for name, record in file_entries.items():
        payload = (output / name).read_bytes()
        assert len(payload) == record["size_bytes"]
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]
    assert report["numerical_comparison"]["case_count"] == 43
    assert report["range_and_input_failure_checks"]["check_count"] == 10
    assert len(report["numerical_case_results"]) == 43
    assert len(report["failure_check_results"]) == 10
    assert report["numerical_comparison"]["cases_with_analyte_ids_matching"] == 43
    assert report["numerical_comparison"]["cases_with_pair_ids_matching"] == 43
    assert report["numerical_comparison"]["cases_with_statuses_matching"] == 43
    assert (
        report["numerical_comparison"]["analyte_id_elements_match"]
        == report["numerical_comparison"]["analyte_id_element_count"]
    )
    assert (
        report["numerical_comparison"]["pair_id_elements_match"]
        == report["numerical_comparison"]["pair_id_element_count"]
    )
    assert (
        report["numerical_comparison"]["status_field_match_count"]
        == report["numerical_comparison"]["status_field_count"]
    )
    assert (
        report["numerical_comparison"]["null_semantics_match_count"]
        == report["numerical_comparison"]["null_semantics_check_count"]
    )
    assert report["scope"]["fresh_bo_readiness"] is False
    assert report["scope"]["physical_peak_widths_qualified"] is False


def test_runtime_errors_preserve_all_fixed_oracle_coverage_denominators(tmp_path, monkeypatch):
    def raise_runtime_error(**_kwargs):
        raise RuntimeError("synthetic failure for coverage accounting")

    monkeypatch.setattr(qualification, "simulate_gaussian_centroids", raise_runtime_error)
    report = qualification.qualify_gaussian_producer(tmp_path / "exhausted")
    summary = report["numerical_comparison"]

    assert summary["case_count"] == 43
    assert summary["passed_case_count"] == 0
    assert summary["analyte_id_element_count"] == 559
    assert summary["analyte_id_elements_match"] == 0
    assert summary["pair_id_element_count"] == 812
    assert summary["pair_id_elements_match"] == 0
    assert summary["status_field_count"] == 2083
    assert summary["status_field_match_count"] == 0
    assert summary["null_semantics_check_count"] == 2489
    assert summary["null_semantics_match_count"] == 0
    assert summary["numeric_comparison_count"] == 2057
    assert summary["numeric_fields_evaluated"] == 0
    assert summary["numeric_match_count"] == 0
    assert summary["numeric_unmatched_count"] == 2057
    assert summary["cases_with_null_semantics_matching"] == 0
    assert len(report["numerical_case_results"]) == 43
    assert all(
        row["calculation_error_type"] == "RuntimeError"
        and row["analyte_id_element_count"] == row["expected_analyte_count"]
        and row["analyte_id_elements_match"] == 0
        and row["pair_id_element_count"] == 2 * row["expected_pair_count"]
        and row["pair_id_elements_match"] == 0
        and row["status_field_count"]
        == 3 * row["expected_analyte_count"] + row["expected_pair_count"]
        and row["status_field_match_count"] == 0
        and row["null_semantics_check_count"]
        == 3 * row["expected_analyte_count"] + 2 * row["expected_pair_count"]
        and row["null_semantics_match_count"] == 0
        and row["null_semantics_match"] is False
        and row["numeric_comparison_count"] > 0
        and row["numeric_match_count"] == 0
        and row["numeric_unmatched_count"] == row["numeric_comparison_count"]
        for row in report["numerical_case_results"]
    )


@pytest.mark.parametrize(
    ("exception_type", "passes"),
    [
        (OverflowError, True),
        (ValueError, True),
        (TypeError, False),
        (RuntimeError, False),
        (None, False),
    ],
)
def test_numeric_overflow_enforces_the_oracles_allowed_exception_types(
    monkeypatch, exception_type, passes
):
    case = {
        "case_id": "resolution_overflow_must_fail",
        "input_sha256": "f" * 64,
        "expected_failure": {
            "category": "numeric_overflow",
            "allowed_exception_types": ["OverflowError", "ValueError"],
            "must_not_return_resolution": True,
        },
    }

    def calculate(_):
        if exception_type is not None:
            raise exception_type("Injected overflow-path behavior")
        return {}

    monkeypatch.setattr(qualification, "_run_case_inputs", calculate)
    result = qualification._run_failure_case(case)
    assert result["case_passed"] is passes
    assert result["returned_any_result"] is (exception_type is None)


def test_wrong_overflow_exception_publishes_unqualified_evidence(tmp_path, monkeypatch):
    original = qualification._run_case_inputs

    def wrong_overflow(case):
        if case["case_id"] == "resolution_overflow_must_fail":
            raise TypeError("Broken numerical overflow branch")
        return original(case)

    monkeypatch.setattr(qualification, "_run_case_inputs", wrong_overflow)
    output = tmp_path / "overflow-type-disagreement"
    report = qualification.qualify_gaussian_producer(output)
    assert report["qualified"] is False
    assert report["numerical_comparison"]["passed_case_count"] == 43
    assert report["range_and_input_failure_checks"]["passed_check_count"] == 9
    overflow = next(
        row
        for row in report["failure_check_results"]
        if row["case_id"] == "resolution_overflow_must_fail"
    )
    assert overflow["observed_exception_type"] == "TypeError"
    assert overflow["case_passed"] is False
    assert (output / "complete.json").is_file()
