"""Qualify the retention kernel against independent, integrity-bound oracles.

This adapter reads safe compound-set JSON members from the verified MoSeS ZIP,
then delegates every retention calculation to ``measure_methods.retention``.
It does not import upstream model code, load pickle files, fit parameters, or
infer physical or instrument properties.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import stat
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import measure_methods.retention as retention_kernel
from measure_methods.retention import RetentionParameters, predict_retention_time, retention_factor
from measure_service import retention as retention_service
from measure_service.benchmarks.evidence import file_sha256, staged_bundle
from measure_service.benchmarks.moses_archive import inspect_archive, read_member
from measure_service.method_backend import method_backend_info
from measure_service.moses_score_qualification import _load_json

PROFILE = "moses-retention-input-qualification/1"
DEFAULT_LAW_ORACLE = Path(__file__).with_name("reference_data") / "retention-law-upstream-v1.json"
DEFAULT_TRAJECTORY_ORACLE = (
    Path(__file__).with_name("reference_data") / "retention-ideal-binary64-v3.json"
)
EXPECTED_SOURCE_ARCHIVE_SHA256 = "880533ded926f766ce9504d4f164e9c5195b775e40061c18d70ae79497f03f71"
EXPECTED_SOURCE_IMPLEMENTATION = {
    "revision": "5818c698b61652bbe6e718881ca99c04a3d497bd",
    "source_sha256": "9c2f545fcc4668a521d4a59a696bb18bce1c6f8c517bda45defc72cd19801ea0",
    "logical_path": "src/moses/simulator/retention_models.py",
}
EXPECTED_LAW_ORACLE_SHA256 = "92a43a7fbfa148d8d25d99506c9202ca4c3ad435c1c233cc70cb51a18167eb30"
EXPECTED_TRAJECTORY_ORACLE_SHA256 = (
    "7ecd483f1a0ca5e2ee75e7a4f19940e7288a8199b164d90d58202d2b2770d02d"
)
LAW_PROFILE = "moses-retention-law-upstream-reference/1"
TRAJECTORY_ORACLE_NAME = "moses_ideal_continuous_retention_independent_oracle"
TRAJECTORY_ORACLE_SCHEMA_VERSION = 3
LAW_NAMES = ("linear", "quadratic", "neue-kuss")
COMPOSITION_GRID = tuple(index / 20 for index in range(21))
_COMPOUND_MEMBER = re.compile(r"^results/compound_sets/\d+x\d+C_20min_\d+/compound_set_\d+\.json$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_LAW_ORACLE_BYTES = 16 * 1024 * 1024
_MAX_TRAJECTORY_ORACLE_BYTES = 8 * 1024 * 1024
_MAX_COMPOUND_MEMBER_BYTES = 64 * 1024 * 1024
_LAW_RELATIVE_TOLERANCE = 1e-12
_RETENTION_ABSOLUTE_TOLERANCE_MIN = 1e-8
_RETENTION_RELATIVE_TOLERANCE = 1e-8
_PREDICTOR_TOLERANCE_MIN = 1e-9
_PREDICTOR_MAX_EVALUATIONS = 100_000
_EXPECTED_COMPOUND_SET_MEMBER_COUNT = 50
_EXPECTED_ANALYTE_COUNT = 10
_EXPECTED_RETENTION_CASE_IDS = frozenset(
    {
        "isocratic_linear_known_t0_1plus_k",
        "linear_ramp_closed_form_crosscheck",
        "piecewise_gradient_quadratic",
        "piecewise_gradient_neue_kuss",
        "quadratic_exact_phi_zero",
        "neue_kuss_exact_phi_one",
        "weak_retention_early_elution",
        "strong_retention_elutes_within_window",
        "right_censored_strong_retention",
        "decimal_intent_endpoint_rounds_to_right_censor",
        "binary_exact_inclusive_acquisition_endpoint",
        "small_geometry_same_dead_time",
        "large_geometry_same_dead_time",
        "post_transport_base",
        "post_transport_volume_only_shift",
        "neue_kuss_s2_zero_linear_limit",
        "neue_kuss_positive_parameters_nonmonotone_k",
        "right_censored_before_dead_time",
    }
)
_ORACLE_FLOAT_FIELDS = (
    "column_dead_time_min",
    "acquisition_duration_min",
    "integration_horizon_zeta_min",
    "integral_at_detector_horizon_min",
    "column_fraction_at_detector_horizon",
    "post_transport_delay_min",
    "retention_time_min",
    "zeta_elution_min",
    "root_residual_min",
)
_ORACLE_NULLABLE_FLOAT_FIELDS = {
    "retention_time_min",
    "zeta_elution_min",
    "root_residual_min",
}


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant is not allowed: {value}")


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _require_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA256 digest")
    return value


def _decimal_float(value: Any, name: str) -> float:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a decimal string")
    try:
        decimal_value = Decimal(value)
        result = float(decimal_value)
    except (InvalidOperation, OverflowError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal string") from error
    if not decimal_value.is_finite() or not math.isfinite(result):
        raise ValueError(f"{name} must be a finite decimal string")
    return result


def _json_number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite JSON number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(
            f"{name} must be finite and positive" if positive else f"{name} must be finite"
        )
    return result


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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _regular_file_identity(path: Path, name: str) -> dict[str, int | str]:
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


def _read_bounded_oracle(
    path: Path, *, maximum_bytes: int, name: str
) -> tuple[Any, dict[str, Any], dict[str, int | str]]:
    identity = _regular_file_identity(path, name)
    if identity["size_bytes"] > maximum_bytes:
        raise ValueError(f"{name} exceeds the {maximum_bytes}-byte limit")
    document = _load_json(path, maximum_bytes=maximum_bytes)
    digest = file_sha256(path)
    return document, {"sha256": digest, "size_bytes": identity["size_bytes"]}, identity


def _archive_identity(path: Path) -> tuple[dict[str, Any], dict[str, int | str]]:
    before = _regular_file_identity(path, "source archive")
    manifest = inspect_archive(path, verify=True)
    source = _require_object(manifest.get("source"), "archive source manifest")
    observed = _require_object(source.get("observed"), "archive observed identity")
    sha256 = _require_sha256(observed.get("sha256"), "archive SHA256")
    size_bytes = observed.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        raise ValueError("archive observed size must be an integer")
    if sha256 != EXPECTED_SOURCE_ARCHIVE_SHA256:
        raise ValueError("source archive SHA256 does not match the qualified source")
    if source.get("matches_expected") is not True:
        raise ValueError("source archive does not match its published archive identity")
    return manifest, {
        "sha256": sha256,
        "size_bytes": size_bytes,
        "file_identity": before,
    }


def _compound_member_names(archive_manifest: dict[str, Any]) -> list[str]:
    archive = _require_object(archive_manifest.get("archive"), "archive manifest")
    members = archive.get("members")
    if not isinstance(members, list):
        raise ValueError("archive manifest members must be a list")
    names = []
    for entry in members:
        entry = _require_object(entry, "archive member entry")
        name = entry.get("name")
        if not isinstance(name, str):
            raise ValueError("archive member name must be text")
        if name.startswith("results/compound_sets/") and name.endswith(".json"):
            if _COMPOUND_MEMBER.fullmatch(name) is None:
                raise ValueError("source archive contains an unexpected compound-set JSON path")
            names.append(name)
    if len(names) != _EXPECTED_COMPOUND_SET_MEMBER_COUNT or len(set(names)) != len(names):
        raise ValueError("source archive compound-set JSON member count or identity is invalid")
    return sorted(names)


def _parse_compound_member(payload: bytes, name: str) -> list[dict[str, Any]]:
    try:
        rows = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"compound-set member is not valid strict JSON: {name}") from error
    if not isinstance(rows, list) or len(rows) != _EXPECTED_ANALYTE_COUNT:
        raise ValueError(
            f"compound-set member must contain {_EXPECTED_ANALYTE_COUNT} analytes: {name}"
        )
    for index, row in enumerate(rows):
        row = _require_object(row, f"compound row {index}")
        if set(row) != {"log_k0", "S_1", "S_2", "area"}:
            raise ValueError(f"compound row fields are incomplete or unexpected: {name}")
        for field in ("log_k0", "S_1", "S_2", "area"):
            _json_number(row[field], f"{name}[{index}].{field}")
        if row["area"] <= 0:
            raise ValueError(f"compound area must be positive: {name}")
    return rows


def _validate_law_oracle(
    document: Any,
    *,
    archive_sha256: str,
    archive_size_bytes: int,
    archive_member_names: list[str],
) -> list[dict[str, Any]]:
    document = _require_object(document, "law oracle")
    required_top = {
        "schema_version",
        "profile",
        "scope",
        "source_archive",
        "source_implementation",
        "arithmetic",
        "composition_grid",
        "members",
    }
    if set(document) != required_top:
        raise ValueError("law oracle has missing or unexpected top-level fields")
    if document.get("schema_version") != 1 or document.get("profile") != LAW_PROFILE:
        raise ValueError("unsupported law-oracle schema or profile")
    if not isinstance(document.get("scope"), str) or not document["scope"].strip():
        raise ValueError("law oracle scope must be nonblank text")

    source_archive = _require_object(document.get("source_archive"), "law oracle source archive")
    if set(source_archive) != {"sha256", "size_bytes"}:
        raise ValueError("law oracle source archive identity has unexpected fields")
    if (
        source_archive.get("sha256") != archive_sha256
        or source_archive.get("size_bytes") != archive_size_bytes
    ):
        raise ValueError("law oracle is bound to a different source archive")

    source_implementation = _require_object(
        document.get("source_implementation"), "law oracle source implementation"
    )
    if source_implementation != EXPECTED_SOURCE_IMPLEMENTATION:
        raise ValueError("law oracle source implementation provenance is unsupported")
    arithmetic = _require_object(document.get("arithmetic"), "law oracle arithmetic")
    if arithmetic.get("dtype") != "torch.float64":
        raise ValueError("law oracle must contain upstream torch.float64 values")

    grid = document.get("composition_grid")
    if not isinstance(grid, list) or len(grid) != len(COMPOSITION_GRID):
        raise ValueError("law oracle composition grid must contain 21 coordinates")
    parsed_grid = [_json_number(value, "composition grid coordinate") for value in grid]
    if parsed_grid != list(COMPOSITION_GRID):
        raise ValueError("law oracle composition grid differs from the required 0..1 grid")

    members = document.get("members")
    if not isinstance(members, list) or len(members) != _EXPECTED_COMPOUND_SET_MEMBER_COUNT:
        raise ValueError("law oracle must include every expected compound-set member")
    oracle_names = []
    for member in members:
        member = _require_object(member, "law oracle member")
        if set(member) != {"name", "sha256", "analytes"}:
            raise ValueError("law oracle member has missing or unexpected fields")
        name = member.get("name")
        if not isinstance(name, str) or _COMPOUND_MEMBER.fullmatch(name) is None:
            raise ValueError("law oracle contains an unsafe or unsupported member name")
        oracle_names.append(name)
        _require_sha256(member.get("sha256"), f"law oracle member SHA256 for {name}")
        analytes = member.get("analytes")
        if not isinstance(analytes, list) or len(analytes) != _EXPECTED_ANALYTE_COUNT:
            raise ValueError(f"law oracle member must contain {_EXPECTED_ANALYTE_COUNT} analytes")
        indices = []
        for analyte in analytes:
            analyte = _require_object(analyte, "law oracle analyte")
            if set(analyte) != {"analyte_index", "parameter_sha256", "reference_values"}:
                raise ValueError("law oracle analyte has missing or unexpected fields")
            index = analyte.get("analyte_index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise ValueError("law oracle analyte index must be an integer")
            indices.append(index)
            _require_sha256(analyte.get("parameter_sha256"), "parameter SHA256")
            reference_values = _require_object(analyte.get("reference_values"), "reference values")
            if set(reference_values) != set(LAW_NAMES):
                raise ValueError("law oracle must include all three declared retention laws")
            for law in LAW_NAMES:
                values = reference_values[law]
                if not isinstance(values, list) or len(values) != len(COMPOSITION_GRID):
                    raise ValueError(f"law oracle {law} values must match the composition grid")
                for position, value in enumerate(values):
                    _json_number(value, f"law oracle {law}[{position}]", positive=True)
        if sorted(indices) != list(range(_EXPECTED_ANALYTE_COUNT)):
            raise ValueError("law oracle analyte indices must be unique and complete")
    if len(set(oracle_names)) != len(oracle_names):
        raise ValueError("law oracle contains duplicate source members")
    if sorted(oracle_names) != archive_member_names:
        raise ValueError("law oracle member set does not match the source archive")
    return members


def _validate_retention_case(case: Any) -> dict[str, Any]:
    case = _require_object(case, "retention oracle case")
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("retention oracle case ID must be nonblank text")
    inputs = _require_object(case.get("input"), f"{case_id} input")
    expected = _require_object(case.get("expected"), f"{case_id} expected result")
    parameter_input = _require_object(inputs.get("parameters"), f"{case_id} parameters")
    model = parameter_input.get("model")
    if model not in LAW_NAMES:
        raise ValueError(f"{case_id} retention model is unsupported")
    ln_k0 = _decimal_float(parameter_input.get("ln_k0"), f"{case_id}.ln_k0")
    s1 = _decimal_float(parameter_input.get("S1"), f"{case_id}.S1")
    s2_value = parameter_input.get("S2")
    if model == "linear":
        if s2_value is not None:
            raise ValueError(f"{case_id} linear model must omit S2")
        s2 = None
    else:
        s2 = _decimal_float(s2_value, f"{case_id}.S2")
    parameters = RetentionParameters(model=model, ln_k0=ln_k0, S1=s1, S2=s2)

    binary64_evidence = _require_object(
        case.get("binary64_input_evidence"), f"{case_id} binary64 input evidence"
    )
    evidence_keys = {
        "parameters",
        "gradient_times_min",
        "gradient_fractions",
        "profile_arguments",
    }
    if set(binary64_evidence) != evidence_keys:
        raise ValueError(f"{case_id} binary64 input evidence has missing or unexpected fields")

    def check_binary64_value(evidence: Any, source_decimal: Any, name: str) -> None:
        evidence = _require_object(evidence, f"{case_id} {name} binary64 evidence")
        if set(evidence) != {"source_decimal", "binary64_hex", "exact_binary64_rational"}:
            raise ValueError(f"{case_id} {name} binary64 evidence is incomplete")
        if evidence.get("source_decimal") != source_decimal:
            raise ValueError(f"{case_id} {name} binary64 evidence changed its source decimal")
        rounded = _decimal_float(source_decimal, f"{case_id}.{name}")
        if evidence.get("binary64_hex") != rounded.hex():
            raise ValueError(
                f"{case_id} {name} binary64 hexadecimal value does not match its input"
            )
        numerator, denominator = rounded.as_integer_ratio()
        if evidence.get("exact_binary64_rational") != [str(numerator), str(denominator)]:
            raise ValueError(f"{case_id} {name} exact binary64 rational does not match its input")

    parameter_evidence = _require_object(
        binary64_evidence.get("parameters"), f"{case_id} parameter binary64 evidence"
    )
    if set(parameter_evidence) != {"ln_k0", "S1", "S2"}:
        raise ValueError(f"{case_id} parameter binary64 evidence is incomplete")
    for name in ("ln_k0", "S1"):
        check_binary64_value(parameter_evidence[name], parameter_input[name], f"parameters.{name}")
    if s2_value is None:
        if parameter_evidence["S2"] is not None:
            raise ValueError(f"{case_id} linear S2 binary64 evidence must be null")
    else:
        check_binary64_value(parameter_evidence["S2"], s2_value, "parameters.S2")

    times_input = inputs.get("gradient_times_min")
    fractions_input = inputs.get("gradient_fractions")
    if not isinstance(times_input, list) or not isinstance(fractions_input, list):
        raise ValueError(f"{case_id} gradient coordinates must be lists")
    if len(times_input) < 2 or len(times_input) != len(fractions_input) or len(times_input) > 128:
        raise ValueError(f"{case_id} gradient coordinates have invalid lengths")
    times = [_decimal_float(value, f"{case_id}.gradient_times_min") for value in times_input]
    fractions = [
        _decimal_float(value, f"{case_id}.gradient_fractions") for value in fractions_input
    ]
    duration = _decimal_float(
        inputs.get("acquisition_duration_min"), f"{case_id}.acquisition_duration_min"
    )
    flow = _decimal_float(inputs.get("flow_rate_ml_min"), f"{case_id}.flow_rate_ml_min")
    length = _decimal_float(inputs.get("column_length_m"), f"{case_id}.column_length_m")
    area = _decimal_float(inputs.get("effective_area_m2"), f"{case_id}.effective_area_m2")
    post_volume = _decimal_float(
        inputs.get("post_column_volume_ml"), f"{case_id}.post_column_volume_ml"
    )
    if duration <= 0 or flow <= 0 or length <= 0 or area <= 0 or post_volume < 0:
        raise ValueError(f"{case_id} geometry or duration is outside the supported domain")
    if times[0] != 0 or times[-1] != duration:
        raise ValueError(f"{case_id} gradient must cover the declared acquisition duration")
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError(f"{case_id} gradient times must be strictly increasing")
    if any(not 0 <= value <= 1 for value in fractions):
        raise ValueError(f"{case_id} gradient fractions must lie in [0, 1]")
    for field, source_values in (
        ("gradient_times_min", times_input),
        ("gradient_fractions", fractions_input),
    ):
        evidence_values = binary64_evidence.get(field)
        if not isinstance(evidence_values, list) or len(evidence_values) != len(source_values):
            raise ValueError(f"{case_id} {field} binary64 evidence is incomplete")
        for index, (evidence, source_value) in enumerate(
            zip(evidence_values, source_values, strict=True)
        ):
            check_binary64_value(evidence, source_value, f"{field}[{index}]")

    profile_arguments = _require_object(
        binary64_evidence.get("profile_arguments"), f"{case_id} profile binary64 evidence"
    )
    profile_fields = {
        "flow_rate_ml_min",
        "column_length_m",
        "effective_area_m2",
        "acquisition_duration_min",
        "post_column_volume_ml",
    }
    if set(profile_arguments) != profile_fields:
        raise ValueError(f"{case_id} profile binary64 evidence is incomplete")
    for field in sorted(profile_fields):
        check_binary64_value(profile_arguments[field], inputs[field], field)

    required_expected = {
        "status",
        "retention_time_min",
        "zeta_elution_min",
        "column_dead_time_min",
        "acquisition_duration_min",
        "integration_horizon_zeta_min",
        "integral_at_detector_horizon_min",
        "column_fraction_at_detector_horizon",
        "post_transport_delay_min",
        "retention_model",
    }
    if not required_expected <= set(expected):
        raise ValueError(f"{case_id} expected result is missing required fields")
    status = expected.get("status")
    if status not in {"eluted_within_duration", "eluted_at_endpoint", "right_censored"}:
        raise ValueError(f"{case_id} expected status is unsupported")
    if expected.get("retention_model") != model:
        raise ValueError(f"{case_id} expected model does not match the declared inputs")
    expected_duration_float = _decimal_float(
        expected.get("acquisition_duration_min"), f"{case_id}.expected acquisition_duration_min"
    )
    if expected_duration_float != duration:
        raise ValueError(f"{case_id} expected duration does not match its binary64 input")
    parsed_expected: dict[str, float | None] = {}
    for field in _ORACLE_FLOAT_FIELDS:
        if field not in expected:
            if field in _ORACLE_NULLABLE_FLOAT_FIELDS and status == "right_censored":
                parsed_expected[field] = None
                continue
            if field == "root_residual_min" and status != "right_censored":
                # The oracle root residual is provenance for its own solver, not
                # a value exposed by the service kernel and is not compared.
                continue
            raise ValueError(f"{case_id} expected result is missing {field}")
        value = expected[field]
        if value is None:
            if field not in _ORACLE_NULLABLE_FLOAT_FIELDS or status != "right_censored":
                raise ValueError(f"{case_id} has an unexpected null numeric result")
            parsed_expected[field] = None
        else:
            parsed_expected[field] = _decimal_float(value, f"{case_id}.{field}")

    if status == "right_censored":
        if (
            parsed_expected.get("retention_time_min") is not None
            or parsed_expected.get("zeta_elution_min") is not None
        ):
            raise ValueError(f"{case_id} censored result must not invent an event time")
    else:
        if (
            parsed_expected.get("retention_time_min") is None
            or parsed_expected.get("zeta_elution_min") is None
        ):
            raise ValueError(f"{case_id} eluted result must include both event times")
        expected_time_float = _decimal_float(
            expected["retention_time_min"], f"{case_id}.expected retention_time_min"
        )
        if status == "eluted_at_endpoint" and expected_time_float != duration:
            raise ValueError(f"{case_id} endpoint detector time must equal acquisition duration")
        if status == "eluted_within_duration" and expected_time_float >= duration:
            raise ValueError(
                f"{case_id} within-duration detector time must precede acquisition end"
            )

    if not isinstance(case.get("oracle_checks"), dict) or not all(
        value is True for value in case["oracle_checks"].values()
    ):
        raise ValueError(f"{case_id} independent oracle checks did not all pass")
    canonical_input = _canonical_json_bytes(inputs)
    return {
        "case_id": case_id,
        "input_sha256": _sha256_bytes(canonical_input),
        "parameters": parameters,
        "gradient_times_min": times,
        "gradient_fractions": fractions,
        "flow_rate_ml_min": flow,
        "column_length_m": length,
        "effective_area_m2": area,
        "acquisition_duration_min": duration,
        "post_column_volume_ml": post_volume,
        "expected_status": status,
        "expected": parsed_expected,
    }


def _validate_trajectory_oracle(document: Any) -> list[dict[str, Any]]:
    document = _require_object(document, "trajectory oracle")
    if (
        document.get("schema_version") != TRAJECTORY_ORACLE_SCHEMA_VERSION
        or document.get("oracle_name") != TRAJECTORY_ORACLE_NAME
    ):
        raise ValueError("unsupported trajectory-oracle schema or profile")
    if document.get("all_synthetic_oracle_checks_passed") is not True:
        raise ValueError("trajectory oracle reports failed synthetic checks")
    case_count = document.get("case_count")
    cases = document.get("cases")
    if (
        isinstance(case_count, bool)
        or not isinstance(case_count, int)
        or not isinstance(cases, list)
    ):
        raise ValueError("trajectory oracle case count or cases list is invalid")
    if case_count != len(cases) or case_count != len(_EXPECTED_RETENTION_CASE_IDS):
        raise ValueError("trajectory oracle case count is incomplete")
    arithmetic = _require_object(document.get("arithmetic"), "trajectory oracle arithmetic")
    precision = arithmetic.get("decimal_precision_digits")
    if isinstance(precision, bool) or not isinstance(precision, int) or precision < 70:
        raise ValueError("trajectory oracle must contain at least 70-digit arithmetic")
    if "binary64" not in arithmetic.get("input_interpretation", ""):
        raise ValueError("trajectory oracle must declare binary64 input interpretation")
    if "float.hex()" not in arithmetic.get("binary64_conversion_evidence", ""):
        raise ValueError("trajectory oracle is missing binary64 conversion evidence")
    input_contract = _require_object(document.get("input_contract"), "trajectory oracle contract")
    if "T - column_dead_time" not in input_contract.get("detector_horizon_rule", ""):
        raise ValueError("trajectory oracle does not use the verified detector-time horizon")
    if "inclusive endpoint" not in input_contract.get("endpoint_rule", ""):
        raise ValueError("trajectory oracle does not declare inclusive detector endpoints")
    if input_contract.get("float64_comparison_fields") is None:
        raise ValueError("trajectory oracle is missing its float64 comparison field declaration")

    validated = [_validate_retention_case(case) for case in cases]
    case_ids = [case["case_id"] for case in validated]
    if len(set(case_ids)) != len(case_ids) or set(case_ids) != set(_EXPECTED_RETENTION_CASE_IDS):
        raise ValueError("trajectory oracle case IDs are duplicate, missing, or unexpected")
    return sorted(validated, key=lambda item: item["case_id"])


def _source_rows(
    archive_path: Path,
    archive_member_names: list[str],
    law_members: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    law_by_name = {item["name"]: item for item in law_members}
    if set(law_by_name) != set(archive_member_names):
        raise ValueError("law oracle members do not cover the verified source archive")
    rows = []
    member_digests = []
    for name in archive_member_names:
        payload = read_member(archive_path, name, max_bytes=_MAX_COMPOUND_MEMBER_BYTES)
        observed_digest = _sha256_bytes(payload)
        reference_member = law_by_name[name]
        if observed_digest != reference_member["sha256"]:
            raise ValueError(f"law oracle source member digest does not match {name}")
        compounds = _parse_compound_member(payload, name)
        if len(reference_member["analytes"]) != len(compounds):
            raise ValueError(f"law oracle analyte count differs from {name}")
        reference_by_index = {
            entry["analyte_index"]: entry for entry in reference_member["analytes"]
        }
        for index, compound in enumerate(compounds):
            if index not in reference_by_index:
                raise ValueError(f"law oracle is missing analyte {index} for {name}")
            parameter_object = {
                "log_k0": compound["log_k0"],
                "S_1": compound["S_1"],
                "S_2": compound["S_2"],
            }
            parameter_digest = _sha256_bytes(_canonical_json_bytes(parameter_object))
            reference = reference_by_index[index]
            if parameter_digest != reference["parameter_sha256"]:
                raise ValueError(
                    f"law oracle parameter binding does not match {name} analyte {index}"
                )
            rows.append(
                {
                    "source_member": name,
                    "source_member_sha256": observed_digest,
                    "analyte_index": index,
                    "parameter_sha256": parameter_digest,
                    "parameter_values": parameter_object,
                    "reference_values": reference["reference_values"],
                }
            )
        member_digests.append(
            {"name": name, "sha256": observed_digest, "analyte_count": len(compounds)}
        )
    return rows, member_digests


def _fit_range_status(source_values: dict[str, float], law: str, index: int) -> bool:
    analyte = retention_service.AnalyteRetentionParameters(
        analyte_id=f"archive-{index}",
        model=law,
        ln_k0=source_values["log_k0"],
        S1=source_values["S_1"],
        S2=None if law == "linear" else source_values["S_2"],
    )
    return not retention_service._fit_range_warnings(analyte)


def _relative_error(absolute_error: float, reference: float) -> float | None:
    if not math.isfinite(absolute_error) or not math.isfinite(reference) or reference == 0:
        return None
    ratio = absolute_error / abs(reference)
    return ratio if math.isfinite(ratio) else None


def _law_comparisons(
    rows: list[dict[str, Any]], grid: tuple[float, ...]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output_rows = []
    summaries = {}
    for law in LAW_NAMES:
        evaluation_count = 0
        matching_count = 0
        parameter_domain_count = 0
        domain_record_count = 0
        fit_compatible_count = 0
        max_absolute_error = None
        max_relative_error = None
        invalid_evaluation_count = 0
        for row in rows:
            values = row["parameter_values"]
            reference_values = row["reference_values"][law]
            parameter_model = RetentionParameters(
                model=law,
                ln_k0=values["log_k0"],
                S1=values["S_1"],
                S2=None if law == "linear" else values["S_2"],
            )
            parameter_domain_count += 1
            fit_compatible = _fit_range_status(values, law, row["analyte_index"])
            fit_compatible_count += int(fit_compatible)
            absolute_errors = []
            relative_errors = []
            failures = 0
            for composition, expected in zip(grid, reference_values, strict=True):
                evaluation_count += 1
                try:
                    observed = _json_number(
                        retention_factor(parameters=parameter_model, composition=composition),
                        "native retention factor",
                        positive=True,
                    )
                    absolute_error = abs(observed - expected)
                    relative_error = _relative_error(absolute_error, expected)
                    if relative_error is None:
                        raise ArithmeticError("relative comparison is outside the numeric range")
                except (ArithmeticError, RuntimeError, TypeError, ValueError):
                    failures += 1
                    invalid_evaluation_count += 1
                    continue
                absolute_errors.append(absolute_error)
                relative_errors.append(relative_error)
                max_absolute_error = (
                    absolute_error
                    if max_absolute_error is None
                    else max(max_absolute_error, absolute_error)
                )
                max_relative_error = (
                    relative_error
                    if max_relative_error is None
                    else max(max_relative_error, relative_error)
                )
            domain_valid = failures == 0
            domain_record_count += int(domain_valid)
            within_tolerance = (
                domain_valid
                and len(absolute_errors) == len(grid)
                and all(
                    error <= _LAW_RELATIVE_TOLERANCE * abs(reference)
                    for error, reference in zip(absolute_errors, reference_values, strict=True)
                )
            )
            matching_count += int(within_tolerance)
            reference_digest = _sha256_bytes(_canonical_json_bytes(reference_values))
            output_rows.append(
                {
                    "source_member": row["source_member"],
                    "source_member_sha256": row["source_member_sha256"],
                    "analyte_index": row["analyte_index"],
                    "parameter_sha256": row["parameter_sha256"],
                    "law_reference_sha256": reference_digest,
                    "law": law,
                    "composition_count": len(grid),
                    "finite_positive_kernel_values": len(absolute_errors),
                    "kernel_evaluation_failures": failures,
                    "source_fit_range_compatible": fit_compatible,
                    "max_absolute_error": max(absolute_errors, default=None),
                    "max_relative_error": max(relative_errors, default=None),
                    "relative_tolerance": _LAW_RELATIVE_TOLERANCE,
                    "comparison_passed": within_tolerance,
                }
            )
        record_count = len(rows)
        summaries[law] = {
            "record_count": record_count,
            "records_mathematically_in_domain": parameter_domain_count,
            "mathematical_domain_basis": "RetentionParameters accepted finite coefficients and the selected model's parameter domain; this is not physical validation.",
            "reference_values_compared": evaluation_count,
            "records_with_finite_positive_kernel_values_on_grid": domain_record_count,
            "records_with_all_values_within_relative_tolerance": matching_count,
            "kernel_evaluation_failures": invalid_evaluation_count,
            "source_fit_range_compatible_records": fit_compatible_count,
            "source_fit_range_outside_records": record_count - fit_compatible_count,
            "relative_tolerance": _LAW_RELATIVE_TOLERANCE,
            "maximum_absolute_error": max_absolute_error,
            "maximum_relative_error": max_relative_error,
            "qualified": record_count == matching_count and invalid_evaluation_count == 0,
        }
    return output_rows, summaries


def _numeric_error(observed: float, expected: float) -> tuple[float, float, bool]:
    absolute_error = abs(observed - expected)
    relative_error = _relative_error(absolute_error, expected)
    tolerance = _RETENTION_ABSOLUTE_TOLERANCE_MIN + _RETENTION_RELATIVE_TOLERANCE * abs(expected)
    return absolute_error, relative_error, absolute_error <= tolerance


def _compare_nullable(
    observed: Any, expected: float | None
) -> tuple[float | None, float | None, bool]:
    if expected is None:
        return None, None, observed is None
    if isinstance(observed, bool) or not isinstance(observed, (float, int)):
        return None, None, False
    numeric = float(observed)
    if not math.isfinite(numeric):
        return None, None, False
    absolute_error, relative_error, passed = _numeric_error(numeric, expected)
    return absolute_error, relative_error, passed


def _retention_case_comparisons(
    cases: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output_rows = []
    status_counts: Counter[str] = Counter()
    pass_count = 0
    for case in cases:
        expected_status = case["expected_status"]
        case_input = {
            "parameters": case["parameters"],
            "gradient_times_min": case["gradient_times_min"],
            "gradient_fractions": case["gradient_fractions"],
            "flow_rate_ml_min": case["flow_rate_ml_min"],
            "column_length_m": case["column_length_m"],
            "effective_area_m2": case["effective_area_m2"],
            "acquisition_duration_min": case["acquisition_duration_min"],
            "post_column_volume_ml": case["post_column_volume_ml"],
        }
        expected = case["expected"]
        try:
            observed = predict_retention_time(
                **case_input,
                absolute_tolerance_min=_PREDICTOR_TOLERANCE_MIN,
                max_evaluations=_PREDICTOR_MAX_EVALUATIONS,
            )
            observed_status = observed["status"]
            error_type = None
        except (ArithmeticError, RuntimeError, TypeError, ValueError) as error:
            observed = {}
            observed_status = "calculation_error"
            error_type = type(error).__name__

        expected_native_status = "not_eluted" if expected_status == "right_censored" else "eluted"
        status_match = observed_status == expected_native_status
        if observed_status == "eluted":
            observed_time = observed.get("retention_time_min")
            observed_zeta = observed.get("retention_zeta_min")
            if expected_status == "eluted_at_endpoint":
                status_match = status_match and observed_time == case["acquisition_duration_min"]
            elif expected_status == "eluted_within_duration":
                status_match = (
                    status_match
                    and isinstance(observed_time, (float, int))
                    and observed_time < case["acquisition_duration_min"]
                )
        else:
            observed_time = observed.get("retention_time_min")
            observed_zeta = observed.get("retention_zeta_min")
        if expected_status == "right_censored":
            status_match = (
                status_match
                and observed_time is None
                and observed_zeta is None
                and observed.get("right_censor_bound_min") == case["acquisition_duration_min"]
            )

        time_abs, time_rel, time_pass = _compare_nullable(
            observed_time, expected.get("retention_time_min")
        )
        zeta_abs, zeta_rel, zeta_pass = _compare_nullable(
            observed_zeta, expected.get("zeta_elution_min")
        )
        dead_abs, dead_rel, dead_pass = _compare_nullable(
            observed.get("dead_time_min"), expected.get("column_dead_time_min")
        )
        post_abs, post_rel, post_pass = _compare_nullable(
            observed.get("post_column_transit_min"), expected.get("post_transport_delay_min")
        )
        # The service kernel does not expose the independent quadrature's root
        # residual, detector-horizon integral, or traversed fraction. Those
        # fields remain independent-oracle diagnostics and are not reconstructed.
        case_pass = status_match and time_pass and zeta_pass and dead_pass and post_pass
        pass_count += int(case_pass)
        status_counts[f"{expected_status}:{observed_status}"] += 1
        output_rows.append(
            {
                "case_id": case["case_id"],
                "input_sha256": case["input_sha256"],
                "expected_status": expected_status,
                "observed_status": observed_status,
                "status_match": status_match,
                "retention_time_abs_error_min": time_abs,
                "retention_time_relative_error": time_rel,
                "retention_time_within_tolerance": time_pass,
                "retention_zeta_abs_error_min": zeta_abs,
                "retention_zeta_relative_error": zeta_rel,
                "retention_zeta_within_tolerance": zeta_pass,
                "dead_time_abs_error_min": dead_abs,
                "dead_time_relative_error": dead_rel,
                "dead_time_within_tolerance": dead_pass,
                "post_transport_abs_error_min": post_abs,
                "post_transport_relative_error": post_rel,
                "post_transport_within_tolerance": post_pass,
                "absolute_tolerance_min": _RETENTION_ABSOLUTE_TOLERANCE_MIN,
                "relative_tolerance": _RETENTION_RELATIVE_TOLERANCE,
                "root_residual_comparable": False,
                "case_passed": case_pass,
                "calculation_error_type": error_type,
            }
        )
    return output_rows, {
        "case_count": len(cases),
        "cases_with_all_status_and_numeric_comparisons_passing": pass_count,
        "status_pair_counts": dict(sorted(status_counts.items())),
        "absolute_tolerance_min": _RETENTION_ABSOLUTE_TOLERANCE_MIN,
        "relative_tolerance": _RETENTION_RELATIVE_TOLERANCE,
        "predictor_absolute_tolerance_min": _PREDICTOR_TOLERANCE_MIN,
        "predictor_max_evaluations": _PREDICTOR_MAX_EVALUATIONS,
        "qualified": pass_count == len(cases),
    }


def _implementation_provenance() -> dict[str, Any]:
    return {
        "measure_service": method_backend_info(),
        "service_adapter": {
            "module": "measure_service.benchmarks.retention_qualification",
            "source_sha256": file_sha256(Path(__file__)),
        },
        "retention_kernel": {
            "module": "measure_methods.retention",
            "source_sha256": file_sha256(Path(retention_kernel.__file__)),
        },
    }


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def _render_report(report: dict[str, Any]) -> str:
    laws = report["law_comparison"]
    cases = report["retention_case_comparison"]
    lines = [
        "# MoSeS retention input qualification",
        "",
        f"Profile: `{report['profile']}`. Numerical reference result: **{report['qualification_status']}**.",
        "",
        "This compares the packaged retention calculations with two fixed independent references: actual upstream float64 retention laws and a high-precision oracle evaluated on exact parsed binary64 inputs. Each archived parameter set is assessed conditionally under all three laws; its original model assignment remains unknown.",
        "",
        f"Verified source ZIP SHA256: `{report['source_archive']['sha256']}`.",
        f"Safe JSON members: {report['source_archive']['safe_compound_set_member_count']}; analytes: {report['source_archive']['compound_record_count']}; pointwise law comparisons: {laws['reference_value_count']}.",
        "",
        "## Pointwise source-law comparisons",
        "",
        "| Law | Mathematical domain | All grid values agree | Within source fit bounds | Maximum relative difference |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for law, value in laws["by_law"].items():
        total = value["record_count"]
        maximum = value["maximum_relative_error"]
        display = "unavailable" if maximum is None else f"{maximum:.4g}"
        lines.append(
            f"| {law} | {value['records_mathematically_in_domain']}/{total} | {value['records_with_all_values_within_relative_tolerance']}/{total} | {value['source_fit_range_compatible_records']}/{total} | {display} |"
        )
    lines.extend(
        [
            "",
            f"Law agreement uses relative tolerance {laws['relative_tolerance']:g} and no absolute tolerance that could conceal errors in very small retention factors. Source fitting ranges are advisory metadata; they do not establish physical validity or fit quality. Values are not clipped.",
            "",
            "## Continuous ideal-retention cases",
            "",
            f"{cases['cases_with_all_status_and_numeric_comparisons_passing']}/{cases['case_count']} cases pass all available status and numeric comparisons. The predictor requests absolute tolerance {cases['predictor_absolute_tolerance_min']:g} min. Comparison tolerances are explicitly {cases['absolute_tolerance_min']:g} min absolute plus {cases['relative_tolerance']:g} relative; per-field absolute differences are retained in [retention-cases.csv](retention-cases.csv).",
            "",
            "The detector acquisition horizon includes column dead time and post-column transit. Endpoint inclusion uses the parsed binary64 inputs; the fixture retains a decimal-rounding counterexample and a separate binary-exact endpoint. Solver tolerance does not widen the acquisition window. Numerical failures are recorded as calculation errors with unavailable values and failed qualification, rather than elution or censoring.",
            "",
            "## Scientific scope",
            "",
            "These results qualify the named numerical reference and its explicit input conventions. Physical parameter provenance, original model assignment, fitted plate-height units, peak widths, gradient/injection delays, signal processing, instruments and fit quality remain separate work. Fresh fitting and fresh optimization have not run; `fresh_bo_readiness` remains false.",
            "",
            "The continuous ideal reference differs from the source simulator's sampled grid and Gaussian mask. Its numerical error guards are estimates, not formal interval proofs or calibrated model uncertainty.",
            "",
            "The output contains aggregate counts, per-source comparison diagnostics and digests. It does not copy the authors' coefficient or reference-value arrays. [law-comparisons.csv](law-comparisons.csv) binds every row to exact source/member/parameter/reference identities. `complete.json` seals all core files; checksums provide integrity, not signatures or historical runtime provenance.",
            "",
        ]
    )
    return "\n".join(lines)


def _verify_inputs_unchanged(inputs: dict[str, tuple[Path, dict[str, int | str], str]]) -> None:
    for name, (path, initial_identity, initial_digest) in inputs.items():
        current_identity = _regular_file_identity(path, name)
        current_digest = file_sha256(path)
        if current_identity != initial_identity or current_digest != initial_digest:
            raise ValueError(f"{name} changed during retention qualification")


def qualify_retention_inputs(
    archive: Path,
    output_dir: Path,
    *,
    law_oracle_json: Path | None = None,
    trajectory_oracle_json: Path | None = None,
) -> dict[str, Any]:
    """Compare the native retention kernel with two independent references.

    The source ZIP must match the pinned public source archive. Its safe
    compound-set JSON members are joined to the upstream float64 law oracle
    through member and canonical parameter digests before any comparisons are
    made. The independent ideal-profile oracle is checked against the service
    predictor. Publication is atomic; existing output files are never replaced.

    Numeric or status disagreements are written explicitly and make
    ``qualified`` false. Malformed, incomplete, mismatched, or changed inputs
    fail before publication.
    """

    archive_path = Path(archive)
    law_path = DEFAULT_LAW_ORACLE if law_oracle_json is None else Path(law_oracle_json)
    trajectory_path = (
        DEFAULT_TRAJECTORY_ORACLE
        if trajectory_oracle_json is None
        else Path(trajectory_oracle_json)
    )
    target = Path(output_dir)

    # Verify the immutable source before reading and joining either oracle.
    archive_manifest, archive_identity = _archive_identity(archive_path)
    archive_member_names = _compound_member_names(archive_manifest)

    law_document, law_input, law_file_identity = _read_bounded_oracle(
        law_path,
        maximum_bytes=_MAX_LAW_ORACLE_BYTES,
        name="law oracle",
    )
    if law_input["sha256"] != EXPECTED_LAW_ORACLE_SHA256:
        raise ValueError("law oracle SHA256 does not match the reviewed reference")
    law_source_archive = _require_object(
        law_document.get("source_archive"), "law oracle source archive"
    )
    if law_source_archive.get("sha256") != archive_identity["sha256"]:
        raise ValueError("law oracle is not bound to the verified source archive")
    law_members = _validate_law_oracle(
        law_document,
        archive_sha256=archive_identity["sha256"],
        archive_size_bytes=int(archive_identity["size_bytes"]),
        archive_member_names=archive_member_names,
    )

    trajectory_document, trajectory_input, trajectory_file_identity = _read_bounded_oracle(
        trajectory_path,
        maximum_bytes=_MAX_TRAJECTORY_ORACLE_BYTES,
        name="trajectory oracle",
    )
    if trajectory_input["sha256"] != EXPECTED_TRAJECTORY_ORACLE_SHA256:
        raise ValueError("trajectory oracle SHA256 does not match the reviewed reference")
    trajectory_cases = _validate_trajectory_oracle(trajectory_document)

    # Only allowlisted compound-set JSON payloads are decompressed. All member
    # and parameter bindings are validated before evaluating either reference.
    source_rows, member_digests = _source_rows(
        archive_path,
        archive_member_names,
        law_members,
    )
    if len(source_rows) != _EXPECTED_COMPOUND_SET_MEMBER_COUNT * _EXPECTED_ANALYTE_COUNT:
        raise ValueError("source archive analyte count does not match the law oracle")

    law_rows, law_summary = _law_comparisons(source_rows, COMPOSITION_GRID)
    retention_rows, retention_summary = _retention_case_comparisons(trajectory_cases)
    qualified = (
        all(item["qualified"] for item in law_summary.values()) and retention_summary["qualified"]
    )

    identity_inputs = {
        "source archive": (
            archive_path,
            archive_identity["file_identity"],
            str(archive_identity["sha256"]),
        ),
        "law oracle": (
            law_path,
            law_file_identity,
            str(law_input["sha256"]),
        ),
        "trajectory oracle": (
            trajectory_path,
            trajectory_file_identity,
            str(trajectory_input["sha256"]),
        ),
    }

    report = {
        "schema_version": 1,
        "profile": PROFILE,
        "qualified": qualified,
        "qualification_status": "qualified" if qualified else "disagreement",
        "scope": {
            "actual_retention_model_assignment_known": False,
            "physical_parameters_qualified": False,
            "chromatographic_signal_qualified": False,
            "instrument_or_acquisition_qualified": False,
            "fresh_fit_performed": False,
            "fresh_optimizer_run": False,
            "fresh_bo_readiness": False,
            "model_law_assessment": "Each archived parameter row is evaluated conditionally under all three laws; source model selection is unknown.",
            "source_fit_range_warnings_are_metadata_only": True,
            "neue_kuss_monotonicity_assumed": False,
            "positive_coefficients_do_not_establish_curve_monotonicity": True,
            "not_established": [
                "physical parameter provenance or interpretation",
                "model assignment for any archived analyte",
                "peak width, plate height, dispersion, or chromatographic signal parity",
                "instrument acquisition behavior",
                "fresh model fitting or Bayesian optimization readiness",
            ],
        },
        "source_archive": {
            "sha256": archive_identity["sha256"],
            "size_bytes": archive_identity["size_bytes"],
            "safe_compound_set_member_count": len(member_digests),
            "compound_record_count": len(source_rows),
            "members": member_digests,
        },
        "inputs": {
            "law_oracle": {
                **law_input,
                "profile": LAW_PROFILE,
                "schema_version": law_document["schema_version"],
                "source_implementation": law_document["source_implementation"],
                "composition_count": len(COMPOSITION_GRID),
                "analyte_count": len(source_rows),
            },
            "trajectory_oracle": {
                **trajectory_input,
                "oracle_name": trajectory_document["oracle_name"],
                "schema_version": trajectory_document["schema_version"],
                "case_count": len(trajectory_cases),
                "decimal_precision_digits": trajectory_document["arithmetic"][
                    "decimal_precision_digits"
                ],
            },
        },
        "implementation": _implementation_provenance(),
        "law_comparison": {
            "reference_dtype": "torch.float64",
            "composition_count_per_analyte": len(COMPOSITION_GRID),
            "reference_value_count": len(source_rows) * len(COMPOSITION_GRID) * len(LAW_NAMES),
            "relative_tolerance": _LAW_RELATIVE_TOLERANCE,
            "model_assignment_unknown": True,
            "by_law": law_summary,
        },
        "retention_case_comparison": retention_summary,
        "reconciliation": {
            "source_members_joined_by_member_sha256": True,
            "analytes_joined_by_canonical_parameter_sha256": True,
            "all_expected_members_and_analytes_present": True,
            "all_expected_trajectory_cases_present": True,
            "oracle_numeric_disagreements_are_reported": True,
        },
    }

    with staged_bundle(target) as stage:
        _write_csv(
            stage / "law-comparisons.csv",
            [
                "source_member",
                "source_member_sha256",
                "analyte_index",
                "parameter_sha256",
                "law_reference_sha256",
                "law",
                "composition_count",
                "finite_positive_kernel_values",
                "kernel_evaluation_failures",
                "source_fit_range_compatible",
                "max_absolute_error",
                "max_relative_error",
                "relative_tolerance",
                "comparison_passed",
            ],
            law_rows,
        )
        _write_csv(
            stage / "retention-cases.csv",
            [
                "case_id",
                "input_sha256",
                "expected_status",
                "observed_status",
                "status_match",
                "retention_time_abs_error_min",
                "retention_time_relative_error",
                "retention_time_within_tolerance",
                "retention_zeta_abs_error_min",
                "retention_zeta_relative_error",
                "retention_zeta_within_tolerance",
                "dead_time_abs_error_min",
                "dead_time_relative_error",
                "dead_time_within_tolerance",
                "post_transport_abs_error_min",
                "post_transport_relative_error",
                "post_transport_within_tolerance",
                "absolute_tolerance_min",
                "relative_tolerance",
                "root_residual_comparable",
                "case_passed",
                "calculation_error_type",
            ],
            retention_rows,
        )
        (stage / "report.json").write_text(
            json.dumps(report, allow_nan=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (stage / "report.md").write_text(_render_report(report), encoding="utf-8")
        _verify_inputs_unchanged(identity_inputs)

    return report


__all__ = ["qualify_retention_inputs"]
