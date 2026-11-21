"""Portable reports for retained LC-MS TIC/XIC trace evidence.

The service owns extraction and evidence retention. This client module only
collects the bounded pages and renders them; it does not invoke an R worker or
reinterpret the trace scientifically.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
import re
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_client import _call
from .ms_trace_evidence import MAX_EVIDENCE_BYTES, MAX_PAGE, MAX_PAGE_BYTES, PROFILE
from .storage import canonical, sha256

MAX_POINTS = 50_000
MAX_SOURCE_ROWS = 250_000
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_HTML_ROWS = 2_000
REPORT_FILENAMES = ("report.html", "xic.svg", "evidence.json", "xic.csv", "source-rows.csv")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = {
    "missing_scan",
    "empty_scan",
    "empty_window",
    "saturated",
    "missing_intensity",
    "calculated",
}
_MANIFEST_FIELDS = {
    "profile",
    "trace_id",
    "acquisition_id",
    "input_sha256",
    "created_at",
    "processing",
    "provenance",
    "warnings",
    "point_count",
    "source_row_count",
    "evidence_sha256",
}
_POINT_FIELDS = {
    "scan_id",
    "scan_index",
    "source_scan_id",
    "retention_time",
    "value",
    "status",
    "selected_points",
    "saturation_unknown",
}
_SOURCE_FIELDS = {"scan_id", "point_index", "source_row", "mz", "intensity", "saturated"}


def _json(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )


def _xml_safe(value: str) -> str:
    output = []
    for char in value:
        codepoint = ord(char)
        allowed = (
            codepoint in {9, 10, 13}
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        )
        output.append(char if allowed else f"\\u{codepoint:04x}")
    return "".join(output)


def _e(value: Any) -> str:
    if value is None:
        text = "Unavailable"
    elif isinstance(value, str):
        text = value
    else:
        text = _json(value)
    return html.escape(_xml_safe(text), quote=True)


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("CSV evidence cannot contain non-finite numbers")
        return _json(value)
    text = _json(value) if isinstance(value, (dict, list)) else _xml_safe(str(value))
    first = text.lstrip(" \t\r\n\v\f\x00\ufeff")[:1]
    return "'" + text if first in {"=", "+", "-", "@"} else text


def _csv_bytes(fields: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(fields)
    for row in rows:
        writer.writerow(_csv_value(row.get(field)) for field in fields)
    return stream.getvalue().encode("utf-8")


def _identifier(value: Any, field: str) -> None:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"MS trace {field} must be a nonblank text identifier")


def _digest(value: Any, field: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"MS trace {field} must be a lowercase SHA-256 value")


def _integer(value: Any, field: str, *, minimum: int = 0, maximum: int | None = None) -> None:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"MS trace {field} is outside its bounded integer contract")


def _finite(value: Any, field: str, *, positive: bool = False, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (positive and value <= 0)
    ):
        raise ValueError(f"MS trace {field} must be finite")


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    if set(manifest) != _MANIFEST_FIELDS:
        raise ValueError("MS trace manifest has an unexpected shape")
    if manifest["profile"] != PROFILE:
        raise ValueError("unsupported MS trace evidence profile")
    for field in ("trace_id", "acquisition_id"):
        _identifier(manifest[field], field)
    _digest(manifest["input_sha256"], "input_sha256")
    _digest(manifest["evidence_sha256"], "evidence_sha256")
    _integer(manifest["point_count"], "point_count", maximum=MAX_POINTS)
    _integer(manifest["source_row_count"], "source_row_count", maximum=MAX_SOURCE_ROWS)
    if not isinstance(manifest["processing"], Mapping) or not isinstance(
        manifest["provenance"], Mapping
    ):
        raise ValueError("MS trace processing and provenance must be objects")
    if type(manifest["warnings"]) is not list or not all(
        isinstance(item, (str, Mapping)) for item in manifest["warnings"]
    ):
        raise ValueError("MS trace warnings must be a list of text or objects")
    bounds = manifest["processing"].get("mz_range")
    if not isinstance(bounds, list) or len(bounds) != 2:
        raise ValueError("XIC processing must contain two m/z bounds")
    for bound in bounds:
        _finite(bound, "m/z bound", positive=True)
    if bounds[0] > bounds[1]:
        raise ValueError("XIC m/z bounds must be ordered")
    canonical(manifest)


def _validate_point(row: Any) -> None:
    if not isinstance(row, Mapping) or set(row) != _POINT_FIELDS:
        raise ValueError("MS trace point row has an unexpected shape")
    _identifier(row["scan_id"], "scan_id")
    _integer(row["scan_index"], "scan_index", minimum=1)
    if row["source_scan_id"] is not None:
        _identifier(row["source_scan_id"], "source_scan_id")
    _finite(row["retention_time"], "retention_time")
    if row["status"] not in _STATUSES:
        raise ValueError("MS trace point status is invalid")
    _integer(row["selected_points"], "selected_points")
    if type(row["saturation_unknown"]) is not bool:
        raise ValueError("MS trace saturation_unknown must be boolean")
    if row["value"] is not None:
        _finite(row["value"], "trace value")
        if row["value"] < 0:
            raise ValueError("MS trace values must be nonnegative")
    if (
        row["status"] in {"missing_scan", "saturated", "missing_intensity"}
        and row["value"] is not None
    ):
        raise ValueError("MS trace missing-value statuses require a null value")
    if row["status"] in {"empty_scan", "empty_window"} and row["value"] != 0.0:
        raise ValueError("MS trace empty statuses require zero")
    if row["status"] == "calculated" and row["value"] is None:
        raise ValueError("MS trace calculated status requires a value")


def _validate_source(row: Any) -> None:
    if not isinstance(row, Mapping) or set(row) != _SOURCE_FIELDS:
        raise ValueError("MS trace source row has an unexpected shape")
    _identifier(row["scan_id"], "source scan_id")
    _integer(row["point_index"], "source point_index", minimum=1)
    if row["source_row"] is not None:
        _integer(row["source_row"], "source_row", minimum=1)
    _finite(row["mz"], "source m/z", positive=True)
    _finite(row["intensity"], "source intensity", nullable=True)
    if type(row["saturated"]) not in {bool, type(None)}:
        raise ValueError("MS trace source saturation must be boolean or null")


def _validate_bundle(bundle: Mapping[str, Any]) -> None:
    required = _MANIFEST_FIELDS | {"points", "source_rows"}
    if not isinstance(bundle, Mapping) or set(bundle) != required:
        raise ValueError("MS trace evidence document has an unexpected shape")
    _validate_manifest({key: bundle[key] for key in _MANIFEST_FIELDS})
    if type(bundle["points"]) is not list or len(bundle["points"]) != bundle["point_count"]:
        raise ValueError("MS trace points do not match point_count")
    if (
        type(bundle["source_rows"]) is not list
        or len(bundle["source_rows"]) != bundle["source_row_count"]
    ):
        raise ValueError("MS trace source rows do not match source_row_count")
    for point in bundle["points"]:
        _validate_point(point)
    for source in bundle["source_rows"]:
        _validate_source(source)
    if len(canonical(bundle).encode("utf-8")) > (MAX_EVIDENCE_BYTES + 128):
        raise ValueError("MS trace evidence exceeds the portable document limit")
    without_digest = dict(bundle)
    del without_digest["evidence_sha256"]
    if sha256(without_digest) != bundle["evidence_sha256"]:
        raise ValueError("MS trace evidence digest does not match the complete document")


def validate_xic_evidence(bundle: Mapping[str, Any]) -> None:
    """Validate the complete retained MS-trace document used by the report."""
    _validate_bundle(bundle)


def _unit(processing: Mapping[str, Any], name: str, default: str) -> str:
    value = processing.get(name)
    if value is None:
        return default
    if type(value) is not str or not value:
        raise ValueError(f"MS trace processing {name} must be nonempty text")
    return value


def _axis_mapper(domain: Sequence[float], start: float, extent: float):
    low, high = min(domain), max(domain)
    with localcontext() as context:
        context.prec = 120
        low_decimal = Decimal.from_float(low)
        span = Decimal.from_float(high) - low_decimal
        start_decimal, extent_decimal = Decimal.from_float(start), Decimal.from_float(extent)

    def mapper(value: float) -> Decimal:
        with localcontext() as context:
            context.prec = 80
            fraction = (
                Decimal("0.5") if span == 0 else (Decimal.from_float(value) - low_decimal) / span
            )
            return start_decimal + fraction * extent_decimal

    return mapper


def _ticks(domain: Sequence[float]) -> list[tuple[Decimal, str]]:
    low, high = min(domain), max(domain)
    with localcontext() as context:
        context.prec = 120
        low_decimal, high_decimal = Decimal.from_float(low), Decimal.from_float(high)
        if low_decimal == high_decimal:
            values = [low_decimal]
        else:
            span = high_decimal - low_decimal
            raw_step = span / Decimal(4)
            magnitude = Decimal(1).scaleb(raw_step.adjusted())
            candidates = [
                factor * magnitude
                for factor in (Decimal(1), Decimal(2), Decimal(2.5), Decimal(5), Decimal(10))
            ]
            step = min(candidates, key=lambda candidate: abs(span / candidate - Decimal(4)))
            first = int((low_decimal / step).to_integral_value(rounding="ROUND_CEILING"))
            last = int((high_decimal / step).to_integral_value(rounding="ROUND_FLOOR"))
            values = [step * Decimal(index) for index in range(first, last + 1)]
            if not values or len(values) > 12:
                values = [low_decimal, high_decimal]
    return [(value, format(value, ".8g")) for value in values]


def _svg(bundle: Mapping[str, Any]) -> bytes:
    points = bundle["points"]
    x_values = [float(point["retention_time"]) for point in points]
    y_values = [float(point["value"]) for point in points if point["value"] is not None]
    width, height = 1100, 520
    left, right, top, bottom = 90.0, 30.0, 46.0, 76.0
    plot_width, plot_height = width - left - right, height - top - bottom
    x = _axis_mapper(x_values, left, plot_width)
    y_domain = y_values or [0.0]
    if min(y_domain) >= 0:
        y_domain.append(0.0)
    y = _axis_mapper(y_domain, top + plot_height, -plot_height)
    paths, current = [], []
    for point in points:
        if point["value"] is None:
            if current:
                paths.append(" ".join(current))
                current = []
            continue
        command = "M" if not current else "L"
        current.append(
            f"{command} {format(x(float(point['retention_time'])), '.17g')} {format(y(float(point['value'])), '.17g')}"
        )
    if current:
        paths.append(" ".join(current))
    processing = bundle["processing"]
    time_unit, value_unit = (
        _unit(processing, "time_unit", "unit unavailable"),
        _unit(processing, "value_unit", "unit unavailable"),
    )
    ticks_x = []
    for value, label in _ticks(x_values):
        coordinate = format(x(float(value)), ".17g")
        ticks_x.extend(
            [
                f'<line class="tick" x1="{coordinate}" y1="{top + plot_height:.3f}" x2="{coordinate}" y2="{top + plot_height + 5:.3f}" />',
                f'<text class="x-tick" x="{coordinate}" y="{top + plot_height + 22:.3f}" text-anchor="middle">{_e(label)}</text>',
            ]
        )
    ticks_y = []
    for value, label in _ticks(y_domain):
        coordinate = y(float(value))
        ticks_y.extend(
            [
                f'<line class="grid" x1="{left}" y1="{format(coordinate, ".17g")}" x2="{left + plot_width}" y2="{format(coordinate, ".17g")}" />',
                f'<text class="y-tick" x="{left - 9}" y="{format(coordinate + Decimal(4), ".17g")}" text-anchor="end">{_e(label)}</text>',
            ]
        )
    mz_range = processing.get("mz_range")
    title = (
        f"m/z {mz_range[0]}–{mz_range[1]}"
        if isinstance(mz_range, list) and len(mz_range) == 2
        else "TIC"
    )
    description = f"Retained LC-MS {title} trace. {len(points)} supplied scan points; null values create gaps rather than connecting lines."
    markers = []
    for index, point in enumerate(points):
        if point["value"] is None:
            continue
        label = _e(f"{point['scan_id']}, {point['value']} {value_unit}, {point['status']}")
        marker = (
            f'<circle class="point" cx="{x(float(point["retention_time"])):.17g}" '
            f'cy="{y(float(point["value"])):.17g}" r="4"><title>{label}</title></circle>'
        )
        target = f"report.html#scan-{index}" if index < MAX_HTML_ROWS else "xic.csv"
        markers.append(f'<a href="{target}" aria-label="{label}">{marker}</a>')
    circles = "".join(markers)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="group" aria-labelledby="plot-title plot-desc"><title id="plot-title">LC-MS {_e(title)} trace</title><desc id="plot-desc">{_e(description)}</desc><style>text {{ font: 13px sans-serif; fill: #263238; }} .axis,.tick {{ stroke: #65727a; stroke-width: 1; }} .grid {{ stroke: #e2e7e9; stroke-width: 1; }} .x-tick,.y-tick {{ font-size: 11px; }} .trace {{ fill: none; stroke: #007f78; stroke-width: 1.8; }} .point {{ fill: #007f78; stroke: white; stroke-width: .8; }}</style><rect width="100%" height="100%" fill="white" />{"".join(ticks_y)}<line class="axis" x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" /><line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" />{"".join(f'<path class="trace" d="{_e(path)}" />' for path in paths)}{circles}{"".join(ticks_x)}<text x="{left + plot_width / 2:.3f}" y="{height - 18}" text-anchor="middle">Retention time ({_e(time_unit)})</text><text x="18" y="{top + plot_height / 2:.3f}" transform="rotate(-90 18 {top + plot_height / 2:.3f})" text-anchor="middle">Intensity ({_e(value_unit)})</text><text x="{left}" y="24">{_e(title)}</text></svg>'''
    return svg.encode("utf-8")


def _warnings(bundle: Mapping[str, Any]) -> list[str]:
    warnings = [
        str(value.get("message", value) if isinstance(value, Mapping) else value)
        for value in bundle["warnings"]
    ]
    return warnings


def _html(bundle: Mapping[str, Any], svg: bytes) -> bytes:
    processing, provenance = bundle["processing"], bundle["provenance"]
    points, source_rows = bundle["points"], bundle["source_rows"]
    rows_by_scan: dict[str, list[int]] = {}
    for index, row in enumerate(source_rows):
        rows_by_scan.setdefault(row["scan_id"], []).append(index)
    metadata = [
        ("Trace ID", bundle["trace_id"]),
        ("Acquisition ID", bundle["acquisition_id"]),
        ("Stream ID", processing.get("stream_id")),
        ("Polarity", processing.get("polarity")),
        ("MS level", processing.get("ms_level")),
        ("m/z range", processing.get("mz_range")),
        ("Point rows", bundle["point_count"]),
        ("Source rows", bundle["source_row_count"]),
        ("Evidence SHA-256", bundle["evidence_sha256"]),
        ("Input SHA-256", bundle["input_sha256"]),
    ]
    metadata_html = "".join(
        f'<tr><th scope="row">{_e(name)}</th><td>{_e(value)}</td></tr>' for name, value in metadata
    )
    warnings = _warnings(bundle)
    warning_html = (
        '<section class="warning"><h2>Worker warnings</h2><ul>'
        + "".join(f"<li>{_e(value)}</li>" for value in warnings)
        + "</ul></section>"
        if warnings
        else ""
    )
    point_rows = []
    for index, point in enumerate(points[:MAX_HTML_ROWS]):
        links = []
        for source_index in rows_by_scan.get(point["scan_id"], []):
            if source_index >= MAX_HTML_ROWS:
                links.append('<a href="source-rows.csv">Remaining centroids in CSV</a>')
                break
            row = source_rows[source_index]
            text = (
                f"row {row['source_row']}"
                if row["source_row"] is not None
                else f"centroid {row['point_index']} (source row unknown)"
            )
            links.append(f'<a href="#source-row-{source_index}">{_e(text)}</a>')
        point_rows.append(
            f'<tr id="scan-{index}" tabindex="-1"><th scope="row">{_e(point["scan_id"])}</th>'
            f"<td>{_e(point['source_scan_id'])}</td><td>{_e(point['retention_time'])}</td>"
            f"<td>{_e(point['value']) if point['value'] is not None else '—'}</td>"
            f"<td>{_e(point['status'])}</td><td>{', '.join(links) or 'None selected'}</td></tr>"
        )
    point_note = (
        f"The tables show up to {MAX_HTML_ROWS:,} rows each. The CSV files contain every row."
        if max(len(points), len(source_rows)) > MAX_HTML_ROWS
        else ""
    )
    source_fields = ("scan_id", "point_index", "source_row", "mz", "intensity", "saturated")
    source_header = "".join(f'<th scope="col">{_e(field)}</th>' for field in source_fields)
    source_body = (
        "".join(
            f'<tr id="source-row-{index}" tabindex="-1">'
            + "".join(f"<td>{_e(row[field])}</td>" for field in source_fields)
            + "</tr>"
            for index, row in enumerate(source_rows[:MAX_HTML_ROWS])
        )
        or f'<tr><td colspan="{len(source_fields)}">No contributing source rows.</td></tr>'
    )
    links = " | ".join(
        f'<a href="{name}">{name}</a>' for name in REPORT_FILENAMES if name != "report.html"
    )
    chart = svg.decode("utf-8").replace('href="report.html#scan-', 'href="#scan-')
    unit = _e(_unit(processing, "value_unit", "unit unavailable"))
    time_unit = _e(_unit(processing, "time_unit", "unit unavailable"))
    lo, hi = processing["mz_range"]
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Extracted-ion chromatogram · m/z {_e(lo)}–{_e(hi)}</title>
<style>
* {{ box-sizing:border-box }} body {{ margin:0;background:#faf9f6;color:#182e31;font:16px/1.5 system-ui,sans-serif }}
main {{ max-width:72rem;margin:auto;padding:clamp(1rem,4vw,3.25rem) }} h1,h2 {{ font-family:Georgia,serif;font-weight:normal;line-height:1.15 }}
h1 {{ font-size:clamp(2rem,5vw,3.7rem);margin:.2em 0 }} h2 {{ font-size:1.65rem;margin:0 0 .55em }}
section,details {{ padding:1.3rem 0;border-top:1px solid #b5c6c3 }} header p {{ margin:.3rem 0 1.4rem }}
.figure {{ background:white;border:1px solid #b5c6c3;border-radius:6px;padding:.5rem }} svg {{ width:100%;height:auto;display:block }}
a {{ color:#005951;text-underline-offset:3px }} :focus-visible {{ outline:3px solid #bd5c14 }} tr:target {{ background:#fff0c2;outline:2px solid #bc7300;outline-offset:-2px }}
.table-scroll {{ overflow:auto }} table {{ border-collapse:collapse;min-width:100%;font-variant-numeric:tabular-nums }}
th,td {{ text-align:left;vertical-align:top;padding:.48rem .7rem;border-bottom:1px solid #ced9d5 }} th {{ font-weight:600 }}
thead {{ background:#e9efec }} code,pre,td {{ overflow-wrap:anywhere }} pre {{ white-space:pre-wrap }} .table-scroll td {{ min-width:5rem }}
details table {{ table-layout:fixed }} summary {{ cursor:pointer;font-weight:600 }} .downloads a {{ margin-right:.5rem }}
</style></head><body><main>
<header><h1>Extracted-ion chromatogram</h1><p>m/z {_e(lo)}–{_e(hi)} · {_e(processing.get("polarity"))} polarity · MS{_e(processing.get("ms_level"))} · stream {_e(processing.get("stream_id"))}</p></header>
<div class="figure">{chart}</div>
<p>Select a plotted point to inspect its scan. A missing or saturated signal leaves a gap. An acquired empty scan plots at zero.</p>
<section><h2>Scans</h2><p>{_e(point_note)}</p><div class="table-scroll" tabindex="0" role="region" aria-label="Scan results">
<table><thead><tr><th>Scan</th><th>Source scan</th><th>Time ({time_unit})</th><th>Signal ({unit})</th><th>Status</th><th>Contributing rows</th></tr></thead>
<tbody>{"".join(point_rows)}</tbody></table></div></section>
<section><h2>Contributing centroids</h2><div class="table-scroll" tabindex="0" role="region" aria-label="Centroid evidence">
<table><thead><tr>{source_header}</tr></thead><tbody>{source_body}</tbody></table></div></section>
{warning_html}
<details><summary>Acquisition, settings and provenance</summary><table><tbody>{metadata_html}</tbody></table>
<pre>{_e({"processing": processing, "provenance": provenance})}</pre></details>
<section class="downloads"><h2>Downloads</h2><p>{links}</p></section></main></body></html>"""
    return document.encode("utf-8")


