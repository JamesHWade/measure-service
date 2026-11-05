from __future__ import annotations

import copy

import pytest

from measure_service.analysis_evidence import (
    MAX_PAGE,
    PROFILE,
    assemble_evidence,
    build_evidence,
    manifest_for,
    page_for,
    validate_evidence,
)
from measure_service.storage import sha256


def _peak(peak_id: int, location: float) -> dict:
    return {
        "peak_id": peak_id,
        "location": location,
        "height": 2.5,
        "left_base": location - 0.1,
        "right_base": location + 0.1,
        "prominence": 1.5,
        "area": None if peak_id == 2 else 0.4,
    }


def _inputs(*, peaks: list | None = None, signal: list | None = None) -> tuple[dict, dict, str]:
    metadata = {
        "trace_id": "lc-1",
        "sample_id": "sample-1",
        "injection_id": "injection-1",
        "channel": "uv-214",
        "separation": "LC",
        "detector": "DAD",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "sample_type": "unknown",
        "batch_id": None,
        "run_order": 1,
    }
    axis = [0.5, 0.75, 1.0, 1.25, 1.5]
    source_signal = [1.0, 2.0, 3.0, 2.0, 1.0] if signal is None else signal
    dataset = {"title": "synthetic", "traces": [metadata | {"axis": axis, "signal": source_signal}]}
    digest = sha256(dataset)
    method = {"baseline": "none", "integration_baseline": "local"}
    result = {
        "kind": "chromatogram",
        "peak_count": 2 if peaks is None else len(peaks),
        "peaks": [_peak(1, 0.75), _peak(2, 1.25)] if peaks is None else peaks,
        "processed_trace": {
            "axis": list(axis),
            "signal": [0.0, 1.0, 2.0, 1.0, 0.0],
        },
        "notes": ["Area is not concentration."],
        "warnings": [],
        "packages": {"measure": "0.1.0"},
    }
    record = {
        "analysis_id": "analysis-1",
        "dataset_id": "dataset-1",
        "trace_id": "lc-1",
        "kind": "chromatogram",
        "method": method,
        "result": result,
        "provenance": {
            "input_sha256": digest,
            "method_sha256": sha256(method),
            "worker_sha256": "a" * 64,
            "source_sha256": None,
            "packages": {"measure": "0.1.0"},
            "installed_r_code_md5": None,
            "r_version": "4.5.0",
            "measure_revision": None,
            "measure_sec_revision": None,
        },
        "created_at": "2026-11-05T12:00:00+00:00",
    }
    return record, dataset, digest


def _bundle(**kwargs) -> dict:
    record, dataset, digest = _inputs(**kwargs)
    return build_evidence(record, dataset, digest)


def _reseal(bundle: dict) -> None:
    bundle["evidence_sha256"] = sha256(
        {key: value for key, value in bundle.items() if key != "evidence_sha256"}
    )


def test_builds_portable_document_without_recomputing_worker_outputs():
    record, dataset, digest = _inputs()
    bundle = build_evidence(record, dataset, digest)

    assert bundle["profile"] == PROFILE
    assert bundle["point_count"] == 5
    assert bundle["peak_count"] == 2
    assert bundle["points"][0] == {
        "sample_index": 0,
        "axis": 0.5,
        "processed_axis": 0.5,
        "raw_signal": 1.0,
        "processed_signal": 0.0,
    }
    assert bundle["peaks"][1]["area"] is None
    assert bundle["analysis"]["stored_result_sha256"] == sha256(record["result"])
    assert bundle["analysis"]["provenance"] == record["provenance"]
    assert bundle["result"] == {
        key: value
        for key, value in record["result"].items()
        if key not in {"peaks", "processed_trace"}
    }
    assert bundle["availability"] == {
        "compound_identity": "not_assigned",
        "calibration": "not_linked",
        "deconvolution": "not_performed",
        "physical_validation": "not_established",
    }
    validate_evidence(bundle)


def test_manifest_pages_and_page_order_reconstruct_exact_document():
    bundle = _bundle()
    manifest = manifest_for(bundle)
    trace_pages = [page_for(bundle, "trace", offset, 2) for offset in (0, 2, 4)]
    peak_pages = [page_for(bundle, "peaks", offset, 1) for offset in (0, 1)]

    assert set(manifest) == set(bundle) - {"points", "peaks"}
    assert [page["next_offset"] for page in trace_pages] == [2, 4, None]
    assert trace_pages[0]["count"] == 5
    assembled = assemble_evidence(manifest, trace_pages, peak_pages)
    assert assembled == bundle


