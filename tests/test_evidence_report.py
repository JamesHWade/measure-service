"""Tests for the standalone local chromatogram evidence report."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from measure_service import evidence_report
from measure_service.analysis_evidence import build_evidence
from measure_service.storage import sha256


def _bundle(
    *,
    axis: list[float] | None = None,
    processed_axis: list[float] | None = None,
    raw: list[float] | None = None,
    processed: list[float] | None = None,
    peak: dict | None = None,
    peaks: list[dict] | None = None,
    area_basis: str | None = None,
    analysis_id: str = "analysis-1",
    axis_unit: str = "min",
    signal_unit: str = "mAU",
    method_name: str = "UV integration",
    notes: str = "Peak areas use the specified baseline and trapezoidal integration; negative integrand values are clipped to zero.",
) -> dict:
    axis = axis or [0.0, 1.0, 2.0, 3.0]
    processed_axis = processed_axis or axis
    raw = raw or [0.0, -1.0, 2.0, 0.0]
    processed = processed or [0.0, -0.5, 1.5, 0.0]
    peak = peak or {
        "peak_id": 1,
        "location": 2.0,
        "height": 1.5,
        "left_base": 1.0,
        "right_base": 3.0,
        "area": 0.0,
    }
    peak = dict(peak)
    if area_basis is not None:
        peak["area_basis"] = area_basis
    method = {
        "name": method_name,
        "integration_baseline": {"type": "linear", "negative_integrand": "clip_to_zero"},
    }
    trace = {
        "trace_id": "trace-1",
        "sample_id": '<img src=x onerror="alert(1)">',
        "injection_id": "inj-1",
        "channel": "UV-1",
        "separation": "LC",
        "detector": "DAD",
        "axis_unit": axis_unit,
        "signal_unit": signal_unit,
        "sample_type": "standard",
        "batch_id": "batch-1",
        "run_order": 1,
        "axis": axis,
        "signal": raw,
    }
    dataset = {"traces": [trace]}
    digest = sha256(dataset)
    result = {
        "kind": "chromatogram",
        "peak_count": len(peaks) if peaks is not None else 1,
        "peaks": peaks if peaks is not None else [peak],
        "processed_trace": {"axis": processed_axis, "signal": processed},
        "warnings": ["No detector-specific annotation is attached."],
        "notes": notes,
        "area_semantics": "stored signal area",
    }
    record = {
        "analysis_id": analysis_id,
        "dataset_id": "dataset-1",
        "trace_id": "trace-1",
        "kind": "chromatogram",
        "method": method,
        "result": result,
        "provenance": {
            "input_sha256": digest,
            "method_sha256": sha256(method),
            "worker_sha256": "a" * 64,
            "source_sha256": "b" * 64,
            "package_versions": {"measure": "0.1.0", "measure.sec": "0.2.0"},
            "r_version": "4.5.2",
        },
        "created_at": "2026-11-05T13:00:00Z",
    }
    return build_evidence(record, dataset, digest)


def _csv_rows(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    text = io.StringIO(content.decode("utf-8"), newline="")
    reader = csv.DictReader(text)
    return list(reader.fieldnames or []), list(reader)


def _path_commands(svg: bytes, css_class: str) -> list[str]:
    root = ET.fromstring(svg)
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    path = root.find(f".//svg:path[@class='{css_class}']", namespace)
    assert path is not None
    return re.findall(r"[ML]", path.attrib["d"])


def _path_points(svg: bytes, css_class: str) -> list[tuple[str, str]]:
    root = ET.fromstring(svg)
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    path = root.find(f".//svg:path[@class='{css_class}']", namespace)
    assert path is not None
    return re.findall(r"[ML]\s+([^\s]+)\s+([^\s]+)", path.attrib["d"])


def _tick_labels(svg: bytes, css_class: str) -> list[str]:
    root = ET.fromstring(svg)
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    return [
        element.text or ""
        for element in root.findall(f".//svg:text[@class='{css_class}']", namespace)
    ]


def test_report_preserves_complete_bundle_and_all_csv_rows() -> None:
    bundle = _bundle()
    artifacts = evidence_report.render_report(bundle)

    assert set(artifacts) == {
        "report.html",
        "chromatogram.svg",
        "evidence.json",
        "trace.csv",
        "peaks.csv",
    }
    assert json.loads(artifacts["evidence.json"]) == bundle
    for signal in ("raw", "processed"):
        assert len(_path_commands(artifacts["chromatogram.svg"], signal)) == bundle["point_count"]

    trace_fields, trace_rows = _csv_rows(artifacts["trace.csv"])
    assert trace_fields == [
        "analysis_id",
        "evidence_sha256",
        "axis_unit",
        "signal_unit",
        "sample_index",
        "axis",
        "processed_axis",
        "raw_signal",
        "processed_signal",
    ]
    assert len(trace_rows) == bundle["point_count"]
    assert all(row["analysis_id"] == bundle["analysis"]["analysis_id"] for row in trace_rows)
    assert all(row["evidence_sha256"] == bundle["evidence_sha256"] for row in trace_rows)
    assert [row["sample_index"] for row in trace_rows] == ["0", "1", "2", "3"]
    assert trace_rows[1]["raw_signal"] == "-1.0"

    peak_fields, peak_rows = _csv_rows(artifacts["peaks.csv"])
    assert peak_fields == [
        "analysis_id",
        "evidence_sha256",
        "axis_unit",
        "signal_unit",
        "area_unit",
        "peak_id",
        "location",
        "height",
        "left_base",
        "right_base",
        "area",
        "area_basis",
    ]
    assert len(peak_rows) == 1
    assert peak_rows[0]["analysis_id"] == bundle["analysis"]["analysis_id"]
    assert peak_rows[0]["evidence_sha256"] == bundle["evidence_sha256"]
    assert peak_rows[0]["area_unit"] == "mAU * min"
    assert peak_rows[0]["area"] == "0.0"
    assert peak_rows[0]["area_basis"] == ""
    assert "area_basis" in artifacts["report.html"].decode("utf-8")
    assert "Unavailable" in artifacts["report.html"].decode("utf-8")
    report = artifacts["report.html"].decode("utf-8")
    assert report.index("<figure>") < report.index("<h2>Stored peak rows")
    assert report.index("<h2>Stored peak rows") < report.index("<h2>Trace and method")
    assert "Apex location (min)" in report
    assert "Area (mAU * min)" in report
    assert "Area basis" in report
    assert "Shaded bands mark recorded peak-boundary intervals" in report
    assert "<details>" in report and "Provenance digests" in report
    details_start = report.index("<details>")
    assert report.index("Warnings") < details_start
    assert report.index("Notes") < details_start
    assert report.index("Stored method details") < details_start
    assert report.index("Provenance digests") > details_start


def test_responsive_tables_keep_accessible_structure_and_exact_values() -> None:
    peak = {
        "peak_id": 42,
        "location": 2.345678901234567,
        "height": 1.2345678901234567,
        "left_base": 1.1234567890123457,
        "right_base": 3.987654321098765,
        "area": 987654321.0123457,
        "prominence": 0.00000012345678901234567,
    }
    bundle = _bundle(
        axis=[0.0, 1.0, 2.0, 3.0, 4.0],
        raw=[0.0, -1.0, 2.0, 0.0, 0.0],
        processed=[0.0, -0.5, 1.5, 0.0, 0.0],
        peak=peak,
    )
    report = evidence_report.render_report(bundle)["report.html"].decode("utf-8")

    assert (
        '<div class="table-scroll peak-table-region" role="region" '
        'aria-label="Stored peak rows" aria-describedby="peak-table-scroll-hint" tabindex="0">'
        in report
    )
    assert (
        '<p class="scroll-hint" id="peak-table-scroll-hint">'
        "Scroll horizontally to view all peak columns.</p>"
    ) in report
    assert '<table class="peak-table"><thead>' in report
    assert report.count('class="table-scroll metadata-table-region"') == 4
    assert '<th scope="row">Stored method details</th>' in report
    assert '<th scope="row">Evidence SHA-256</th>' in report
    for value in (
        peak["location"],
        peak["height"],
        peak["left_base"],
        peak["right_base"],
        peak["area"],
        peak["prominence"],
    ):
        assert json.dumps(value) in report
    assert "Apex location (min)" in report
    assert "Area (mAU * min)" in report
    assert "Unavailable" in report
    assert '<pre class="provenance-json">' in report


def test_responsive_html_change_keeps_other_artifacts_byte_identical() -> None:
    artifacts = evidence_report.render_report(_bundle())
    expected_hashes = {
        "chromatogram.svg": "dbfc1ad7cc932dcd07420b954ee26a8839a2319997b1be1d56eda8a359ca78b6",
        "evidence.json": "4b21ee12af03a15d751a41d5273c1c22d851b939e2634e561be26f7c3ab541ec",
        "peaks.csv": "e2405e3d29e84bd0819cd97aa52942500b2b39834d664f3d92976c5d8c1bbfb1",
        "trace.csv": "246c782e72a8a3dd743b394e6a47aee2d53e3cf1691a4feae07afa831427b72a",
    }

    for filename, expected in expected_hashes.items():
        assert hashlib.sha256(artifacts[filename]).hexdigest() == expected


def test_report_escapes_metadata_and_keeps_complete_provenance_visible() -> None:
    bundle = _bundle(
        method_name='<script>alert("method")</script>',
        axis_unit="<x&axis>",
        signal_unit="=SUM(A1:A2)",
        notes=(
            "<script>alert('note')</script> Peak areas use the specified baseline and "
            "trapezoidal integration; negative integrand values are clipped to zero."
        ),
    )
    artifacts = evidence_report.render_report(bundle)
    report = artifacts["report.html"].decode("utf-8")
    svg = artifacts["chromatogram.svg"].decode("utf-8")

    assert "<script>" not in report
    assert 'onerror="alert(1)"' not in report
    assert "&lt;script&gt;alert(&quot;method&quot;)&lt;/script&gt;" in report
    assert "&lt;script&gt;alert(&#x27;note&#x27;)&lt;/script&gt;" in report
    assert "Axis (&lt;x&amp;axis&gt;)" in svg
    assert "Signal (=SUM(A1:A2))" in svg
    assert "Input dataset SHA-256" in report
    assert "package_versions" in report and "measure.sec" in report
    assert "r_version" in report and "4.5.2" in report
    assert "Record SHA-256" in report
    assert "Content-Security-Policy" in report
    assert "default-src 'none'" in report
    assert "no purity or concentration inference is made" in report
    assert "not analyte assignments" in report
    assert "negative integrand values are clipped to zero" in report

    trace_fields, trace_rows = _csv_rows(artifacts["trace.csv"])
    assert trace_fields[2:4] == ["axis_unit", "signal_unit"]
    assert trace_rows[0]["axis_unit"] == "<x&axis>"
    assert trace_rows[0]["signal_unit"] == "'=SUM(A1:A2)"
    assert trace_rows[1]["raw_signal"] == "-1.0"


def test_forbidden_xml_codepoints_are_escaped_in_markup_and_csv_only() -> None:
    forbidden = "\x00\x01\x08\x0b\x0c\x1f\ufffe\uffff"
    axis_unit = f"mAU{forbidden}"
    method_name = f"method{forbidden}"
    notes = f"note{forbidden}"
    area_basis = f"observed{forbidden}"
    bundle = _bundle(
        axis_unit=axis_unit,
        method_name=method_name,
        notes=notes,
        area_basis=area_basis,
    )
    artifacts = evidence_report.render_report(bundle)
    report = artifacts["report.html"].decode("utf-8")
    svg = artifacts["chromatogram.svg"].decode("utf-8")
    ET.fromstring(artifacts["chromatogram.svg"])
    _, trace_rows = _csv_rows(artifacts["trace.csv"])
    _, peak_rows = _csv_rows(artifacts["peaks.csv"])
    document = json.loads(artifacts["evidence.json"])

    for char in forbidden:
        assert char not in report
        assert char not in svg
    assert "mAU\\u0000\\u0001\\u0008\\u000b\\u000c\\u001f\\ufffe\\uffff" in svg
    assert "method\\u0000\\u0001" in report
    assert "note\\u0000\\u0001" in report
    assert "observed\\u0000\\u0001" in report
    assert document["trace"]["axis_unit"] == axis_unit
    assert document["analysis"]["method"]["name"] == method_name
    assert document["result"]["notes"] == notes
    assert document["peaks"][0]["area_basis"] == area_basis
    assert (
        trace_rows[0]["axis_unit"] == "mAU\\u0000\\u0001\\u0008\\u000b\\u000c\\u001f\\ufffe\\uffff"
    )
    assert (
        peak_rows[0]["area_basis"]
        == "observed\\u0000\\u0001\\u0008\\u000b\\u000c\\u001f\\ufffe\\uffff"
    )


def test_csv_preserves_tabs_newlines_carriage_returns_unicode_and_backslashes() -> None:
    axis_unit = "µm\\column,\tmode\nnext\rfinal"
    area_basis = "observed\\integral"
    bundle = _bundle(axis_unit=axis_unit, signal_unit="mAU·s", area_basis=area_basis)
    artifacts = evidence_report.render_report(bundle)
    trace_csv = artifacts["trace.csv"].decode("utf-8")
    _, trace_rows = _csv_rows(artifacts["trace.csv"])
    _, peak_rows = _csv_rows(artifacts["peaks.csv"])
    document = json.loads(artifacts["evidence.json"])

    assert artifacts["trace.csv"].endswith(b"\r\n")
    assert '"µm\\column,\tmode\nnext\rfinal"' in trace_csv
    assert trace_rows[0]["axis_unit"] == axis_unit
    assert trace_rows[0]["signal_unit"] == "mAU·s"
    assert peak_rows[0]["area_basis"] == area_basis
    assert document["trace"]["axis_unit"] == axis_unit
    assert document["peaks"][0]["area_basis"] == area_basis


def test_optional_prominence_and_null_values_are_presented_truthfully() -> None:
    peak = {
        "peak_id": 7,
        "location": None,
        "height": None,
        "left_base": None,
        "right_base": None,
        "area": None,
        "prominence": None,
    }
    bundle = _bundle(peak=peak)
    artifacts = evidence_report.render_report(bundle)
    report = artifacts["report.html"].decode("utf-8")
    peak_fields, peak_rows = _csv_rows(artifacts["peaks.csv"])

    assert "Prominence (mAU)" in report
    assert peak_fields[-2:] == ["prominence", "area_basis"]
    assert peak_rows[0]["location"] == ""
    assert peak_rows[0]["area"] == ""
    assert peak_rows[0]["prominence"] == ""
    assert peak_rows[0]["area_basis"] == ""
    assert "Unavailable" in report
    assert json.loads(artifacts["evidence.json"])["peaks"][0]["area"] is None


def test_report_handles_negative_flat_and_extreme_finite_values() -> None:
    negative = _bundle(
        raw=[-3.0, -2.0, -1.0, -4.0],
        processed=[-3.0, -2.5, -1.5, -4.0],
    )
    flat = _bundle(raw=[0.0] * 4, processed=[0.0] * 4)
    extreme = _bundle(
        axis=[0.0, 5e-324, 1.0, 2.0],
        raw=[-1.7e308, -1e308, 1e308, 1.7e308],
        processed=[-1.6e308, -0.5e308, 0.5e308, 1.6e308],
        peak={
            "peak_id": 3,
            "location": 1.0,
            "height": 1.7e308,
            "left_base": 5e-324,
            "right_base": 2.0,
            "area": 0.0,
        },
    )

    for bundle in (negative, flat, extreme):
        artifacts = evidence_report.render_report(bundle)
        svg = artifacts["chromatogram.svg"].decode("utf-8")
        ET.fromstring(artifacts["chromatogram.svg"])
        assert "nan" not in svg.lower()
        assert "inf" not in svg.lower()
        assert len(_path_commands(artifacts["chromatogram.svg"], "raw")) == 4
        assert len(_path_commands(artifacts["chromatogram.svg"], "processed")) == 4
        x_ticks = _tick_labels(artifacts["chromatogram.svg"], "x-tick")
        y_ticks = _tick_labels(artifacts["chromatogram.svg"], "y-tick")
        assert 1 <= len(x_ticks) <= 5
        assert 1 <= len(y_ticks) <= 5
        assert all(float(label) == float(label) for label in x_ticks + y_ticks)
    negative_svg = evidence_report.render_report(negative)["chromatogram.svg"]
    assert "-4" in _tick_labels(negative_svg, "y-tick")
    flat_svg = evidence_report.render_report(flat)["chromatogram.svg"]
    assert _tick_labels(flat_svg, "y-tick") == ["0"]
    extreme_svg = evidence_report.render_report(extreme)["chromatogram.svg"]
    extreme_ticks = [float(label) for label in _tick_labels(extreme_svg, "y-tick")]
    assert min(extreme_ticks) < 0 < max(extreme_ticks)
    assert all(math.isfinite(value) for value in extreme_ticks)
    assert "-1.7e+308" in evidence_report.render_report(extreme)["trace.csv"].decode("utf-8")


def test_signal_coordinates_use_exact_differences_near_large_offsets() -> None:
    values = [
        1e10,
        1e10 + 0.000002,
        1e10 + 0.000004,
        1e10 + 0.000006,
    ]
    bundle = _bundle(raw=values, processed=values)
    svg = evidence_report.render_report(bundle)["chromatogram.svg"]
    actual_y = [float(y) for _, y in _path_points(svg, "raw")]

    exact_values = [Fraction.from_float(value) for value in values]
    low = min(exact_values)
    span = max(exact_values) - low
    expected_fraction = [float((value - low) / span) for value in exact_values]
    actual_fraction = [(448.0 - value) / 410.0 for value in actual_y]

    assert actual_fraction == pytest.approx(expected_fraction, abs=1e-12)
    assert actual_fraction[1] == pytest.approx(1 / 3, abs=1e-12)
    assert actual_fraction[2] == pytest.approx(2 / 3, abs=1e-12)


def test_conventional_ticks_cover_fractional_and_symmetric_domains() -> None:
    bundle = _bundle(
        axis=[0.0, 2.5, 5.0],
        raw=[-0.0109342, 2.231, 0.25],
        processed=[-0.0109342, 2.231, 0.25],
        peak={
            "peak_id": 1,
            "location": 2.5,
            "height": 2.231,
            "left_base": 0.0,
            "right_base": 5.0,
            "area": 0.0,
        },
    )
    svg = evidence_report.render_report(bundle)["chromatogram.svg"]

    assert _tick_labels(svg, "x-tick") == ["0", "1", "2", "3", "4", "5"]
    assert _tick_labels(svg, "y-tick") == ["0", "0.5", "1", "1.5", "2"]

    fractional = _bundle(
        axis=[0.0, 0.1, 0.2],
        raw=[-1.0, 0.0, 1.0],
        processed=[-1.0, 0.0, 1.0],
        peak={
            "peak_id": 1,
            "location": 0.1,
            "height": 1.0,
            "left_base": 0.0,
            "right_base": 0.2,
            "area": 0.0,
        },
    )
    fractional_svg = evidence_report.render_report(fractional)["chromatogram.svg"]
    assert _tick_labels(fractional_svg, "x-tick") == ["0", "0.05", "0.1", "0.15", "0.2"]
    assert _tick_labels(fractional_svg, "y-tick") == ["-1", "-0.5", "0", "0.5", "1"]


def test_near_ulp_domain_ticks_remain_bounded_and_finite() -> None:
    first = 1e308
    second = math.nextafter(first, math.inf)
    third = math.nextafter(second, math.inf)
    fourth = math.nextafter(third, math.inf)
    axis = [first, second, third, fourth]
    bundle = _bundle(
        axis=axis,
        raw=[0.0] * 4,
        processed=[0.0] * 4,
        peak={
            "peak_id": 1,
            "location": second,
            "height": 1.0,
            "left_base": first,
            "right_base": fourth,
            "area": 0.0,
        },
    )
    svg = evidence_report.render_report(bundle)["chromatogram.svg"]
    x_ticks = _tick_labels(svg, "x-tick")
    values = [float(label) for label in x_ticks]

    assert 1 <= len(values) <= 12
    assert all(math.isfinite(value) and first <= value <= fourth for value in values)
    assert len(set(x_ticks)) == len(x_ticks)


def test_raw_and_processed_curves_keep_their_own_axis_grids() -> None:
    bundle = _bundle(
        axis=[0.0, 0.3, 1.0, 2.0],
        processed_axis=[0.0, 0.30000000000000004, 1.25, 2.5],
        raw=[0.0, 1.0, 1.0, 0.0],
        processed=[0.0, 1.0, 1.0, 0.0],
        peak={
            "peak_id": 1,
            "location": 1.25,
            "height": 1.0,
            "left_base": 0.30000000000000004,
            "right_base": 2.5,
            "area": 1.5,
        },
    )
    artifacts = evidence_report.render_report(bundle)
    report = artifacts["report.html"].decode("utf-8")
    svg = artifacts["chromatogram.svg"]
    _, trace_rows = _csv_rows(artifacts["trace.csv"])
    raw_points = _path_points(svg, "raw")
    processed_points = _path_points(svg, "processed")

    assert trace_rows[1]["axis"] == "0.3"
    assert trace_rows[1]["processed_axis"] == "0.30000000000000004"
    assert raw_points[1][0] != processed_points[1][0]
    assert raw_points[2][0] != processed_points[2][0]
    assert "Raw and processed recorded axis grids differ" in report


def test_many_peak_annotations_share_one_axis_mapper(monkeypatch) -> None:
    axis = [float(index) for index in range(1000)]
    peaks = [
        {
            "peak_id": index + 1,
            "location": float(index + 1),
            "height": 1.0,
            "left_base": float(index),
            "right_base": float(index + 2),
            "area": 0.5,
        }
        for index in range(500)
    ]
    bundle = _bundle(axis=axis, raw=[0.0] * 1000, processed=[0.0] * 1000, peaks=peaks)
    original = evidence_report._axis_mapper
    calls = 0

    def counted_mapper(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(evidence_report, "_axis_mapper", counted_mapper)
    artifacts = evidence_report.render_report(bundle)

    assert calls == 1
    assert len(_path_commands(artifacts["chromatogram.svg"], "raw")) == 1000
    assert len(_path_commands(artifacts["chromatogram.svg"], "processed")) == 1000
    assert len(_csv_rows(artifacts["peaks.csv"])[1]) == 500


def test_text_csv_formula_guard_does_not_change_numeric_values() -> None:
    bundle = _bundle(
        analysis_id="=2+2",
        axis_unit="+axis",
        signal_unit="-signal",
        area_basis="\t=2+2",
    )
    artifacts = evidence_report.render_report(bundle)
    _, trace_rows = _csv_rows(artifacts["trace.csv"])
    _, peak_rows = _csv_rows(artifacts["peaks.csv"])

    assert trace_rows[0]["analysis_id"] == "'=2+2"
    assert trace_rows[0]["axis_unit"] == "'+axis"
    assert trace_rows[0]["signal_unit"] == "'-signal"
    assert trace_rows[1]["raw_signal"] == "-1.0"
    assert peak_rows[0]["area"] == "0.0"
    assert peak_rows[0]["area_basis"] == "'\t=2+2"


def test_recorded_observed_signal_area_basis_is_preserved() -> None:
    peak = {
        "peak_id": 1,
        "location": 1.51,
        "height": 1.97149,
        "left_base": 0.92,
        "right_base": 2.65,
        "area": 0.8489756075817861,
    }
    bundle = _bundle(peak=peak, area_basis="observed_signal")
    artifacts = evidence_report.render_report(bundle)
    report = artifacts["report.html"].decode("utf-8")
    document = json.loads(artifacts["evidence.json"])
    peak_fields, peak_rows = _csv_rows(artifacts["peaks.csv"])

    assert document["peaks"][0]["area_basis"] == "observed_signal"
    assert "observed_signal" in report
    assert "0.8489756075817861" in report
    assert peak_fields[-1] == "area_basis"
    assert peak_rows[0]["area"] == "0.8489756075817861"
    assert peak_rows[0]["area_basis"] == "observed_signal"


def test_write_report_refuses_existing_output_and_returns_file_digests(tmp_path: Path) -> None:
    bundle = _bundle()
    destination = tmp_path / "report"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        evidence_report.write_report(bundle, destination)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    linked_destination = tmp_path / "linked-report"
    linked_destination.symlink_to(destination, target_is_directory=True)
    with pytest.raises(FileExistsError):
        evidence_report.write_report(bundle, linked_destination)

    output = tmp_path / "new-report"
    written = evidence_report.write_report(bundle, output)
    assert set(written) == {
        "report.html",
        "chromatogram.svg",
        "evidence.json",
        "trace.csv",
        "peaks.csv",
    }
    for name, info in written.items():
        content = (output / name).read_bytes()
        assert info["path"] == str(output / name)
        assert info["sha256"] == hashlib.sha256(content).hexdigest()


def test_artifact_size_limit_is_checked_before_output_creation(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle()
    destination = tmp_path / "too-large"
    monkeypatch.setattr(evidence_report, "_MAX_ARTIFACT_BYTES", 1)

    with pytest.raises(ValueError, match="artifact limit"):
        evidence_report.write_report(bundle, destination)
    assert not destination.exists()