def render_xic_report(bundle: Mapping[str, Any]) -> dict[str, bytes]:
    """Render a complete retained MS-trace document into portable artifacts."""
    _validate_bundle(bundle)
    svg = _svg(bundle)
    point_fields = (
        "trace_id",
        "evidence_sha256",
        "scan_id",
        "scan_index",
        "source_scan_id",
        "retention_time",
        "value",
        "status",
        "selected_points",
        "saturation_unknown",
    )
    source_fields = (
        "trace_id",
        "evidence_sha256",
        "scan_id",
        "point_index",
        "source_row",
        "mz",
        "intensity",
        "saturated",
    )
    points = [
        {
            "trace_id": bundle["trace_id"],
            "evidence_sha256": bundle["evidence_sha256"],
            **{field: row[field] for field in point_fields[2:]},
        }
        for row in bundle["points"]
    ]
    sources = [
        {
            "trace_id": bundle["trace_id"],
            "evidence_sha256": bundle["evidence_sha256"],
            **{field: row[field] for field in source_fields[2:]},
        }
        for row in bundle["source_rows"]
    ]
    artifacts = {
        "report.html": _html(bundle, svg),
        "xic.svg": svg,
        "evidence.json": (_json(bundle, pretty=True) + "\n").encode("utf-8"),
        "xic.csv": _csv_bytes(point_fields, points),
        "source-rows.csv": _csv_bytes(source_fields, sources),
    }
    if sum(len(content) for content in artifacts.values()) > MAX_ARTIFACT_BYTES:
        raise ValueError("Rendered XIC report exceeds the artifact limit")
    return artifacts


