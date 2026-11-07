"""Capture, archive, restore and re-run a portable chromatogram batch.

This example uses one synthetic wide CSV with five 501-point traces.  Four
traces are selected into a durable batch and one remains in the complete
dataset closure but is intentionally unselected.  The source batch is paused
after two successful analyses, one controlled R-start failure, cancellation,
and an explicit retry selection.  Both a metadata-only reference snapshot and
a self-contained snapshot are then collected through public MCP tools.

The self-contained archive is restored into a new local store.  Imported
historical analyses are read and rendered without R, while the new pending
batch is executed explicitly in two fresh R-backed runs.  The comparison is a
declared numerical comparison of stored evidence; it is not a physical,
identity, purity, calibration, or instrument qualification claim.

For an installed environment::

    python examples/batch_snapshot.py \
        --output-dir /tmp/measure-batch-snapshot-output \
        --source-dir /tmp/measure-batch-snapshot-source \
        --restore-dir /tmp/measure-batch-snapshot-restore

The source and restore paths are optional when calling :func:`run` or the
command line.  If omitted, new temporary sibling directories are used.  Any
caller-supplied output, source, restore, or reference-rejection target must be
absent and non-nested before this example creates a directory.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import html
import io
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

try:
    from chromatogram_batch import _call, _normal_r_command, _server_session, _trace_signal
except ModuleNotFoundError:  # pragma: no cover - supports importing from the repository root
    from examples.chromatogram_batch import _call, _normal_r_command, _server_session, _trace_signal

from measure_service.batch_bundle import write_bundle
from measure_service.batch_restore import restore_bundle
from measure_service.batch_snapshot import decode_snapshot, snapshot_manifest
from measure_service.batch_snapshot_client import collect_batch_snapshot
from measure_service.evidence_client import collect_chromatogram_evidence
from measure_service.evidence_report import write_report

PROFILE = "measure-chromatogram-batch-snapshot-example/1"
TRACE_NAMES = ("reference", "blank", "qc", "unknown", "unselected")
SELECTED_NAMES = TRACE_NAMES[:4]
FAILURE_R = "/nonexistent/measure-batch-snapshot-example-r"
ABSOLUTE_TOLERANCE = 1e-10
RELATIVE_TOLERANCE = 1e-8
EXECUTION_METADATA_FIELDS = ("packages", "installed_r_code_md5", "r_version")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_exclusive(path: Path, content: bytes) -> str:
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"Example artifact already exists: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    return _sha256_bytes(content)


def _preflight_new_paths(paths: dict[str, Path]) -> dict[str, Path]:
    """Validate every target before creating any output, store, or restore path."""

    expanded = {name: path.expanduser() for name, path in paths.items()}
    existing = [name for name, path in expanded.items() if os.path.lexists(path)]
    if existing:
        raise FileExistsError(
            "Batch snapshot output, source, restore, and rejection paths must be new: "
            + ", ".join(existing)
        )
    resolved = {name: path.resolve(strict=False) for name, path in expanded.items()}
    names = list(resolved)
    for index, left_name in enumerate(names):
        left = resolved[left_name]
        for right_name in names[index + 1 :]:
            right = resolved[right_name]
            if left == right or left in right.parents or right in left.parents:
                raise ValueError(
                    "Batch snapshot output, source, restore, and rejection paths must be "
                    "separate, non-nested directories"
                )
    return resolved


def _method() -> dict[str, Any]:
    return {
        "baseline": "als",
        "baseline_lambda": 1e6,
        "baseline_p": 0.01,
        "smoothing": "gaussian",
        "smoothing_sigma": 2,
        "min_height": 0,
        "min_prominence": 0.3,
        "min_distance": 0,
        "integration_baseline": "local",
    }


def _axis() -> list[float]:
    return [index / 100 for index in range(501)]


def _csv_text(axis: list[float]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(["time", *TRACE_NAMES])
    signals = {
        name: _trace_signal("unknown" if name == "unselected" else name, axis)
        for name in TRACE_NAMES
    }
    for index, value in enumerate(axis):
        row = [f"{value:.12g}"]
        for name in TRACE_NAMES:
            row.append(f"{signals[name][index]:.16g}")
        writer.writerow(row)
    return stream.getvalue()


def _channels() -> list[dict[str, Any]]:
    sample_types = {
        "reference": "reference",
        "blank": "blank",
        "qc": "qc",
        "unknown": "unknown",
        "unselected": "unknown",
    }
    return [
        {
            "column": name,
            "metadata": {
                "trace_id": name,
                "sample_id": f"snapshot-{name}",
                "injection_id": f"injection-{name}",
                "channel": "UV-254",
                "separation": "LC",
                "detector": "UV",
                "axis_unit": "min",
                "signal_unit": "mAU",
                "sample_type": sample_types[name],
                "batch_id": "synthetic-snapshot-source",
                "run_order": index,
            },
        }
        for index, name in enumerate(TRACE_NAMES)
    ]


def _item_states(page: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(item["ordinal"]): item for item in page["items"]}


def _assert_run_states(run: dict[str, Any], expected: dict[int, str]) -> None:
    states = {int(item["ordinal"]): item["state"] for item in run["items"]}
    for ordinal, state in expected.items():
        if states.get(ordinal) != state:
            raise AssertionError(
                f"run {run.get('run_id')} ordinal {ordinal} was {states.get(ordinal)!r}"
            )


def _archive_info(output_dir: Path, name: str, snapshot: bytes) -> dict[str, Any]:
    path = output_dir / name
    written = write_bundle(snapshot, path)
    return {
        "path": path.relative_to(output_dir).as_posix(),
        "sha256": written["sha256"],
        "size_bytes": written["size_bytes"],
        "snapshot_sha256": written["snapshot_sha256"],
    }


def _report_info(
    output_dir: Path, section: str, ordinal: int, bundle: dict[str, Any]
) -> dict[str, Any]:
    directory = output_dir / section / f"{ordinal:03d}"
    published = write_report(bundle, directory)
    files = {}
    for filename, metadata in published.items():
        path = directory / filename
        relative = path.relative_to(output_dir).as_posix()
        files[filename] = {
            "path": relative,
            "sha256": metadata["sha256"],
            "size_bytes": path.stat().st_size,
        }
    return {
        "section": section,
        "ordinal": ordinal,
        "analysis_id": bundle["analysis"]["analysis_id"],
        "evidence_sha256": bundle["evidence_sha256"],
        "stored_result_sha256": bundle["analysis"]["stored_result_sha256"],
        "point_count": bundle["point_count"],
        "peak_count": bundle["peak_count"],
        "result_metadata": bundle["result"],
        "result_metadata_sha256": _sha256_json(bundle["result"]),
        "files": files,
    }


def _compare_categorical(left: Any, right: Any, label: str, stats: dict[str, float]) -> None:
    stats["comparison_count"] += 1
    if type(left) is not type(right) or left != right:
        raise AssertionError(f"{label} changed from {left!r} to {right!r}")
    stats["categorical_comparison_count"] += 1


def _compare_numeric(left: Any, right: Any, label: str, stats: dict[str, float]) -> None:
    if left is None or right is None:
        _compare_categorical(left, right, label, stats)
        return
    if isinstance(left, bool) or isinstance(right, bool):
        _compare_categorical(left, right, label, stats)
        return
    if not isinstance(left, (int, float)):
        _compare_categorical(left, right, label, stats)
        return
    if not isinstance(right, (int, float)) or isinstance(right, bool):
        raise AssertionError(f"{label} changed from numeric {left!r} to {right!r}")
    stats["comparison_count"] += 1
    if not math.isfinite(float(left)) or not math.isfinite(float(right)):
        raise AssertionError(f"{label} is not finite: {left!r} vs {right!r}")
    stats["numeric_comparison_count"] += 1
    difference = abs(float(left) - float(right))
    scale = max(abs(float(left)), abs(float(right)), 1e-300)
    relative = difference / scale
    stats["max_absolute_error"] = max(stats["max_absolute_error"], difference)
    stats["max_relative_error"] = max(stats["max_relative_error"], relative)
    if not math.isclose(
        float(left),
        float(right),
        abs_tol=ABSOLUTE_TOLERANCE,
        rel_tol=RELATIVE_TOLERANCE,
    ):
        raise AssertionError(f"{label} exceeded declared tolerance: {left!r} vs {right!r}")


def _compare_value(left: Any, right: Any, label: str, stats: dict[str, float]) -> None:
    """Compare nested metadata while applying tolerances only to finite numbers."""

    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict) or set(left) != set(right):
            raise AssertionError(f"{label} changed mapping shape")
        for key in left:
            _compare_value(left[key], right[key], f"{label}.{key}", stats)
        return
    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
            raise AssertionError(f"{label} changed sequence shape")
        for index, (left_value, right_value) in enumerate(zip(left, right)):
            _compare_value(left_value, right_value, f"{label}[{index}]", stats)
        return
    _compare_numeric(left, right, label, stats)


def _compare_evidence(source: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    stats = {
        "max_absolute_error": 0.0,
        "max_relative_error": 0.0,
        "comparison_count": 0,
        "numeric_comparison_count": 0,
        "categorical_comparison_count": 0,
    }
    for label, left, right in (
        ("trace metadata", source["trace"], fresh["trace"]),
        ("processing method", source["analysis"]["method"], fresh["analysis"]["method"]),
    ):
        if _canonical_bytes(left) != _canonical_bytes(right):
            raise AssertionError(f"{label} identity changed during fresh re-execution")
        _compare_categorical(left, right, label, stats)
    scientific_results = [
        {
            key: value
            for key, value in evidence["result"].items()
            if key not in EXECUTION_METADATA_FIELDS
        }
        for evidence in (source, fresh)
    ]
    _compare_value(scientific_results[0], scientific_results[1], "result", stats)
    if len(source["points"]) != len(fresh["points"]):
        raise AssertionError("point counts changed during fresh re-execution")
    if len(source["peaks"]) != len(fresh["peaks"]):
        raise AssertionError("peak row counts changed during fresh re-execution")
    point_numeric = ("axis", "processed_axis", "raw_signal", "processed_signal")
    for index, (left, right) in enumerate(zip(source["points"], fresh["points"])):
        if type(left["sample_index"]) is not int or type(right["sample_index"]) is not int:
            raise AssertionError(f"sample index {index} is not an integer")
        if left["sample_index"] != index or right["sample_index"] != index:
            raise AssertionError(f"sample index {index} changed during fresh re-execution")
        _compare_categorical(
            left["sample_index"], right["sample_index"], f"point {index} sample_index", stats
        )
        for field in point_numeric:
            _compare_numeric(left[field], right[field], f"point {index} {field}", stats)
    peak_numeric = ("location", "height", "left_base", "right_base", "area", "prominence")
    peak_categorical = ("peak_id", "area_basis")
    for index, (left, right) in enumerate(zip(source["peaks"], fresh["peaks"])):
        for field in peak_categorical:
            if field == "peak_id":
                if type(left.get(field)) is not int or type(right.get(field)) is not int:
                    raise AssertionError(f"peak {index} ID is not an integer")
                _compare_categorical(left[field], right[field], f"peak {index} {field}", stats)
            else:
                _compare_value(left.get(field), right.get(field), f"peak {index} {field}", stats)
        for field in peak_numeric:
            _compare_numeric(left.get(field), right.get(field), f"peak {index} {field}", stats)
    return {
        "passed": True,
        "source_analysis_id": source["analysis"]["analysis_id"],
        "fresh_analysis_id": fresh["analysis"]["analysis_id"],
        "point_count": len(source["points"]),
        "peak_count": len(source["peaks"]),
        "absolute_tolerance": ABSOLUTE_TOLERANCE,
        "relative_tolerance": RELATIVE_TOLERANCE,
        "execution_provenance": {
            "equality_required": False,
            "source": source["analysis"].get("provenance"),
            "fresh": fresh["analysis"].get("provenance"),
            "source_result_fields": {
                key: source["result"][key]
                for key in EXECUTION_METADATA_FIELDS
                if key in source["result"]
            },
            "fresh_result_fields": {
                key: fresh["result"][key]
                for key in EXECUTION_METADATA_FIELDS
                if key in fresh["result"]
            },
        },
        **stats,
        "compared_fields": {
            "point_numeric": list(point_numeric),
            "peak_numeric": list(peak_numeric),
            "mixed_metadata": ["scientific_result_metadata"],
            "categorical": ["trace_metadata", "method", "sample_index", *peak_categorical],
            "separately_recorded_execution_metadata": list(EXECUTION_METADATA_FIELDS),
        },
    }


def _csv_bytes(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _summary_html(receipt: dict[str, Any]) -> bytes:
    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    rows = []
    for record in receipt["historical_imports"]:
        rows.append(
            "<tr><td>Imported historical</td>"
            f"<td>{esc(record['source_analysis_id'])}</td>"
            f"<td>{esc(record['analysis_id'])}</td>"
            f'<td><a href="{esc(record["report"]["files"]["report.html"]["path"])}">report</a></td></tr>'
        )
    for record in receipt["fresh_reexecution"]:
        rows.append(
            "<tr><td>Fresh R re-execution</td><td>not applicable</td>"
            f"<td>{esc(record['analysis_id'])}</td>"
            f'<td><a href="{esc(record["report"]["files"]["report.html"]["path"])}">report</a></td></tr>'
        )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portable chromatogram batch snapshot</title>
<style>
body {{ max-width: 1120px; margin: 2rem auto; padding: 0 1rem; color: #23313a; font: 16px/1.5 system-ui, sans-serif; }}
.table-scroll {{ max-width: 100%; box-sizing: border-box; overflow-x: auto; border: 1px solid #d4dbdf; }}
.table-scroll:focus {{ outline: 3px solid #007f78; outline-offset: 2px; }}
.scroll-hint {{ margin: .5rem .75rem; color: #4b5a62; font-size: .9rem; }}
table {{ border-collapse: collapse; min-width: 48rem; width: 100%; }}
th, td {{ border: 1px solid #cbd3d8; padding: .45rem .6rem; text-align: left; vertical-align: top; white-space: nowrap; }}
th {{ background: #f1f5f6; }}
</style></head><body>
<h1>Portable chromatogram batch snapshot</h1>
<p>{esc(receipt["scope"])}</p>
<h2>Imported historical analyses</h2>
<p>These two analyses were restored from stored evidence and rendered without R. They are not fresh computations.</p>
<h2>Fresh re-executions</h2>
<p>These four analyses were produced by explicit post-restore R runs. The comparison covers only the two originally successful traces.</p>
<div class="table-scroll" role="region" aria-label="Snapshot analysis reports" aria-describedby="summary-table-hint" tabindex="0">
<p class="scroll-hint" id="summary-table-hint">Scroll horizontally to view all analysis report links.</p>
<table><thead><tr><th>Section</th><th>Source analysis</th><th>Local analysis</th><th>Report</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
</div>
<p>Declared comparison tolerances: absolute {esc(ABSOLUTE_TOLERANCE)}, relative {esc(RELATIVE_TOLERANCE)}. No identity, purity, concentration, calibration, or instrument qualification is inferred.</p>
<p>Trace metadata and processing methods match exactly. Runtime versions and code fingerprints are retained separately and do not impose an exact-version equality requirement on the numerical comparison.</p>
</body></html>
"""
    return document.encode("utf-8")


