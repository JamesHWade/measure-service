"""Run a synthetic, restartable chromatogram batch over the public MCP API.

The example deliberately uses four small, owned synthetic datasets and one
frozen method.  It demonstrates mixed durable outcomes, a controlled worker
failure, cancellation, explicit retry and replay after process restarts.  The
reports are read from retained evidence and rendered locally; no claim about
compound identity, concentration, purity or instrument qualification is made.

Run from an installed environment, for example::

    python examples/chromatogram_batch.py --output-dir /tmp/chromatogram-batch

``--data-dir`` is optional and is useful when a caller wants to retain the
local SQLite store after the example exits.  Without it, the example uses a
temporary local store and only the requested output directory is retained.
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
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from measure_service.evidence_client import collect_chromatogram_evidence
from measure_service.evidence_report import write_report

PROFILE = "measure-chromatogram-batch-example/1"
TRACE_ID = "uv-254"
ITEM_NAMES = ("reference", "blank", "qc", "unknown")
FAILURE_R = "/nonexistent/measure-example-r"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _normal_r_command() -> str:
    return os.environ.get("MEASURE_MCP_R", "R")


def _preflight_output(output_dir: Path) -> None:
    """Reject an existing output target before starting a worker process."""

    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(f"Batch output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=False)


def _write_exclusive(path: Path, content: bytes) -> str:
    """Write one new local artifact and return its byte hash."""

    if path.exists() or path.is_symlink():
        raise FileExistsError(f"Batch artifact already exists: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
    return _sha256_bytes(content)


@asynccontextmanager
async def _server_session(data_dir: str, *, r_command: str) -> AsyncIterator[ClientSession]:
    """Start one child service process and yield its public MCP session."""

    environment = dict(os.environ)
    environment.update(
        {
            "MEASURE_MCP_DATA_DIR": data_dir,
            "MEASURE_MCP_IDENTITY": "local",
            # The setting is scoped to this child process.  In particular,
            # the deliberate failure below never mutates the caller's R env.
            "MEASURE_MCP_R": r_command,
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "measure_service"],
        env=environment,
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


async def _call(client: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one public tool without retrying or falling back to private APIs."""

    response = await client.call_tool(name, arguments)
    if response.isError or not isinstance(response.structuredContent, dict):
        detail = next(
            (
                item.text
                for item in getattr(response, "content", [])
                if getattr(item, "type", None) == "text"
            ),
            "",
        )
        suffix = f": {detail[:1000]}" if detail else ""
        raise RuntimeError(f"{name} failed{suffix}")
    return response.structuredContent


def _trace_signal(kind: str, axis: list[float]) -> list[float]:
    if kind == "blank":
        return [0.0 for _ in axis]
    scale = {"reference": (2.0, 1.4), "qc": (1.15, 0.8), "unknown": (1.45, 1.0)}[kind]
    first, second = scale
    return [
        0.12
        + first * math.exp(-0.5 * ((value - 1.5) / 0.17) ** 2)
        + second * math.exp(-0.5 * ((value - 3.5) / 0.23) ** 2)
        for value in axis
    ]


def _dataset_payload(kind: str, axis: list[float]) -> dict[str, Any]:
    return {
        "title": f"Synthetic {kind} UV chromatogram",
        "traces": [
            {
                "trace_id": TRACE_ID,
                "sample_id": f"synthetic-{kind}",
                "injection_id": f"injection-{kind}",
                "channel": "UV 254 nm",
                "separation": "LC",
                "detector": "UV",
                "axis_unit": "min",
                "signal_unit": "mAU",
                "sample_type": kind,
                "axis": axis,
                "signal": _trace_signal(kind, axis),
            }
        ],
    }


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


def _item_states(page: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(item["ordinal"]): item for item in page["items"]}


def _assert_states(run: dict[str, Any], expected: dict[int, str]) -> None:
    states = {int(item["ordinal"]): item["state"] for item in run["items"]}
    for ordinal, state in expected.items():
        if states.get(ordinal) != state:
            raise AssertionError(
                f"run {run.get('run_id')} ordinal {ordinal} was {states.get(ordinal)!r}"
            )