def write_xic_report(
    bundle: Mapping[str, Any], output_dir: str | os.PathLike[str]
) -> dict[str, dict[str, str]]:
    """Write report artifacts into a new directory without replacing files."""
    artifacts = render_xic_report(bundle)
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


def read_xic_evidence(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and validate retained ``evidence.json`` without an MCP session."""
    candidate = Path(path)
    if candidate.is_dir():
        candidate = candidate / "evidence.json"
    if candidate.is_symlink() or not candidate.is_file():
        raise FileNotFoundError(f"XIC evidence file does not exist: {candidate}")
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("XIC evidence JSON could not be read") from exc
    if not isinstance(value, Mapping):
        raise ValueError("XIC evidence JSON must contain an object")
    _validate_bundle(value)
    return dict(value)


async def collect_xic_evidence(
    client: Any, trace_id: str, *, page_size: int = MAX_PAGE
) -> dict[str, Any]:
    """Collect one retained trace through the service's bounded pages."""
    _identifier(trace_id, "trace_id")
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE:
        raise ValueError(f"page_size must be an integer from 1 through {MAX_PAGE}")
    manifest = await _call(client, "measurement_get_ms_trace", {"trace_id": trace_id})
    if (
        not isinstance(manifest, Mapping)
        or set(manifest) != _MANIFEST_FIELDS
        or manifest["trace_id"] != trace_id
    ):
        raise ValueError("MS trace manifest is incomplete or identifies another trace")
    _validate_manifest(manifest)
    complete = dict(manifest)
    for kind, count_name, maximum in (
        ("points", "point_count", MAX_POINTS),
        ("source_rows", "source_row_count", MAX_SOURCE_ROWS),
    ):
        count = manifest[count_name]
        if count > maximum:
            raise ValueError(f"MS trace {count_name} exceeds the portable limit")
        rows: list[Any] = []
        offset = 0
        while offset < count:
            page = await _call(
                client,
                "measurement_read_ms_trace",
                {
                    "trace_id": trace_id,
                    "evidence_sha256": manifest["evidence_sha256"],
                    "kind": kind,
                    "offset": offset,
                    "limit": page_size,
                },
            )
            expected = {
                "profile",
                "trace_id",
                "evidence_sha256",
                "kind",
                "offset",
                "count",
                "next_offset",
                "data",
            }
            if not isinstance(page, Mapping) or set(page) != expected:
                raise ValueError("MS trace page has an unexpected shape")
            if (
                page["profile"] != PROFILE
                or page["trace_id"] != trace_id
                or page["evidence_sha256"] != manifest["evidence_sha256"]
                or page["kind"] != kind
                or page["offset"] != offset
            ):
                raise ValueError("MS trace page identity or position changed")
            if len(canonical(page).encode("utf-8")) > MAX_PAGE_BYTES:
                raise ValueError("MS trace page exceeds the response byte limit")
            data = page["data"]
            if (
                type(data) is not list
                or not data
                or page["count"] != len(data)
                or len(data) > min(page_size, count - offset)
            ):
                raise ValueError("MS trace page has an invalid data array")
            expected_next = offset + len(data) if offset + len(data) < count else None
            if page["next_offset"] != expected_next:
                raise ValueError("MS trace page continuation is inconsistent")
            rows.extend(data)
            offset += len(data)
        complete[kind] = rows
    _validate_bundle(complete)
    return complete


async def export_xic_report(
    client: Any, trace_id: str, output_dir: str | os.PathLike[str], *, page_size: int = MAX_PAGE
) -> dict[str, dict[str, str]]:
    """Collect ``trace_id`` and write a portable XIC report."""
    evidence = await collect_xic_evidence(client, trace_id, page_size=page_size)
    return write_xic_report(evidence, output_dir)