async def _workflow(
    output_dir: Path,
    source_dir: Path,
    restore_dir: Path,
    reference_restore_dir: Path,
) -> dict[str, Any]:
    axis = _axis()
    method = _method()
    source_dataset: dict[str, Any]
    batch_id: str
    first_run: dict[str, Any]
    failure_run: dict[str, Any]
    retry_selection: dict[str, Any]
    first_ids: list[str]
    source_analysis_records: list[dict[str, Any]]
    original_evidence: list[dict[str, Any]]

    async with _server_session(str(source_dir), r_command=_normal_r_command()) as client:
        source_dataset = await _call(
            client,
            "measurement_register_wide_csv",
            {
                "title": "Synthetic five-trace snapshot source",
                "csv_text": _csv_text(axis),
                "axis_column": "time",
                "channels": _channels(),
                "delimiter": ",",
            },
        )
        created = await _call(
            client,
            "measurement_create_chromatogram_batch",
            {
                "batch": {
                    "schema_version": "1.0",
                    "profile": "measure-chromatogram-batch/1",
                    "title": "Synthetic portable batch snapshot",
                    "method": method,
                    "inputs": [
                        {
                            "dataset_id": source_dataset["dataset_id"],
                            "trace_id": name,
                            "role": name,
                            "internal_standard_references": (
                                ["caller-declared reference label"] if name == "unknown" else []
                            ),
                        }
                        for name in SELECTED_NAMES
                    ],
                },
                "idempotency_key": "snapshot-example-create-v1",
            },
        )
        batch_id = created["batch_id"]
        initial = await _call(client, "measurement_get_chromatogram_batch", {"batch_id": batch_id})
        initial_items = await _call(
            client,
            "measurement_read_chromatogram_batch_items",
            {"batch_id": batch_id, "offset": 0, "limit": 20},
        )
        initial_by_ordinal = _item_states(initial_items)
        if [initial_by_ordinal[i]["role"] for i in range(4)] != list(SELECTED_NAMES):
            raise AssertionError("selected roles were not frozen in declared order")
        if [initial_by_ordinal[i]["trace"]["sample_type"] for i in range(4)] != list(
            SELECTED_NAMES
        ):
            raise AssertionError("original selected sample types were not preserved")
        if initial_by_ordinal[3]["internal_standard_references"] != [
            "caller-declared reference label"
        ]:
            raise AssertionError("unknown caller declaration was not retained")
        first_run = await _call(
            client,
            "measurement_run_chromatogram_batch",
            {
                "batch_id": batch_id,
                "idempotency_key": "snapshot-example-run-first-v1",
                "expected_revision": initial["revision"],
                "max_items": 2,
            },
        )
        _assert_run_states(first_run, {0: "completed", 1: "completed"})
        first_ids = [item["analysis_id"] for item in first_run["items"]]
        if len(first_ids) != 2 or not all(isinstance(identifier, str) for identifier in first_ids):
            raise AssertionError("first two successful analyses lacked IDs")
        source_analysis_records = [
            await _call(client, "measurement_get_analysis", {"analysis_id": identifier})
            for identifier in first_ids
        ]
        after_first = await _call(
            client, "measurement_get_chromatogram_batch", {"batch_id": batch_id}
        )

    async with _server_session(str(source_dir), r_command=FAILURE_R) as client:
        failure_run = await _call(
            client,
            "measurement_run_chromatogram_batch",
            {
                "batch_id": batch_id,
                "idempotency_key": "snapshot-example-run-failure-v1",
                "expected_revision": after_first["revision"],
                "max_items": 1,
            },
        )
        _assert_run_states(failure_run, {2: "failed"})
        failed_attempts = await _call(
            client,
            "measurement_read_chromatogram_batch_attempts",
            {"batch_id": batch_id, "ordinal": 2, "offset": 0, "limit": 20},
        )
        if failed_attempts["total"] != 1:
            raise AssertionError("controlled failure did not retain exactly one attempt")
        original_failure = failed_attempts["attempts"][0]
        failure_error = original_failure.get("error") or {}
        if original_failure["runtime"] != {"runtime_status": "unknown"}:
            raise AssertionError("controlled failure did not retain unknown runtime evidence")
        if failure_error.get("r_runtime_evidence", "unknown_until_success") != (
            "unknown_until_success"
        ):
            raise AssertionError("controlled failure unexpectedly asserted observed R evidence")
        before_cancel = await _call(
            client, "measurement_get_chromatogram_batch", {"batch_id": batch_id}
        )
        cancelled = await _call(
            client,
            "measurement_cancel_chromatogram_batch",
            {
                "batch_id": batch_id,
                "idempotency_key": "snapshot-example-cancel-v1",
                "expected_revision": before_cancel["revision"],
            },
        )
        if cancelled["counts"] != {
            "pending": 0,
            "running": 0,
            "completed": 2,
            "failed": 1,
            "interrupted": 0,
            "cancelled": 1,
        }:
            raise AssertionError("cancellation did not checkpoint the fourth pending item")
        cancellation_checkpoint = await _call(
            client,
            "measurement_read_chromatogram_batch_items",
            {"batch_id": batch_id, "offset": 0, "limit": 20},
        )
        retry_selection = await _call(
            client,
            "measurement_retry_chromatogram_batch_items",
            {
                "batch_id": batch_id,
                "ordinals": [2, 3],
                "idempotency_key": "snapshot-example-retry-2-3-v1",
                "expected_revision": cancelled["revision"],
            },
        )
        if retry_selection["counts"] != {
            "pending": 2,
            "running": 0,
            "completed": 2,
            "failed": 0,
            "interrupted": 0,
            "cancelled": 0,
        }:
            raise AssertionError("retry did not select exactly ordinals 2 and 3")
        frozen = await _call(client, "measurement_get_chromatogram_batch", {"batch_id": batch_id})
        attempts = {
            str(ordinal): await _call(
                client,
                "measurement_read_chromatogram_batch_attempts",
                {"batch_id": batch_id, "ordinal": ordinal, "offset": 0, "limit": 20},
            )
            for ordinal in range(4)
        }
        controls = await _call(
            client,
            "measurement_read_chromatogram_batch_control_events",
            {"batch_id": batch_id, "offset": 0, "limit": 20},
        )
        if frozen["counts"]["completed"] != 2 or frozen["counts"]["pending"] != 2:
            raise AssertionError("source was not frozen at two completed and two pending items")
        if sum(page["total"] for page in attempts.values()) != 3 or controls["total"] != 2:
            raise AssertionError("source history did not retain three attempts and two controls")
        source_db_hash_before = _sha256_file(source_dir / "measure.sqlite3")
        references = await collect_batch_snapshot(
            client, batch_id, frozen["revision"], "references", page_size=16_000
        )
        self_contained = await collect_batch_snapshot(
            client, batch_id, frozen["revision"], "self_contained", page_size=16_000
        )
        original_evidence = [
            await collect_chromatogram_evidence(client, identifier, page_size=128)
            for identifier in first_ids
        ]
    source_db_hash_after = _sha256_file(source_dir / "measure.sqlite3")
    if source_db_hash_before != source_db_hash_after:
        raise AssertionError("snapshot exports changed the source database")

    reference_document = decode_snapshot(references)
    self_contained_document = decode_snapshot(self_contained)
    if reference_document["mode"] != "references" or self_contained_document["mode"] != (
        "self_contained"
    ):
        raise AssertionError("snapshot modes were not preserved")
    dataset_entry = self_contained_document["datasets"][0]
    dataset_payload = dataset_entry["payload"]
    source_traces = dataset_payload["traces"]
    source_trace_ids = [trace["trace_id"] for trace in source_traces]
    unselected_trace_ids = [
        trace_id for trace_id in source_trace_ids if trace_id not in SELECTED_NAMES
    ]
    selected_point_count = sum(
        len(trace["axis"]) for trace in source_traces if trace["trace_id"] in SELECTED_NAMES
    )
    original_sample_types = {trace["trace_id"]: trace["sample_type"] for trace in source_traces}
    if dataset_entry["trace_count"] != 5 or dataset_entry["point_count"] != 2505:
        raise AssertionError("self-contained snapshot did not retain all five 501-point traces")
    if selected_point_count != 2004 or not unselected_trace_ids:
        raise AssertionError("selected or unselected dataset closure was not retained")
    if reference_document["datasets"][0]["payload"] is not None:
        raise AssertionError("reference snapshot unexpectedly carried a dataset payload")
    if reference_document["datasets"][0]["source"]["content"] is not None:
        raise AssertionError("reference snapshot unexpectedly carried source text")
    if any(entry["result"] is not None for entry in reference_document["analyses"]):
        raise AssertionError("reference snapshot unexpectedly carried analysis results")
    if len(self_contained_document["analyses"]) != 2:
        raise AssertionError("self-contained snapshot did not retain the two successes")
    if (
        len(self_contained_document["attempts"]) != 3
        or len(self_contained_document["control_events"]) != 2
        or len(self_contained_document["runs"]) != 2
    ):
        raise AssertionError("self-contained snapshot did not retain source operational history")
    if not any(run["status"] == "failed" for run in self_contained_document["runs"]):
        raise AssertionError("self-contained snapshot lost the original failed run")

    reference_archive = _archive_info(output_dir, "source-references.zip", references)
    self_contained_archive = _archive_info(output_dir, "source-self-contained.zip", self_contained)
    reference_error = None
    try:
        restore_bundle(output_dir / reference_archive["path"], reference_restore_dir)
    except ValueError as error:
        reference_error = str(error)
    if reference_error is None or reference_restore_dir.exists():
        raise AssertionError("reference restore did not fail before creating its target")
    restore_receipt = restore_bundle(output_dir / self_contained_archive["path"], restore_dir)
    restored_batch_id = restore_receipt["new_batch_id"]
    if len(restore_receipt["dataset_map"]) != 1 or len(restore_receipt["analysis_map"]) != 2:
        raise AssertionError("restore did not import one dataset and two historical analyses")
    if any(
        source_id == local_id for source_id, local_id in restore_receipt["analysis_map"].items()
    ):
        raise AssertionError("restore reused a source analysis ID")

    historical_imports: list[dict[str, Any]] = []
    async with _server_session(str(restore_dir), r_command=FAILURE_R) as client:
        restored_batch = await _call(
            client, "measurement_get_chromatogram_batch", {"batch_id": restored_batch_id}
        )
        restored_items = await _call(
            client,
            "measurement_read_chromatogram_batch_items",
            {"batch_id": restored_batch_id, "offset": 0, "limit": 20},
        )
        restored_controls = await _call(
            client,
            "measurement_read_chromatogram_batch_control_events",
            {"batch_id": restored_batch_id, "offset": 0, "limit": 20},
        )
        restored_attempts = {
            str(ordinal): await _call(
                client,
                "measurement_read_chromatogram_batch_attempts",
                {"batch_id": restored_batch_id, "ordinal": ordinal, "offset": 0, "limit": 20},
            )
            for ordinal in range(4)
        }
        import_receipt = await _call(
            client,
            "measurement_get_analytical_import",
            {"import_id": restore_receipt["import_id"]},
        )
        if import_receipt != restore_receipt:
            raise AssertionError("public import receipt differed from the restore receipt")
        imported_ids = [restore_receipt["analysis_map"][identifier] for identifier in first_ids]
        for ordinal, (source_id, local_id, source_record, source_bundle) in enumerate(
            zip(first_ids, imported_ids, source_analysis_records, original_evidence)
        ):
            analysis = await _call(client, "measurement_get_analysis", {"analysis_id": local_id})
            if "import_origin" not in analysis or "input_import_origin" not in analysis:
                raise AssertionError("imported analysis did not expose both import origins")
            if analysis["provenance"] != source_record["provenance"]:
                raise AssertionError("imported analysis provenance was rewritten")
            evidence = await collect_chromatogram_evidence(client, local_id, page_size=128)
            if (
                evidence["analysis"]["stored_result_sha256"]
                != source_bundle["analysis"]["stored_result_sha256"]
            ):
                raise AssertionError("imported stored result changed before fresh execution")
            if evidence["result"] != source_bundle["result"]:
                raise AssertionError("imported result JSON changed before fresh execution")
            report = _report_info(output_dir, "historical", ordinal, evidence)
            historical_imports.append(
                {
                    "source_analysis_id": source_id,
                    "analysis_id": local_id,
                    "analysis": analysis,
                    "evidence_sha256": evidence["evidence_sha256"],
                    "stored_result_sha256": evidence["analysis"]["stored_result_sha256"],
                    "report": report,
                }
            )

    if restored_batch["counts"] != {
        "pending": 4,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "interrupted": 0,
        "cancelled": 0,
    }:
        raise AssertionError("restored batch did not start with four pending items")
    if (
        restored_controls["total"] != 0
        or sum(page["total"] for page in restored_attempts.values()) != 0
    ):
        raise AssertionError("historical operational history became active after restore")
    restored_item_states = _item_states(restored_items)
    if [restored_item_states[i]["role"] for i in range(4)] != list(SELECTED_NAMES):
        raise AssertionError("restored roles changed")

    async with _server_session(str(restore_dir), r_command=_normal_r_command()) as client:
        first_fresh_run = await _call(
            client,
            "measurement_run_chromatogram_batch",
            {
                "batch_id": restored_batch_id,
                "idempotency_key": "snapshot-example-restored-run-first-v1",
                "expected_revision": restored_batch["revision"],
                "max_items": 2,
            },
        )
        _assert_run_states(first_fresh_run, {0: "completed", 1: "completed"})
        after_first_fresh = await _call(
            client, "measurement_get_chromatogram_batch", {"batch_id": restored_batch_id}
        )

    async with _server_session(str(restore_dir), r_command=_normal_r_command()) as client:
        second_fresh_run = await _call(
            client,
            "measurement_run_chromatogram_batch",
            {
                "batch_id": restored_batch_id,
                "idempotency_key": "snapshot-example-restored-run-second-v1",
                "expected_revision": after_first_fresh["revision"],
                "max_items": 2,
            },
        )
        _assert_run_states(second_fresh_run, {2: "completed", 3: "completed"})
        final_batch = await _call(
            client, "measurement_get_chromatogram_batch", {"batch_id": restored_batch_id}
        )
        final_items = await _call(
            client,
            "measurement_read_chromatogram_batch_items",
            {"batch_id": restored_batch_id, "offset": 0, "limit": 20},
        )
        fresh_items = _item_states(final_items)
        fresh_ids = [fresh_items[index]["analysis_id"] for index in range(4)]
        fresh_analysis_records = [
            await _call(client, "measurement_get_analysis", {"analysis_id": identifier})
            for identifier in fresh_ids
        ]
        fresh_evidence = [
            await collect_chromatogram_evidence(client, identifier, page_size=128)
            for identifier in fresh_ids
        ]
        fresh_reports = [
            _report_info(output_dir, "fresh", ordinal, evidence)
            for ordinal, evidence in enumerate(fresh_evidence)
        ]

    if final_batch["counts"] != {
        "pending": 0,
        "running": 0,
        "completed": 4,
        "failed": 0,
        "interrupted": 0,
        "cancelled": 0,
    }:
        raise AssertionError("fresh restored runs did not complete four items")
    if len(set(first_ids + fresh_ids)) != 6:
        raise AssertionError("historical and fresh analyses did not retain six unique IDs")
    if any("import_origin" in analysis for analysis in fresh_analysis_records):
        raise AssertionError("fresh analyses incorrectly claimed analytical import origin")
    if not all("input_import_origin" in analysis for analysis in fresh_analysis_records):
        raise AssertionError("fresh analyses did not retain imported dataset origin")
    comparisons = [
        _compare_evidence(original_evidence[index], fresh_evidence[index]) for index in range(2)
    ]

    table_rows = [
        {
            "section": "historical_import",
            "ordinal": record["report"]["ordinal"],
            "source_analysis_id": record["source_analysis_id"],
            "local_analysis_id": record["analysis_id"],
            "report_path": record["report"]["files"]["report.html"]["path"],
            "comparison": "not fresh computation",
        }
        for record in historical_imports
    ] + [
        {
            "section": "fresh_reexecution",
            "ordinal": record["ordinal"],
            "source_analysis_id": "not applicable",
            "local_analysis_id": record["analysis_id"],
            "report_path": record["files"]["report.html"]["path"],
            "comparison": "compared" if record["ordinal"] < 2 else "outside comparison scope",
        }
        for record in fresh_reports
    ]
    table_name = "analysis-index.csv"
    table_path = output_dir / table_name
    table_hash = _write_exclusive(
        table_path,
        _csv_bytes(
            table_rows,
            (
                "section",
                "ordinal",
                "source_analysis_id",
                "local_analysis_id",
                "report_path",
                "comparison",
            ),
        ),
    )
    summary_path = output_dir / "summary.html"
    summary_hash = _write_exclusive(
        summary_path,
        _summary_html(
            {
                "scope": (
                    "Synthetic chromatogram evidence only; stored results are retained and compared "
                    "without optimization, identity, purity, concentration, calibration, or instrument "
                    "qualification claims."
                ),
                "historical_imports": historical_imports,
                "fresh_reexecution": fresh_reports,
            }
        ),
    )

    final_ids = [fresh_items[index]["analysis_id"] for index in range(4)]
    canonical_file_hashes = {
        reference_archive["path"]: reference_archive["sha256"],
        self_contained_archive["path"]: self_contained_archive["sha256"],
        table_name: table_hash,
        "summary.html": summary_hash,
    }
    for record in historical_imports:
        canonical_file_hashes.update(
            {
                metadata["path"]: metadata["sha256"]
                for metadata in record["report"]["files"].values()
            }
        )
    for report in fresh_reports:
        canonical_file_hashes.update(
            {metadata["path"]: metadata["sha256"] for metadata in report["files"].values()}
        )
    receipt: dict[str, Any] = {
        "profile": PROFILE,
        "scope": (
            "Synthetic chromatogram evidence only; stored results are retained and compared "
            "without optimization, identity, purity, concentration, calibration, or instrument "
            "qualification claims."
        ),
        "source": {
            "dataset": {
                "dataset_id": source_dataset["dataset_id"],
                "input_sha256": source_dataset["input_sha256"],
                "source_sha256": source_dataset["source_sha256"],
                "trace_count": source_dataset["trace_count"],
                "point_count": source_dataset["point_count"],
                "selected_point_count": selected_point_count,
                "selected_trace_ids": list(SELECTED_NAMES),
                "unselected_trace_ids": unselected_trace_ids,
                "original_sample_types": original_sample_types,
                "source_text_line_ending": (
                    "CRLF"
                    if dataset_entry["source"]["content"].endswith("\r\n")
                    and "\r\n" in dataset_entry["source"]["content"]
                    else "other"
                ),
            },
            "batch_id": batch_id,
            "freeze_revision": frozen["revision"],
            "database_sha256_before_exports": source_db_hash_before,
            "database_sha256_after_exports": source_db_hash_after,
            "history": {
                "initial": initial,
                "after_first_run": after_first,
                "after_failure": before_cancel,
                "after_cancel": cancelled,
                "after_retry_selection": retry_selection,
                "frozen": frozen,
                "runs": {"first_success": first_run, "controlled_failure": failure_run},
                "attempts": attempts,
                "controls": controls,
                "cancellation_checkpoint": cancellation_checkpoint,
                "original_failure_unknowns": {
                    "state": original_failure["state"],
                    "runtime": original_failure["runtime"],
                    "r_runtime_evidence": failure_error.get(
                        "r_runtime_evidence", "unknown_until_success"
                    ),
                    "error": failure_error,
                },
            },
            "snapshots": {
                "references": {
                    "manifest": snapshot_manifest(references),
                    "archive": reference_archive,
                    "dataset_payload_present": False,
                    "source_text_present": False,
                    "analysis_results_present": False,
                },
                "self_contained": {
                    "manifest": snapshot_manifest(self_contained),
                    "archive": self_contained_archive,
                    "dataset_trace_count": dataset_entry["trace_count"],
                    "dataset_point_count": dataset_entry["point_count"],
                    "source_text_present": True,
                    "analysis_results_present": True,
                    "attempt_count": len(self_contained_document["attempts"]),
                    "control_event_count": len(self_contained_document["control_events"]),
                    "run_count": len(self_contained_document["runs"]),
                    "original_failed_run_retained": any(
                        run["status"] == "failed" for run in self_contained_document["runs"]
                    ),
                },
            },
        },
        "restore": {
            "reference_restore_rejected": True,
            "reference_restore_error": reference_error,
            "reference_target_created": reference_restore_dir.exists(),
            "receipt": restore_receipt,
            "public_receipt": import_receipt,
            "restored_batch_before_fresh": restored_batch,
            "restored_history_before_fresh": {
                "attempts": sum(page["total"] for page in restored_attempts.values()),
                "controls": restored_controls["total"],
                "runs": 0,
                "no_execution_performed": restore_receipt["no_execution_performed"],
            },
        },
        "historical_imports": historical_imports,
        "fresh_reexecution": [
            {
                "ordinal": ordinal,
                "analysis_id": identifier,
                "analysis": fresh_analysis_records[ordinal],
                "evidence_sha256": evidence["evidence_sha256"],
                "stored_result_sha256": evidence["analysis"]["stored_result_sha256"],
                "report": report,
            }
            for ordinal, (identifier, fresh_analysis, evidence, report) in enumerate(
                zip(fresh_ids, fresh_analysis_records, fresh_evidence, fresh_reports)
            )
        ],
        "comparison_scope": {
            "original_successful_item_count": 2,
            "fresh_completed_item_count": 4,
            "compared_item_count": len(comparisons),
            "original_unresolved_source_items": [
                {"ordinal": 2, "role": "qc", "state": "pending"},
                {"ordinal": 3, "role": "unknown", "state": "pending"},
            ],
            "unresolved_items_are_not_reclassified": True,
        },
        "comparisons": comparisons,
        "tables": {
            table_name: {"path": table_name, "sha256": table_hash, "row_count": len(table_rows)}
        },
        "summary": {"path": "summary.html", "sha256": summary_hash},
        "canonical_file_hashes": canonical_file_hashes,
        "analysis_ids": {
            "original_successes": first_ids,
            "historical_imports": [record["analysis_id"] for record in historical_imports],
            "fresh_reexecutions": final_ids,
            "restored_store_unique_total": len(
                set([record["analysis_id"] for record in historical_imports] + final_ids)
            ),
            "source_and_restore_unique_total": len(
                set(
                    first_ids + [record["analysis_id"] for record in historical_imports] + final_ids
                )
            ),
        },
        "assertions": {
            "all_analysis_calls_used_public_mcp": True,
            "wide_csv_retained_five_traces_and_2505_points": dataset_entry["point_count"] == 2505
            and source_dataset["point_count"] == 2505,
            "selected_batch_point_count_is_2004": frozen["point_count"]
            == selected_point_count
            == 2004,
            "unselected_trace_retained_in_complete_closure": bool(unselected_trace_ids),
            "roles_and_original_sample_types_preserved": True,
            "source_frozen_two_completed_two_pending": frozen["counts"]["completed"] == 2
            and frozen["counts"]["pending"] == 2,
            "source_history_three_attempts_two_controls": sum(
                page["total"] for page in attempts.values()
            )
            == 3
            and controls["total"] == 2,
            "failed_runtime_unknown": original_failure["runtime"] == {"runtime_status": "unknown"},
            "references_rejected_before_target_creation": reference_error is not None
            and not reference_restore_dir.exists(),
            "historical_imports_are_not_active_attempts": sum(
                page["total"] for page in restored_attempts.values()
            )
            == 0,
            "two_historical_and_four_fresh_reports": len(historical_imports) == 2
            and len(fresh_reports) == 4,
            "fresh_analyses_have_dataset_origin_only": all(
                "import_origin" not in analysis and "input_import_origin" in analysis
                for analysis in fresh_analysis_records
            ),
            "two_original_successes_compare_with_declared_tolerances": all(
                comparison["passed"] for comparison in comparisons
            ),
            "no_hidden_retry_or_optimization_or_physical_claim": True,
        },
    }
    receipt_bytes = (
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_exclusive(output_dir / "workflow_receipt.json", receipt_bytes)
    return receipt


def run(
    output_dir: Path,
    *,
    source_dir: str | None = None,
    restore_dir: str | None = None,
) -> dict[str, Any]:
    """Run the complete public MCP snapshot/restore demonstration."""

    output_path = Path(output_dir).expanduser()
    if output_path.exists() or output_path.is_symlink():
        raise FileExistsError(
            "Batch snapshot output, source, restore, and rejection paths must be new: output"
        )
    temporary_root: Path | None = None
    if source_dir is None and restore_dir is None:
        temporary_root = Path(tempfile.mkdtemp(prefix="measure-batch-snapshot-"))
        source_path = temporary_root / "source-store"
        restore_path = temporary_root / "restore-store"
    elif source_dir is None or restore_dir is None:
        raise ValueError("source_dir and restore_dir must be supplied together")
    else:
        source_path = Path(source_dir).expanduser()
        restore_path = Path(restore_dir).expanduser()
    reference_restore_path = restore_path.with_name(restore_path.name + "-references")
    paths = _preflight_new_paths(
        {
            "output": output_path,
            "source": source_path,
            "restore": restore_path,
            "reference_restore": reference_restore_path,
        }
    )
    output_path = paths["output"]
    source_path = paths["source"]
    restore_path = paths["restore"]
    reference_restore_path = paths["reference_restore"]
    try:
        # Reserve the source store itself before starting any MCP child.  A
        # concurrent caller that creates it after preflight therefore fails
        # here, rather than being opened by the server with exist_ok=True.
        source_path.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError as error:
        raise FileExistsError(
            "Batch snapshot source directory was created after preflight"
        ) from error
    output_path.mkdir(parents=True, exist_ok=False)
    try:
        return asyncio.run(
            _workflow(output_path, source_path, restore_path, reference_restore_path)
        )
    finally:
        # The caller owns explicit source/restore stores.  Temporary stores are
        # deliberately retained for operator inspection until the process exits.
        _ = temporary_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--restore-dir", type=Path)
    arguments = parser.parse_args(argv)
    receipt = run(
        arguments.output_dir,
        source_dir=None if arguments.source_dir is None else str(arguments.source_dir),
        restore_dir=None if arguments.restore_dir is None else str(arguments.restore_dir),
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by installed CLI tests
    raise SystemExit(main())
