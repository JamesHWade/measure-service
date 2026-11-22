"""Offline sparse DAD-plane report contract tests."""

import csv
import json

import pytest

from measure_service.dad_report import (
    MAX_POINTS,
    REGION_PROFILE,
    _heatmap_svg,
    read_dad_evidence,
    render_dad_report,
    validate_dad_plane,
    write_dad_report,
)
from measure_service.storage import sha256


def synthetic_plane():
    plane = {
        "profile": "measure-dad-plane/1",
        "acquisition_id": "acquisition-1",
        "input_sha256": "a" * 64,
        "array_id": 'array-<img src=x onerror="alert(1)">',
        "stream_id": "dad-stream",
        "injection_id": "injection-1",
        "channel_id": "dad-1",
        "source_id": None,
        "time_axis": 1,
        "time_unit": "s",
        "wavelength_unit": "nm",
        "value_unit": "AU",
        "points": [
            {
                "point_index": 1,
                "time": 0.12345678901234566,
                "wavelength": 200.0,
                "value": 1.25,
                "source_row": 11,
                "saturated": False,
            },
            {
                "point_index": 2,
                "time": 1.5,
                "wavelength": 210.0,
                "value": None,
                "source_row": None,
                "saturated": True,
            },
            {
                "point_index": 3,
                "time": 2.5,
                "wavelength": 200.0,
                "value": 0.0,
                "source_row": 13,
                "saturated": None,
            },
        ],
        "provenance": {
            "worker_sha256": "b" * 64,
            "packages": {"measure": "0.9.0.9000", "Matrix": None},
            "installed_r_code_md5": {"measure": "c" * 32},
            "r_version": "4.6.1",
        },
        "warnings": ['worker warning: </script><img src=x onerror="alert(1)">'],
    }
    plane["evidence_sha256"] = sha256(plane)
    return plane


def synthetic_region():
    plane = synthetic_plane()
    plane["profile"] = REGION_PROFILE
    plane["selection"] = {"time_range": [0.0, 3.0], "wavelength_range": None}
    plane["native_point_count"] = 7
    plane["points"] = [plane["points"][0], plane["points"][2]]
    plane["evidence_sha256"] = sha256(
        {key: value for key, value in plane.items() if key != "evidence_sha256"}
    )
    return plane


def test_validates_honest_digest_and_numeric_precision():
    plane = synthetic_plane()
    validate_dad_plane(plane)
    evidence = json.loads(render_dad_report(plane)["evidence.json"])
    assert evidence == plane
    assert evidence["points"][0]["time"] == plane["points"][0]["time"]
    assert evidence["evidence_sha256"] == sha256(
        {key: value for key, value in plane.items() if key != "evidence_sha256"}
    )


def test_region_preserves_native_indices_and_declares_selection():
    plane = synthetic_region()
    validate_dad_plane(plane)
    artifacts = render_dad_report(plane)
    html = artifacts["report.html"].decode("utf-8")
    assert 'data-point-index="1"' in html
    assert 'data-point-index="3"' in html
    assert 'id="point-3"' in html
    assert "Selected region (time 0.0–3.0 s)" in html
    assert "Windowed chromatogram" in html
    assert "native points" in html
    assert json.loads(artifacts["evidence.json"])["points"][1]["point_index"] == 3


def test_region_can_be_empty_and_reports_inside_vs_outside_window():
    plane = synthetic_region()
    plane["points"] = []
    plane["selection"] = {"time_range": [10.0, 12.0], "wavelength_range": None}
    plane["evidence_sha256"] = sha256(
        {key: value for key, value in plane.items() if key != "evidence_sha256"}
    )
    validate_dad_plane(plane)
    html = render_dad_report(plane)["report.html"].decode("utf-8")
    assert "Selected region (time 10.0–12.0 s)" in html
    assert "Outside selected window" in html
    assert "Absent from selected window" in html


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"selection": {"time_range": None, "wavelength_range": None}}, "must include"),
        ({"selection": {"time_range": [3.0, 1.0], "wavelength_range": None}}, "ordered"),
        (
            {
                "selection": {"time_range": [0.0, 3.0], "wavelength_range": None},
                "native_point_count": 2,
            },
            "native_point_count",
        ),
        ({"selection": {"time_range": [0.0, 1.0], "wavelength_range": None}}, "outside"),
    ],
)
def test_region_validation_rejects_ambiguous_or_inconsistent_selection(change, message):
    plane = synthetic_region()
    plane.update(change)
    plane["evidence_sha256"] = sha256(
        {key: value for key, value in plane.items() if key != "evidence_sha256"}
    )
    with pytest.raises(ValueError, match=message):
        validate_dad_plane(plane)


