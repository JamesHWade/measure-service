"""Render a validated chromatogram evidence bundle as local, portable files."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from pathlib import Path
from typing import Any, Callable

from .analysis_evidence import validate_evidence

_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_FILENAMES = ("report.html", "chromatogram.svg", "evidence.json", "trace.csv", "peaks.csv")
_PEAK_REQUIRED_FIELDS = (
    "peak_id",
    "location",
    "height",
    "left_base",
    "right_base",
    "area",
)
_PEAK_OPTIONAL_FIELDS = ("prominence",)
_TRACE_FIELDS = (
    "analysis_id",
    "evidence_sha256",
    "axis_unit",
    "signal_unit",
    "sample_index",
    "axis",
    "processed_axis",
    "raw_signal",
    "processed_signal",
)


def _json(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _display(value: Any) -> str:
    if value is None:
        return "Unavailable"
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return _json(value)
    return _json(value)


def _e(value: Any) -> str:
    return html.escape(_xml_safe_text(_display(value)), quote=True)


def _xml_safe_text(value: str) -> str:
    return "".join(_xml10_char(char) for char in value)


def _xml10_char(char: str) -> str:
    codepoint = ord(char)
    allowed = (
        codepoint in {0x09, 0x0A, 0x0D}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )
    if allowed:
        return char
    if codepoint <= 0xFFFF:
        return f"\\u{codepoint:04x}"
    return f"\\U{codepoint:08x}"


def _number(value: Any) -> str:
    if value is None:
        return ""
    return _json(value)


def _csv_text(value: Any) -> str:
    """Protect spreadsheet-imported text while keeping numeric evidence numeric."""
    if value is None:
        return ""
    if isinstance(value, str):
        value = _xml_safe_text(value)
        first = value.lstrip(" \t\r\n\v\f\x00\ufeff")[:1]
        return "'" + value if first in {"=", "+", "-", "@"} else value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _number(value)
    return _json(value)


def _csv_value(name: str, value: Any) -> str:
    if name in {
        "analysis_id",
        "evidence_sha256",
        "axis_unit",
        "signal_unit",
        "area_unit",
        "peak_id",
        "area_basis",
    }:
        return _csv_text(value)
    return _number(value)


def _csv_bytes(fields: tuple[str, ...], rows: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(fields)
    for row in rows:
        writer.writerow(_csv_value(name, row.get(name)) for name in fields)
    return stream.getvalue().encode("utf-8")


def _mapped_positions(values: list[float], domain: list[float]) -> list[float]:
    """Normalize finite values by exact differences, including extreme ranges."""
    if not domain:
        return []
    low = min(domain)
    high = max(domain)
    if low == high:
        return [0.5] * len(values)
    with localcontext() as context:
        context.prec = 1200
        low_decimal = Decimal.from_float(low)
        span_decimal = Decimal.from_float(high) - low_decimal
        return [float((Decimal.from_float(value) - low_decimal) / span_decimal) for value in values]


def _scaled_positions(values: list[float]) -> list[float]:
    """Map finite values to [0, 1] without overflowing on extreme ranges."""
    return _mapped_positions(values, values)


def _axis_mapper(
    domain: list[float], start: float, extent: float
) -> Callable[[float], Decimal | None]:
    """Build an O(1) precise axis mapper for points and peak annotations."""
    low = min(domain)
    high = max(domain)
    with localcontext() as context:
        context.prec = 1200
        low_decimal = Decimal.from_float(low)
        high_decimal = Decimal.from_float(high)
        span_decimal = high_decimal - low_decimal
        start_decimal = Decimal.from_float(start)
        extent_decimal = Decimal.from_float(extent)

    def map_value(value: float) -> Decimal | None:
        if value < low or value > high:
            return None
        with localcontext() as context:
            context.prec = 80
            if span_decimal == 0:
                fraction = Decimal("0.5")
            else:
                fraction = (Decimal.from_float(value) - low_decimal) / span_decimal
            return start_decimal + fraction * extent_decimal

    return map_value


def _domain_ticks(domain: list[float], count: int = 5) -> list[tuple[Decimal, Decimal, str]]:
    """Return nice 1/2/2.5/5 ticks inside the exact display domain."""
    low = min(domain)
    high = max(domain)
    with localcontext() as context:
        context.prec = 1200
        low_decimal = Decimal.from_float(low)
        high_decimal = Decimal.from_float(high)
        if low_decimal == high_decimal:
            values = [(low_decimal, Decimal("0.5"))]
        else:
            span = high_decimal - low_decimal
            target_intervals = Decimal(count - 1)
            raw_step = span / target_intervals
            magnitude = Decimal(1).scaleb(raw_step.adjusted())
            step_candidates = [
                multiplier * magnitude
                for multiplier in (Decimal(1), Decimal(2), Decimal("2.5"), Decimal(5), Decimal(10))
            ]
            step = min(
                step_candidates,
                key=lambda candidate: abs(span / candidate - target_intervals),
            )
            first_index = int((low_decimal / step).to_integral_value(rounding=ROUND_CEILING))
            last_index = int((high_decimal / step).to_integral_value(rounding=ROUND_FLOOR))
            tick_count = last_index - first_index + 1
            if tick_count < 1 or tick_count > 12:
                values = [(low_decimal, Decimal(0)), (high_decimal, Decimal(1))]
            else:
                values = []
                for index in range(first_index, last_index + 1):
                    value = step * Decimal(index)
                    fraction = (value - low_decimal) / span
                    values.append((value, fraction))

    for precision in (6, 10, 14, 17):
        labels = [_format_decimal(value, precision) for value, _ in values]
        if len(set(labels)) == len(labels):
            return [
                (value, fraction, label)
                for (value, fraction), label in zip(values, labels, strict=True)
            ]

    endpoints = values if len(values) == 1 else [values[0], values[-1]]
    return [(value, fraction, _format_decimal(value, 17)) for value, fraction in endpoints]


def _format_decimal(value: Decimal, precision: int) -> str:
    if value.is_zero():
        return "0"
    label = format(value, f".{precision}g")
    if "e" in label:
        mantissa, exponent = label.split("e", 1)
        mantissa = mantissa.rstrip("0").rstrip(".")
        return f"{mantissa}e{exponent}"
    if "." in label:
        return label.rstrip("0").rstrip(".")
    return label


def _svg(bundle: dict[str, Any]) -> bytes:
    points = bundle["points"]
    width, height = 1100, 520
    left, right, top, bottom = 82.0, 28.0, 38.0, 72.0
    plot_width = width - left - right
    plot_height = height - top - bottom
    raw_axis = [float(row["axis"]) for row in points]
    processed_axis = [float(row["processed_axis"]) for row in points]
    raw = [float(row["raw_signal"]) for row in points]
    processed = [float(row["processed_signal"]) for row in points]
    x_domain = raw_axis + processed_axis
    map_x = _axis_mapper(x_domain, left, plot_width)
    y_domain = raw + processed
    y_fraction = _scaled_positions(y_domain)
    raw_fraction = y_fraction[: len(raw)]
    processed_fraction = y_fraction[len(raw) :]

    def px(axis_value: float) -> str:
        coordinate = map_x(axis_value)
        if coordinate is None:
            raise ValueError("trace axis value fell outside the combined plot domain")
        return format(coordinate, ".17g")

    def py(fraction: float) -> str:
        return f"{top + (1.0 - fraction) * plot_height:.17g}"

    raw_path = " ".join(
        f"{'M' if index == 0 else 'L'} {px(raw_axis[index])} {py(raw_fraction[index])}"
        for index in range(len(points))
    )
    processed_path = " ".join(
        f"{'M' if index == 0 else 'L'} {px(processed_axis[index])} {py(processed_fraction[index])}"
        for index in range(len(points))
    )

    trace = bundle["trace"]
    x_label = f"Axis ({_display(trace.get('axis_unit'))})"
    y_label = f"Signal ({_display(trace.get('signal_unit'))})"
    peak_marks: list[str] = []
    for peak in bundle["peaks"]:
        left_base = peak.get("left_base")
        right_base = peak.get("right_base")
        location = peak.get("location")
        if left_base is not None and right_base is not None:
            bounds = [
                map_x(float(left_base)),
                map_x(float(right_base)),
            ]
            if all(fraction is not None for fraction in bounds):
                x1, x2 = sorted(bounds)
                width_in_plot = max(x2 - x1, Decimal("1.0"))
                peak_marks.append(
                    f'<rect x="{format(x1, ".17g")}" y="{top:.3f}" '
                    f'width="{format(width_in_plot, ".17g")}" '
                    f'height="{plot_height:.3f}" fill="#f0b44d" opacity="0.14" />'
                )
                peak_marks.extend(
                    f'<line x1="{format(x, ".17g")}" y1="{top:.3f}" '
                    f'x2="{format(x, ".17g")}" '
                    f'y2="{top + plot_height:.3f}" stroke="#b77a15" stroke-dasharray="4 4" />'
                    for x in (x1, x2)
                )
        if location is not None:
            x = map_x(float(location))
            if x is not None:
                label = _e(peak.get("peak_id"))
                peak_marks.append(
                    f'<text x="{format(x, ".17g")}" y="{top + 16:.3f}" text-anchor="middle" '
                    f'class="peak-label">{label}</text>'
                )

    x_ticks = _domain_ticks(x_domain)
    y_ticks = _domain_ticks(y_domain)
    x_tick_marks = []
    for _, fraction, label in x_ticks:
        with localcontext() as context:
            context.prec = 80
            x = Decimal.from_float(left) + fraction * Decimal.from_float(plot_width)
        x_text = format(x, ".17g")
        x_tick_marks.extend(
            [
                f'<line class="tick" x1="{x_text}" y1="{top + plot_height:.3f}" '
                f'x2="{x_text}" y2="{top + plot_height + 5:.3f}" />',
                f'<text class="x-tick" x="{x_text}" y="{top + plot_height + 21:.3f}" '
                f'text-anchor="middle">{_e(label)}</text>',
            ]
        )

    y_tick_marks = []
    for _, fraction, label in y_ticks:
        with localcontext() as context:
            context.prec = 80
            y = Decimal.from_float(top) + (Decimal(1) - fraction) * Decimal.from_float(plot_height)
        y_text = format(y, ".17g")
        y_tick_marks.extend(
            [
                f'<line class="grid" x1="{left}" y1="{y_text}" '
                f'x2="{left + plot_width}" y2="{y_text}" />',
                f'<line class="tick" x1="{left - 5}" y1="{y_text}" x2="{left}" y2="{y_text}" />',
                f'<text class="y-tick" x="{left - 9}" y="{format(y + Decimal("4"), ".17g")}" '
                f'text-anchor="end">{_e(label)}</text>',
            ]
        )

    title = "Chromatogram evidence trace"
    grids_differ = any(raw_x != processed_x for raw_x, processed_x in zip(raw_axis, processed_axis))
    grid_description = (
        "Raw and processed recorded axis grids differ; each signal is plotted on its own grid."
        if grids_differ
        else "Raw and processed signals share the same recorded axis grid."
    )
    description = (
        f"All {len(points)} supplied points are shown in sample order. "
        f"Raw signal is gray; processed signal is teal. {grid_description}"
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="plot-title plot-desc">
<title id="plot-title">{_e(title)}</title>
<desc id="plot-desc">{_e(description)}</desc>
<style>text {{ font: 13px sans-serif; fill: #263238; }} .axis {{ stroke: #65727a; stroke-width: 1; }} .tick {{ stroke: #65727a; stroke-width: 1; }} .grid {{ stroke: #e2e7e9; stroke-width: 1; }} .x-tick, .y-tick {{ font-size: 11px; }} .raw {{ fill: none; stroke: #858b90; stroke-width: 1.4; }} .processed {{ fill: none; stroke: #007f78; stroke-width: 1.8; }} .peak-label {{ font-size: 11px; fill: #744c0d; }}</style>
<rect width="100%" height="100%" fill="white" />
{"".join(peak_marks)}
{"".join(y_tick_marks)}
<line class="axis" x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" />
<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" />
<path class="raw" d="{raw_path}" />
<path class="processed" d="{processed_path}" />
{"".join(x_tick_marks)}
<text x="{left + plot_width / 2:.3f}" y="{height - 18}" text-anchor="middle">{_e(x_label)}</text>
<text x="18" y="{top + plot_height / 2:.3f}" transform="rotate(-90 18 {top + plot_height / 2:.3f})" text-anchor="middle">{_e(y_label)}</text>
<line x1="{left + 8}" y1="20" x2="{left + 34}" y2="20" class="raw" /><text x="{left + 40}" y="24">Raw signal</text>
<line x1="{left + 130}" y1="20" x2="{left + 156}" y2="20" class="processed" /><text x="{left + 162}" y="24">Processed signal</text>
</svg>'''
    return svg.encode("utf-8")


def _metadata(bundle: dict[str, Any]) -> tuple[list[tuple[str, Any]], list[tuple[str, Any]]]:
    analysis = bundle["analysis"]
    trace = bundle["trace"]
    result = bundle["result"]
    method = analysis.get("method")
    method_label = (
        method.get("name", method.get("method", "Stored method"))
        if isinstance(method, dict)
        else method
    )
    baseline = method.get("integration_baseline") if isinstance(method, dict) else None
    if baseline is None:
        baseline = "Not specified in stored method"
    identifiers = [
        ("Analysis", analysis.get("analysis_id")),
        ("Created at", analysis.get("created_at")),
        ("Trace", trace.get("trace_id")),
        ("Sample", trace.get("sample_id")),
        ("Injection", trace.get("injection_id")),
        ("Channel", trace.get("channel")),
        ("Separation", trace.get("separation")),
        ("Detector", trace.get("detector")),
        ("Sample type", trace.get("sample_type")),
        ("Batch", trace.get("batch_id")),
        ("Run order", trace.get("run_order")),
        ("Axis unit", trace.get("axis_unit")),
        ("Signal unit", trace.get("signal_unit")),
        ("Method", method_label),
        ("Stored method details", method),
        ("Integration baseline rule", baseline),
        ("Warnings", result.get("warnings")),
        ("Notes", result.get("notes")),
    ]
    provenance = analysis.get("provenance", {})
    digests = [
        ("Input dataset SHA-256", provenance.get("input_sha256")),
        ("Source SHA-256", provenance.get("source_sha256")),
        ("Method SHA-256", provenance.get("method_sha256")),
        ("Worker SHA-256", provenance.get("worker_sha256")),
        (
            "Record SHA-256",
            analysis.get("stored_result_sha256") or provenance.get("record_sha256"),
        ),
        ("Evidence SHA-256", bundle.get("evidence_sha256")),
    ]
    return identifiers, digests


def _peak_fields(peaks: list[dict[str, Any]]) -> tuple[str, ...]:
    optional = tuple(
        field for field in _PEAK_OPTIONAL_FIELDS if any(field in peak for peak in peaks)
    )
    return (*_PEAK_REQUIRED_FIELDS, *optional, "area_basis")


def _peak_heading(field: str, axis_unit: Any, signal_unit: Any) -> str:
    axis = _display(axis_unit)
    signal = _display(signal_unit)
    headings = {
        "peak_id": "Peak ID",
        "location": f"Apex location ({axis})",
        "height": f"Height ({signal})",
        "left_base": f"Left boundary ({axis})",
        "right_base": f"Right boundary ({axis})",
        "area": f"Area ({signal} * {axis})",
        "prominence": f"Prominence ({signal})",
        "area_basis": "Area basis",
    }
    return headings[field]


def _html(bundle: dict[str, Any], svg: bytes) -> bytes:
    identifiers, digests = _metadata(bundle)
    availability = bundle["availability"]
    identifier_rows = "\n".join(
        f'<tr><th scope="row">{_e(name)}</th><td>{_e(value)}</td></tr>'
        for name, value in identifiers
    )
    digest_rows = "\n".join(
        f'<tr><th scope="row">{_e(name)}</th><td><code>{_e(value)}</code></td></tr>'
        for name, value in digests
    )
    provenance = bundle["analysis"].get("provenance", {})
    provenance_json = _e(provenance)
    availability_rows = "\n".join(
        f'<tr><th scope="row">{_e(name)}</th><td>{_e(value)}</td></tr>'
        for name, value in availability.items()
    )
    peak_fields = _peak_fields(bundle["peaks"])
    axis_unit = bundle["trace"].get("axis_unit")
    signal_unit = bundle["trace"].get("signal_unit")
    peak_header = "".join(
        f'<th scope="col">{_e(_peak_heading(field, axis_unit, signal_unit))}</th>'
        for field in peak_fields
    )
    peak_rows = "\n".join(
        "<tr>" + "".join(f"<td>{_e(peak.get(field))}</td>" for field in peak_fields) + "</tr>"
        for peak in bundle["peaks"]
    )
    if not peak_rows:
        peak_rows = f'<tr><td colspan="{len(peak_fields)}">No peak rows were supplied.</td></tr>'
    result_rows = "\n".join(
        f'<tr><th scope="row">{_e(name)}</th><td>{_e(value)}</td></tr>'
        for name, value in bundle["result"].items()
    )
    if not result_rows:
        result_rows = '<tr><td colspan="2">No stored result metadata.</td></tr>'
    point_count = bundle["point_count"]
    point_noun = "point" if point_count == 1 else "points"
    grids_differ = any(row["axis"] != row["processed_axis"] for row in bundle["points"])
    grid_caption = (
        "The recorded raw and processed axis grids differ; each curve uses its own grid."
        if grids_differ
        else "The raw and processed curves share the same recorded axis grid."
    )
    embed_svg = svg.decode("utf-8")
    artifacts = " | ".join(
        f'<a href="{name}">{name}</a>' for name in _FILENAMES if name != "report.html"
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; form-action 'none'">
<title>Chromatogram evidence report</title>
<style>
body {{ max-width: 1120px; margin: 2rem auto; padding: 0 1rem; color: #23313a; font: 16px/1.5 system-ui, sans-serif; }}
h1, h2 {{ line-height: 1.2; }} table {{ border-collapse: collapse; width: 100%; margin: 0; }}
th, td {{ border: 1px solid #cbd3d8; padding: .45rem .6rem; text-align: left; vertical-align: top; }}
th {{ background: #f1f5f6; }} figure {{ margin: 1.5rem 0; }} figure svg {{ width: 100%; height: auto; border: 1px solid #d4dbdf; }}
.table-scroll {{ max-width: 100%; box-sizing: border-box; overflow-x: auto; margin: 1rem 0 2rem; border: 1px solid #d4dbdf; -webkit-overflow-scrolling: touch; }}
.table-scroll:focus {{ outline: 3px solid #007f78; outline-offset: 2px; }}
.table-scroll table {{ margin: 0; }}
.peak-table {{ min-width: 48rem; }}
.peak-table th, .peak-table td {{ white-space: nowrap; overflow-wrap: normal; }}
.metadata-table {{ min-width: 30rem; }}
.metadata-table th {{ min-width: 12rem; white-space: nowrap; overflow-wrap: normal; }}
.metadata-table td {{ overflow-wrap: anywhere; word-break: normal; }}
.scroll-hint {{ margin: .45rem .75rem; color: #4b5a62; font-size: .9rem; }}
.provenance-json {{ max-width: 100%; box-sizing: border-box; margin: 1rem 0 2rem; padding: .8rem; border: 1px solid #d4dbdf; white-space: pre-wrap; overflow-wrap: anywhere; overflow: auto; }}
.notice {{ padding: .8rem 1rem; background: #fff6df; border-left: 4px solid #b77a15; }}
</style>
</head>
<body>
<header><h1>Chromatogram evidence report</h1>
<p>Validated stored evidence; this report renders the supplied samples and peak rows without recalculating them.</p>
<p>{point_count} {point_noun}; {bundle["peak_count"]} peak rows.</p></header>
<figure>{embed_svg}<figcaption>All supplied points are plotted in sample order. The vertical scale is shared between raw and processed signal. Shaded bands mark recorded peak-boundary intervals. {grid_caption}</figcaption></figure>
<h2>Stored peak rows</h2><p>Peak IDs identify stored rows; they are not analyte assignments. Null measurements are shown as “Unavailable” in HTML and blank in CSV. An unavailable area_basis means the source record did not state a basis; none is inferred. Numeric values are shown as stored.</p>
<div class="table-scroll peak-table-region" role="region" aria-label="Stored peak rows" aria-describedby="peak-table-scroll-hint" tabindex="0">
<p class="scroll-hint" id="peak-table-scroll-hint">Scroll horizontally to view all peak columns.</p>
<table class="peak-table"><thead><tr>{peak_header}</tr></thead><tbody>{peak_rows}</tbody></table>
</div>
<h2>Trace and method</h2><div class="table-scroll metadata-table-region" role="region" aria-label="Trace and method" tabindex="0"><table class="metadata-table"><tbody>{identifier_rows}</tbody></table></div>
<h2>Availability</h2><div class="table-scroll metadata-table-region" role="region" aria-label="Availability" tabindex="0"><table class="metadata-table"><tbody>{availability_rows}</tbody></table></div>
<h2>Stored result metadata</h2><div class="table-scroll metadata-table-region" role="region" aria-label="Stored result metadata" tabindex="0"><table class="metadata-table"><tbody>{result_rows}</tbody></table></div>
<p class="notice">Peak area is the stored signal-area value. Chemical assignment, calibration, deconvolution, and physical qualification are unavailable in this report; no purity or concentration inference is made.</p>
<details><summary>Provenance digests and backend metadata</summary>
<h2>Provenance digests</h2><div class="table-scroll metadata-table-region" role="region" aria-label="Provenance digests" tabindex="0"><table class="metadata-table"><tbody>{digest_rows}</tbody></table></div>
<h2>Complete stored provenance</h2><pre class="provenance-json">{provenance_json}</pre>
</details>
<h2>Download evidence</h2><p>{artifacts}</p>
</body></html>
"""
    return document.encode("utf-8")


