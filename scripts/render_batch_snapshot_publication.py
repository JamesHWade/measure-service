"""Render a standalone, accessible index for a frozen batch snapshot delivery.

The generator reads an existing ``workflow_receipt.json`` and its immutable
report artifacts.  It writes one new HTML file; report bytes are never copied
or rewritten.  Links are deliberately relative so a caller can publish the
delivery directory below a site root, for example::

    python scripts/render_batch_snapshot_publication.py \
        --report-dir output \
        --destination index.html

The default report prefix is ``output``.  It is a URL-like relative path, not
a filesystem path, and cannot contain traversal or a URL scheme.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

PROFILE = "measure-chromatogram-batch-snapshot-example/1"
RECEIPT_NAME = "workflow_receipt.json"
REPORT_FILES = ("report.html", "chromatogram.svg", "evidence.json", "trace.csv", "peaks.csv")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DISPLAY_LABELS = {
    "reference": "Reference",
    "blank": "Blank",
    "qc": "QC",
    "unknown": "Unknown",
    "unselected": "Unselected",
}
REQUIRED_ASSERTIONS = (
    "all_analysis_calls_used_public_mcp",
    "failed_runtime_unknown",
    "fresh_analyses_have_dataset_origin_only",
    "historical_imports_are_not_active_attempts",
    "no_hidden_retry_or_optimization_or_physical_claim",
    "references_rejected_before_target_creation",
    "roles_and_original_sample_types_preserved",
    "selected_batch_point_count_is_2004",
    "source_frozen_two_completed_two_pending",
    "source_history_three_attempts_two_controls",
    "two_historical_and_four_fresh_reports",
    "two_original_successes_compare_with_declared_tolerances",
    "unselected_trace_retained_in_complete_closure",
    "wide_csv_retained_five_traces_and_2505_points",
)


def _error(message: str) -> ValueError:
    return ValueError(f"Cannot render batch snapshot publication: {message}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise _error(f"{label} must be an array")
    return value


def _required(value: dict[str, Any], key: str, label: str) -> Any:
    if key not in value:
        raise _error(f"{label} is missing required field {key!r}")
    return value[key]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)


def _safe_relative(value: Any, label: str) -> str:
    """Return a normalized local URL path, rejecting traversal and URLs."""

    if not isinstance(value, str) or not value:
        raise _error(f"{label} must be a non-empty relative path")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise _error(f"{label} must be a relative report artifact path, not a URL")
    if "\\" in value:
        raise _error(f"{label} must use relative POSIX path separators")
    if "%" in value or any(ord(character) < 0x20 for character in value):
        raise _error(f"{label} contains an unsafe encoded or control character")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise _error(f"{label} must be a relative report artifact path without traversal")
    return "/".join(path.parts)


def _safe_artifact(report_dir: Path, relative: Any, label: str) -> tuple[str, Path]:
    normalized = _safe_relative(relative, label)
    root = report_dir.resolve(strict=True)
    raw = root.joinpath(*normalized.split("/"))
    if raw.is_symlink():
        raise _error(f"{label} must not be a symlink")
    resolved = raw.resolve(strict=False)
    if root != resolved and root not in resolved.parents:
        raise _error(f"{label} escapes the report directory")
    if not raw.is_file():
        raise _error(f"{label} does not name a regular file: {normalized}")
    return normalized, raw


def _count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error(f"{label} must be a non-negative integer")
    return value


def _format_count(value: Any, label: str) -> str:
    return f"{_count(value, label):,}"


def _format_number(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(f"{label} must be numeric")
    if not isinstance(value, int) and not math.isfinite(value):
        raise _error(f"{label} must be finite")
    if float(value) == 0:
        return "0"
    return format(value, ".12g")


def _read_receipt(report_dir: Path) -> tuple[dict[str, Any], str]:
    if not report_dir.is_dir():
        raise _error(f"report directory does not exist: {report_dir}")
    receipt_path = report_dir / RECEIPT_NAME
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise _error(f"report directory is missing {RECEIPT_NAME}")
    data = receipt_path.read_bytes()
    try:
        receipt = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _error(f"{RECEIPT_NAME} is not valid UTF-8 JSON: {error}") from error
    return _mapping(receipt, RECEIPT_NAME), _sha256(data)


def _validate_receipt(receipt: dict[str, Any], report_dir: Path) -> dict[str, Any]:
    if receipt.get("profile") != PROFILE:
        raise _error(f"receipt is missing required profile {PROFILE!r}")
    required_top = (
        "assertions",
        "canonical_file_hashes",
        "comparison_scope",
        "comparisons",
        "fresh_reexecution",
        "historical_imports",
        "restore",
        "source",
    )
    for key in required_top:
        if key not in receipt:
            raise _error(f"receipt is missing required field {key!r}")

    assertions = _mapping(receipt["assertions"], "receipt assertions")
    missing_assertions = [key for key in REQUIRED_ASSERTIONS if key not in assertions]
    if missing_assertions:
        raise _error(f"receipt assertions are missing: {', '.join(missing_assertions)}")
    failed_assertions = [key for key in REQUIRED_ASSERTIONS if assertions[key] is not True]
    if failed_assertions:
        raise _error(f"receipt assertions are not all true: {', '.join(failed_assertions)}")

    source = _mapping(receipt["source"], "receipt source")
    dataset = _mapping(_required(source, "dataset", "receipt source"), "source dataset")
    for key in ("trace_count", "point_count", "selected_point_count"):
        _format_count(_required(dataset, key, "source dataset"), f"source dataset.{key}")
    selected_trace_ids = _required(dataset, "selected_trace_ids", "source dataset")
    if not isinstance(selected_trace_ids, list) or not all(
        isinstance(trace_id, str) and trace_id for trace_id in selected_trace_ids
    ):
        raise _error("source dataset.selected_trace_ids must be a non-empty array of IDs")
    history = _mapping(_required(source, "history", "receipt source"), "source history")
    frozen = _mapping(_required(history, "frozen", "source history"), "frozen source batch")
    frozen_counts = _mapping(_required(frozen, "counts", "frozen source batch"), "frozen counts")
    for key in ("completed", "pending"):
        _format_count(_required(frozen_counts, key, "frozen counts"), f"frozen counts.{key}")
    attempts = _mapping(_required(history, "attempts", "source history"), "source attempts")
    attempts_total = 0
    for ordinal, page in attempts.items():
        page_mapping = _mapping(page, f"source attempts {ordinal}")
        attempts_total += _count(
            _required(page_mapping, "total", f"source attempts {ordinal}"), "attempt total"
        )
    controls = _mapping(_required(history, "controls", "source history"), "source controls")
    controls_total = _count(_required(controls, "total", "source controls"), "control total")

    comparison_scope = _mapping(receipt["comparison_scope"], "comparison scope")
    for key in (
        "original_successful_item_count",
        "fresh_completed_item_count",
        "compared_item_count",
    ):
        _format_count(
            _required(comparison_scope, key, "comparison scope"), f"comparison scope.{key}"
        )
    unresolved = _list(
        _required(comparison_scope, "original_unresolved_source_items", "comparison scope"),
        "unresolved source items",
    )
    comparisons = _list(receipt["comparisons"], "comparisons")
    if not comparisons:
        raise _error("receipt comparisons must contain at least one comparison")
    checked = numeric = categorical = 0
    max_absolute = 0.0
    max_relative = 0.0
    absolute_tolerances: set[float] = set()
    relative_tolerances: set[float] = set()
    for index, comparison_value in enumerate(comparisons):
        comparison = _mapping(comparison_value, f"comparison {index}")
        for key in (
            "passed",
            "comparison_count",
            "numeric_comparison_count",
            "categorical_comparison_count",
            "max_absolute_error",
            "max_relative_error",
            "absolute_tolerance",
            "relative_tolerance",
            "point_count",
            "peak_count",
            "source_analysis_id",
            "fresh_analysis_id",
        ):
            _required(comparison, key, f"comparison {index}")
        if comparison["passed"] is not True:
            raise _error(f"comparison {index} did not pass")
        comparison_count = _count(
            comparison["comparison_count"], f"comparison {index}.comparison_count"
        )
        numeric_count = _count(
            comparison["numeric_comparison_count"], f"comparison {index}.numeric_comparison_count"
        )
        categorical_count = _count(
            comparison["categorical_comparison_count"],
            f"comparison {index}.categorical_comparison_count",
        )
        if comparison_count != numeric_count + categorical_count:
            raise _error(f"comparison {index} count partition is inconsistent")
        checked += comparison_count
        numeric += numeric_count
        categorical += categorical_count
        maximum_absolute = float(comparison["max_absolute_error"])
        maximum_relative = float(comparison["max_relative_error"])
        if (
            not math.isfinite(maximum_absolute)
            or not math.isfinite(maximum_relative)
            or maximum_absolute < 0
            or maximum_relative < 0
        ):
            raise _error(f"comparison {index} errors must be non-negative")
        max_absolute = max(max_absolute, maximum_absolute)
        max_relative = max(max_relative, maximum_relative)
        absolute_tolerance = float(comparison["absolute_tolerance"])
        relative_tolerance = float(comparison["relative_tolerance"])
        if (
            not math.isfinite(absolute_tolerance)
            or not math.isfinite(relative_tolerance)
            or absolute_tolerance < 0
            or relative_tolerance < 0
        ):
            raise _error(f"comparison {index} tolerances must be non-negative")
        absolute_tolerances.add(absolute_tolerance)
        relative_tolerances.add(relative_tolerance)

    historical = _list(receipt["historical_imports"], "historical imports")
    fresh = _list(receipt["fresh_reexecution"], "fresh re-execution")
    if not historical or not fresh:
        raise _error("receipt must contain historical imports and fresh re-executions")
    records = []
    by_analysis_id: dict[str, dict[str, Any]] = {}
    by_source_analysis_id: dict[str, dict[str, Any]] = {}
    for section, values in (("historical", historical), ("fresh", fresh)):
        for index, record_value in enumerate(values):
            record = _mapping(record_value, f"{section} record {index}")
            analysis = _mapping(
                _required(record, "analysis", f"{section} record {index}"), f"{section} analysis"
            )
            analysis_id = _required(record, "analysis_id", f"{section} record {index}")
            if not isinstance(analysis_id, str) or not analysis_id:
                raise _error(f"{section} record {index} has an invalid analysis ID")
            if analysis_id in by_analysis_id:
                raise _error(f"analysis ID is duplicated: {analysis_id}")
            if analysis.get("analysis_id") != analysis_id:
                raise _error(f"{section} record {index} analysis ID does not match its record")
            trace_id = analysis.get("trace_id")
            if not isinstance(trace_id, str) or not trace_id:
                raise _error(f"{section} record {index} is missing trace identity")
            report = _mapping(
                _required(record, "report", f"{section} record {index}"), f"{section} report"
            )
            for count_key in ("ordinal", "point_count", "peak_count"):
                _format_count(
                    _required(report, count_key, f"{section} report"),
                    f"{section} report.{count_key}",
                )
            files = _mapping(
                _required(report, "files", f"{section} report"), f"{section} report files"
            )
            paths: dict[str, str] = {}
            for filename in REPORT_FILES:
                if filename not in files:
                    raise _error(f"{section} report {index} is missing {filename}")
                metadata = _mapping(files[filename], f"{section} report {index} {filename}")
                relative, path = _safe_artifact(
                    report_dir,
                    _required(metadata, "path", f"{section} report {index} {filename}"),
                    f"{section} report {index} {filename} path",
                )
                expected_hash = _required(
                    metadata, "sha256", f"{section} report {index} {filename}"
                )
                if not isinstance(expected_hash, str) or not SHA256.fullmatch(expected_hash):
                    raise _error(f"{section} report {index} {filename} has an invalid SHA-256")
                expected_size = _required(
                    metadata, "size_bytes", f"{section} report {index} {filename}"
                )
                if (
                    not isinstance(expected_size, int)
                    or isinstance(expected_size, bool)
                    or expected_size < 0
                ):
                    raise _error(f"{section} report {index} {filename} has an invalid size")
                if path.stat().st_size != expected_size:
                    raise _error(
                        f"{section} report {index} {filename} size does not match the receipt"
                    )
                actual_hash = _sha256(path.read_bytes())
                if actual_hash != expected_hash:
                    raise _error(
                        f"{section} report {index} {filename} hash does not match the receipt"
                    )
                paths[filename] = relative
            descriptor = {
                "section": section,
                "analysis": analysis,
                "record": record,
                "report": report,
                "files": files,
                "paths": paths,
            }
            records.append(descriptor)
            by_analysis_id[analysis_id] = descriptor
            if section == "historical":
                source_analysis_id = _required(
                    record, "source_analysis_id", f"{section} record {index}"
                )
                if not isinstance(source_analysis_id, str) or not source_analysis_id:
                    raise _error(f"{section} record {index} has an invalid source analysis ID")
                if source_analysis_id in by_source_analysis_id:
                    raise _error(f"source analysis ID is duplicated: {source_analysis_id}")
                by_source_analysis_id[source_analysis_id] = descriptor

    canonical = _mapping(receipt["canonical_file_hashes"], "canonical file hashes")
    if not canonical:
        raise _error("canonical_file_hashes must not be empty")
    for relative_value, expected_hash in canonical.items():
        relative, path = _safe_artifact(report_dir, relative_value, "canonical artifact path")
        if not isinstance(expected_hash, str) or not SHA256.fullmatch(expected_hash):
            raise _error(f"canonical artifact {relative} has an invalid SHA-256")
        if _sha256(path.read_bytes()) != expected_hash:
            raise _error(f"canonical artifact {relative} hash does not match the receipt")

    snapshots = _mapping(_required(source, "snapshots", "receipt source"), "source snapshots")
    archives: dict[str, str] = {}
    for mode in ("references", "self_contained"):
        snapshot = _mapping(_required(snapshots, mode, "source snapshots"), f"{mode} snapshot")
        archive = _mapping(_required(snapshot, "archive", f"{mode} snapshot"), f"{mode} archive")
        relative, archive_path = _safe_artifact(
            report_dir,
            _required(archive, "path", f"{mode} archive"),
            f"{mode} archive path",
        )
        expected_hash = _required(archive, "sha256", f"{mode} archive")
        if not isinstance(expected_hash, str) or not SHA256.fullmatch(expected_hash):
            raise _error(f"{mode} archive has an invalid SHA-256")
        expected_size = _required(archive, "size_bytes", f"{mode} archive")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise _error(f"{mode} archive has an invalid size")
        if archive_path.stat().st_size != expected_size:
            raise _error(f"{mode} archive size does not match the receipt")
        if _sha256(archive_path.read_bytes()) != expected_hash:
            raise _error(f"{mode} archive hash does not match the receipt")
        archives[mode] = relative

    report_paths = [record["paths"] for record in records]
    for relative in [path for paths in report_paths for path in paths.values()] + list(
        archives.values()
    ):
        if relative not in canonical:
            raise _error(f"canonical_file_hashes is missing referenced artifact {relative}")
    return {
        "source": source,
        "dataset": dataset,
        "history": history,
        "frozen": frozen,
        "frozen_counts": frozen_counts,
        "attempts_total": attempts_total,
        "controls_total": controls_total,
        "comparison_scope": comparison_scope,
        "unresolved": unresolved,
        "comparisons": comparisons,
        "checked": checked,
        "numeric": numeric,
        "categorical": categorical,
        "max_absolute": max_absolute,
        "max_relative": max_relative,
        "absolute_tolerances": sorted(absolute_tolerances),
        "relative_tolerances": sorted(relative_tolerances),
        "records": records,
        "by_analysis_id": by_analysis_id,
        "by_source_analysis_id": by_source_analysis_id,
        "archives": archives,
    }


def _url(prefix: str, relative: str) -> str:
    return html.escape(f"{prefix}/{relative}" if prefix else relative, quote=True)


def _record_link(record: dict[str, Any], filename: str, prefix: str) -> str:
    return _url(prefix, record["paths"][filename])


def _trace_rows(data: dict[str, Any]) -> str:
    inputs = _mapping(
        _required(data["frozen"], "manifest", "frozen source batch"), "frozen manifest"
    ).get("inputs")
    rows = []
    for item_value in _list(inputs, "frozen manifest inputs"):
        item = _mapping(item_value, "frozen manifest input")
        trace = _mapping(_required(item, "trace", "frozen manifest input"), "trace identity")
        trace_id = _escape(_required(item, "trace_id", "frozen manifest input"))
        label = _escape(DISPLAY_LABELS.get(str(item["trace_id"]), str(item["trace_id"])))
        rows.append(
            "<tr>"
            f'<th scope="row">{label}</th>'
            f"<td>{trace_id}</td>"
            f"<td>{_escape(item.get('role', ''))}</td>"
            f"<td>{_escape(trace.get('sample_type', ''))}</td>"
            f"<td>{_escape(trace.get('axis_unit', ''))}</td>"
            f"<td>{_escape(trace.get('signal_unit', ''))}</td>"
            f"<td>{_escape(item.get('point_count', ''))}</td>"
            "</tr>"
        )
    return "".join(rows)


def _comparison_rows(data: dict[str, Any], prefix: str) -> str:
    rows = []
    for comparison_value in data["comparisons"]:
        comparison = _mapping(comparison_value, "comparison")
        source_record = data["by_source_analysis_id"][comparison["source_analysis_id"]]
        fresh_record = data["by_analysis_id"][comparison["fresh_analysis_id"]]
        trace_id = source_record["analysis"]["trace_id"]
        label = _escape(DISPLAY_LABELS.get(trace_id, trace_id))
        source_link = _record_link(source_record, "report.html", prefix)
        fresh_link = _record_link(fresh_record, "report.html", prefix)
        rows.append(
            "<tr>"
            f'<th scope="row">{label}</th>'
            f'<td><code>{_escape(comparison["source_analysis_id"])}</code><br><a href="{source_link}">historical report</a></td>'
            f'<td><code>{_escape(comparison["fresh_analysis_id"])}</code><br><a href="{fresh_link}">fresh report</a></td>'
            f"<td>{_format_count(comparison['point_count'], 'comparison point count')}</td>"
            f"<td>{_format_count(comparison['peak_count'], 'comparison peak count')}</td>"
            f"<td>{_format_number(comparison['max_absolute_error'], 'maximum absolute error')}</td>"
            f"<td>{_format_number(comparison['max_relative_error'], 'maximum relative error')}</td>"
            "</tr>"
        )
    return "".join(rows)


def _report_cards(data: dict[str, Any], prefix: str) -> str:
    cards = []
    section_order = {"historical": 0, "fresh": 1}
    ordered = sorted(
        data["records"],
        key=lambda record: (section_order.get(record["section"], 2), record["report"]["ordinal"]),
    )
    for record in ordered:
        analysis = record["analysis"]
        trace_id = analysis["trace_id"]
        trace_label = DISPLAY_LABELS.get(trace_id, trace_id)
        section_label = "Historical import" if record["section"] == "historical" else "Fresh R"
        alt = f"{trace_label} {record['section']} stored chromatogram trace"
        report_link = _record_link(record, "report.html", prefix)
        svg_link = _record_link(record, "chromatogram.svg", prefix)
        card_links = " ".join(
            f'<a href="{_record_link(record, filename, prefix)}">{label}</a>'
            for filename, label in (
                ("report.html", "Open report"),
                ("chromatogram.svg", "SVG"),
                ("evidence.json", "Evidence JSON"),
                ("trace.csv", "Trace CSV"),
                ("peaks.csv", "Peaks CSV"),
            )
        )
        cards.append(
            '<article class="report-card">'
            f"<h3>{_escape(section_label)} · {_escape(trace_label)}</h3>"
            f'<p class="card-meta">{_escape(analysis["analysis_id"])} · {_escape(record["report"]["point_count"])} points · {_escape(record["report"]["peak_count"])} peaks</p>'
            f'<figure><a href="{svg_link}"><img src="{svg_link}" alt="{_escape(alt)}" loading="lazy"></a><figcaption>Stored SVG preview; the report and raw evidence remain the source artifacts.</figcaption></figure>'
            f'<p class="card-links">{card_links}</p>'
            f'<p><a class="primary-link" href="{report_link}">Read {_escape(section_label.lower())} report for {_escape(trace_label)}</a></p>'
            "</article>"
        )
    return "".join(cards)


def _runtime_observations(data: dict[str, Any]) -> list[dict[str, Any]]:
    observations = []
    for index, comparison_value in enumerate(data["comparisons"]):
        comparison = _mapping(comparison_value, f"comparison {index}")
        provenance = _mapping(
            _required(comparison, "execution_provenance", f"comparison {index}"),
            f"comparison {index} execution provenance",
        )
        observations.append(
            {
                "comparison": index + 1,
                "source": {
                    "provenance": provenance.get("source"),
                    "result_fields": provenance.get("source_result_fields"),
                },
                "fresh": {
                    "provenance": provenance.get("fresh"),
                    "result_fields": provenance.get("fresh_result_fields"),
                },
                "equality_required": provenance.get("equality_required"),
            }
        )
    return observations


CSS = """
:root { color-scheme: light; --paper: #fbf7ef; --ink: #152c3b; --muted: #5b6d73; --teal: #007f78; --teal-dark: #005c58; --gold: #b77a15; --line: #d9d3c8; --card: #fffdf8; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.55 system-ui, sans-serif; }
a { color: var(--teal-dark); text-decoration-thickness: .1em; text-underline-offset: .15em; }
a:hover { color: #003d3a; }
a:focus-visible, summary:focus-visible { outline: 3px solid #e1a62d; outline-offset: 3px; }
main, header, footer { width: min(1160px, calc(100% - 2rem)); margin-inline: auto; }
header { padding: 4.5rem 0 2.5rem; }
.eyebrow { color: var(--teal-dark); font-size: .82rem; font-weight: 750; letter-spacing: .12em; text-transform: uppercase; }
h1 { max-width: 900px; margin: .5rem 0 1rem; font-size: clamp(2.2rem, 5vw, 4.6rem); line-height: 1.03; letter-spacing: -.045em; }
h2 { margin: 3.5rem 0 1rem; font-size: clamp(1.5rem, 2vw, 2.2rem); line-height: 1.1; }
h3 { margin: 0; line-height: 1.2; }
.lede { max-width: 760px; margin: 0; color: #314954; font-size: 1.2rem; }
.scope { max-width: 760px; margin: 1.25rem 0 0; padding: .8rem 1rem; border-left: 4px solid var(--gold); background: #fff4dc; }
.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin: 2rem 0 1rem; }
.stat { min-height: 7.3rem; padding: 1.2rem; border: 1px solid var(--line); border-radius: 1rem; background: var(--card); box-shadow: 0 8px 28px rgba(21,44,59,.06); }
.stat dt { color: var(--muted); font-size: .88rem; }
.stat dd { margin: .3rem 0 0; font-size: clamp(1.65rem, 3vw, 2.6rem); font-weight: 760; letter-spacing: -.04em; }
.stat small { display: block; margin-top: .1rem; color: var(--muted); font-size: .86rem; }
.support-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-top: 1rem; }
.support { padding: 1.1rem 1.2rem; border-top: 3px solid var(--teal); background: rgba(255,253,248,.68); }
.support strong { display: block; font-size: 1.2rem; }
.support span { color: var(--muted); }
.flow { display: grid; grid-template-columns: 1fr auto 1fr auto 1fr; align-items: stretch; gap: .75rem; }
.phase { padding: 1.3rem; border: 1px solid var(--line); border-radius: 1rem; background: var(--card); }
.phase-number { display: inline-grid; width: 2rem; height: 2rem; place-items: center; margin-bottom: .75rem; border-radius: 50%; background: var(--teal); color: white; font-weight: 800; }
.phase p { margin: .55rem 0 0; color: var(--muted); }
.flow-arrow { display: grid; place-items: center; color: var(--teal); font-size: 2rem; }
.downloads { display: flex; flex-wrap: wrap; gap: .75rem; margin-top: 1rem; }
.download { display: inline-block; padding: .65rem .9rem; border: 1px solid var(--teal); border-radius: 999px; background: #e6f3ef; font-weight: 700; }
.table-scroll { max-width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: .8rem; background: var(--card); }
.table-scroll:focus { outline: 3px solid #e1a62d; outline-offset: 3px; }
table { width: 100%; border-collapse: collapse; }
caption { padding: .9rem 1rem; text-align: left; color: var(--muted); font-weight: 700; }
th, td { padding: .7rem .8rem; border-top: 1px solid var(--line); text-align: left; vertical-align: top; }
th { font-weight: 750; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
code { overflow-wrap: anywhere; }
.comparison-table { min-width: 58rem; }
.comparison-table td code { font-size: .8rem; }
.report-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.25rem; }
.report-card { min-width: 0; padding: 1.25rem; border: 1px solid var(--line); border-radius: 1rem; background: var(--card); box-shadow: 0 8px 28px rgba(21,44,59,.05); }
.card-meta { margin: .45rem 0 1rem; color: var(--muted); font-size: .88rem; overflow-wrap: anywhere; }
figure { margin: 0; }
figure img { display: block; width: 100%; height: auto; border: 1px solid var(--line); background: white; }
figcaption { margin-top: .5rem; color: var(--muted); font-size: .87rem; }
.card-links { display: flex; flex-wrap: wrap; gap: .35rem .7rem; margin: 1rem 0 .7rem; font-size: .9rem; }
.primary-link { font-weight: 750; }
details { margin: 1rem 0; padding: 1rem 1.2rem; border: 1px solid var(--line); border-radius: .8rem; background: var(--card); }
summary { cursor: pointer; font-weight: 750; }
pre { max-width: 100%; overflow: auto; padding: 1rem; background: #f3eee4; color: #223944; font-size: .8rem; white-space: pre-wrap; overflow-wrap: anywhere; }
.note { color: var(--muted); }
footer { padding: 3rem 0 4rem; color: var(--muted); font-size: .9rem; }
@media (max-width: 850px) { .stat-grid { grid-template-columns: repeat(2, 1fr); } .support-grid { grid-template-columns: 1fr; } .flow { grid-template-columns: 1fr; } .flow-arrow { transform: rotate(90deg); min-height: 1.5rem; } .report-grid { grid-template-columns: 1fr; } }
@media (max-width: 500px) { main, header, footer { width: min(100% - 1.25rem, 1160px); } header { padding-top: 2.5rem; } .stat-grid { gap: .65rem; } .stat { min-height: 6.5rem; padding: .9rem; } .stat dd { font-size: 1.55rem; } }
"""


def _render_html(data: dict[str, Any], receipt: dict[str, Any], report_prefix: str) -> bytes:
    source_dataset = data["dataset"]
    scope = _escape(receipt.get("scope", "Synthetic LC evidence publication"))
    comparison_scope = data["comparison_scope"]
    frozen_counts = data["frozen_counts"]
    historical_count = len(
        [record for record in data["records"] if record["section"] == "historical"]
    )
    fresh_count = len([record for record in data["records"] if record["section"] == "fresh"])
    source_archive = _url(report_prefix, data["archives"]["references"])
    self_archive = _url(report_prefix, data["archives"]["self_contained"])
    max_absolute = _format_number(data["max_absolute"], "maximum absolute error")
    max_relative = _format_number(data["max_relative"], "maximum relative error")
    absolute_tolerance = ", ".join(
        _format_number(value, "absolute tolerance") for value in data["absolute_tolerances"]
    )
    relative_tolerance = ", ".join(
        _format_number(value, "relative tolerance") for value in data["relative_tolerances"]
    )
    stats = "".join(
        (
            f'<div class="stat"><dt>Matched comparisons</dt><dd>{_format_count(comparison_scope["compared_item_count"], "compared items")}/{_format_count(comparison_scope["original_successful_item_count"], "original successes")}</dd><small>original successes compared</small></div>',
            f'<div class="stat"><dt>Checked values</dt><dd>{_format_count(data["checked"], "checked values")}</dd><small>{_format_count(data["numeric"], "numeric values")} numeric · {_format_count(data["categorical"], "categorical values")} categorical</small></div>',
            f'<div class="stat"><dt>Largest observed error</dt><dd>{_escape(max_absolute)} / {_escape(max_relative)}</dd><small>absolute / relative; declared tolerances retained</small></div>',
            f'<div class="stat"><dt>Fresh analyses</dt><dd>{_format_count(fresh_count, "fresh analyses")}</dd><small>new-store R re-executions</small></div>',
        )
    )
    unresolved_text = ", ".join(
        _escape(f"{item.get('role', 'item')} ({item.get('state', 'unresolved')})")
        for item in data["unresolved"]
    )
    unresolved_labels = [
        DISPLAY_LABELS.get(str(item.get("role", "item")), str(item.get("role", "item")))
        for item in data["unresolved"]
    ]
    if len(unresolved_labels) == 1:
        unresolved_summary = f"{unresolved_labels[0]} source item remains pending"
    elif unresolved_labels:
        unresolved_summary = (
            f"{', '.join(unresolved_labels[:-1])} and {unresolved_labels[-1].lower()} "
            "source items remain pending"
        )
    else:
        unresolved_summary = "No unresolved source items remain pending"
    method = _required(data["frozen"], "method", "frozen source batch")
    runtime = _runtime_observations(data)
    trace_count = source_dataset["trace_count"]
    point_count = source_dataset["point_count"]
    selected_count = source_dataset["selected_point_count"]
    html_text = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chromatogram batch snapshot publication</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <p class="eyebrow">Portable chromatogram evidence · snapshot publication</p>
  <h1>{_format_count(historical_count, "historical import count")} archived successes are matched to fresh R executions.</h1>
  <p class="lede">The source batch retained {_format_count(len(source_dataset["selected_trace_ids"]), "selected trace count")} selected traces, {_format_count(comparison_scope["original_successful_item_count"], "original successful item count")} successful analyses, a controlled failed attempt, cancellation, and an explicit retry selection. A new store imported the historical results and then produced {_format_count(fresh_count, "fresh analysis count")} fresh analyses.</p>
  <p class="scope">Synthetic scalar LC workflow. Physical qualification and MoSeS reproduction remain open.</p>
  <dl class="stat-grid">{stats}</dl>
  <div class="support-grid">
    <div class="support"><strong>Source: {_format_count(frozen_counts["completed"], "source completed")} completed / {_format_count(frozen_counts["pending"], "source pending")} pending</strong><span>{_format_count(data["attempts_total"], "attempts")} retained attempts · {_format_count(data["controls_total"], "controls")} controls</span></div>
    <div class="support"><strong>{_format_count(trace_count, "trace count")} traces · {_format_count(point_count, "raw point count")} raw points</strong><span>{_format_count(len(source_dataset["selected_trace_ids"]), "selected trace count")} selected traces · {_format_count(selected_count, "selected point count")} selected points</span></div>
    <div class="support"><strong>{_format_count(historical_count, "historical import count")} historical + {_format_count(fresh_count, "fresh count")} fresh</strong><span>Historical copies and fresh results retain separate identities.</span></div>
  </div>
</header>
<main>
  <section aria-labelledby="flow-heading">
    <h2 id="flow-heading">Source → archive → new store</h2>
    <div class="flow" aria-label="Three phase snapshot workflow">
      <article class="phase"><span class="phase-number">1</span><h3>Source batch</h3><p>{_format_count(trace_count, "trace count")} traces were imported; {_format_count(len(source_dataset["selected_trace_ids"]), "selected trace count")} were selected. The source was frozen at {_format_count(frozen_counts["completed"], "source completed")} completed and {_format_count(frozen_counts["pending"], "source pending")} pending.</p></article>
      <div class="flow-arrow" aria-hidden="true">→</div>
      <article class="phase"><span class="phase-number">2</span><h3>Portable archive</h3><p>Reference metadata and a self-contained result archive are retained as separate modes. The reference mode is not restorable.</p><div class="downloads"><a class="download" href="{source_archive}">Download references ZIP</a><a class="download" href="{self_archive}">Download self-contained ZIP</a></div></article>
      <div class="flow-arrow" aria-hidden="true">→</div>
      <article class="phase"><span class="phase-number">3</span><h3>New store</h3><p>{_format_count(historical_count, "historical import count")} historical results were read without R; {_format_count(fresh_count, "fresh count")} fresh R analyses were then executed explicitly.</p></article>
    </div>
    <p class="note">The references ZIP contains metadata only and cannot be restored. The self-contained ZIP carries the dataset closure and stored results.</p>
  </section>

  <section aria-labelledby="comparison-heading">
    <h2 id="comparison-heading">Declared comparison scope</h2>
    <p>Only {_format_count(comparison_scope["compared_item_count"], "compared items")} of {_format_count(comparison_scope["fresh_completed_item_count"], "fresh completed items")} fresh outcomes are compared: the {_format_count(comparison_scope["original_successful_item_count"], "original successful item count")} originally successful source traces. {_escape(unresolved_summary)}; no source outcome is reclassified. <span class="note">Receipt states: {unresolved_text}</span></p>
    <div class="table-scroll" role="region" aria-label="Matched chromatogram comparisons" tabindex="0">
      <table class="comparison-table"><caption>Matched historical and fresh evidence</caption><thead><tr><th scope="col">Trace</th><th scope="col">Historical analysis</th><th scope="col">Fresh analysis</th><th scope="col">Points</th><th scope="col">Peaks</th><th scope="col">Max absolute error</th><th scope="col">Max relative error</th></tr></thead><tbody>{_comparison_rows(data, report_prefix)}</tbody></table>
    </div>
    <p class="note">Declared numerical tolerances are absolute {absolute_tolerance} and relative {relative_tolerance}. Trace identity, method, categorical fields, nulls, and structural identifiers are compared exactly. Backend versions are observations, not an equality gate.</p>
  </section>

  <section aria-labelledby="reports-heading">
    <h2 id="reports-heading">{_format_count(len(data["records"]), "report count")} retained report views</h2>
    <p>Each card links directly to the unchanged report bytes, SVG preview, evidence JSON, and CSV exports. Historical cards are imported stored results; fresh cards are new-store R re-executions.</p>
    <div class="report-grid">{_report_cards(data, report_prefix)}</div>
  </section>

  <section aria-labelledby="details-heading">
    <h2 id="details-heading">Methods, trace identity, and reproduction</h2>
    <details><summary>Exact method and selected trace identity</summary>
      <p>Selected trace metadata and the frozen method are shown as recorded by the workflow receipt.</p>
      <div class="table-scroll" role="region" aria-label="Selected trace identity" tabindex="0"><table><caption>Selected trace identities</caption><thead><tr><th scope="col">Label</th><th scope="col">Trace ID</th><th scope="col">Role</th><th scope="col">Sample type</th><th scope="col">Axis unit</th><th scope="col">Signal unit</th><th scope="col">Points</th></tr></thead><tbody>{_trace_rows(data)}</tbody></table></div>
      <h3>Frozen method JSON</h3><pre>{_escape(_pretty_json(method))}</pre>
    </details>
    <details><summary>Observed runtime provenance</summary>
      <p>These are the exact source and fresh observations retained in the receipt. They document runtime context; they are not asserted equal across executions.</p>
      <pre>{_escape(_pretty_json(runtime))}</pre>
    </details>
    <details><summary>Scope of the retained run</summary><p>{scope}</p></details>
    <details><summary>Reproduction instructions</summary>
      <ol><li>Place this index beside the unchanged report directory published at <code>{_escape(report_prefix)}</code>.</li><li>Open this local HTML file without a web server; all links are local relative paths and no JavaScript or external resources are required.</li><li>To regenerate, use <code>python scripts/render_batch_snapshot_publication.py --report-dir output --destination index.html --report-prefix output</code>. The destination must be new.</li></ol>
    </details>
  </section>
</main>
<footer><p>Generated from the frozen workflow receipt and verified report hashes. Original report, CSV, JSON, SVG, and ZIP bytes are not rewritten.</p></footer>
</body>
</html>
"""
    return html_text.encode("utf-8")


def _write_exclusive(destination: Path, data: bytes) -> None:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Publication destination must be new: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(destination, flags, 0o644)
    except FileExistsError as error:
        raise FileExistsError(f"Publication destination must be new: {destination}") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)


def render_publication(
    report_dir: Path | str,
    destination: Path | str,
    *,
    report_prefix: str = "output",
) -> dict[str, Any]:
    """Validate a frozen delivery and write one deterministic publication index."""

    report_root = Path(report_dir).expanduser()
    destination_path = Path(destination).expanduser()
    prefix = _safe_relative(report_prefix, "report prefix")
    receipt, receipt_sha256 = _read_receipt(report_root)
    data = _validate_receipt(receipt, report_root)
    if destination_path.exists() or destination_path.is_symlink():
        raise FileExistsError(f"Publication destination must be new: {destination_path}")
    content = _render_html(data, receipt, prefix)
    _write_exclusive(destination_path, content)
    return {
        "sha256": _sha256(content),
        "size_bytes": len(content),
        "source_receipt_sha256": receipt_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--report-prefix", default="output")
    arguments = parser.parse_args(argv)
    try:
        metadata = render_publication(
            arguments.report_dir,
            arguments.destination,
            report_prefix=arguments.report_prefix,
        )
    except (FileExistsError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