def test_empty_peak_set_and_null_peak_measurements_are_preserved():
    bundle = _bundle(peaks=[])
    assert bundle["peak_count"] == 0
    empty_page = page_for(bundle, "peaks", 0, 100)
    assert empty_page["data"] == []
    assert assemble_evidence(manifest_for(bundle), bundle["points"], [empty_page]) == bundle


def test_six_field_worker_peak_rows_do_not_invent_prominence():
    record, dataset, digest = _inputs()
    for peak in record["result"]["peaks"]:
        peak.pop("prominence")
    bundle = build_evidence(record, dataset, digest)
    assert "prominence" not in bundle["peaks"][0]
    assert set(bundle["peaks"][0]) == {
        "peak_id",
        "location",
        "height",
        "left_base",
        "right_base",
        "area",
    }


def test_builder_rejects_digest_method_and_trace_identity_mismatches():
    record, dataset, digest = _inputs()
    with pytest.raises(ValueError, match="dataset digest"):
        build_evidence(record, dataset, "0" * 64)

    bad_method = copy.deepcopy(record)
    bad_method["method"]["baseline"] = "changed"
    with pytest.raises(ValueError, match="method digest"):
        build_evidence(bad_method, dataset, digest)

    bad_input = copy.deepcopy(record)
    bad_input["provenance"]["input_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="input digest"):
        build_evidence(bad_input, dataset, digest)

    bad_trace = copy.deepcopy(record)
    bad_trace["trace_id"] = "missing"
    with pytest.raises(ValueError, match="trace identity"):
        build_evidence(bad_trace, dataset, digest)


def test_builder_preserves_independent_source_and_processed_axes():
    record, dataset, digest = _inputs(peaks=[])
    raw_axis = [0.1, 0.2, 0.30000000000000004, 0.4, 0.5]
    processed_axis = [0.1, 0.2, 0.3, 0.4, 0.5]
    dataset["traces"][0]["axis"] = raw_axis
    record["result"]["processed_trace"]["axis"] = processed_axis
    digest = sha256(dataset)
    record["provenance"]["input_sha256"] = digest
    bundle = build_evidence(record, dataset, digest)
    assert bundle["points"][2]["axis"] == 0.30000000000000004
    assert bundle["points"][2]["processed_axis"] == 0.3


def test_builder_rejects_nonincreasing_axes_and_nonfinite_values():
    record, dataset, digest = _inputs()
    dataset["traces"][0]["axis"][3] = dataset["traces"][0]["axis"][2]
    digest = sha256(dataset)
    record["provenance"]["input_sha256"] = digest
    with pytest.raises(ValueError, match="strictly increasing"):
        build_evidence(record, dataset, digest)

    record, dataset, digest = _inputs()
    record["result"]["processed_trace"]["axis"][3] = record["result"]["processed_trace"]["axis"][2]
    with pytest.raises(ValueError, match="processed trace axes must be strictly increasing"):
        build_evidence(record, dataset, digest)

    record, dataset, digest = _inputs()
    record["result"]["processed_trace"]["signal"][0] = float("inf")
    with pytest.raises(ValueError, match="finite number"):
        build_evidence(record, dataset, digest)


def test_builder_rejects_peak_schema_duplicate_ids_and_stored_count_mismatch():
    record, dataset, digest = _inputs()
    record["result"]["peaks"][0]["unexpected"] = "extra"
    with pytest.raises(ValueError, match="unexpected shape"):
        build_evidence(record, dataset, digest)

    record, dataset, digest = _inputs()
    record["result"]["peaks"][1]["peak_id"] = 1
    with pytest.raises(ValueError, match="unique"):
        build_evidence(record, dataset, digest)

    record, dataset, digest = _inputs()
    record["result"]["peak_count"] = 4
    with pytest.raises(ValueError, match="stored peak_count"):
        build_evidence(record, dataset, digest)


@pytest.mark.parametrize(
    ("left_base", "location", "right_base", "error"),
    [
        (1.2, 0.75, 1.1, "left_base must not exceed right_base"),
        (0.7, 0.6, 1.0, "left_base must not exceed location"),
        (0.6, 1.1, 1.0, "location must not exceed right_base"),
        (0.4, 0.75, 1.6, "within the trace axis range"),
    ],
)
def test_builder_rejects_inconsistent_or_out_of_trace_peak_boundaries(
    left_base, location, right_base, error
):
    record, dataset, digest = _inputs()
    peak = record["result"]["peaks"][0]
    peak.update(left_base=left_base, location=location, right_base=right_base)
    with pytest.raises(ValueError, match=error):
        build_evidence(record, dataset, digest)


@pytest.mark.parametrize(
    ("kind", "offset", "limit"),
    [
        ("bad", 0, 1),
        ("trace", True, 1),
        ("trace", -1, 1),
        ("trace", 0, True),
        ("trace", 0, 0),
        ("trace", 0, MAX_PAGE + 1),
    ],
)
def test_page_arguments_are_strict_and_bounded(kind, offset, limit):
    with pytest.raises(ValueError):
        page_for(_bundle(), kind, offset, limit)


def test_validation_detects_content_and_method_tampering():
    bundle = _bundle()
    changed_point = copy.deepcopy(bundle)
    changed_point["points"][0]["raw_signal"] = 100
    with pytest.raises(ValueError, match="digest"):
        validate_evidence(changed_point)

    changed_method = copy.deepcopy(bundle)
    changed_method["analysis"]["method"]["baseline"] = "different"
    with pytest.raises(ValueError, match="digest"):
        validate_evidence(changed_method)

    wrong_result_count = copy.deepcopy(bundle)
    wrong_result_count["result"]["peak_count"] = 7
    _reseal(wrong_result_count)
    with pytest.raises(ValueError, match="result peak_count"):
        validate_evidence(wrong_result_count)

    reordered = copy.deepcopy(bundle)
    reordered["points"][0], reordered["points"][1] = reordered["points"][1], reordered["points"][0]
    _reseal(reordered)
    with pytest.raises(ValueError, match="sample_index"):
        validate_evidence(reordered)


def test_builder_requires_worker_diagnostic_fields_to_keep_their_stored_shapes():
    record, dataset, digest = _inputs()
    record["result"]["warnings"] = "warning as scalar"
    with pytest.raises(ValueError, match="warnings must be a list"):
        build_evidence(record, dataset, digest)

    record, dataset, digest = _inputs()
    record["result"]["notes"] = ["ok", None]
    with pytest.raises(ValueError, match="notes must be"):
        build_evidence(record, dataset, digest)


def test_assembly_rejects_missing_duplicate_reordered_or_foreign_pages():
    bundle = _bundle()
    manifest = manifest_for(bundle)
    first = page_for(bundle, "trace", 0, 2)
    second = page_for(bundle, "trace", 2, 2)
    last = page_for(bundle, "trace", 4, 2)
    peak_page = page_for(bundle, "peaks", 0, 10)

    with pytest.raises(ValueError, match="ordering"):
        assemble_evidence(manifest, [first, last], [peak_page])
    with pytest.raises(ValueError, match="ordering"):
        assemble_evidence(manifest, [first, first, second, last], [peak_page])

    foreign = copy.deepcopy(second)
    foreign["evidence_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="identity or ordering"):
        assemble_evidence(manifest, [first, foreign, last], [peak_page])

    with pytest.raises(ValueError, match="identity or ordering"):
        assemble_evidence(manifest, [second, first, last], [peak_page])

    rows = list(bundle["points"])
    rows[1], rows[2] = rows[2], rows[1]
    with pytest.raises(ValueError, match="sample_index"):
        assemble_evidence(manifest, rows, bundle["peaks"])


def test_full_bundle_and_page_byte_limits_are_enforced():
    record, dataset, digest = _inputs()
    record["result"]["notes"] = ["x" * (16 * 1024 * 1024)]
    with pytest.raises(ValueError, match="size limit"):
        build_evidence(record, dataset, digest)

    record, dataset, digest = _inputs()
    record["result"]["peaks"] = [
        {
            "peak_id": 10**220 + peak_id,
            "location": 1.0,
            "height": float("1.2345678901234567e+308"),
            "left_base": 0.9,
            "right_base": 1.1,
            "prominence": float("1.2345678901234567e+308"),
            "area": float("1.2345678901234567e+308"),
        }
        for peak_id in range(1, 1_001)
    ]
    record["result"]["peak_count"] = 1_000
    bundle = build_evidence(record, dataset, digest)
    with pytest.raises(ValueError, match="page-size limit"):
        page_for(bundle, "peaks", 0, 1_000)