def test_render_is_offline_linked_and_escapes_markup():
    html = render_dad_report(synthetic_plane())["report.html"].decode("utf-8")
    assert "Absent from sparse plane" in html
    assert "per-wavelength chromatogram" in html.lower()
    assert "per-time spectrum" in html.lower()
    assert "Detector values as supplied" in html
    assert 'href="evidence.json"' in html
    assert 'href="points.csv"' in html
    assert 'href="http' not in html.lower()
    assert "<img" not in html.lower()
    assert r"\u003c/script\u003e" in html
    assert "Blank coordinates have no observation" in html
    assert "outside numeric scale" in html
    assert "function ticks(low, high)" in html
    assert "Complete observed rows (3)" in html
    assert "scrollIntoView" not in html


def test_heatmap_colors_numeric_range_and_keeps_saturation_marker():
    plane = synthetic_plane()
    plane["points"][0]["value"] = -2.0
    plane["points"][1]["value"] = 4.0
    plane["points"][1]["saturated"] = True
    plane["evidence_sha256"] = sha256(
        {key: value for key, value in plane.items() if key != "evidence_sha256"}
    )
    svg = _heatmap_svg(plane).decode("utf-8")
    assert "Low -2" in svg and "High 4" in svg
    assert 'style="--point-color:#2457a6"' in svg
    assert 'style="--point-color:#e8a32f"' in svg
    assert 'class="heatmap-mark observed saturated"' in svg
    assert "stroke: #b91c1c" in svg

    plane["points"][0]["value"] = -2.0
    plane["points"][1]["value"] = -2.0
    plane["points"][2]["value"] = -2.0
    plane["evidence_sha256"] = sha256(
        {key: value for key, value in plane.items() if key != "evidence_sha256"}
    )
    constant_svg = _heatmap_svg(plane).decode("utf-8")
    assert 'style="--point-color:#867d6a"' in constant_svg
    assert constant_svg.count('style="--point-color:#867d6a"') >= 2


def test_heatmap_coordinates_remain_finite_for_extreme_numeric_axes():
    plane = synthetic_plane()
    plane["points"] = [
        {**plane["points"][0], "point_index": 1, "time": -1e308, "wavelength": 1e-308},
        {**plane["points"][1], "point_index": 2, "time": 1e308, "wavelength": 1e308},
    ]
    plane["evidence_sha256"] = sha256(
        {key: value for key, value in plane.items() if key != "evidence_sha256"}
    )
    svg = _heatmap_svg(plane).decode("utf-8")
    assert "Infinity" not in svg
    assert "NaN" not in svg
    assert svg.count("<circle") == 2
    assert "normalized(value, low, high)" in render_dad_report(plane)["report.html"].decode()


def test_write_read_and_inspect_sparse_rows(tmp_path):
    output = tmp_path / "dad-report"
    result = write_dad_report(synthetic_plane(), output)
    assert set(result) == {"report.html", "evidence.json", "points.csv"}
    assert read_dad_evidence(output) == synthetic_plane()
    with output.joinpath("points.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[1]["value"] == ""
    assert rows[1]["source_row"] == ""
    assert rows[2]["value"] == "0.0"
    with pytest.raises(FileExistsError):
        write_dad_report(synthetic_plane(), output)


def test_digest_and_point_limit_are_checked():
    bad = synthetic_plane()
    bad["points"][0]["value"] = 999.0
    with pytest.raises(ValueError, match="digest"):
        render_dad_report(bad)

    too_many = synthetic_plane()
    first = too_many["points"][0]
    too_many["points"] = [
        {
            **first,
            "point_index": index,
            "time": float(index),
            "source_row": index,
        }
        for index in range(1, MAX_POINTS + 2)
    ]
    too_many["evidence_sha256"] = sha256(
        {key: value for key, value in too_many.items() if key != "evidence_sha256"}
    )
    with pytest.raises(ValueError, match="at most"):
        render_dad_report(too_many)


@pytest.mark.parametrize(
    "field,value,pattern",
    [
        ("time_axis", 3, "time_axis"),
        ("time_unit", "", "time_unit"),
        ("source_id", 4, "source_id"),
    ],
)
def test_invalid_declared_fields_fail_closed(field, value, pattern):
    plane = synthetic_plane()
    plane[field] = value
    plane["evidence_sha256"] = sha256(
        {key: item for key, item in plane.items() if key != "evidence_sha256"}
    )
    with pytest.raises(ValueError, match=pattern):
        validate_dad_plane(plane)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({"wavelength": 0}, "positive"),
        ({}, "unique"),
    ],
)
def test_replay_checks_coordinates_even_with_a_valid_digest(replacement, message):
    plane = synthetic_plane()
    plane["points"][1] = {**plane["points"][0], "point_index": 2, **replacement}
    plane["evidence_sha256"] = sha256(
        {key: value for key, value in plane.items() if key != "evidence_sha256"}
    )
    with pytest.raises(ValueError, match=message):
        validate_dad_plane(plane)
