"""Qualify the Gaussian-centroid producer against a fixed synthetic oracle.

The adapter delegates all producer calculations to
``measure_methods.gaussian_observations``. It reads no source optimizer data,
does not fit models, and does not infer physical widths, plate height, signal,
or instrument behavior.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import platform
import stat
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

import measure_methods.chromatography as chromatography_backend
import measure_methods.gaussian_observations as gaussian_kernel
from measure_methods.gaussian_observations import GaussianAnalyte, simulate_gaussian_centroids
from measure_service.benchmarks.evidence import file_sha256, staged_bundle
from measure_service.method_backend import method_backend_info
from measure_service.moses_score_qualification import _load_json

PROFILE = "measure-service-gaussian-centroid-qualification/1"
PRODUCER_PROFILE = "ideal-gaussian-centroid/1"
RESOLUTION_DEFINITION = "gaussian-baseline-4sigma-finite-centroids/1"
DEFAULT_REFERENCE_ORACLE = (
    Path(__file__).with_name("reference_data") / "gaussian-centroid-binary64-v1.json"
)
EXPECTED_REFERENCE_SHA256 = "c508f761b8547cc40a64c8b3dc603d5e2ec69103f93c3396008c9c32fed43dd7"
_FWHM_PER_SIGMA = 2.3548
_MAX_REFERENCE_BYTES = 8 * 1024 * 1024
_ABSOLUTE_TOLERANCE = Decimal(0)
_RELATIVE_TOLERANCE = Decimal("1e-12")
_MAX_ERROR_DETAILS_PER_CASE = 100
_MISSING = object()
_VALID_CASE_IDS = frozenset(
    [f"cohort_size_{size:02d}" for size in range(1, 33)]
    + [
        "all_censored",
        "input_permutation_reversed",
        "input_permutation_forward",
        "exact_acquisition_boundary",
        "time_scale_base",
        "time_scale_sixty_fold",
        "unequal_width_minimum_not_nearest_time_neighbor",
        "binary64_cancellation_tie",
        "extreme_finite_resolution",
        "extreme_finite_centroids",
        "large_widths_finite_resolution",
    ]
)
_FAILURE_CASE_IDS = frozenset(
    {
        "negative_centroid",
        "negative_width",
        "zero_width",
        "beyond_acquisition",
        "censored_with_event",
        "eluted_without_event",
        "duplicate_ids",
        "empty_cohort",
        "cohort_size_33",
        "resolution_overflow_must_fail",
    }
)
_SCOPE = {
    "adjacency": "adjacent_finite_eluted_centroids_ordered_by_retention_time_then_analyte_id",
    "analyte_resolution": "minimum_of_incident_adjacent_pair_resolutions",
    "widths": "caller_supplied_synthetic_fwhm; fwhm_per_sigma_approximation_2.3548",
    "interpretation": "ideal_latent_model_only_not_trace_simulation_or_physical_qualification",
}
_INPUT_CONVERSION = (
    "Each decimal input is converted once to Python float; mpmath receives the exact rational "
    "returned by float.as_integer_ratio()."
)
_EXPECTED_SCOPE_KEYS = {
    "cohort_range",
    "pair_rule",
    "resolution_rule",
    "fwhm_per_sigma_limitation",
    "singleton_rule",
    "censoring_rule",
    "tie_rule",
    "per_analyte_rule",
    "numeric_inputs",
    "nonclaims",
}


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate Gaussian oracle JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant is not allowed: {value}")


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} has missing or unexpected fields")


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("value cannot be encoded as canonical finite JSON") from error


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_identity(path: Path, name: str) -> dict[str, int | str]:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ValueError(f"{name} does not exist") from error
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{name} must be a regular file")
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _decimal_string(value: Any, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a finite decimal string") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal string")
    return result


def _binary64_string(value: Any, name: str) -> tuple[float, dict[str, Any]]:
    decimal = _decimal_string(value, name)
    try:
        number = float(decimal)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} does not convert to finite binary64") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} does not convert to finite binary64")
    numerator, denominator = number.as_integer_ratio()
    return number, {
        "source_decimal": value,
        "binary64_hex": number.hex(),
        "exact_binary64_rational": [str(numerator), str(denominator)],
    }


def _validate_input(case: dict[str, Any], case_id: str) -> tuple[float, list[dict[str, Any]]]:
    input_value = _require_object(case.get("input"), f"{case_id}.input")
    _exact_keys(input_value, {"acquisition_duration_min", "analytes"}, f"{case_id}.input")
    duration, duration_evidence = _binary64_string(
        input_value["acquisition_duration_min"], f"{case_id}.acquisition_duration_min"
    )
    analyte_values = _require_list(input_value["analytes"], f"{case_id}.input.analytes")
    converted = []
    evidence_rows = []
    for index, value in enumerate(analyte_values):
        item = _require_object(value, f"{case_id}.input.analytes[{index}]")
        _exact_keys(
            item,
            {"analyte_id", "retention_status", "retention_time_min", "fwhm_min"},
            f"{case_id}.input.analytes[{index}]",
        )
        identifier = item["analyte_id"]
        status = item["retention_status"]
        if not isinstance(identifier, str) or not identifier:
            raise ValueError(f"{case_id}.analyte_id must be nonempty text")
        if not isinstance(status, str) or status not in {"eluted", "not_eluted"}:
            raise ValueError(f"{case_id}.retention_status is unknown")
        fwhm, fwhm_evidence = _binary64_string(item["fwhm_min"], f"{case_id}.{identifier}.fwhm_min")
        if item["retention_time_min"] is None:
            retention_time = None
            time_evidence = None
        else:
            retention_time, time_evidence = _binary64_string(
                item["retention_time_min"], f"{case_id}.{identifier}.retention_time_min"
            )
        converted.append(
            {
                "analyte_id": identifier,
                "retention_status": status,
                "retention_time_min": retention_time,
                "fwhm_min": fwhm,
            }
        )
        evidence_rows.append(
            {
                "analyte_id": identifier,
                "retention_time_min": time_evidence,
                "fwhm_min": fwhm_evidence,
            }
        )
    evidence = {
        "acquisition_duration_min": duration_evidence,
        "analytes": evidence_rows,
        "binary64_float_conversion": _INPUT_CONVERSION,
    }
    if case.get("binary64_input_evidence") != evidence:
        raise ValueError(f"{case_id} binary64 input evidence does not match its input strings")
    return duration, converted


def _expected_input_sha256(case: dict[str, Any], name: str) -> str:
    expected = _sha256_bytes(_canonical_json_bytes(case.get("input")))
    if case.get("input_sha256") != expected:
        raise ValueError(f"{name} input SHA256 does not match canonical inputs")
    return expected


def _validate_expected(case: dict[str, Any], case_id: str, converted: list[dict[str, Any]]) -> None:
    expected = _require_object(case.get("expected"), f"{case_id}.expected")
    expected_fields = {
        "profile",
        "resolution_definition",
        "fwhm_per_sigma",
        "acquisition_duration_min",
        "analytes",
        "pairs",
        "scope",
    }
    _exact_keys(expected, expected_fields, f"{case_id}.expected")
    if expected["profile"] != PRODUCER_PROFILE:
        raise ValueError(f"{case_id} has an unknown producer profile")
    if expected["resolution_definition"] != RESOLUTION_DEFINITION:
        raise ValueError(f"{case_id} has an unknown resolution definition")
    if (
        isinstance(expected["fwhm_per_sigma"], bool)
        or not isinstance(expected["fwhm_per_sigma"], (int, float))
        or float(expected["fwhm_per_sigma"]).hex() != float(_FWHM_PER_SIGMA).hex()
    ):
        raise ValueError(f"{case_id} has an unsupported FWHM convention")
    _decimal_string(expected["acquisition_duration_min"], f"{case_id}.expected.duration")
    if expected["scope"] != _SCOPE:
        raise ValueError(f"{case_id} has unexpected producer scope metadata")

    expected_analytes = _require_list(expected["analytes"], f"{case_id}.expected.analytes")
    input_by_id = {item["analyte_id"]: item for item in converted}
    expected_ids = sorted(input_by_id)
    if not _compare_number(
        float(case["input"]["acquisition_duration_min"]), expected["acquisition_duration_min"]
    )[0]:
        raise ValueError(f"{case_id} reference duration does not match its input")
    if len(expected_analytes) != len(converted):
        raise ValueError(f"{case_id} reference does not preserve the complete cohort")
    observed_ids = []
    for index, value in enumerate(expected_analytes):
        item = _require_object(value, f"{case_id}.expected.analytes[{index}]")
        _exact_keys(
            item,
            {
                "analyte_id",
                "retention_status",
                "retention_time_min",
                "fwhm_min",
                "resolution_status",
                "resolution",
                "right_censor_bound_min",
                "resolution_reason",
            },
            f"{case_id}.expected.analytes[{index}]",
        )
        observed_ids.append(item["analyte_id"])
        input_item = input_by_id.get(item["analyte_id"])
        if input_item is None or item["retention_status"] != input_item["retention_status"]:
            raise ValueError(f"{case_id} reference retention status is not bound to its input")
        if not _compare_number(input_item["fwhm_min"], item["fwhm_min"])[0]:
            raise ValueError(f"{case_id} reference FWHM is not bound to its input")
        if (item["retention_time_min"] is None) != (input_item["retention_time_min"] is None):
            raise ValueError(f"{case_id} reference event null state is not bound to its input")
        if (
            item["retention_time_min"] is not None
            and not _compare_number(input_item["retention_time_min"], item["retention_time_min"])[0]
        ):
            raise ValueError(f"{case_id} reference event time is not bound to its input")
        if item["retention_status"] not in {"eluted", "not_eluted"}:
            raise ValueError(f"{case_id} reference has an unknown retention status")
        if item["resolution_status"] not in {"observed", "unsupported", "censored"}:
            raise ValueError(f"{case_id} reference has an unknown resolution status")
        _decimal_string(item["fwhm_min"], f"{case_id}.expected.fwhm_min")
        for field in ("retention_time_min", "resolution", "right_censor_bound_min"):
            if item[field] is not None:
                _decimal_string(item[field], f"{case_id}.expected.{field}")
        if item["resolution_status"] == "observed":
            if item["resolution"] is None or item["right_censor_bound_min"] is not None:
                raise ValueError(f"{case_id} observed reference has invalid null semantics")
            if item["resolution_reason"] is not None:
                raise ValueError(f"{case_id} observed reference has an unexpected reason")
        elif item["resolution_status"] == "censored":
            if (
                item["retention_status"] != "not_eluted"
                or item["retention_time_min"] is not None
                or item["resolution"] is not None
                or item["right_censor_bound_min"] is None
                or item["resolution_reason"] != "retention_not_eluted_within_acquisition_duration"
            ):
                raise ValueError(f"{case_id} censored reference has invalid null semantics")
            if not _compare_number(
                float(case["input"]["acquisition_duration_min"]),
                item["right_censor_bound_min"],
            )[0]:
                raise ValueError(f"{case_id} censored bound does not match acquisition duration")
        elif (
            item["retention_status"] != "eluted"
            or item["resolution"] is not None
            or item["right_censor_bound_min"] is not None
            or item["resolution_reason"] != "no_adjacent_finite_centroid"
        ):
            raise ValueError(f"{case_id} unsupported reference has invalid null semantics")
    if observed_ids != expected_ids:
        raise ValueError(
            f"{case_id} reference does not preserve each analyte identity once in order"
        )

    finite = sorted(
        (item for item in converted if item["retention_status"] == "eluted"),
        key=lambda item: (item["retention_time_min"], item["analyte_id"]),
    )
    expected_pair_ids = sorted(
        [
            sorted((left["analyte_id"], right["analyte_id"]))
            for left, right in zip(finite, finite[1:])
        ]
    )
    expected_pairs = _require_list(expected["pairs"], f"{case_id}.expected.pairs")
    pair_ids = []
    for index, value in enumerate(expected_pairs):
        pair = _require_object(value, f"{case_id}.expected.pairs[{index}]")
        _exact_keys(
            pair,
            {"analyte_ids", "resolution_status", "resolution", "right_censor_bound_min"},
            f"{case_id}.expected.pairs[{index}]",
        )
        identifiers = pair["analyte_ids"]
        if (
            not isinstance(identifiers, list)
            or len(identifiers) != 2
            or any(not isinstance(identifier, str) for identifier in identifiers)
            or identifiers != sorted(identifiers)
        ):
            raise ValueError(f"{case_id} reference pair identity is invalid")
        pair_ids.append(identifiers)
        if pair["resolution_status"] != "observed" or pair["right_censor_bound_min"] is not None:
            raise ValueError(f"{case_id} reference pair status/null semantics are invalid")
        _decimal_string(pair["resolution"], f"{case_id}.expected.pair_resolution")
    if pair_ids != expected_pair_ids:
        raise ValueError(f"{case_id} reference pair identities do not follow finite adjacency")
    incident_analyte_ids = {identifier for pair in pair_ids for identifier in pair}
    status_by_id = {item["analyte_id"]: item["resolution_status"] for item in expected_analytes}
    for identifier, item in input_by_id.items():
        if item["retention_status"] == "not_eluted":
            if status_by_id[identifier] != "censored":
                raise ValueError(f"{case_id} censored input is not preserved in the reference")
        elif status_by_id[identifier] != (
            "observed" if identifier in incident_analyte_ids else "unsupported"
        ):
            raise ValueError(f"{case_id} eluted input has inconsistent adjacency status")


def _validate_failure_case(case: dict[str, Any], case_id: str) -> None:
    description = case.get("description")
    if not isinstance(description, str) or not description:
        raise ValueError(f"{case_id} failure description must be nonempty")
    failure = _require_object(case.get("expected_failure"), f"{case_id}.expected_failure")
    if failure.get("must_not_return_resolution") is not True:
        raise ValueError(f"{case_id} failure case must require an explicit calculation failure")
    category = failure.get("category")
    if not isinstance(category, str) or category not in {"input_validation", "numeric_overflow"}:
        raise ValueError(f"{case_id} has an unknown failure category")
    if (case_id == "resolution_overflow_must_fail") != (category == "numeric_overflow"):
        raise ValueError(f"{case_id} has a failure category inconsistent with its identity")
    if category == "numeric_overflow":
        _exact_keys(
            failure,
            {"category", "allowed_exception_types", "must_not_return_resolution"},
            f"{case_id}.expected_failure",
        )
        if failure.get("allowed_exception_types") != ["OverflowError", "ValueError"]:
            raise ValueError(f"{case_id} has an unsupported numeric-overflow failure contract")
        diagnostic = _require_object(case.get("reference_diagnostic"), f"{case_id}.diagnostic")
        if diagnostic.get("high_precision_result_class") != "greater_than_maximum_finite_binary64":
            raise ValueError(f"{case_id} lacks the reviewed overflow diagnostic")
        reference = _decimal_string(
            diagnostic.get("reference_resolution_decimal"), f"{case_id}.reference_resolution"
        )
        if reference <= Decimal.from_float(sys.float_info.max):
            raise ValueError(f"{case_id} reference diagnostic is not outside binary64 range")
    else:
        _exact_keys(
            failure,
            {"category", "must_not_return_resolution"},
            f"{case_id}.expected_failure",
        )


def _validate_reference(document: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    document = _require_object(document, "Gaussian reference")
    expected_document_keys = {
        "schema_version",
        "profile",
        "producer_profile",
        "resolution_definition",
        "fwhm_per_sigma",
        "scope",
        "arithmetic",
        "case_count",
        "cohort_sizes_covered",
        "cases",
        "expected_failure_case_count",
        "expected_failure_cases",
        "cross_case_checks",
    }
    _exact_keys(document, expected_document_keys, "Gaussian reference")
    if (
        document["schema_version"] != 1
        or document["profile"] != "measure-service-gaussian-centroid-independent-reference/1"
    ):
        raise ValueError("unsupported Gaussian oracle schema or profile")
    if document["producer_profile"] != PRODUCER_PROFILE:
        raise ValueError("Gaussian oracle is bound to an unsupported producer profile")
    if document["resolution_definition"] != RESOLUTION_DEFINITION:
        raise ValueError("Gaussian oracle is bound to an unsupported resolution definition")
    if (
        isinstance(document["fwhm_per_sigma"], bool)
        or not isinstance(document["fwhm_per_sigma"], (int, float))
        or float(document["fwhm_per_sigma"]).hex() != float(_FWHM_PER_SIGMA).hex()
    ):
        raise ValueError("Gaussian oracle uses an unsupported FWHM convention")
    scope = _require_object(document["scope"], "Gaussian reference scope")
    _exact_keys(scope, _EXPECTED_SCOPE_KEYS, "Gaussian reference scope")
    if scope.get("cohort_range") != [1, 32] or not isinstance(scope.get("nonclaims"), list):
        raise ValueError("Gaussian reference scope is incomplete")
    arithmetic = _require_object(document["arithmetic"], "Gaussian reference arithmetic")
    if (
        arithmetic.get("engine") != "mpmath arbitrary precision"
        or arithmetic.get("decimal_precision_digits") != 70
        or arithmetic.get("absolute_tolerance") != "0"
        or arithmetic.get("relative_tolerance") != "1e-12"
    ):
        raise ValueError("Gaussian reference arithmetic or tolerance contract is unsupported")
    constant_evidence = _require_object(
        arithmetic.get("fwhm_per_sigma_binary64"), "FWHM-per-sigma evidence"
    )
    constant = float(_FWHM_PER_SIGMA)
    numerator, denominator = constant.as_integer_ratio()
    if constant_evidence != {
        "decimal": repr(_FWHM_PER_SIGMA),
        "hex": constant.hex(),
        "exact_rational": [str(numerator), str(denominator)],
    }:
        raise ValueError("Gaussian reference FWHM constant evidence is invalid")
    if document["cohort_sizes_covered"] != list(range(1, 33)):
        raise ValueError("Gaussian reference does not cover cohort sizes 1 through 32")

    cases = _require_list(document["cases"], "Gaussian reference cases")
    if document["case_count"] != len(_VALID_CASE_IDS) or len(cases) != len(_VALID_CASE_IDS):
        raise ValueError("Gaussian reference case count is incomplete")
    valid_ids = []
    for index, value in enumerate(cases):
        case = _require_object(value, f"Gaussian reference case {index}")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id not in _VALID_CASE_IDS:
            raise ValueError("Gaussian reference has an unknown numerical case identity")
        _exact_keys(
            case,
            {"case_id", "scenario", "input", "input_sha256", "binary64_input_evidence", "expected"},
            f"{case_id}",
        )
        if not isinstance(case.get("scenario"), str):
            raise ValueError(f"{case_id} scenario must be text")
        _expected_input_sha256(case, case_id)
        _, converted = _validate_input(case, case_id)
        _validate_expected(case, case_id, converted)
        valid_ids.append(case_id)
    if len(set(valid_ids)) != len(valid_ids) or set(valid_ids) != _VALID_CASE_IDS:
        raise ValueError("Gaussian reference is missing or duplicates a numerical case")

    failure_cases = _require_list(
        document["expected_failure_cases"], "Gaussian expected failure cases"
    )
    if document["expected_failure_case_count"] != len(_FAILURE_CASE_IDS) or len(
        failure_cases
    ) != len(_FAILURE_CASE_IDS):
        raise ValueError("Gaussian expected failure case count is incomplete")
    failure_ids = []
    for index, value in enumerate(failure_cases):
        case = _require_object(value, f"Gaussian expected failure case {index}")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id not in _FAILURE_CASE_IDS:
            raise ValueError("Gaussian reference has an unknown failure-case identity")
        _exact_keys(
            case,
            {
                "case_id",
                "description",
                "input",
                "input_sha256",
                "binary64_input_evidence",
                "expected_failure",
                *(
                    {"reference_diagnostic"}
                    if case_id == "resolution_overflow_must_fail"
                    else set()
                ),
            },
            f"{case_id}",
        )
        _expected_input_sha256(case, case_id)
        _validate_input(case, case_id)
        _validate_failure_case(case, case_id)
        failure_ids.append(case_id)
    if len(set(failure_ids)) != len(failure_ids) or set(failure_ids) != _FAILURE_CASE_IDS:
        raise ValueError("Gaussian reference is missing or duplicates a failure case")

    cross_checks = _require_object(document["cross_case_checks"], "Gaussian cross-case checks")
    for name in ("permutation_invariance", "sixty_fold_time_scale_invariance"):
        item = _require_object(cross_checks.get(name), f"Gaussian {name}")
        if item.get("passed") is not True:
            raise ValueError(f"Gaussian oracle cross-case check failed: {name}")
    by_id = {case["case_id"]: case for case in cases}
    if (
        by_id["input_permutation_forward"]["expected"]
        != by_id["input_permutation_reversed"]["expected"]
    ):
        raise ValueError("Gaussian oracle permutation fixtures do not agree")
    base_pairs = by_id["time_scale_base"]["expected"]["pairs"]
    scaled_pairs = by_id["time_scale_sixty_fold"]["expected"]["pairs"]
    if [row["resolution"] for row in base_pairs] != [row["resolution"] for row in scaled_pairs]:
        raise ValueError("Gaussian oracle scale-invariance fixtures do not agree")
    return cases, failure_cases


def _load_reference(path: Path) -> tuple[dict[str, Any], str, dict[str, int | str]]:
    identity = _file_identity(path, "Gaussian reference oracle")
    if identity["size_bytes"] > _MAX_REFERENCE_BYTES:
        raise ValueError(f"Gaussian reference exceeds {_MAX_REFERENCE_BYTES}-byte limit")
    payload = path.read_bytes()
    digest = _sha256_bytes(payload)
    if digest != EXPECTED_REFERENCE_SHA256:
        raise ValueError("Gaussian reference SHA256 does not match the reviewed oracle")
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Gaussian reference is not valid strict UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise ValueError("Gaussian reference must be a JSON object")
    # Reuse the service's bounded loader as a second regular-file/size guard.
    _load_json(path, maximum_bytes=_MAX_REFERENCE_BYTES)
    return document, digest, identity


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return "0" if value.is_zero() else format(value, ".18E")


def _compare_number(observed: Any, expected: Any) -> tuple[bool, Decimal | None, Decimal | None]:
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return False, None, None
    actual = float(observed)
    if not math.isfinite(actual):
        return False, None, None
    if isinstance(expected, bool) or not isinstance(expected, (str, int, float)):
        return False, None, None
    try:
        expected_decimal = (
            _decimal_string(expected, "expected numeric value")
            if isinstance(expected, str)
            else Decimal.from_float(float(expected))
        )
    except (ValueError, OverflowError):
        return False, None, None
    actual_decimal = Decimal.from_float(actual)
    with localcontext() as context:
        context.prec = 1200
        absolute_error = abs(actual_decimal - expected_decimal)
        relative_error = None if expected_decimal == 0 else absolute_error / abs(expected_decimal)
        threshold = _ABSOLUTE_TOLERANCE + _RELATIVE_TOLERANCE * abs(expected_decimal)
        return absolute_error <= threshold, absolute_error, relative_error


def _run_case_inputs(case: dict[str, Any]) -> dict[str, Any]:
    input_value = case["input"]
    duration, _ = _binary64_string(input_value["acquisition_duration_min"], "duration")
    analytes = []
    for item in input_value["analytes"]:
        retention_time = item["retention_time_min"]
        analytes.append(
            GaussianAnalyte(
                analyte_id=item["analyte_id"],
                retention_status=item["retention_status"],
                retention_time_min=(
                    None if retention_time is None else _binary64_string(retention_time, "time")[0]
                ),
                fwhm_min=_binary64_string(item["fwhm_min"], "fwhm")[0],
            )
        )
    return simulate_gaussian_centroids(
        analytes=analytes,
        acquisition_duration_min=duration,
    )


def _case_coverage_denominators(case: dict[str, Any]) -> dict[str, int]:
    """Derive expected comparison counts from immutable reference rows only."""
    expected_analytes = case["expected"]["analytes"]
    expected_pairs = case["expected"]["pairs"]
    analyte_count = len(expected_analytes)
    pair_count = len(expected_pairs)
    expected_nullable_values = [
        item[field]
        for item in expected_analytes
        for field in ("retention_time_min", "resolution", "right_censor_bound_min")
    ] + [
        item[field] for item in expected_pairs for field in ("resolution", "right_censor_bound_min")
    ]
    nullable_field_count = len(expected_nullable_values)
    numeric_field_count = (
        2 + analyte_count + sum(value is not None for value in expected_nullable_values)
    )
    return {
        "analyte_id_element_count": analyte_count,
        "pair_id_element_count": 2 * pair_count,
        "status_field_count": 3 * analyte_count + pair_count,
        "null_semantics_check_count": nullable_field_count,
        "numeric_comparison_count": numeric_field_count,
    }


def _field_or_missing(value: Any, name: str) -> Any:
    if not isinstance(value, dict) or name not in value:
        return _MISSING
    return value[name]


def _compare_case_output(case: dict[str, Any], observed: Any) -> dict[str, Any]:
    expected = case["expected"]
    case_id = case["case_id"]
    coverage = _case_coverage_denominators(case)
    issues: list[dict[str, str]] = []
    issue_counts: Counter[str] = Counter()
    numeric_evaluated_count = 0
    numeric_matches = 0
    null_matches = 0
    max_absolute_error: Decimal | None = None
    max_relative_error: Decimal | None = None

    def issue(path: str, category: str) -> None:
        issue_counts[category] += 1
        if len(issues) < _MAX_ERROR_DETAILS_PER_CASE:
            issues.append({"field_path": path, "category": category})

    def numeric(path: str, actual_value: Any, expected_value: Any) -> None:
        nonlocal numeric_evaluated_count, numeric_matches, max_absolute_error, max_relative_error
        numeric_evaluated_count += 1
        passed, absolute_error, relative_error = _compare_number(actual_value, expected_value)
        if absolute_error is not None:
            if max_absolute_error is None or absolute_error > max_absolute_error:
                max_absolute_error = absolute_error
        if relative_error is not None:
            if max_relative_error is None or relative_error > max_relative_error:
                max_relative_error = relative_error
        if passed:
            numeric_matches += 1
        else:
            issue(path, "numeric_mismatch")

    def nullable_numeric(path: str, actual_value: Any, expected_value: Any) -> None:
        nonlocal null_matches
        if actual_value is _MISSING or (actual_value is None) != (expected_value is None):
            issue(path, "null_semantics_mismatch")
            if expected_value is not None:
                numeric(path, actual_value, expected_value)
        else:
            null_matches += 1
            if actual_value is not None:
                numeric(path, actual_value, expected_value)

    if not isinstance(observed, dict):
        issue("output", "output_shape_mismatch")
        return {
            "case_id": case_id,
            "input_sha256": case["input_sha256"],
            "case_kind": "numerical_reference",
            "case_passed": False,
            "expected_analyte_count": len(expected["analytes"]),
            "observed_analyte_count": None,
            "expected_pair_count": len(expected["pairs"]),
            "observed_pair_count": None,
            "analyte_ids_match": False,
            "analyte_id_element_count": coverage["analyte_id_element_count"],
            "analyte_id_elements_match": 0,
            "pair_ids_match": False,
            "pair_id_element_count": coverage["pair_id_element_count"],
            "pair_id_elements_match": 0,
            "status_field_count": coverage["status_field_count"],
            "status_field_match_count": 0,
            "statuses_match": False,
            "null_semantics_match": False,
            "null_semantics_check_count": coverage["null_semantics_check_count"],
            "null_semantics_match_count": 0,
            "numeric_comparison_count": coverage["numeric_comparison_count"],
            "numeric_fields_evaluated": 0,
            "numeric_match_count": 0,
            "numeric_mismatch_count": 0,
            "numeric_unmatched_count": coverage["numeric_comparison_count"],
            "maximum_absolute_error": None,
            "maximum_relative_error": None,
            "issue_count": dict(sorted(issue_counts.items())),
            "issues": issues,
        }

    expected_top_keys = {
        "profile",
        "resolution_definition",
        "fwhm_per_sigma",
        "acquisition_duration_min",
        "analytes",
        "pairs",
        "scope",
    }
    if set(observed) != expected_top_keys:
        issue("output.keys", "output_shape_mismatch")
    if observed.get("profile") != expected["profile"]:
        issue("profile", "profile_mismatch")
    if observed.get("resolution_definition") != expected["resolution_definition"]:
        issue("resolution_definition", "profile_mismatch")
    if observed.get("scope") != expected["scope"]:
        issue("scope", "scope_mismatch")
    numeric(
        "fwhm_per_sigma",
        _field_or_missing(observed, "fwhm_per_sigma"),
        expected["fwhm_per_sigma"],
    )
    numeric(
        "acquisition_duration_min",
        _field_or_missing(observed, "acquisition_duration_min"),
        expected["acquisition_duration_min"],
    )

    expected_analytes = expected["analytes"]
    observed_analytes = observed.get("analytes")
    analyte_ids_match = False
    status_counts_match = True
    analyte_id_elements_match = 0
    pair_id_elements_match = 0
    status_field_match_count = 0
    if not isinstance(observed_analytes, list):
        issue("analytes", "output_shape_mismatch")
        observed_analytes = []
    expected_ids = [item["analyte_id"] for item in expected_analytes]
    observed_ids = [
        item.get("analyte_id") if isinstance(item, dict) else None for item in observed_analytes
    ]
    analyte_ids_match = observed_ids == expected_ids
    if not analyte_ids_match:
        issue("analytes.ids", "identity_mismatch")
    if len(observed_analytes) != len(expected_analytes):
        issue("analytes.length", "cohort_size_mismatch")
    for index, reference in enumerate(expected_analytes):
        identifier = reference["analyte_id"]
        prefix = f"analytes[{identifier}]"
        if index >= len(observed_analytes) or not isinstance(observed_analytes[index], dict):
            actual = _MISSING
            issue(prefix, "identity_missing")
            status_counts_match = False
        else:
            actual = observed_analytes[index]
        if _field_or_missing(actual, "analyte_id") == identifier:
            analyte_id_elements_match += 1
        else:
            issue(f"{prefix}.analyte_id", "identity_mismatch")
        if not isinstance(actual, dict) or set(actual) != {
            "analyte_id",
            "retention_status",
            "retention_time_min",
            "fwhm_min",
            "resolution_status",
            "resolution",
            "right_censor_bound_min",
            "resolution_reason",
        }:
            issue(f"{prefix}.keys", "output_shape_mismatch")
        for field in ("retention_status", "resolution_status", "resolution_reason"):
            actual_value = _field_or_missing(actual, field)
            if actual_value is _MISSING or actual_value != reference[field]:
                issue(f"{prefix}.{field}", "status_mismatch")
                status_counts_match = False
            else:
                status_field_match_count += 1
        for field in ("retention_time_min", "resolution", "right_censor_bound_min"):
            nullable_numeric(
                f"{prefix}.{field}", _field_or_missing(actual, field), reference[field]
            )
        numeric(
            f"{prefix}.fwhm_min",
            _field_or_missing(actual, "fwhm_min"),
            reference["fwhm_min"],
        )

    expected_pairs = expected["pairs"]
    observed_pairs = observed.get("pairs")
    if not isinstance(observed_pairs, list):
        issue("pairs", "output_shape_mismatch")
        observed_pairs = []
    expected_pair_ids = [item["analyte_ids"] for item in expected_pairs]
    observed_pair_ids = [
        item.get("analyte_ids") if isinstance(item, dict) else None for item in observed_pairs
    ]
    pair_ids_match = observed_pair_ids == expected_pair_ids
    if not pair_ids_match:
        issue("pairs.ids", "identity_mismatch")
    if len(observed_pairs) != len(expected_pairs):
        issue("pairs.length", "pair_count_mismatch")
    for index, reference in enumerate(expected_pairs):
        pair = tuple(reference["analyte_ids"])
        prefix = f"pairs[{pair[0]}|{pair[1]}]"
        if index >= len(observed_pairs) or not isinstance(observed_pairs[index], dict):
            actual = _MISSING
            issue(prefix, "identity_missing")
            status_counts_match = False
        else:
            actual = observed_pairs[index]
        actual_pair_ids = _field_or_missing(actual, "analyte_ids")
        if isinstance(actual_pair_ids, list):
            pair_id_elements_match += sum(
                left == right
                for left, right in zip(actual_pair_ids, reference["analyte_ids"], strict=False)
            )
        if actual_pair_ids != reference["analyte_ids"]:
            issue(f"{prefix}.analyte_ids", "identity_mismatch")
        if not isinstance(actual, dict) or set(actual) != {
            "analyte_ids",
            "resolution_status",
            "resolution",
            "right_censor_bound_min",
        }:
            issue(f"{prefix}.keys", "output_shape_mismatch")
        actual_status = _field_or_missing(actual, "resolution_status")
        if actual_status is _MISSING or actual_status != reference["resolution_status"]:
            issue(f"{prefix}.resolution_status", "status_mismatch")
            status_counts_match = False
        else:
            status_field_match_count += 1
        nullable_numeric(
            f"{prefix}.resolution",
            _field_or_missing(actual, "resolution"),
            reference["resolution"],
        )
        nullable_numeric(
            f"{prefix}.right_censor_bound_min",
            _field_or_missing(actual, "right_censor_bound_min"),
            reference["right_censor_bound_min"],
        )

    null_semantics_match = (
        null_matches == coverage["null_semantics_check_count"]
        and issue_counts["null_semantics_mismatch"] == 0
    )
    passed = not issue_counts
    return {
        "case_id": case_id,
        "input_sha256": case["input_sha256"],
        "case_kind": "numerical_reference",
        "case_passed": passed,
        "expected_analyte_count": len(expected_analytes),
        "observed_analyte_count": len(observed_analytes),
        "expected_pair_count": len(expected_pairs),
        "observed_pair_count": len(observed_pairs),
        "analyte_ids_match": analyte_ids_match,
        "analyte_id_element_count": coverage["analyte_id_element_count"],
        "analyte_id_elements_match": analyte_id_elements_match,
        "pair_ids_match": pair_ids_match,
        "pair_id_element_count": coverage["pair_id_element_count"],
        "pair_id_elements_match": pair_id_elements_match,
        "status_field_count": coverage["status_field_count"],
        "status_field_match_count": status_field_match_count,
        "statuses_match": status_counts_match,
        "null_semantics_match": null_semantics_match,
        "null_semantics_check_count": coverage["null_semantics_check_count"],
        "null_semantics_match_count": null_matches,
        "numeric_comparison_count": coverage["numeric_comparison_count"],
        "numeric_fields_evaluated": numeric_evaluated_count,
        "numeric_match_count": numeric_matches,
        "numeric_mismatch_count": issue_counts["numeric_mismatch"],
        "numeric_unmatched_count": coverage["numeric_comparison_count"] - numeric_matches,
        "maximum_absolute_error": _decimal_text(max_absolute_error),
        "maximum_relative_error": _decimal_text(max_relative_error),
        "issue_count": dict(sorted(issue_counts.items())),
        "issues": issues,
        "omitted_issue_detail_count": max(0, sum(issue_counts.values()) - len(issues)),
    }


def _run_numerical_case(case: dict[str, Any]) -> dict[str, Any]:
    try:
        observed = _run_case_inputs(case)
    except Exception as error:
        coverage = _case_coverage_denominators(case)
        return {
            "case_id": case["case_id"],
            "input_sha256": case["input_sha256"],
            "case_kind": "numerical_reference",
            "case_passed": False,
            "expected_analyte_count": len(case["expected"]["analytes"]),
            "observed_analyte_count": None,
            "expected_pair_count": len(case["expected"]["pairs"]),
            "observed_pair_count": None,
            "analyte_ids_match": False,
            "analyte_id_element_count": coverage["analyte_id_element_count"],
            "analyte_id_elements_match": 0,
            "pair_ids_match": False,
            "pair_id_element_count": coverage["pair_id_element_count"],
            "pair_id_elements_match": 0,
            "status_field_count": coverage["status_field_count"],
            "status_field_match_count": 0,
            "statuses_match": False,
            "null_semantics_match": False,
            "null_semantics_check_count": coverage["null_semantics_check_count"],
            "null_semantics_match_count": 0,
            "numeric_comparison_count": coverage["numeric_comparison_count"],
            "numeric_fields_evaluated": 0,
            "numeric_match_count": 0,
            "numeric_mismatch_count": 0,
            "numeric_unmatched_count": coverage["numeric_comparison_count"],
            "maximum_absolute_error": None,
            "maximum_relative_error": None,
            "issue_count": {"calculation_error": 1},
            "issues": [{"field_path": "calculation", "category": "calculation_error"}],
            "calculation_error_type": type(error).__name__,
        }
    return _compare_case_output(case, observed)


def _run_failure_case(case: dict[str, Any]) -> dict[str, Any]:
    exception_type = None
    returned = False
    try:
        result = _run_case_inputs(case)
    except Exception as error:
        exception_type = type(error).__name__
    else:
        returned = True
        # Retain only whether anything was returned; never serialize a possibly
        # false numeric value from a case whose oracle requires explicit failure.
        del result
    category = case["expected_failure"]["category"]
    allowed_types = (
        set(case["expected_failure"]["allowed_exception_types"])
        if category == "numeric_overflow"
        else {"TypeError", "ValueError"}
    )
    passed = not returned and exception_type in allowed_types
    return {
        "case_id": case["case_id"],
        "input_sha256": case["input_sha256"],
        "case_kind": "expected_failure_check",
        "case_passed": passed,
        "failure_category": category,
        "explicit_failure_observed": not returned,
        "observed_exception_type": exception_type,
        "returned_any_result": returned,
        "issue_count": {} if passed else {"expected_failure_not_observed": 1},
        "issues": []
        if passed
        else [{"field_path": "calculation", "category": "expected_failure_not_observed"}],
    }


def _implementation_provenance() -> dict[str, Any]:
    return {
        "runtime": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
        },
        "package": method_backend_info(),
        "service_adapter": {
            "module": "measure_service.benchmarks.gaussian_qualification",
            "source_sha256": file_sha256(Path(__file__)),
        },
        "gaussian_kernel": {
            "module": "measure_methods.gaussian_observations",
            "source_sha256": file_sha256(Path(gaussian_kernel.__file__)),
        },
        "shared_chromatography_helper": {
            "module": "measure_methods.chromatography",
            "source_sha256": file_sha256(Path(chromatography_backend.__file__)),
        },
    }


def _render_report(report: dict[str, Any]) -> str:
    numerical = report["numerical_comparison"]
    failures = report["range_and_input_failure_checks"]
    lines = [
        "# Gaussian-centroid producer qualification",
        "",
        f"Profile: `{report['profile']}`. Result: **{report['qualification_status']}**.",
        "",
        f"Reviewed synthetic oracle SHA256: `{report['inputs']['reference_oracle']['sha256']}`.",
        f"Numerical cases: {numerical['passed_case_count']}/{numerical['case_count']}; expected-failure checks: {failures['passed_check_count']}/{failures['check_count']}.",
        "",
        "## Cohort, identity and status checks",
        "",
        f"Complete cohort ID lists matched in {numerical['cases_with_analyte_ids_matching']}/{numerical['case_count']} cases ({numerical['analyte_id_elements_match']}/{numerical['analyte_id_element_count']} analyte IDs). Pair ID lists matched in {numerical['cases_with_pair_ids_matching']}/{numerical['case_count']} cases ({numerical['pair_id_elements_match']}/{numerical['pair_id_element_count']} pair IDs). Status fields matched in {numerical['status_field_match_count']}/{numerical['status_field_count']}; nullable event, resolution and censor-bound semantics matched in {numerical['null_semantics_match_count']}/{numerical['null_semantics_check_count']} fields across {numerical['cases_with_null_semantics_matching']}/{numerical['case_count']} cases.",
        "",
        f"The oracle defines {numerical['numeric_comparison_count']} numeric fields; the adapter evaluated {numerical['numeric_fields_evaluated']} and matched {numerical['numeric_match_count']}. Tolerances are absolute {numerical['absolute_tolerance']} and relative {numerical['relative_tolerance']}; unmatched fields include calculations unavailable after an error. Maximum observed absolute difference: `{numerical['maximum_absolute_error']}`; maximum relative difference: `{numerical['maximum_relative_error']}`.",
        "",
        "Every reference cohort is compared as a complete list, including censored analytes, unsupported singleton resolution, adjacent pair identities, statuses, reasons and null fields. Numeric and status disagreements remain in `cases.csv` and the separate numerical-case or failure-check result arrays in `report.json`; none are converted to zero or omitted.",
        "",
        "## Scientific scope",
        "",
        "This qualifies only the named ideal Gaussian-centroid arithmetic with caller-supplied synthetic FWHM values and the declared rounded FWHM-per-sigma constant `2.3548`. That constant differs from the exact Gaussian conversion `2*sqrt(2*ln(2))` (approximately `2.354820045`).",
        "",
        "The result does not qualify physical peak widths, plate height, chromatography signals, instruments, acquisitions, chemical identities, model fitting, or fresh Bayesian optimization. A qualification pass does not establish those properties. The independent reference uses high-precision arithmetic over the exact parsed binary64 input values; tolerance checks are numerical comparisons, not formal error bounds.",
        "",
        "`complete.json` records hashes and byte sizes for the report and case table. Checksums establish file integrity, not signatures or historical runtime provenance.",
        "",
    ]
    return "\n".join(lines)


def _verify_input_unchanged(
    path: Path, initial_identity: dict[str, int | str], digest: str
) -> None:
    current_identity = _file_identity(path, "Gaussian reference oracle")
    if current_identity != initial_identity or file_sha256(path) != digest:
        raise ValueError("Gaussian reference oracle changed during qualification")


def _aggregate_case_rows(rows: list[dict[str, Any]], *, kind: str) -> dict[str, Any]:
    chosen = [row for row in rows if row["case_kind"] == kind]
    passed_count = sum(row["case_passed"] is True for row in chosen)
    aggregate_issue_counts: Counter[str] = Counter()
    numeric_comparison_count = 0
    numeric_fields_evaluated = 0
    numeric_match_count = 0
    for row in chosen:
        aggregate_issue_counts.update(row.get("issue_count", {}))
        numeric_comparison_count += row.get("numeric_comparison_count", 0)
        numeric_fields_evaluated += row.get("numeric_fields_evaluated", 0)
        numeric_match_count += row.get("numeric_match_count", 0)
    result: dict[str, Any] = {
        "case_count" if kind == "numerical_reference" else "check_count": len(chosen),
        "passed_case_count"
        if kind == "numerical_reference"
        else "passed_check_count": passed_count,
        "all_passed": passed_count == len(chosen),
        "issue_counts": dict(sorted(aggregate_issue_counts.items())),
    }
    if kind == "numerical_reference":
        result.update(
            {
                "numeric_comparison_count": numeric_comparison_count,
                "numeric_fields_evaluated": numeric_fields_evaluated,
                "numeric_match_count": numeric_match_count,
                "numeric_mismatch_count": aggregate_issue_counts["numeric_mismatch"],
                "numeric_unmatched_count": numeric_comparison_count - numeric_match_count,
                "null_semantics_check_count": sum(
                    row.get("null_semantics_check_count", 0) for row in chosen
                ),
                "null_semantics_match_count": sum(
                    row.get("null_semantics_match_count", 0) for row in chosen
                ),
                "absolute_tolerance": str(_ABSOLUTE_TOLERANCE),
                "relative_tolerance": str(_RELATIVE_TOLERANCE),
                "cases_with_analyte_ids_matching": sum(
                    row.get("analyte_ids_match") is True for row in chosen
                ),
                "cases_with_pair_ids_matching": sum(
                    row.get("pair_ids_match") is True for row in chosen
                ),
                "cases_with_statuses_matching": sum(
                    row.get("statuses_match") is True for row in chosen
                ),
                "cases_with_null_semantics_matching": sum(
                    row.get("null_semantics_match") is True for row in chosen
                ),
                "expected_analyte_record_count": sum(
                    row.get("expected_analyte_count", 0) for row in chosen
                ),
                "observed_analyte_record_count": sum(
                    row.get("observed_analyte_count") or 0 for row in chosen
                ),
                "analyte_id_element_count": sum(
                    row.get("analyte_id_element_count", 0) for row in chosen
                ),
                "analyte_id_elements_match": sum(
                    row.get("analyte_id_elements_match", 0) for row in chosen
                ),
                "expected_pair_record_count": sum(
                    row.get("expected_pair_count", 0) for row in chosen
                ),
                "observed_pair_record_count": sum(
                    row.get("observed_pair_count") or 0 for row in chosen
                ),
                "pair_id_element_count": sum(row.get("pair_id_element_count", 0) for row in chosen),
                "pair_id_elements_match": sum(
                    row.get("pair_id_elements_match", 0) for row in chosen
                ),
                "status_field_count": sum(row.get("status_field_count", 0) for row in chosen),
                "status_field_match_count": sum(
                    row.get("status_field_match_count", 0) for row in chosen
                ),
                "maximum_absolute_error": _max_error_text(chosen, "maximum_absolute_error"),
                "maximum_relative_error": _max_error_text(chosen, "maximum_relative_error"),
            }
        )
    return result


def _max_error_text(rows: list[dict[str, Any]], field: str) -> str | None:
    values = []
    for row in rows:
        value = row.get(field)
        if isinstance(value, str):
            try:
                values.append(Decimal(value))
            except InvalidOperation:
                continue
    return None if not values else str(max(values))


def qualify_gaussian_producer(
    output_dir: Path,
    *,
    reference_json: Path | None = None,
) -> dict[str, Any]:
    """Compare the pure Gaussian-centroid kernel with a fixed synthetic oracle.

    The reviewed reference is pinned by bytes and covers 43 numerical cases
    plus 10 expected-failure checks. Numerical/status disagreements are
    reported and make ``qualified`` false. Missing, malformed, changed or
    mismatched oracle files fail before output publication.
    """

    reference_path = DEFAULT_REFERENCE_ORACLE if reference_json is None else Path(reference_json)
    target = Path(output_dir)
    reference_document, reference_digest, reference_identity = _load_reference(reference_path)
    cases, failure_cases = _validate_reference(reference_document)

    rows = [_run_numerical_case(case) for case in cases]
    rows.extend(_run_failure_case(case) for case in failure_cases)
    numerical_summary = _aggregate_case_rows(rows, kind="numerical_reference")
    failure_summary = _aggregate_case_rows(rows, kind="expected_failure_check")
    qualified = numerical_summary["all_passed"] and failure_summary["all_passed"]

    report = {
        "schema_version": 1,
        "profile": PROFILE,
        "qualified": qualified,
        "qualification_status": "qualified" if qualified else "disagreement",
        "inputs": {
            "reference_oracle": {
                "name": reference_path.name,
                "sha256": reference_digest,
                "size_bytes": reference_identity["size_bytes"],
                "profile": reference_document["profile"],
                "schema_version": reference_document["schema_version"],
                "numerical_case_count": len(cases),
                "expected_failure_check_count": len(failure_cases),
                "arithmetic_engine": reference_document["arithmetic"]["engine"],
                "decimal_precision_digits": reference_document["arithmetic"][
                    "decimal_precision_digits"
                ],
            }
        },
        "implementation": _implementation_provenance(),
        "numerical_comparison": numerical_summary,
        "range_and_input_failure_checks": failure_summary,
        "scope": {
            "ideal_gaussian_centroid_arithmetic_qualified": qualified,
            "physical_peak_widths_qualified": False,
            "plate_height_qualified": False,
            "chromatographic_signal_qualified": False,
            "instrument_or_acquisition_qualified": False,
            "chemical_identity_established": False,
            "retention_model_assignment_known": False,
            "fresh_fit_performed": False,
            "fresh_optimizer_run": False,
            "fresh_bo_readiness": False,
            "synthetic_widths_are_supplied_inputs": True,
            "fwhm_per_sigma_exact_constant": False,
            "not_established": [
                "physical peak widths or plate height",
                "chromatographic signals, response, noise or deconvolution",
                "instrument or acquisition behavior",
                "chemical identity or physical retention-model assignment",
                "model fitting, fresh optimizer runs or Bayesian-optimization readiness",
            ],
        },
        "reconciliation": {
            "all_reviewed_numerical_case_ids_present": True,
            "all_expected_failure_check_ids_present": True,
            "complete_analyte_cohorts_compared": True,
            "pair_ids_statuses_and_null_semantics_compared": True,
            "numeric_and_status_disagreements_are_reported": True,
            "scientific_calculations_delegated_to_gaussian_kernel": True,
        },
        "numerical_case_results": [
            row for row in rows if row["case_kind"] == "numerical_reference"
        ],
        "failure_check_results": [
            row for row in rows if row["case_kind"] == "expected_failure_check"
        ],
    }

    with staged_bundle(target, input_dirs=[reference_path.parent]) as stage:
        with (stage / "cases.csv").open("w", newline="", encoding="utf-8") as output:
            _write_cases_csv(output, rows)
        (stage / "report.json").write_text(
            json.dumps(report, allow_nan=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (stage / "report.md").write_text(_render_report(report), encoding="utf-8")
        _verify_input_unchanged(reference_path, reference_identity, reference_digest)
    return report


def _write_cases_csv(output, rows: list[dict[str, Any]]) -> None:
    fields = [
        "case_id",
        "input_sha256",
        "case_kind",
        "case_passed",
        "expected_analyte_count",
        "observed_analyte_count",
        "expected_pair_count",
        "observed_pair_count",
        "analyte_ids_match",
        "analyte_id_element_count",
        "analyte_id_elements_match",
        "pair_ids_match",
        "pair_id_element_count",
        "pair_id_elements_match",
        "status_field_count",
        "status_field_match_count",
        "statuses_match",
        "null_semantics_match",
        "null_semantics_check_count",
        "null_semantics_match_count",
        "numeric_comparison_count",
        "numeric_fields_evaluated",
        "numeric_match_count",
        "numeric_mismatch_count",
        "numeric_unmatched_count",
        "maximum_absolute_error",
        "maximum_relative_error",
        "failure_category",
        "explicit_failure_observed",
        "observed_exception_type",
        "returned_any_result",
        "issue_count",
        "calculation_error_type",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        item = dict(row)
        item["issue_count"] = json.dumps(item.get("issue_count", {}), sort_keys=True)
        writer.writerow(item)


__all__ = ["qualify_gaussian_producer"]
