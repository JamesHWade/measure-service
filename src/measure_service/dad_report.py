"""Render a sparse diode-array-detector plane as a portable report.

The DAD report is deliberately an offline renderer.  It validates the complete
``measure-dad-plane/1`` evidence document, writes that document unchanged, and
uses only the supplied point rows for its visualizations.  In particular, it
does not create a regular grid, interpolate missing cells, or apply a signal
processing method.
"""

from __future__ import annotations

import json
import math
import os
import re
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Mapping, Sequence

from .dad_plane import MAX_PLANE_BYTES, PROFILE, REGION_PROFILE, validate_range
from .portable_report import _axis_mapper, _csv_bytes, _e, _json, _ticks, write_report_files
from .storage import canonical, sha256

MAX_POINTS = 4_096
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
REPORT_FILENAMES = ("report.html", "evidence.json", "points.csv")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MD5 = re.compile(r"^[0-9a-f]{32}$")
_TOP_FIELDS = {
    "profile",
    "acquisition_id",
    "input_sha256",
    "array_id",
    "stream_id",
    "injection_id",
    "channel_id",
    "source_id",
    "time_axis",
    "time_unit",
    "wavelength_unit",
    "value_unit",
    "points",
    "provenance",
    "warnings",
    "evidence_sha256",
}
_REGION_FIELDS = {"selection", "native_point_count"}
_SELECTION_FIELDS = {"time_range", "wavelength_range"}
_POINT_FIELDS = {
    "point_index",
    "time",
    "wavelength",
    "value",
    "source_row",
    "saturated",
}
_PROVENANCE_FIELDS = {"worker_sha256", "packages", "installed_r_code_md5", "r_version"}


def _embedded_json(value: Any) -> str:
    """Serialize JSON safely inside a ``script`` element.

    Escaping ``<`` is important even though this is JSON: without it a hostile
    string containing ``</script>`` could terminate the data element before
    ``JSON.parse`` sees it.  The JavaScript reads this element with
    ``textContent`` and therefore receives the original values after parsing.
    """

    return (
        _json(value)
        .replace("&", r"\u0026")
        .replace("<", r"\u003c")
        .replace(">", r"\u003e")
        .replace("\u2028", r"\u2028")
        .replace("\u2029", r"\u2029")
    )


def _identifier(value: Any, field: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"DAD {field} must be a nonblank text identifier")


def _digest(value: Any, field: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"DAD {field} must be a lowercase SHA-256 value")


def _md5(value: Any, field: str) -> None:
    if type(value) is not str or _MD5.fullmatch(value) is None:
        raise ValueError(f"DAD {field} must be a lowercase MD5 value")


def _finite(value: Any, field: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"DAD {field} must be a finite number")


def _text(value: Any, field: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"DAD {field} must be nonblank text")