async def _workflow(output_dir: Path, data_dir: str) -> dict[str, Any]:
    axis = [index / 100 for index in range(501)]
    method = _method()
    datasets: dict[str, dict[str, Any]] = {}
    batch_id: str
    first_run: dict[str, Any]
    failed_run: dict[str, Any]
    retry_run: dict[str, Any]
    first_revision: int
    failure_revision: int

    # Registration and the first bounded run happen in one service process.
    async with _server_session(data_dir, r_command=_normal_r_command()) as client:
        for kind in ITEM_NAMES:
            datasets[kind] = await _call(
                client,
                "measurement_register_dataset",
                {"dataset": _dataset_payload(kind, axis)},
            )

        batch_spec = {
            "schema_version": "1.0",
            "profile": "measure-chromatogram-batch/1",
            "title": "Synthetic four-trace resumable chromatogram batch",
            "method": method,
            "inputs": [
                {
                    "dataset_id": datasets[kind]["dataset_id"],
                    "trace_id": TRACE_ID,
                    "role": kind,
                    "internal_standard_references": (
                        ["caller-declared reference label"] if kind == "unknown" else []
                    ),
                }
                for kind in ITEM_NAMES
            ],
        }
        created = await _call(
            client,
            "measurement_create_chromatogram_batch",
            {"batch": batch_spec, "idempotency_key": "batch-example-create-v1"},
        )
        batch_id = created["batch_id"]
        initial_batch = await _call(
            client, "measurement_get_chromatogram_batch", {"batch_id": batch_id}
        )
        first_revision = int(initial_batch["revision"])
        initial_items = await _call(
            client,
            "measurement_read_chromatogram_batch_items",
            {"batch_id": batch_id, "offset": 0, "limit": 4},
        )
        initial_by_ordinal = _item_states(initial_items)
        if [initial_by_ordinal[i]["role"] for i in range(4)] != list(ITEM_NAMES):
            raise AssertionError("batch roles were not frozen in declared order")
        if [initial_by_ordinal[i]["trace"]["sample_type"] for i in range(4)] != list(ITEM_NAMES):
            raise AssertionError("original trace sample types were not preserved")
        if initial_by_ordinal[3]["internal_standard_references"] != [
            "caller-declared reference label"
        ]:
            raise AssertionError("unknown declaration was not retained")

        first_run = await _call(
            client,
            "measurement_run_chromatogram_batch",
            {
                "batch_id": batch_id,
                "idempotency_key": "batch-example-run-first-v1",
                "expected_revision": first_revision,
                "max_items": 2,
            },
        )
        _assert_states(first_run, {0: "completed", 1: "completed"})
        first_ids = [item["analysis_id"] for item in first_run["items"]]
        if not all(isinstance(value, str) and value for value in first_ids):
            raise AssertionError("the first two completed items lacked analysis IDs")
        after_first = await _call(
            client, "measurement_get_chromatogram_batch", {"batch_id": batch_id}
        )

    # A fresh process with a deliberately missing R executable records a
    # failed attempt.  This is a child-only environment change, not a global
    # runtime mutation or a synthetic failure injected into the state tables.
    async with _server_session(data_dir, r_command=FAILURE_R) as client:
        failure_revision = int(after_first["revision"])
        failed_run = await _call(
            client,
            "measurement_run_chromatogram_batch",
            {
                "batch_id": batch_id,
                "idempotency_key": "batch-example-run-failure-v1",
                "expected_revision": failure_revision,
                "max_items": 1,
            },
        )
        _assert_states(failed_run, {2: "failed"})
        failed_attempts = await _call(
            client,
            "measurement_read_chromatogram_batch_attempts",
            {"batch_id": batch_id, "ordinal": 2, "offset": 0, "limit": 20},
        )
        if failed_attempts["count"] != 1:
            raise AssertionError("controlled failure did not retain one attempt")
        original_failure = failed_attempts["attempts"][0]
        if (
            original_failure["state"] != "failed"
            or original_failure["runtime"].get("runtime_status") != "unknown"
        ):
            raise AssertionError("failed attempt did not preserve unknown runtime evidence")

        before_cancel = await _call(
            client, "measurement_get_chromatogram_batch", {"batch_id": batch_id}
        )
        cancelled = await _call(
            client,
            "measurement_cancel_chromatogram_batch",
            {
                "batch_id": batch_id,
                "idempotency_key": "batch-example-cancel-v1",
                "expected_revision": before_cancel["revision"],
            },
        )
        if cancelled["counts"]["cancelled"] != 1 or cancelled["counts"]["pending"] != 0:
            raise AssertionError("cancellation did not checkpoint the fourth pending item")
        cancellation_checkpoint = await _call(
            client,
            "measurement_read_chromatogram_batch_items",
            {"batch_id": batch_id, "offset": 0, "limit": 4},
        )

        retried = await _call(
            client,
            "measurement_retry_chromatogram_batch_items",
            {
                "batch_id": batch_id,
                "ordinals": [2, 3],
                "idempotency_key": "batch-example-retry-2-3-v1",
                "expected_revision": cancelled["revision"],
            },
        )
        if (
            retried["counts"]["pending"] != 2
            or retried["counts"]["failed"] != 0
            or retried["counts"]["cancelled"] != 0
        ):
            raise AssertionError("retry did not select exactly ordinals 2 and 3")
        retry_revision = int(retried["revision"])

    # The retry runs with normal R after another process restart.
    async with _server_session(data_dir, r_command=_normal_r_command()) as client:
        retry_run = await _call(
            client,
            "measurement_run_chromatogram_batch",
            {
                "batch_id": batch_id,
                "idempotency_key": "batch-example-run-retry-v1",
                "expected_revision": retry_revision,
                "max_items": 2,
            },
        )
        _assert_states(retry_run, {2: "completed", 3: "completed"})
        after_retry = await _call(
            client, "measurement_get_chromatogram_batch", {"batch_id": batch_id}
        )
        final_items = await _call(
            client,
            "measurement_read_chromatogram_batch_items",
            {"batch_id": batch_id, "offset": 0, "limit": 4},
        )

    # Replay both old request keys in a new process with no R.  These are
    # retained receipts, so neither call can acquire capacity or recompute.
    async with _server_session(data_dir, r_command=FAILURE_R) as client:
        replay_first = await _call(
            client,
            "measurement_run_chromatogram_batch",
            {
                "batch_id": batch_id,
                "idempotency_key": "batch-example-run-first-v1",
                "expected_revision": first_revision,
                "max_items": 2,
            },
        )
        replay_failed = await _call(
            client,
            "measurement_run_chromatogram_batch",
            {
                "batch_id": batch_id,
                "idempotency_key": "batch-example-run-failure-v1",
                "expected_revision": failure_revision,
                "max_items": 1,
            },
        )
        first_replay_ids = [item["analysis_id"] for item in replay_first["items"]]
        if first_replay_ids != first_ids:
            raise AssertionError("replayed successful run changed its retained analysis IDs")
        if replay_failed["status"] != "failed" or replay_failed["items"][0]["state"] != "failed":
            raise AssertionError("old failed run was rewritten after the retry succeeded")
        if replay_failed["items"][0]["item_state"] != "completed":
            raise AssertionError("failed run did not expose its current retried item state")

        attempt_pages = {
            str(ordinal): await _call(
                client,
                "measurement_read_chromatogram_batch_attempts",
                {"batch_id": batch_id, "ordinal": ordinal, "offset": 0, "limit": 20},
            )
            for ordinal in range(4)
        }
        final_batch = await _call(
            client, "measurement_get_chromatogram_batch", {"batch_id": batch_id}
        )
        control_history = await _call(
            client, "measurement_read_chromatogram_batch_control_events", {"batch_id": batch_id}
        )
        if [event["kind"] for event in control_history["events"]] != [
            "cancel_requested",
            "retry_selected",
        ]:
            raise AssertionError("durable control history did not retain cancellation and retry")
        if control_history["events"][0]["items"] != [
            {"ordinal": 3, "from_state": "pending", "to_state": "cancelled"}
        ]:
            raise AssertionError("the original pending-item cancellation was lost")
        # Evidence collection is a read-only public MCP workflow.  The report
        # renderer receives exactly the stored evidence bundle and does no R.
        items = final_items["items"]
        reports: list[dict[str, Any]] = []
        canonical_file_hashes: dict[str, str] = {}
        for item in items:
            ordinal = int(item["ordinal"])
            analysis_id = item.get("analysis_id")
            if item.get("state") != "completed" or not isinstance(analysis_id, str):
                raise AssertionError(f"final item {ordinal} was not completed")
            bundle = await collect_chromatogram_evidence(client, analysis_id, page_size=128)
            if bundle["analysis"]["stored_result_sha256"] != item["result_sha256"]:
                raise AssertionError("item and exported evidence identify different stored results")
            report_dir = output_dir / "items" / f"{ordinal:03d}"
            published = write_report(bundle, report_dir)
            files: dict[str, dict[str, str]] = {}
            for filename, metadata in published.items():
                relative = (Path("items") / f"{ordinal:03d}" / filename).as_posix()
                actual_hash = _sha256_bytes((report_dir / filename).read_bytes())
                if actual_hash != metadata["sha256"]:
                    raise AssertionError(f"renderer hash changed for {relative}")
                canonical_file_hashes[relative] = actual_hash
                files[filename] = {"path": relative, "sha256": actual_hash}
            reports.append(
                {
                    "ordinal": ordinal,
                    "analysis_id": analysis_id,
                    "evidence_sha256": bundle["evidence_sha256"],
                    "point_count": bundle["point_count"],
                    "peak_count": bundle["peak_count"],
                    "result_metadata": bundle["result"],
                    "result_metadata_sha256": _sha256_json(bundle["result"]),
                    "stored_result_sha256": bundle["analysis"]["stored_result_sha256"],
                    "files": files,
                }
            )

    final_by_ordinal = _item_states(final_items)
    final_ids = [final_by_ordinal[i]["analysis_id"] for i in range(4)]
    if len(set(final_ids)) != 4:
        raise AssertionError("completed batch items did not retain four unique analysis IDs")
    failed_error = original_failure.get("error") or {}
    original_failure_unknowns = {
        "ordinal": 2,
        "attempt_id": original_failure.get("attempt_id"),
        "run_id": original_failure.get("run_id"),
        "state": original_failure.get("state"),
        "runtime": original_failure.get("runtime"),
        "r_runtime_evidence": failed_error.get("r_runtime_evidence", "unknown"),
        "error": failed_error,
    }
    snapshots = {
        "initial": initial_batch,
        "after_first_run": after_first,
        "after_failure": before_cancel,
        "after_cancel": cancelled,
        "after_retry_selection": retried,
        "after_retry_run": after_retry,
        "final": final_batch,
    }
    run_records = {
        "first_success": first_run,
        "controlled_failure": failed_run,
        "retry_success": retry_run,
        "replay_first_success_no_r": replay_first,
        "replay_original_failure_no_r": replay_failed,
    }
    transitions = [
        {
            "name": "create_batch",
            "idempotency_key": "batch-example-create-v1",
            "expected_revision": None,
        },
        {
            "name": "first_success_run",
            "idempotency_key": "batch-example-run-first-v1",
            "expected_revision": first_revision,
            "max_items": 2,
        },
        {
            "name": "controlled_failure_run",
            "idempotency_key": "batch-example-run-failure-v1",
            "expected_revision": failure_revision,
            "max_items": 1,
        },
        {
            "name": "cancel_pending_fourth",
            "idempotency_key": "batch-example-cancel-v1",
            "expected_revision": before_cancel["revision"],
        },
        {
            "name": "retry_selected_ordinals",
            "idempotency_key": "batch-example-retry-2-3-v1",
            "expected_revision": cancelled["revision"],
            "ordinals": [2, 3],
        },
        {
            "name": "retry_success_run",
            "idempotency_key": "batch-example-run-retry-v1",
            "expected_revision": retry_revision,
            "max_items": 2,
        },
        {
            "name": "replay_first_success_without_r",
            "idempotency_key": "batch-example-run-first-v1",
            "expected_revision": first_revision,
            "replay": True,
        },
        {
            "name": "replay_original_failure_without_r",
            "idempotency_key": "batch-example-run-failure-v1",
            "expected_revision": failure_revision,
            "replay": True,
        },
    ]
    receipt: dict[str, Any] = {
        "profile": PROFILE,
        "scope": (
            "Synthetic UV traces only. Stored signal areas and peak rows are reported; "
            "no compound identity, concentration, purity, calibration or instrument claim is made."
        ),
        "internal_standard_references_scope": (
            "The unknown trace label is a caller declaration only; it does not establish an "
            "assignment, correction or calibration."
        ),
        "method": method,
        "datasets": {
            kind: {
                "dataset_id": datasets[kind]["dataset_id"],
                "input_sha256": datasets[kind]["input_sha256"],
                "role": kind,
                "original_sample_type": kind,
                "trace_id": TRACE_ID,
                "point_count": 501,
            }
            for kind in ITEM_NAMES
        },
        "batch_id": batch_id,
        "transitions": transitions,
        "snapshots": snapshots,
        "run_records": run_records,
        "counts": final_batch["counts"],
        "analysis_ids": final_ids,
        "first_two_analysis_ids": first_ids,
        "original_failure_unknowns": original_failure_unknowns,
        "cancellation_checkpoint": cancellation_checkpoint,
        "attempt_pages": attempt_pages,
        "control_history": control_history,
        "reports": reports,
        "canonical_file_hashes": canonical_file_hashes,
        "assertions": {
            "all_calls_used_public_mcp": True,
            "roles_and_original_sample_types_preserved": True,
            "first_two_completed_before_restart": all(
                item["state"] == "completed" for item in first_run["items"]
            ),
            "controlled_failure_runtime_unknown": original_failure_unknowns["runtime"]
            == {"runtime_status": "unknown"},
            "fourth_pending_item_cancelled": cancellation_checkpoint["items"][3]["state"]
            == "cancelled",
            "retry_selected_ordinals_2_and_3": retried["counts"]["pending"] == 2,
            "four_unique_completed_analyses": len(set(final_ids)) == 4,
            "first_two_analysis_ids_unchanged_after_replay": first_replay_ids == first_ids,
            "original_failed_run_remains_failed": replay_failed["status"] == "failed"
            and replay_failed["items"][0]["state"] == "failed",
            "failed_attempt_and_cancellation_checkpoint_retained": attempt_pages["2"]["total"] == 2
            and cancellation_checkpoint["items"][3]["state"] == "cancelled",
            "no_hidden_retry": True,
        },
    }
    receipt["tables"] = _write_tables(receipt, final_items["items"], output_dir)
    receipt["canonical_file_hashes"].update(
        {name: item["sha256"] for name, item in receipt["tables"].items()}
    )
    summary = _summary_html(receipt)
    summary_hash = _write_exclusive(output_dir / "summary.html", summary.encode("utf-8"))
    receipt["canonical_file_hashes"]["summary.html"] = summary_hash
    receipt_bytes = (
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_exclusive(output_dir / "workflow_receipt.json", receipt_bytes)
    return receipt


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    value = "".join(
        f"\\u{ord(char):04x}" if ord(char) < 32 and char not in "\t\r\n" else char for char in value
    )
    return "'" + value if value.lstrip(" \t\r\n\ufeff")[:1] in {"=", "+", "-", "@"} else value


def _write_tables(receipt: dict[str, Any], items: list[dict[str, Any]], directory: Path) -> dict:
    """Export separate item, numerical-attempt, control and control-item grains."""
    identifier = receipt["batch_id"]
    manifest = receipt["snapshots"]["initial"]["manifest"]["inputs"]
    reports = {report["ordinal"]: report for report in receipt["reports"]}
    tables = {
        "items.csv": [
            {
                "batch_id": identifier,
                "ordinal": item["ordinal"],
                "role": item["role"],
                "original_sample_type": manifest[item["ordinal"]]["trace"]["sample_type"],
                "dataset_id": item["dataset_id"],
                "trace_id": item["trace_id"],
                "sample_id": manifest[item["ordinal"]]["trace"]["sample_id"],
                "injection_id": manifest[item["ordinal"]]["trace"]["injection_id"],
                "channel": manifest[item["ordinal"]]["trace"]["channel"],
                "axis_unit": manifest[item["ordinal"]]["trace"]["axis_unit"],
                "signal_unit": manifest[item["ordinal"]]["trace"]["signal_unit"],
                "state": item["state"],
                "attempt_count": item["attempt_count"],
                "analysis_id": item["analysis_id"],
                "point_count": reports[item["ordinal"]]["point_count"],
                "peak_count": reports[item["ordinal"]]["peak_count"],
                "stored_result_sha256": item["result_sha256"],
                "evidence_sha256": item["evidence_sha256"],
            }
            for item in items
        ],
        "attempts.csv": [
            {
                "batch_id": identifier,
                "ordinal": attempt["ordinal"],
                "attempt_no": attempt["attempt_no"],
                "attempt_id": attempt["attempt_id"],
                "run_id": attempt["run_id"],
                "state": attempt["state"],
                "analysis_id": attempt["analysis_id"],
                "r_version": attempt["runtime"].get("r_version"),
                "started_at": attempt["started_at"],
                "finished_at": attempt["finished_at"],
                "error_message": (attempt.get("error") or {}).get("message"),
            }
            for page in receipt["attempt_pages"].values()
            for attempt in page["attempts"]
        ],
        "control-events.csv": [
            {
                "batch_id": identifier,
                "event_id": event["event_id"],
                "event_no": event["event_no"],
                "kind": event["kind"],
                "revision_before": event["revision_before"],
                "revision_after": event["revision_after"],
                "affected_item_count": len(event["items"]),
                "created_at": event["created_at"],
                "event_sha256": event["event_sha256"],
            }
            for event in receipt["control_history"]["events"]
        ],
        "control-items.csv": [
            {
                "batch_id": identifier,
                "event_id": event["event_id"],
                "event_no": event["event_no"],
                "ordinal": item["ordinal"],
                "from_state": item["from_state"],
                "to_state": item["to_state"],
            }
            for event in receipt["control_history"]["events"]
            for item in event["items"]
        ],
    }
    published = {}
    for name, rows in tables.items():
        if not rows:
            raise AssertionError(f"Expected nonempty constructed example table: {name}")
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=tuple(rows[0]), lineterminator="\r\n")
        writer.writeheader()
        writer.writerows({key: _csv_cell(value) for key, value in row.items()} for row in rows)
        digest = _write_exclusive(directory / name, buffer.getvalue().encode("utf-8"))
        published[name] = {"sha256": digest, "row_count": len(rows)}
    return published