def render_report(bundle: dict[str, Any]) -> dict[str, bytes]:
    """Render a validated evidence bundle into five standalone local artifacts."""
    validate_evidence(bundle)
    svg = _svg(bundle)
    evidence_json = (_json(bundle, pretty=True) + "\n").encode("utf-8")
    analysis_id = bundle["analysis"].get("analysis_id")
    evidence_digest = bundle["evidence_sha256"]
    trace = bundle["trace"]
    axis_unit = trace.get("axis_unit")
    signal_unit = trace.get("signal_unit")
    trace_rows = [
        {
            **row,
            "analysis_id": analysis_id,
            "evidence_sha256": evidence_digest,
            "axis_unit": axis_unit,
            "signal_unit": signal_unit,
        }
        for row in bundle["points"]
    ]
    peak_rows = [
        {
            **row,
            "analysis_id": analysis_id,
            "evidence_sha256": evidence_digest,
            "axis_unit": axis_unit,
            "signal_unit": signal_unit,
            "area_unit": f"{_display(signal_unit)} * {_display(axis_unit)}",
        }
        for row in bundle["peaks"]
    ]
    trace_csv = _csv_bytes(_TRACE_FIELDS, trace_rows)
    peaks_csv = _csv_bytes(
        (
            "analysis_id",
            "evidence_sha256",
            "axis_unit",
            "signal_unit",
            "area_unit",
            *_peak_fields(bundle["peaks"]),
        ),
        peak_rows,
    )
    report_html = _html(bundle, svg)
    artifacts = {
        "report.html": report_html,
        "chromatogram.svg": svg,
        "evidence.json": evidence_json,
        "trace.csv": trace_csv,
        "peaks.csv": peaks_csv,
    }
    total = sum(len(content) for content in artifacts.values())
    if total > _MAX_ARTIFACT_BYTES:
        raise ValueError(
            f"Rendered report is {total} bytes, above the {_MAX_ARTIFACT_BYTES}-byte artifact limit"
        )
    return artifacts


def write_report(
    bundle: dict[str, Any], output_dir: str | os.PathLike[str]
) -> dict[str, dict[str, str]]:
    """Write report artifacts to a new directory without replacing existing files."""
    validate_evidence(bundle)
    artifacts = render_report(bundle)
    destination = Path(output_dir)
    if destination.is_symlink() or destination.exists():
        raise FileExistsError(f"Report output directory already exists: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    written: list[Path] = []
    try:
        for filename, content in artifacts.items():
            path = destination / filename
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            written.append(path)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
        return {
            name: {"path": str(destination / name), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in artifacts.items()
        }
    except Exception:
        for path in written:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            destination.rmdir()
        except OSError:
            pass
        raise