def _validate_provenance(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != _PROVENANCE_FIELDS:
        raise ValueError("DAD provenance has an unexpected shape")
    _digest(value["worker_sha256"], "provenance worker_sha256")
    _text(value["r_version"], "provenance r_version")
    for field in ("packages", "installed_r_code_md5"):
        entries = value[field]
        if not isinstance(entries, Mapping) or any(type(key) is not str for key in entries):
            raise ValueError(f"DAD provenance {field} must be a mapping")
        if field == "packages":
            for key, item in entries.items():
                if item is not None:
                    _text(item, f"provenance packages.{key}", nullable=True)
        else:
            for key, item in entries.items():
                if item is not None:
                    _md5(item, f"provenance installed_r_code_md5.{key}")


def _validate_point(point: Any) -> None:
    if not isinstance(point, Mapping) or set(point) != _POINT_FIELDS:
        raise ValueError("DAD point row has an unexpected shape")
    if type(point["point_index"]) is not int or point["point_index"] < 1:
        raise ValueError("DAD point_index values must be positive one-based integers")
    _finite(point["time"], "point time")
    _finite(point["wavelength"], "point wavelength")
    _finite(point["value"], "point value", nullable=True)
    if point["source_row"] is not None and (
        type(point["source_row"]) is not int or point["source_row"] < 1
    ):
        raise ValueError("DAD source_row must be a positive integer or null")
    if point["saturated"] is not None and type(point["saturated"]) is not bool:
        raise ValueError("DAD saturated must be true, false, or null")


def _validate_plane(plane: Mapping[str, Any]) -> None:
    if not isinstance(plane, Mapping):
        raise ValueError("DAD plane has an unexpected shape")
    profile = plane.get("profile")
    if profile not in (PROFILE, REGION_PROFILE):
        raise ValueError("unsupported DAD plane evidence profile")
    expected_fields = _TOP_FIELDS | (_REGION_FIELDS if profile == REGION_PROFILE else set())
    if set(plane) != expected_fields:
        raise ValueError("DAD plane has an unexpected shape")
    for field in (
        "acquisition_id",
        "array_id",
        "stream_id",
        "injection_id",
        "channel_id",
    ):
        _identifier(plane[field], field)
    _identifier(plane["source_id"], "source_id", nullable=True)
    _digest(plane["input_sha256"], "input_sha256")
    _digest(plane["evidence_sha256"], "evidence_sha256")
    if type(plane["time_axis"]) is not int or plane["time_axis"] not in {1, 2}:
        raise ValueError("DAD time_axis must be 1 or 2")
    _text(plane["time_unit"], "time_unit")
    _text(plane["wavelength_unit"], "wavelength_unit")
    _text(plane["value_unit"], "value_unit", nullable=True)
    points = plane["points"]
    if type(points) is not list or len(points) > MAX_POINTS:
        raise ValueError(f"DAD points must contain at most {MAX_POINTS} rows")
    region = profile == REGION_PROFILE
    time_range = wavelength_range = None
    if region:
        selection_value = plane["selection"]
        if not isinstance(selection_value, Mapping) or set(selection_value) != _SELECTION_FIELDS:
            raise ValueError("DAD selection has an unexpected shape")
        time_range = validate_range(selection_value["time_range"])
        wavelength_range = validate_range(selection_value["wavelength_range"], wavelength=True)
        if time_range is None and wavelength_range is None:
            raise ValueError("DAD region selection must include a time or wavelength range")
        if type(plane["native_point_count"]) is not int or plane["native_point_count"] < 0:
            raise ValueError("DAD native_point_count must be a nonnegative integer")
        if len(points) > plane["native_point_count"]:
            raise ValueError("DAD native_point_count cannot be smaller than selected points")
    coordinates = set()
    previous_index = 0
    for index, point in enumerate(points, start=1):
        _validate_point(point)
        if region:
            if point["point_index"] <= previous_index:
                raise ValueError("DAD selected point indices must be strictly increasing")
            if point["point_index"] > plane["native_point_count"]:
                raise ValueError("DAD selected point index exceeds native_point_count")
            previous_index = point["point_index"]
        elif point["point_index"] != index:
            raise ValueError("DAD point indices must be contiguous and one-based")
        if point["wavelength"] <= 0:
            raise ValueError("DAD wavelengths must be positive")
        if time_range is not None and not time_range[0] <= point["time"] <= time_range[1]:
            raise ValueError("DAD point time falls outside the selected region")
        if (
            wavelength_range is not None
            and not wavelength_range[0] <= point["wavelength"] <= wavelength_range[1]
        ):
            raise ValueError("DAD point wavelength falls outside the selected region")
        coordinate = (point["time"], point["wavelength"])
        if coordinate in coordinates:
            raise ValueError("DAD time and wavelength coordinate pairs must be unique")
        coordinates.add(coordinate)
    warnings = plane["warnings"]
    if type(warnings) is not list or not all(type(item) is str for item in warnings):
        raise ValueError("DAD warnings must be a list of text")
    _validate_provenance(plane["provenance"])
    try:
        serialized = canonical(dict(plane)).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as error:
        raise ValueError("DAD plane is not valid finite JSON") from error
    if len(serialized) > MAX_PLANE_BYTES:
        raise ValueError("DAD evidence exceeds the portable document limit")
    without_digest = {key: value for key, value in plane.items() if key != "evidence_sha256"}
    if sha256(without_digest) != plane["evidence_sha256"]:
        raise ValueError("DAD evidence digest does not match the complete document")


def validate_dad_plane(plane: Mapping[str, Any]) -> None:
    """Validate a complete detector plane or region evidence document."""

    _validate_plane(plane)


def _is_region(plane: Mapping[str, Any]) -> bool:
    return plane["profile"] == REGION_PROFILE


def _selection_summary(plane: Mapping[str, Any]) -> str:
    if not _is_region(plane):
        return ""
    selection = plane["selection"]
    parts = []
    if selection["time_range"] is not None:
        low, high = selection["time_range"]
        parts.append(f"time {_number(low)}–{_number(high)} {plane['time_unit']}")
    if selection["wavelength_range"] is not None:
        low, high = selection["wavelength_range"]
        parts.append(f"wavelength {_number(low)}–{_number(high)} {plane['wavelength_unit']}")
    return (
        f"Selected region ({', '.join(parts)}), with {len(plane['points'])} observed rows "
        f"from {plane['native_point_count']} native points."
    )


def _number(value: Any) -> str:
    return "—" if value is None else _json(value)


def _point_state(point: Mapping[str, Any]) -> str:
    if point["saturated"] is True:
        return "saturated"
    if point["value"] is None:
        return "missing value"
    return "observed"


def _value_bounds(points: Sequence[Mapping[str, Any]]) -> tuple[float, float] | None:
    values = [float(point["value"]) for point in points if point["value"] is not None]
    if not values:
        return None
    return min(values), max(values)


def _signal_color(value: float, bounds: tuple[float, float] | None) -> str:
    """Map one supplied finite value to a deterministic sequential palette."""

    if bounds is None:
        return "#6f9ca6"
    low, high = bounds
    if low == high:
        fraction = 0.5
    else:
        with localcontext() as context:
            context.prec = 120
            fraction = float(
                (Decimal.from_float(value) - Decimal.from_float(low))
                / (Decimal.from_float(high) - Decimal.from_float(low))
            )
        fraction = min(1.0, max(0.0, fraction))
    # A blue-to-gold sequential palette remains readable for negative values;
    # the exact numeric value remains available in the point tooltip/table.
    low_rgb, high_rgb = (36, 87, 166), (232, 163, 47)
    channels = [
        round(start + fraction * (finish - start))
        for start, finish in zip(low_rgb, high_rgb, strict=True)
    ]
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def _mark_radius(point_count: int) -> float:
    """Keep dense point clouds legible without making sparse marks tiny."""

    if point_count == 0:
        return 4.0
    return max(2.25, min(7.0, 180.0 / math.sqrt(point_count)))


def _heatmap_svg(plane: Mapping[str, Any]) -> bytes:
    points = plane["points"]
    times = [float(point["time"]) for point in points]
    wavelengths = [float(point["wavelength"]) for point in points]
    value_bounds = _value_bounds(points)
    mark_radius = _mark_radius(len(points))
    width, height = 1_100, 560
    left, right, top, bottom = 96.0, 32.0, 46.0, 82.0
    plot_width, plot_height = width - left - right, height - top - bottom
    time_x = _axis_mapper(times, left, plot_width)
    wavelength_y = _axis_mapper(wavelengths, top + plot_height, -plot_height)
    ticks_x, ticks_y = _ticks(times, digits=10), _ticks(wavelengths, digits=10)
    grid: list[str] = []
    for value, _ in ticks_x:
        label = format(float(value), ".8g")
        coordinate = time_x(float(value))
        coordinate_text = format(coordinate, ".17g")
        grid.extend(
            [
                f'<line class="grid" x1="{coordinate_text}" y1="{top}" x2="{coordinate_text}" y2="{top + plot_height}" />',
                f'<text class="tick" x="{coordinate_text}" y="{height - bottom + 24:.3f}" text-anchor="middle">{_e(label)}</text>',
            ]
        )
    for value, _ in ticks_y:
        label = format(float(value), ".8g")
        coordinate = wavelength_y(float(value))
        coordinate_text = format(coordinate, ".17g")
        grid.extend(
            [
                f'<line class="grid" x1="{left}" y1="{coordinate_text}" x2="{left + plot_width}" y2="{coordinate_text}" />',
                f'<text class="tick" x="{left - 10}" y="{format(coordinate + Decimal(4), ".17g")}" text-anchor="end">{_e(label)}</text>',
            ]
        )
    marks: list[str] = []
    for point in points:
        state = _point_state(point)
        classes = ["heatmap-mark", "missing-value" if point["value"] is None else "observed"]
        if point["saturated"] is True:
            classes.append("saturated")
        state_class = state.replace(" ", "-")
        if state_class not in classes:
            classes.append(state_class)
        label = (
            f"point {point['point_index']}, time {_number(point['time'])}, "
            f"wavelength {_number(point['wavelength'])}, "
            f"value {_number(point['value'])}; state {state}"
        )
        x_coordinate = format(time_x(float(point["time"])), ".17g")
        y_coordinate = format(wavelength_y(float(point["wavelength"])), ".17g")
        color_style = (
            f' style="--point-color:{_signal_color(float(point["value"]), value_bounds)}"'
            if point["value"] is not None
            else ""
        )
        marks.append(
            f'<a class="{" ".join(classes)}" data-point-index="{point["point_index"]}" '
            f'href="#point-{point["point_index"]}" tabindex="0" role="button" aria-label="{_e(label)}">'
            f'<circle cx="{x_coordinate}" cy="{y_coordinate}" r="{mark_radius:.3g}"{color_style}><title>{_e(label)}</title></circle></a>'
        )
    empty = (
        '<text class="empty" x="50%" y="50%" text-anchor="middle">No observed point rows</text>'
        if not points
        else ""
    )
    title = "DAD sparse heatmap"
    description = (
        "Observed DAD point rows positioned at their supplied time and wavelength coordinates. "
        "No regular grid or interpolation is drawn."
    )
    scale = ""
    if value_bounds is not None:
        low_value, high_value = value_bounds
        scale = f'''<defs><linearGradient id="dad-value-gradient" x1="0%" x2="100%" y1="0%" y2="0%"><stop offset="0%" stop-color="{_signal_color(low_value, value_bounds)}" /><stop offset="100%" stop-color="{_signal_color(high_value, value_bounds)}" /></linearGradient></defs>
<rect class="value-scale" x="{width - 250}" y="{height - bottom + 29}" width="150" height="12" fill="url(#dad-value-gradient)" /><text class="scale-label" x="{width - 250}" y="{height - bottom + 54}">Low {low_value:.4g}</text><text class="scale-label" x="{width - 100}" y="{height - bottom + 54}" text-anchor="end">High {high_value:.4g}</text>'''
    svg = f'''<svg id="dad-heatmap" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="group" aria-labelledby="heatmap-title heatmap-desc">
<title id="heatmap-title">{_e(title)}</title><desc id="heatmap-desc">{_e(description)}</desc>
<style>text {{ font: 13px system-ui,sans-serif; fill: #233436 }} .axis {{ stroke: #617277; stroke-width: 1 }} .grid {{ stroke: #dce5e2; stroke-width: 1 }} .tick,.scale-label {{ font-size: 11px }} .heatmap-mark {{ cursor: pointer }} .heatmap-mark circle {{ stroke: #fff; stroke-width: 1.2 }} .heatmap-mark.observed circle {{ fill: var(--point-color, #6f9ca6) }} .heatmap-mark.missing-value circle {{ fill: #d97706 }} .heatmap-mark.saturated circle {{ stroke: #b91c1c; stroke-width: 2.5 }} .heatmap-mark:focus circle, .heatmap-mark.selected circle {{ stroke: #111827; stroke-width: 3 }} .empty {{ fill: #5b6b6c }}</style>
<rect width="100%" height="100%" fill="#fff" />{"".join(grid)}
<line class="axis" x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" /><line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" />
{"".join(marks)}{empty}{scale}
<text x="{left + plot_width / 2:.3f}" y="{height - 20}" text-anchor="middle">Time ({_e(plane["time_unit"])})</text>
<text x="18" y="{top + plot_height / 2:.3f}" transform="rotate(-90 18 {top + plot_height / 2:.3f})" text-anchor="middle">Wavelength ({_e(plane["wavelength_unit"])})</text>
</svg>'''
    return svg.encode("utf-8")


def _metadata_rows(plane: Mapping[str, Any]) -> str:
    rows = [
        ("Profile", plane["profile"]),
        ("Acquisition ID", plane["acquisition_id"]),
        ("Array ID", plane["array_id"]),
        ("Stream ID", plane["stream_id"]),
        ("Injection ID", plane["injection_id"]),
        ("Channel ID", plane["channel_id"]),
        ("Source ID", plane["source_id"]),
        ("Declared time axis", plane["time_axis"]),
        ("Time unit", plane["time_unit"]),
        ("Wavelength unit", plane["wavelength_unit"]),
        ("Value unit", plane["value_unit"]),
        ("Observed point rows", len(plane["points"])),
        *(
            [
                ("Native point rows", plane["native_point_count"]),
                ("Selected time range", plane["selection"]["time_range"]),
                ("Selected wavelength range", plane["selection"]["wavelength_range"]),
            ]
            if _is_region(plane)
            else []
        ),
        ("Input SHA-256", plane["input_sha256"]),
        ("Evidence SHA-256", plane["evidence_sha256"]),
    ]
    return "".join(
        f'<tr><th scope="row">{_e(label)}</th><td>{_e(value)}</td></tr>' for label, value in rows
    )


def _point_table(plane: Mapping[str, Any]) -> str:
    headers = (
        "Point",
        f"Time ({plane['time_unit']})",
        f"Wavelength ({plane['wavelength_unit']})",
        f"Value ({plane['value_unit'] or 'unit not declared'})",
        "Source row",
        "Saturated",
        "State",
    )
    header_html = "".join(f'<th scope="col">{_e(header)}</th>' for header in headers)
    rows: list[str] = []
    for point in plane["points"]:
        value_text = _number(point["value"])
        cells = (
            point["point_index"],
            _number(point["time"]),
            _number(point["wavelength"]),
            value_text,
            point["source_row"],
            "Not declared" if point["saturated"] is None else point["saturated"],
            _point_state(point),
        )
        rows.append(
            f'<tr id="point-{point["point_index"]}" data-point-index="{point["point_index"]}" tabindex="0">'
            + "".join(f"<td>{_e(cell)}</td>" for cell in cells)
            + "</tr>"
        )
    if not rows:
        rows.append(f'<tr><td colspan="{len(headers)}">No observed point rows.</td></tr>')
    return (
        f'<table id="dad-points"><thead><tr>{header_html}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _value_scale_note(plane: Mapping[str, Any]) -> str:
    bounds = _value_bounds(plane["points"])
    unit = plane["value_unit"] or "unit not declared"
    if bounds is None:
        return (
            "No finite values were supplied, so the numeric color scale is unavailable; "
            f"missing markers remain explicit ({_e(unit)})."
        )
    low, high = bounds
    if low == high:
        range_text = f"constant at {_e(_number(low))}"
    else:
        range_text = f"from {_e(_number(low))} (low) to {_e(_number(high))} (high)"
    return f"Signal {range_text} {_e(unit)}."


def _html(plane: Mapping[str, Any], heatmap: bytes) -> bytes:
    unit = plane["value_unit"] or "unit not declared"
    region = _is_region(plane)
    scope = "windowed detector response" if region else "detector response"
    selection_summary = _selection_summary(plane)
    selection_html = (
        f'<p class="selection-summary">{_e(selection_summary)}</p>' if selection_summary else ""
    )
    warning_html = (
        '<section class="warning"><h2>Worker warnings</h2><ul>'
        + "".join(f"<li>{_e(warning)}</li>" for warning in plane["warnings"])
        + "</ul></section>"
        if plane["warnings"]
        else ""
    )
    links = " | ".join(
        f'<a href="{_e(name)}">{_e(name)}</a>' for name in REPORT_FILENAMES if name != "report.html"
    )
    embedded = _embedded_json(plane)
    provenance_json = _e(_json(plane["provenance"], pretty=True))
    script = Path(__file__).with_suffix(".js").read_text(encoding="utf-8")
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DAD plane · {_e(plane["array_id"])}</title>
<style>
* {{ box-sizing:border-box }} body {{ margin:0; background:#faf9f6; color:#182e31; font:16px/1.5 system-ui,sans-serif }}
main {{ max-width:76rem; margin:auto; padding:clamp(1rem,4vw,3.25rem) }} h1,h2 {{ font-family:Georgia,serif; font-weight:normal; line-height:1.15 }}
h1 {{ font-size:clamp(2rem,5vw,3.7rem); margin:.2em 0 }} h2 {{ font-size:1.65rem; margin:0 0 .55em }}
section, details {{ padding:1.3rem 0; border-top:1px solid #b5c6c3 }} header p {{ margin:.3rem 0 1.4rem }}
.skip-map {{ position:absolute; clip-path:inset(50%); width:1px; height:1px; overflow:hidden }} .skip-map:focus {{ position:static; clip-path:none; width:auto; height:auto }}
.figure {{ background:white; border:1px solid #b5c6c3; border-radius:6px; padding:.5rem }} svg {{ width:100%; height:auto; display:block }}
a {{ color:#005951; text-underline-offset:3px }} :focus-visible {{ outline:3px solid #bd5c14; outline-offset:2px }}
.table-scroll {{ overflow:auto }} table {{ border-collapse:collapse; min-width:100%; font-variant-numeric:tabular-nums }}
th,td {{ text-align:left; vertical-align:top; padding:.48rem .7rem; border-bottom:1px solid #ced9d5 }} th {{ font-weight:600 }} thead {{ background:#e9efec }}
tbody tr[data-point-index] {{ cursor:pointer }} tbody tr[data-point-index]:hover, tbody tr[data-point-index]:focus {{ background:#fff0c2 }}
code,pre,td {{ overflow-wrap:anywhere }} pre {{ white-space:pre-wrap }} .downloads a {{ margin-right:.5rem }}
.warning {{ background:#fff5df; border:1px solid #d19a46; border-radius:6px; padding:1rem }}
.legend {{ display:flex; flex-wrap:wrap; gap:.8rem 1.2rem; margin:.7rem 0 0 }} .legend span::before {{ content:""; display:inline-block; width:.8rem; height:.8rem; margin-right:.3rem; border-radius:50%; vertical-align:middle }}
.legend .observed::before {{ background:#6f9ca6 }} .legend .missing::before {{ background:#d97706 }} .legend .saturated::before {{ background:transparent; border:2px solid #b91c1c }}
.selection {{ background:#eef5f1; border:1px solid #b5c6c3; border-radius:6px; padding:1rem }} dl {{ display:grid; grid-template-columns:max-content minmax(0,1fr); gap:.25rem 1rem; margin:0 }} dt {{ font-weight:600 }} dd {{ margin:0; overflow-wrap:anywhere }}
form {{ display:flex; flex-wrap:wrap; align-items:end; gap:.75rem; margin:.8rem 0 }} label {{ display:flex; flex-direction:column; gap:.25rem; font-weight:600 }} input {{ min-width:11rem; padding:.4rem; font:inherit }} button {{ padding:.45rem .8rem; font:inherit; cursor:pointer }}
.chart-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:1rem }} .chart {{ min-width:0; background:white; border:1px solid #b5c6c3; border-radius:6px; padding:.3rem }}
.chart .axis {{ stroke:#617277; stroke-width:1 }} .chart .tick-mark {{ stroke:#617277; stroke-width:1 }} .chart .tick-label,.chart .null-label {{ font-size:13px; fill:#425557 }} .chart circle {{ cursor:pointer; stroke:#fff; stroke-width:1 }} .chart circle:focus {{ stroke:#161f20; stroke-width:2.5 }} .chart circle.observed {{ fill:#147d73 }} .chart circle.missing {{ fill:#d97706 }} .chart circle.saturated {{ fill:#b91c1c }}
@media (max-width: 760px) {{ .chart-grid {{ grid-template-columns:1fr }} dl {{ grid-template-columns:1fr }} input {{ min-width:0; width:100% }} form label {{ flex:1 1 12rem }} button {{ flex:1 0 auto }} }}
</style></head><body><main>
<header><h1>Diode-array detector</h1><p>Array <strong>{_e(plane["array_id"])}</strong> · stream {_e(plane["stream_id"])} · {_e(len(plane["points"]))} observed rows</p><p>Detector values as supplied by the acquisition.</p>{selection_html}</header>
<section><h2>{_e(scope.title())}</h2><a class="skip-map" href="#inspect-time">Enter a coordinate</a><div class="figure">{heatmap.decode("utf-8")}</div>
<div class="legend" aria-label="Point state legend"><span class="observed">Observed value</span><span class="missing">Observed coordinate with unavailable value</span><span class="saturated">Saturation declared</span></div>
<p class="value-scale-note">{_value_scale_note(plane)}</p>
<p>Select a measured point to inspect its source row and both slices. Blank coordinates have no observation.{" The displayed points are restricted to the selected inclusive bounds." if region else ""}</p></section>
<section><h2>Inspect a coordinate</h2><form id="inspect-form"><label for="inspect-time">Time ({_e(plane["time_unit"])})<input id="inspect-time" name="time" type="number" step="any" required></label><label for="inspect-wavelength">Wavelength ({_e(plane["wavelength_unit"])})<input id="inspect-wavelength" name="wavelength" type="number" step="any" required></label><button type="submit">Inspect exact coordinate</button></form><p id="inspect-message" aria-live="polite">Select a heatmap mark or enter an exact coordinate.</p>
<div class="selection" aria-live="polite"><dl><dt>Coordinate state</dt><dd id="selected-state">No coordinate selected</dd><dt>Point index</dt><dd id="selected-point">—</dd><dt>Time</dt><dd id="selected-time">—</dd><dt>Wavelength</dt><dd id="selected-wavelength">—</dd><dt>Value ({_e(unit)})</dt><dd id="selected-value">—</dd><dt>Source row</dt><dd id="selected-source-row">—</dd><dt>Saturated</dt><dd id="selected-saturated">—</dd></dl></div></section>
<section><h2>Linked traces</h2><p>Each dot is a supplied reading at the selected wavelength or time.{" These traces are windowed by the selected bounds." if region else ""}</p><div class="chart-grid"><div><h3 id="chrom-heading">Chromatogram</h3><div class="chart"><svg id="wavelength-chart" role="group" aria-label="Per-wavelength chromatogram"></svg></div></div><div><h3 id="spec-heading">Spectrum</h3><div class="chart"><svg id="time-chart" role="group" aria-label="Per-time spectrum"></svg></div></div></div></section>
<details><summary>Complete observed rows ({_e(len(plane["points"]))})</summary><div class="table-scroll" tabindex="0" role="region" aria-label="Complete DAD point rows">{_point_table(plane)}</div></details>
{warning_html}
<details><summary>Acquisition and provenance</summary><table><tbody>{_metadata_rows(plane)}</tbody></table><h3>Worker provenance</h3><pre>{provenance_json}</pre></details>
<section class="downloads"><h2>Downloads</h2><p>{links}</p></section></main>
<script type="application/json" id="dad-data">{embedded}</script><script>{script}</script></body></html>"""
    return document.encode("utf-8")


def render_dad_report(plane: Mapping[str, Any]) -> dict[str, bytes]:
    """Render validated DAD evidence as HTML, CSV and JSON."""

    _validate_plane(plane)
    heatmap = _heatmap_svg(plane)
    point_fields = ("point_index", "time", "wavelength", "value", "source_row", "saturated")
    artifacts = {
        "report.html": _html(plane, heatmap),
        "evidence.json": (_json(plane, pretty=True) + "\n").encode("utf-8"),
        "points.csv": _csv_bytes(point_fields, plane["points"]),
    }
    return artifacts


def write_dad_report(
    plane: Mapping[str, Any], output_dir: str | os.PathLike[str]
) -> dict[str, dict[str, str]]:
    """Write a DAD report into a fresh directory without replacing files."""

    return write_report_files(render_dad_report(plane), output_dir)


def read_dad_evidence(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and validate ``evidence.json`` without an MCP or R session."""

    candidate = Path(path)
    if candidate.is_dir():
        candidate = candidate / "evidence.json"
    if candidate.is_symlink() or not candidate.is_file():
        raise FileNotFoundError(f"DAD evidence file does not exist: {candidate}")
    try:
        with candidate.open("rb") as stream:
            encoded = stream.read(MAX_EVIDENCE_BYTES + 1)
        if len(encoded) > MAX_EVIDENCE_BYTES:
            raise ValueError("DAD evidence JSON exceeds the portable document limit")
        value = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("DAD evidence JSON could not be read") from error
    if not isinstance(value, Mapping):
        raise ValueError("DAD evidence JSON must contain an object")
    _validate_plane(value)
    return dict(value)


__all__ = [
    "MAX_EVIDENCE_BYTES",
    "MAX_POINTS",
    "PROFILE",
    "REPORT_FILENAMES",
    "read_dad_evidence",
    "render_dad_report",
    "validate_dad_plane",
    "write_dad_report",
]