def _summary_html(receipt: dict[str, Any]) -> str:
    cards = []
    for item in receipt["reports"]:
        role = receipt["snapshots"]["initial"]["manifest"]["inputs"][item["ordinal"]]["role"]
        report_path = html.escape(item["files"]["report.html"]["path"], quote=True)
        plot_path = html.escape(item["files"]["chromatogram.svg"]["path"], quote=True)
        cards.append(
            '<article class="card">'
            f'<div class="cardhead"><h3>{html.escape(role.upper())}</h3>'
            '<span class="badge">Completed</span></div>'
            f'<img src="{plot_path}" alt="Raw and processed synthetic {html.escape(role)} '
            f'UV trace; {item["peak_count"]} detected peak rows." loading="lazy">'
            f"<p>{item['point_count']} samples · {item['peak_count']} peak rows</p>"
            f'<p class="identifier">Analysis <code>{html.escape(item["analysis_id"])}</code></p>'
            f'<a class="report" href="{report_path}">Inspect evidence report →</a>'
            "</article>"
        )
    return (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Synthetic chromatogram batch</title>
<style>
:root{color-scheme:light;--ink:#152f3a;--muted:#45616b;--line:#cfdddd;--teal:#00685c}
*{box-sizing:border-box}body{margin:0;background:#f6f8f5;color:var(--ink);font:16px/1.55 system-ui,sans-serif}
main{max-width:1120px;margin:auto;padding:48px 24px}h1{font-size:clamp(2rem,5vw,3.2rem);line-height:1.1;margin:12px 0 20px;letter-spacing:-.035em}
h2{margin:36px 0 14px;font-size:1.45rem}h3{font-size:.9rem;letter-spacing:.1em;margin:0}p{margin:10px 0}
a{color:#005d72;text-underline-offset:.2em}a:focus-visible{outline:3px solid #9a5000;outline-offset:4px}
.eyebrow{font-size:.8rem;letter-spacing:.12em;text-transform:uppercase;color:var(--teal);font-weight:750}
.intro{max-width:750px;color:var(--muted);font-size:1.1rem}.notice{border-left:4px solid #af700a;background:#fff3d9;padding:14px 18px;margin:24px 0}
.stats{display:flex;gap:32px;flex-wrap:wrap;padding:20px 0;border-block:1px solid var(--line)}.stats strong{font-size:1.8rem;display:block}.stats span{color:var(--muted)}
.timeline{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;padding:0;list-style:none;counter-reset:stage}
.timeline li{counter-increment:stage;border-top:3px solid var(--teal);padding-top:12px}.timeline b{display:block}.timeline b:before{content:counter(stage) " / ";color:var(--teal)}.timeline p{font-size:.92rem;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.card{background:white;border:1px solid var(--line);border-radius:12px;padding:20px;min-width:0}
.cardhead{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}.badge{background:#e5f4eb;color:#155632;border-radius:20px;font-size:.78rem;padding:3px 9px}
.card img{display:block;width:100%;height:auto;margin-top:16px}.identifier{color:var(--muted);font-size:.8rem;overflow-wrap:anywhere}.report{display:inline-block;margin-top:8px;font-weight:650}
.downloads{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.downloads a{display:block;background:white;border:1px solid var(--line);border-radius:8px;padding:14px}.downloads span{display:block;font-size:.86rem;color:var(--muted);text-decoration:none}
footer{margin-top:30px;color:var(--muted);font-size:.9rem}code{font-size:.9em} @media(max-width:650px){main{padding:28px 16px}.grid,.timeline,.downloads{grid-template-columns:1fr}.card{padding:16px}.stats{gap:22px}}
</style></head><body><main>
<div class="eyebrow">Measure · retained analytical evidence</div>
<h1>A batch that remembers<br>how it finished.</h1>
<p class="intro">Four synthetic UV traces, one declared method and a durable record of success,
failure, cancellation and selected retry across local MCP process restarts.</p>
<div class="stats" aria-label="Final workflow counts"><div><strong>4 / 4</strong><span>items completed</span></div>
<div><strong>5</strong><span>retained attempts</span></div><div><strong>2</strong><span>control events</span></div>
<div><strong>0</strong><span>replayed computations</span></div></div>
<aside class="notice">Synthetic signal evidence. Detected peaks do not establish compound identity,
concentration, purity or physical method qualification. Caller roles and internal-standard labels are declarations.</aside>
<h2>The retained sequence</h2><ol class="timeline"><li><b>Two succeed</b><p>Reference and blank complete.
Their analysis IDs remain fixed through every restart.</p></li><li><b>Failure and cancellation</b>
<p>An unavailable R executable fails the QC attempt; R runtime stays unknown. The pending unknown item is cancelled.</p></li>
<li><b>Selected retry and replay</b><p>QC and unknown complete after explicit retry. Original run receipts replay with R unavailable;
the original failure remains failed and cancellation remains in history.</p></li></ol>
<h2>Inspect the four results</h2><div class="grid">"""
        + "".join(cards)
        + """</div><h2>Use the evidence</h2>
<p>Full transitions, bounded public reads and artifact hashes: <a href="workflow_receipt.json">workflow_receipt.json</a>.</p>
<div class="downloads"><a href="items.csv">Items · 4 rows<span>One selected trace, its metadata, final state and result.</span></a>
<a href="attempts.csv">Attempts · 5 rows<span>One numerical attempt, including the original failure.</span></a>
<a href="control-events.csv">Control events · 2 rows<span>One cancellation or selected-retry action.</span></a>
<a href="control-items.csv">Affected items · 3 rows<span>One item changed by a control event.</span></a></div>
<footer>Blank runtime cells mean unavailable; numerical zero remains zero. CSV text escapes spreadsheet formula
prefixes and control characters; JSON retains original values. This example covers stored outcomes and local
process restarts. Expired-claim recovery is tested separately; instrument execution and portable restoration remain outside this example.</footer>
</main></body></html>
"""
    )


async def run(output_dir: Path, *, data_dir: str | None = None) -> dict[str, Any]:
    """Execute the restart/retry demonstration and write bounded local files."""

    output_dir = Path(output_dir)
    retained = None if data_dir is None else Path(data_dir)
    if retained is not None and (retained.exists() or retained.is_symlink()):
        raise FileExistsError(f"Use a new batch demonstration data directory: {retained}")
    if retained is not None:
        output_path, store_path = output_dir.resolve(), retained.resolve()
        if (
            output_path == store_path
            or store_path in output_path.parents
            or output_path in store_path.parents
        ):
            raise ValueError("Use separate, non-nested output and data directories")
    _preflight_output(output_dir)
    (output_dir / "items").mkdir()
    if retained is not None:
        retained.mkdir(mode=0o700, parents=True, exist_ok=False)
        return await _workflow(output_dir, str(retained))
    with tempfile.TemporaryDirectory(prefix="measure-chromatogram-batch-") as local_data:
        return await _workflow(output_dir, local_data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="New local SQLite directory to retain; existing directories are rejected.",
    )
    args = parser.parse_args()
    result = asyncio.run(
        run(args.output_dir, data_dir=None if args.data_dir is None else str(args.data_dir))
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
