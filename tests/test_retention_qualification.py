"""Offline qualification tests using small generated source and oracle fixtures."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from measure_methods.retention import RetentionParameters, retention_factor
from measure_service.benchmarks import moses_archive
from measure_service.benchmarks import retention_qualification as qualification

_IMPLEMENTATION = {
    "revision": "5818c698b61652bbe6e718881ca99c04a3d497bd",
    "source_sha256": "9c2f545fcc4668a521d4a59a696bb18bce1c6f8c517bda45defc72cd19801ea0",
    "logical_path": "src/moses/simulator/retention_models.py",
}
_CASE_IDS = {
    "synthetic_within",
    "synthetic_endpoint",
    "synthetic_positive_horizon_censor",
    "synthetic_before_dead_time_censor",
}
_GRID = [index / 20 for index in range(21)]


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _evidence(value: str) -> dict:
    binary = float(value)
    numerator, denominator = binary.as_integer_ratio()
    return {
        "source_decimal": value,
        "binary64_hex": binary.hex(),
        "exact_binary64_rational": [str(numerator), str(denominator)],
    }


def _case(
    case_id: str,
    *,
    duration: str,
    length: str,
    area: str,
    ln_k0: str = "0",
    status: str,
    retention_time: str | None,
    zeta: str | None,
    dead_time: str,
    horizon: str,
    integral: str,
    fraction: str,
    post_volume: str = "0",
    post_delay: str = "0",
) -> dict:
    inputs = {
        "parameters": {"model": "linear", "ln_k0": ln_k0, "S1": "0", "S2": None},
        "gradient_times_min": ["0", duration],
        "gradient_fractions": ["0", "0"],
        "flow_rate_ml_min": "1",
        "column_length_m": length,
        "effective_area_m2": area,
        "acquisition_duration_min": duration,
        "post_column_volume_ml": post_volume,
    }
    expected = {
        "status": status,
        "retention_time_min": retention_time,
        "zeta_elution_min": zeta,
        "column_dead_time_min": dead_time,
        "acquisition_duration_min": duration,
        "integration_horizon_zeta_min": horizon,
        "integral_at_detector_horizon_min": integral,
        "column_fraction_at_detector_horizon": fraction,
        "post_transport_delay_min": post_delay,
        "retention_model": "linear",
        "root_residual_min": "0" if status != "right_censored" else None,
    }
    evidence = {
        "parameters": {
            "ln_k0": _evidence(ln_k0),
            "S1": _evidence("0"),
            "S2": None,
        },
        "gradient_times_min": [_evidence("0"), _evidence(duration)],
        "gradient_fractions": [_evidence("0"), _evidence("0")],
        "profile_arguments": {
            "flow_rate_ml_min": _evidence("1"),
            "column_length_m": _evidence(length),
            "effective_area_m2": _evidence(area),
            "acquisition_duration_min": _evidence(duration),
            "post_column_volume_ml": _evidence(post_volume),
        },
    }
    return {
        "case_id": case_id,
        "tags": ["synthetic"],
        "input": inputs,
        "binary64_input_evidence": evidence,
        "expected": expected,
        "oracle_checks": {"synthetic_reference_case": True},
    }


def _trajectory_document() -> dict:
    exact_area = "0.000000059604644775390625"  # 2**-24, exactly binary64
    exact_dead_time = "0.11920928955078125"
    exact_endpoint = "0.2384185791015625"
    within_dead_time = "0.95367431640625"  # 2**-20 * 1e6
    within_duration = "4"
    within_horizon = "3.04632568359375"
    cases = [
        _case(
            "synthetic_within",
            duration=within_duration,
            length="1",
            area="0.00000095367431640625",
            status="eluted_within_duration",
            retention_time="1.9073486328125",
            zeta=within_dead_time,
            dead_time=within_dead_time,
            horizon=within_horizon,
            integral=within_horizon,
            fraction="3.1953125",
        ),
        _case(
            "synthetic_endpoint",
            duration=exact_endpoint,
            length="2",
            area=exact_area,
            status="eluted_at_endpoint",
            retention_time=exact_endpoint,
            zeta=exact_dead_time,
            dead_time=exact_dead_time,
            horizon=exact_dead_time,
            integral=exact_dead_time,
            fraction="1",
        ),
        _case(
            "synthetic_positive_horizon_censor",
            duration="0.5",
            length="2",
            area=exact_area,
            ln_k0="2",
            status="right_censored",
            retention_time=None,
            zeta=None,
            dead_time=exact_dead_time,
            horizon="0.38079071044921875",
            integral="0.05153",
            fraction="0.432",
        ),
        _case(
            "synthetic_before_dead_time_censor",
            duration="0.1",
            length="2",
            area=exact_area,
            status="right_censored",
            retention_time=None,
            zeta=None,
            dead_time=exact_dead_time,
            horizon="-0.11920928955078125",
            integral="0",
            fraction="0",
            post_volume="0.1",
            post_delay="0.1",
        ),
    ]
    return {
        "schema_version": 3,
        "oracle_name": qualification.TRAJECTORY_ORACLE_NAME,
        "scope": "synthetic profile fixtures",
        "arithmetic": {
            "decimal_precision_digits": 70,
            "input_interpretation": "Each decimal input is converted to binary64.",
            "binary64_conversion_evidence": "float.hex() and exact rational evidence are recorded.",
        },
        "input_contract": {
            "detector_horizon_rule": "zeta_limit = T - column_dead_time - post_column_volume / flow_rate",
            "endpoint_rule": "The inclusive endpoint is detector time T.",
            "float64_comparison_fields": list(qualification._ORACLE_FLOAT_FIELDS),
        },
        "case_count": len(cases),
        "cases": cases,
        "all_synthetic_oracle_checks_passed": True,
    }


def _source_rows():
    return [
        {"log_k0": 3.5, "S_1": 20.0, "S_2": 1.5, "area": 1.25},
        {"log_k0": 2.5, "S_1": 260.0, "S_2": 2.0, "area": 0.8},
    ]


def _law_document(
    archive_sha256: str, archive_size: int, source_payloads: dict[str, bytes]
) -> dict:
    members = []
    for name, payload in sorted(source_payloads.items()):
        source_rows = json.loads(payload)
        analytes = []
        for index, row in enumerate(source_rows):
            parameters = {key: row[key] for key in ("log_k0", "S_1", "S_2")}
            reference_values = {}
            for law in qualification.LAW_NAMES:
                model_parameters = RetentionParameters(
                    model=law,
                    ln_k0=row["log_k0"],
                    S1=row["S_1"],
                    S2=None if law == "linear" else row["S_2"],
                )
                reference_values[law] = [
                    retention_factor(parameters=model_parameters, composition=phi) for phi in _GRID
                ]
            analytes.append(
                {
                    "analyte_index": index,
                    "parameter_sha256": _sha256(_json_bytes(parameters)),
                    "reference_values": reference_values,
                }
            )
        members.append({"name": name, "sha256": _sha256(payload), "analytes": analytes})
    return {
        "schema_version": 1,
        "profile": qualification.LAW_PROFILE,
        "scope": "synthetic source-model law values",
        "source_archive": {"sha256": archive_sha256, "size_bytes": archive_size},
        "source_implementation": dict(qualification.EXPECTED_SOURCE_IMPLEMENTATION),
        "arithmetic": {"dtype": "torch.float64", "python": "test", "torch_version": "test"},
        "composition_grid": list(_GRID),
        "members": members,
    }


def _write_json(path: Path, value: dict) -> bytes:
    payload = json.dumps(value, sort_keys=True, indent=2, allow_nan=True).encode() + b"\n"
    path.write_bytes(payload)
    return payload


def _fixture(tmp_path, monkeypatch):
    archive_path = tmp_path / "source.zip"
    names = [
        "results/compound_sets/2x2C_20min_991/compound_set_0.json",
        "results/compound_sets/2x2C_20min_991/compound_set_1.json",
    ]
    source_payloads = {
        name: json.dumps(_source_rows(), sort_keys=True, allow_nan=False).encode() for name in names
    }
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as source_zip:
        for name, payload in source_payloads.items():
            source_zip.writestr(name, payload)
    archive_bytes = archive_path.read_bytes()
    archive_sha256 = _sha256(archive_bytes)
    monkeypatch.setattr(moses_archive, "MOSES_ARCHIVE_SIZE_BYTES", len(archive_bytes))
    monkeypatch.setattr(
        moses_archive,
        "MOSES_ARCHIVE_MD5",
        hashlib.md5(archive_bytes, usedforsecurity=False).hexdigest(),
    )
    monkeypatch.setattr(qualification, "EXPECTED_SOURCE_ARCHIVE_SHA256", archive_sha256)
    monkeypatch.setattr(qualification, "_EXPECTED_COMPOUND_SET_MEMBER_COUNT", len(names))
    monkeypatch.setattr(qualification, "_EXPECTED_ANALYTE_COUNT", 2)

    law_path = tmp_path / "law-oracle.json"
    law = _law_document(archive_sha256, len(archive_bytes), source_payloads)
    law_payload = _write_json(law_path, law)
    monkeypatch.setattr(qualification, "EXPECTED_LAW_ORACLE_SHA256", _sha256(law_payload))

    trajectory_path = tmp_path / "trajectory-oracle.json"
    trajectory = _trajectory_document()
    trajectory_payload = _write_json(trajectory_path, trajectory)
    monkeypatch.setattr(
        qualification, "EXPECTED_TRAJECTORY_ORACLE_SHA256", _sha256(trajectory_payload)
    )
    monkeypatch.setattr(
        qualification,
        "_EXPECTED_RETENTION_CASE_IDS",
        frozenset(case["case_id"] for case in trajectory["cases"]),
    )
    return archive_path, law_path, trajectory_path, law, trajectory


def _reseal_law(path, document, monkeypatch):
    payload = _write_json(path, document)
    monkeypatch.setattr(qualification, "EXPECTED_LAW_ORACLE_SHA256", _sha256(payload))
    return payload


def _reseal_trajectory(path, document, monkeypatch):
    payload = _write_json(path, document)
    monkeypatch.setattr(qualification, "EXPECTED_TRAJECTORY_ORACLE_SHA256", _sha256(payload))
    return payload


def _qualify(paths, output_dir):
    archive_path, law_path, trajectory_path, *_ = paths
    return qualification.qualify_retention_inputs(
        archive_path, output_dir, law_oracle_json=law_path, trajectory_oracle_json=trajectory_path
    )


def test_qualifies_both_oracles_without_emitting_coefficients_and_seals_outputs(
    tmp_path, monkeypatch
):
    paths = _fixture(tmp_path, monkeypatch)
    archive_path, law_path, trajectory_path, *_ = paths
    before = {path: path.read_bytes() for path in (archive_path, law_path, trajectory_path)}
    output_dir = tmp_path / "qualification"

    report = _qualify(paths, output_dir)

    assert report["qualified"] is True
    assert report["qualification_status"] == "qualified"
    assert report["law_comparison"]["reference_value_count"] == 2 * 2 * 3 * 21
    for law in qualification.LAW_NAMES:
        summary = report["law_comparison"]["by_law"][law]
        assert summary["record_count"] == 4
        assert summary["records_with_all_values_within_relative_tolerance"] == 4
        assert summary["source_fit_range_compatible_records"] == 2
        assert summary["source_fit_range_outside_records"] == 2
    assert report["retention_case_comparison"]["case_count"] == 4
    assert (
        report["retention_case_comparison"]["cases_with_all_status_and_numeric_comparisons_passing"]
        == 4
    )
    assert report["scope"]["actual_retention_model_assignment_known"] is False
    assert report["scope"]["fresh_bo_readiness"] is False
    assert {path: path.read_bytes() for path in before} == before

    report_text = (output_dir / "report.json").read_text()
    assert '"reference_values"' not in report_text
    assert '"log_k0"' not in report_text
    assert '"S_1"' not in report_text
    assert '"area"' not in report_text
    assert "law_reference_sha256" in (output_dir / "law-comparisons.csv").read_text()
    assert "synthetic_endpoint" in (output_dir / "retention-cases.csv").read_text()
    complete = json.loads((output_dir / "complete.json").read_text())
    assert set(complete["files"]) == {
        "report.json",
        "report.md",
        "law-comparisons.csv",
        "retention-cases.csv",
    }
    for name, checksum in complete["files"].items():
        payload = (output_dir / name).read_bytes()
        assert checksum["sha256"] == _sha256(payload)
        assert checksum["size_bytes"] == len(payload)


def test_archive_digest_is_verified_before_oracle_members_are_joined(tmp_path, monkeypatch):
    paths = _fixture(tmp_path, monkeypatch)
    _, law_path, _, law, _ = paths
    law["source_archive"]["sha256"] = "f" * 64
    _reseal_law(law_path, law, monkeypatch)
    read_calls = []
    monkeypatch.setattr(
        qualification, "read_member", lambda *args, **kwargs: read_calls.append(args)
    )

    with pytest.raises(ValueError, match="different source archive|not bound"):
        _qualify(paths, tmp_path / "qualification")

    assert not read_calls
    assert not (tmp_path / "qualification").exists()


@pytest.mark.parametrize("tamper", ["member_digest", "parameter_digest", "missing_member"])
def test_member_and_parameter_binding_fail_before_publication(tmp_path, monkeypatch, tamper):
    paths = _fixture(tmp_path, monkeypatch)
    _, law_path, _, law, _ = paths
    if tamper == "member_digest":
        law["members"][0]["sha256"] = "0" * 64
    elif tamper == "parameter_digest":
        law["members"][0]["analytes"][0]["parameter_sha256"] = "1" * 64
    else:
        law["members"].pop()
    _reseal_law(law_path, law, monkeypatch)

    with pytest.raises(ValueError, match="digest|member|analyte"):
        _qualify(paths, tmp_path / "qualification")

    assert not (tmp_path / "qualification").exists()


@pytest.mark.parametrize("tamper", ["coordinate", "zero_value", "unknown_law"])
def test_law_oracle_coordinate_and_value_schema_is_strict(tmp_path, monkeypatch, tamper):
    paths = _fixture(tmp_path, monkeypatch)
    _, law_path, _, law, _ = paths
    if tamper == "coordinate":
        law["composition_grid"][4] = 0.21
    elif tamper == "zero_value":
        law["members"][0]["analytes"][0]["reference_values"]["linear"][0] = 0.0
    else:
        law["members"][0]["analytes"][0]["reference_values"]["bogus"] = [1.0] * 21
    _reseal_law(law_path, law, monkeypatch)

    with pytest.raises(ValueError, match="composition grid|positive|three declared"):
        _qualify(paths, tmp_path / "qualification")

    assert not (tmp_path / "qualification").exists()


@pytest.mark.parametrize(
    "tamper", ["profile", "unknown_model", "nonfinite", "bad_status", "missing_case"]
)
def test_trajectory_oracle_schema_and_case_completeness_are_strict(tmp_path, monkeypatch, tamper):
    paths = _fixture(tmp_path, monkeypatch)
    _, _, trajectory_path, _, trajectory = paths
    if tamper == "profile":
        trajectory["oracle_name"] = "unknown-profile"
    elif tamper == "unknown_model":
        trajectory["cases"][0]["input"]["parameters"]["model"] = "unknown"
    elif tamper == "nonfinite":
        trajectory["cases"][0]["input"]["parameters"]["ln_k0"] = "NaN"
    elif tamper == "bad_status":
        trajectory["cases"][0]["expected"]["status"] = "not_eluted"
    else:
        trajectory["cases"].pop()
        trajectory["case_count"] -= 1
    _reseal_trajectory(trajectory_path, trajectory, monkeypatch)

    with pytest.raises(ValueError):
        _qualify(paths, tmp_path / "qualification")

    assert not (tmp_path / "qualification").exists()


def test_tampered_binary64_representation_fails_before_publication(tmp_path, monkeypatch):
    paths = _fixture(tmp_path, monkeypatch)
    _, _, trajectory_path, _, trajectory = paths
    trajectory["cases"][0]["binary64_input_evidence"]["profile_arguments"]["effective_area_m2"][
        "binary64_hex"
    ] = "0x0.0p+0"
    _reseal_trajectory(trajectory_path, trajectory, monkeypatch)

    with pytest.raises(ValueError, match="hexadecimal value"):
        _qualify(paths, tmp_path / "qualification")

    assert not (tmp_path / "qualification").exists()


def test_numeric_disagreement_is_published_as_unqualified(tmp_path, monkeypatch):
    paths = _fixture(tmp_path, monkeypatch)
    _, law_path, _, law, _ = paths
    law["members"][0]["analytes"][0]["reference_values"]["linear"][0] *= 1.01
    _reseal_law(law_path, law, monkeypatch)

    report = _qualify(paths, tmp_path / "qualification")

    assert report["qualified"] is False
    assert report["qualification_status"] == "disagreement"
    assert (
        report["law_comparison"]["by_law"]["linear"][
            "records_with_all_values_within_relative_tolerance"
        ]
        == 3
    )
    rows = (tmp_path / "qualification" / "law-comparisons.csv").read_text()
    assert ",False\n" in rows


def test_status_disagreement_is_reported_without_claiming_qualification(tmp_path, monkeypatch):
    paths = _fixture(tmp_path, monkeypatch)
    original_predict = qualification.predict_retention_time

    def disagree_once(**kwargs):
        result = original_predict(**kwargs)
        if kwargs["acquisition_duration_min"] == 4.0:
            result["status"] = "not_eluted"
        return result

    monkeypatch.setattr(qualification, "predict_retention_time", disagree_once)

    report = _qualify(paths, tmp_path / "qualification")

    assert report["qualified"] is False
    assert report["qualification_status"] == "disagreement"
    row = next(
        row
        for row in (tmp_path / "qualification" / "retention-cases.csv").read_text().splitlines()
        if row.startswith("synthetic_within,")
    )
    assert row.split(",")[3] == "not_eluted"
    assert row.split(",")[4] == "False"


def test_changed_input_during_analysis_aborts_atomic_publication(tmp_path, monkeypatch):
    paths = _fixture(tmp_path, monkeypatch)
    _, _, trajectory_path, _, _ = paths
    original_predict = qualification.predict_retention_time
    changed = False

    def mutate_after_first_prediction(**kwargs):
        nonlocal changed
        result = original_predict(**kwargs)
        if not changed:
            trajectory_path.write_bytes(trajectory_path.read_bytes() + b" ")
            changed = True
        return result

    monkeypatch.setattr(qualification, "predict_retention_time", mutate_after_first_prediction)

    with pytest.raises(ValueError, match="changed during retention qualification"):
        _qualify(paths, tmp_path / "qualification")

    assert changed
    assert not (tmp_path / "qualification").exists()
    assert not list(tmp_path.glob(".measure-evidence-*"))


def test_existing_output_is_preserved(tmp_path, monkeypatch):
    paths = _fixture(tmp_path, monkeypatch)
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("existing result")

    with pytest.raises(ValueError, match="existing files are preserved"):
        _qualify(paths, output)

    assert marker.read_text() == "existing result"
    assert set(path.name for path in output.iterdir()) == {"keep.txt"}


def test_v2_decimal_intent_endpoint_status_is_not_reclassified_by_tolerance():
    path = Path(
        "/tmp/moses-retention-independent/reviewed-binary64-inputs-v3/retention_oracle.json"
    )
    if not path.is_file():
        pytest.skip("reviewed external v3 oracle is not present in this checkout")
    document = json.loads(path.read_text())
    case = next(
        case
        for case in document["cases"]
        if case["case_id"] == "decimal_intent_endpoint_rounds_to_right_censor"
    )
    assert case["input"]["column_length_m"] == "0.4"
    assert case["input"]["effective_area_m2"] == "0.0000025"
    assert case["expected"]["status"] == "right_censored"
    assert case["expected"]["retention_time_min"] is None


@pytest.mark.parametrize("failure", ["nan", "zero", "runtime"])
def test_law_calculation_failures_are_retained_as_unqualified_evidence(
    tmp_path, monkeypatch, failure
):
    paths = _fixture(tmp_path, monkeypatch)

    def invalid_law(**kwargs):
        if failure == "runtime":
            raise RuntimeError("retention law numerical precision is unavailable")
        return float("nan") if failure == "nan" else 0.0

    monkeypatch.setattr(qualification, "retention_factor", invalid_law)
    report = _qualify(paths, tmp_path / "qualification")
    assert report["qualified"] is False
    for law in qualification.LAW_NAMES:
        summary = report["law_comparison"]["by_law"][law]
        assert summary["kernel_evaluation_failures"] == 4 * 21
        assert summary["records_with_finite_positive_kernel_values_on_grid"] == 0
        assert summary["maximum_absolute_error"] is None
        assert summary["maximum_relative_error"] is None
    assert (tmp_path / "qualification" / "complete.json").is_file()


def test_retention_solver_failure_is_a_case_result_not_an_elution_or_abort(tmp_path, monkeypatch):
    import csv

    paths = _fixture(tmp_path, monkeypatch)

    def exhausted_solver(**kwargs):
        raise RuntimeError("retention integration exceeded max_evaluations")

    monkeypatch.setattr(qualification, "predict_retention_time", exhausted_solver)
    report = _qualify(paths, tmp_path / "qualification")
    assert report["qualified"] is False
    summary = report["retention_case_comparison"]
    assert summary["cases_with_all_status_and_numeric_comparisons_passing"] == 0
    with (tmp_path / "qualification" / "retention-cases.csv").open(newline="") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 4
    assert {row["observed_status"] for row in rows} == {"calculation_error"}
    assert {row["calculation_error_type"] for row in rows} == {"RuntimeError"}
    assert all(row["retention_time_abs_error_min"] == "" for row in rows)


def test_finite_law_output_with_overflowing_relative_error_publishes_failure(tmp_path, monkeypatch):
    paths = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(qualification, "retention_factor", lambda **kwargs: 1e308)
    report = _qualify(paths, tmp_path / "qualification")
    assert report["qualified"] is False
    assert qualification._relative_error(1e308, 1e-300) is None
    serialized = json.loads((tmp_path / "qualification" / "report.json").read_text())
    assert serialized == report
    assert (tmp_path / "qualification" / "complete.json").is_file()
    assert any(
        row["kernel_evaluation_failures"] > 0 for row in report["law_comparison"]["by_law"].values()
    )
