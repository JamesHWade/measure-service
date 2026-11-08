"""Import mapped outcome tables, correct one result, and freeze observations over MCP.

The example is deliberately R-free.  It records caller-declared scalar outcomes
through the public stdio tools, retains the exact UTF-8 CSV sources, evaluates a
manual analytical observation explicitly, and shows that a later correction
does not rewrite the old observation or its snapshot.

Run from an installed environment with::

    python examples/result_tables.py --output-dir /tmp/measure-result-table-output

``--data-dir`` is optional.  The output and data directories must both be new,
separate, non-nested paths.  The output directory contains an
``EXAMPLE_INCOMPLETE`` marker until every artifact and its manifest have been
written successfully.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import html
import io
import json
import os
import shutil
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from measure_service.result_table_client import collect_result_table_import

PROFILE = "measure-result-table-example/1"
SOURCE_HEADERS = (
    "attempt_id",
    "expected_attempt_revision",
    "metric",
    "status",
    "unit",
    "analyte_reference",
    "value",
    "lower_bound",
    "upper_bound",
    "analysis_id",
    "summary_field",
    "supersedes_result_id",
    "explanation",
    "plate",
    "notes",
)
MAPPING_COLUMNS = {
    "attempt_id": "attempt_id",
    "expected_attempt_revision": "expected_attempt_revision",
    "metric": "metric",
    "status": "status",
    "unit": "unit",
    "analyte_reference": "analyte_reference",
    "value": "value",
    "lower_bound": "lower_bound",
    "upper_bound": "upper_bound",
    "analysis_id": "analysis_id",
    "summary_field": "summary_field",
    "supersedes_result_id": "supersedes_result_id",
    "explanation": "explanation",
}
OUTPUT_NAMES = (
    "initial.csv",
    "correction.csv",
    "rejected.csv",
    "initial-import.json",
    "correction-import.json",
    "results.json",
    "observations.json",
    "snapshot.json",
    "outcome-rows.csv",
    "workflow_receipt.json",
    "index.html",
    "manifest.json",
)
METHOD_DECLARATION = {
    "schema_version": "1.0",
    "time_unit": "min",
    "flow_unit": "mL/min",
    "solvents": ["A", "B"],
    "program": {
        "kind": "isocratic",
        "acquisition_duration": 7,
        "state": {"flow": 0.4, "solvent_fractions": {"A": 0.8, "B": 0.2}},
    },
}
CONTRACT_SPEC = {
    "required_analytes": ["A", "B"],
    "resolution_definition": "caller-declared/1",
    "resolution_threshold": 1.5,
    "max_acquisition_duration_min": 7,
    "runtime_preference_min": 5,
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _write_exclusive(path: Path, content: bytes) -> str:
    if os.path.lexists(path):
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


def _preflight_new_paths(output_dir: Path, data_dir: Path) -> dict[str, Path]:
    paths = {"output": output_dir.expanduser(), "data": data_dir.expanduser()}
    existing = [name for name, path in paths.items() if os.path.lexists(path)]
    if existing:
        raise FileExistsError(
            "Result-table example paths must be new and non-symlinked: " + ", ".join(existing)
        )
    resolved = {name: path.resolve(strict=False) for name, path in paths.items()}
    output = resolved["output"]
    data = resolved["data"]
    if output == data or output in data.parents or data in output.parents:
        raise ValueError(
            "Result-table example output and data paths must be separate and non-nested"
        )
    return resolved


def _reserve_paths(paths: dict[str, Path]) -> None:
    created: list[Path] = []
    try:
        for path in paths.values():
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
            created.append(path)
    except Exception:
        for path in reversed(created):
            try:
                path.rmdir()
            except OSError:
                pass
        raise


@asynccontextmanager
async def _server_session(data_dir: Path) -> AsyncIterator[ClientSession]:
    """Open one isolated local MCP server session without importing example helpers."""

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.update(
        MEASURE_MCP_DATA_DIR=str(data_dir),
        MEASURE_MCP_IDENTITY="local",
        MEASURE_MCP_R="/not-used-by-result-table-example/R",
        PYTHONDONTWRITEBYTECODE="1",
    )
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "measure_service"], env=environment
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


async def _call(client: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = await client.call_tool(name, arguments)
    if response.isError or not isinstance(response.structuredContent, dict):
        detail = " ".join(
            str(getattr(item, "text", ""))
            for item in getattr(response, "content", [])
            if getattr(item, "type", None) == "text"
        )
        raise RuntimeError(f"{name} failed: {detail[:1000]}")
    return response.structuredContent


async def _expected_failure(client: ClientSession, name: str, arguments: dict[str, Any]) -> str:
    response = await client.call_tool(name, arguments)
    if not response.isError:
        raise RuntimeError(f"{name} unexpectedly succeeded")
    return " ".join(
        str(getattr(item, "text", ""))
        for item in getattr(response, "content", [])
        if getattr(item, "type", None) == "text"
    )


def _row(
    attempt_id: str,
    revision: int,
    metric: str,
    status: str,
    unit: str,
    analyte_reference: str | None = None,
    *,
    value: int | float | None = None,
    lower_bound: int | float | None = None,
    upper_bound: int | float | None = None,
    supersedes_result_id: str | None = None,
    explanation: str = "",
    plate: str = "plate-synthetic",
    notes: str = "Caller-declared synthetic outcome",
) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "expected_attempt_revision": revision,
        "metric": metric,
        "status": status,
        "unit": unit,
        "analyte_reference": analyte_reference,
        "value": value,
        "lower_bound": lower_bound,
        "upper_bound": upper_bound,
        "analysis_id": None,
        "summary_field": None,
        "supersedes_result_id": supersedes_result_id,
        "explanation": explanation,
        "plate": plate,
        "notes": notes,
    }


def _csv_text(rows: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(SOURCE_HEADERS)
    for row in rows:
        writer.writerow(
            ["" if row.get(header) is None else row.get(header, "") for header in SOURCE_HEADERS]
        )
    return stream.getvalue()


def _initial_rows(first_attempt: str, second_attempt: str) -> list[dict[str, Any]]:
    return [
        _row(
            first_attempt,
            1,
            "retention_time",
            "observed",
            "min",
            "A",
            value=2,
            plate="plate-A",
            notes="Reference A; Quoted note\r\nwith Unicode Δ",
        ),
        _row(
            first_attempt,
            1,
            "resolution",
            "observed",
            "1",
            "A",
            value=2,
            plate="plate-A",
            notes="Reference A resolution",
        ),
        _row(
            first_attempt,
            1,
            "retention_time",
            "observed",
            "s",
            "B",
            value=240,
            plate="plate-A",
            notes="Reference B retention in seconds",
        ),
        _row(
            first_attempt,
            1,
            "resolution",
            "observed",
            "1",
            "B",
            value=1.5,
            plate="plate-A",
            notes="Reference B resolution",
        ),
        _row(
            second_attempt,
            1,
            "blank_response",
            "observed",
            "mAU",
            value=0,
            plate="plate-B",
            notes="Observed zero is retained as zero",
        ),
        _row(
            second_attempt,
            1,
            "impurity_presence",
            "not_detected",
            "1",
            explanation="No caller-declared impurity signal was detected.",
            plate="plate-B",
            notes="Explicit non-detection, not a numeric zero",
        ),
        _row(
            second_attempt,
            1,
            "impurity_amount",
            "below_limit",
            "mg/L",
            upper_bound=0.05,
            explanation="Caller reports the amount below the reporting limit.",
            plate="plate-B",
            notes="Upper bound is retained as declared",
        ),
        _row(
            second_attempt,
            1,
            "censored_amount",
            "censored",
            "mg/L",
            lower_bound=0.1,
            upper_bound=0.2,
            explanation="Caller reports an interval-censored amount.",
            plate="plate-B",
            notes="Both declared censoring bounds are retained",
        ),
        _row(
            second_attempt,
            1,
            "sample_availability",
            "missing",
            "1",
            explanation="The synthetic sample was unavailable for this measurement.",
            plate="plate-B",
            notes="Missing remains missing; it is not converted to zero",
        ),
        _row(
            second_attempt,
            1,
            "instrument_quality",
            "failed",
            "1",
            explanation="The caller-declared quality check failed before a value was reported.",
            plate="plate-B",
            notes="Failure remains an explicit outcome state",
        ),
        _row(
            second_attempt,
            1,
            "method_feasibility",
            "infeasible",
            "1",
            explanation="The caller declares this synthetic condition infeasible.",
            plate="plate-B",
            notes="Infeasibility remains explicit and unresolved",
        ),
    ]


def _table_spec(source_name: str, csv_text: str) -> dict[str, Any]:
    return {
        "source_name": source_name,
        "csv_text": csv_text,
        "mapping": {"columns": dict(MAPPING_COLUMNS), "constants": {}},
    }


def _binding_digest(rows: list[dict[str, Any]]) -> str:
    return _sha256_json(
        [
            {key: row[key] for key in ("ordinal", "row_sha256", "result_id", "result_sha256")}
            for row in rows
        ]
    )


def _find_row(
    rows: list[dict[str, Any]], attempt_id: str, metric: str, analyte: str
) -> dict[str, Any]:
    matches = [
        row
        for row in rows
        if row["attempt_id"] == attempt_id
        and row["result"]["metric"] == metric
        and row["result"]["analyte_reference"] == analyte
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {metric}/{analyte} row, found {len(matches)}")
    return matches[0]


async def _import_and_collect(
    client: ClientSession, spec: dict[str, Any], key: str
) -> dict[str, Any]:
    preview = await _call(
        client, "measurement_preview_result_table", {"spec": spec, "offset": 0, "limit": 20}
    )
    receipt = await _call(
        client, "measurement_import_result_table", {"spec": spec, "idempotency_key": key}
    )
    collected = await collect_result_table_import(client, receipt["import_id"])
    if collected["receipt"] != receipt or collected["csv_text"] != spec["csv_text"]:
        raise RuntimeError("Public result-table collector did not reproduce the import")
    if preview["input_sha256"] != receipt["input_sha256"]:
        raise RuntimeError("Preview and import input identities differ")
    if _binding_digest(collected["rows"]) != receipt["result_bindings_sha256"]:
        raise RuntimeError("Collected result bindings do not match the immutable receipt")
    return {
        "receipt": receipt,
        "preview": preview,
        "rows": collected["rows"],
        "collector": {
            "row_count": len(collected["rows"]),
            "csv_text_sha256": _sha256_bytes(collected["csv_text"].encode("utf-8")),
            "source_sha256": receipt["source_sha256"],
            "result_bindings_sha256": _binding_digest(collected["rows"]),
            "result_ids": [row["result_id"] for row in collected["rows"]],
        },
    }


async def _full_results(client: ClientSession, result_ids: list[str]) -> list[dict[str, Any]]:
    return [
        await _call(client, "measurement_get_result", {"result_id": result_id})
        for result_id in result_ids
    ]


async def _result_list(
    client: ClientSession, attempt_id: str, include_superseded: bool
) -> dict[str, Any]:
    return await _call(
        client,
        "measurement_list_results",
        {
            "attempt_id": attempt_id,
            "include_superseded": include_superseded,
            "offset": 0,
            "limit": 50,
        },
    )


def _observation_spec(
    rows: list[dict[str, Any]], attempt_id: str, resolution_id: str
) -> dict[str, Any]:
    return {
        "fidelity_role": "high",
        "observation_origin": "manual",
        "analytes": [
            {
                "analyte_id": "A",
                "assignment": "confirmed",
                "assignment_evidence": "Caller-declared synthetic label A in the mapped source table.",
                "retention_result_id": _find_row(rows, attempt_id, "retention_time", "A")[
                    "result_id"
                ],
                "resolution_result_id": _find_row(rows, attempt_id, "resolution", "A")["result_id"],
            },
            {
                "analyte_id": "B",
                "assignment": "confirmed",
                "assignment_evidence": "Caller-declared synthetic label B in the mapped source table.",
                "retention_result_id": _find_row(rows, attempt_id, "retention_time", "B")[
                    "result_id"
                ],
                "resolution_result_id": resolution_id,
            },
        ],
    }


async def _workflow(data_dir: Path) -> dict[str, Any]:
    async with _server_session(data_dir) as client:
        method_evaluation = await _call(
            client, "measurement_validate_method", {"declaration": METHOD_DECLARATION}
        )
        if not method_evaluation.get("feasible"):
            raise RuntimeError("The seven-minute declared method was not feasible")
        method = await _call(
            client,
            "measurement_register_method",
            {
                "title": "Seven-minute synthetic manual method",
                "declaration": METHOD_DECLARATION,
                "idempotency_key": "result-table-example-method",
            },
        )
        if (
            await _call(
                client,
                "measurement_register_method",
                {
                    "title": "Seven-minute synthetic manual method",
                    "declaration": METHOD_DECLARATION,
                    "idempotency_key": "result-table-example-method",
                },
            )
            != method
        ):
            raise RuntimeError("Method retry changed its immutable identity")
        if (
            await _call(client, "measurement_get_method", {"method_id": method["method_id"]})
            != method
        ):
            raise RuntimeError("Method retrieval changed its immutable identity")

        contract = await _call(
            client,
            "measurement_register_observation_contract",
            {
                "title": "Synthetic A/B resolution acceptance",
                "spec": CONTRACT_SPEC,
                "idempotency_key": "result-table-example-contract",
            },
        )
        if (
            await _call(
                client,
                "measurement_get_observation_contract",
                {"contract_id": contract["contract_id"]},
            )
            != contract
        ):
            raise RuntimeError("Observation contract retrieval changed its identity")

        experiment = await _call(
            client,
            "measurement_create_experiment",
            {
                "spec": {
                    "title": "Synthetic mapped outcome table",
                    "source": "synthetic example",
                    "executable_method_id": method["method_id"],
                    "observation_contract_id": contract["contract_id"],
                },
                "idempotency_key": "result-table-example-experiment",
            },
        )
        attempts = []
        for index in (1, 2):
            attempts.append(
                await _call(
                    client,
                    "measurement_record_attempt",
                    {
                        "experiment_id": experiment["experiment_id"],
                        "spec": {
                            "status": "completed",
                            "evaluator_kind": "manual",
                            "fidelity": 1.0,
                            "executable_method_id": method["method_id"],
                            "replicate_group": "synthetic-technical-replicates",
                            "replicate_kind": "technical",
                            "replicate_index": index,
                        },
                        "idempotency_key": f"result-table-example-attempt-{index}",
                    },
                )
            )
        first_attempt, second_attempt = (attempt["attempt_id"] for attempt in attempts)
        initial_rows_source = _initial_rows(first_attempt, second_attempt)
        initial_text = _csv_text(initial_rows_source)
        initial_spec = _table_spec("initial.csv", initial_text)
        initial_import = await _import_and_collect(
            client, initial_spec, "result-table-example-initial-import"
        )
        initial_rows = initial_import["rows"]
        initial_result_ids = [row["result_id"] for row in initial_rows]

        before_rejected = {
            attempt_id: await _result_list(client, attempt_id, True)
            for attempt_id in (first_attempt, second_attempt)
        }
        old_observation = await _call(
            client,
            "measurement_evaluate_observation",
            {
                "contract_id": contract["contract_id"],
                "attempt_id": first_attempt,
                "expected_attempt_revision": 1,
                "spec": _observation_spec(
                    initial_rows,
                    first_attempt,
                    _find_row(initial_rows, first_attempt, "resolution", "B")["result_id"],
                ),
                "idempotency_key": "result-table-example-observation-original",
            },
        )
        if old_observation["observation"]["assessment"]["analytical_acceptance"] != "accepted":
            raise RuntimeError("The constructed original A/B observation was not accepted")
        old_snapshot = await _call(
            client,
            "measurement_create_observation_snapshot",
            {
                "contract_id": contract["contract_id"],
                "observation_ids": [old_observation["observation_id"]],
                "idempotency_key": "result-table-example-snapshot",
            },
        )

        rejected_rows_source = [
            _row(
                first_attempt,
                1,
                "preflight_new_metric",
                "observed",
                "1",
                value=9,
                plate="plate-rejected",
                notes="Valid first row must roll back with the stale second row",
            ),
            _row(
                second_attempt,
                2,
                "stale_revision_metric",
                "observed",
                "1",
                value=8,
                plate="plate-rejected",
                notes="Stale final row intentionally rejects the whole import",
            ),
        ]
        rejected_text = _csv_text(rejected_rows_source)
        rejected_spec = _table_spec("rejected.csv", rejected_text)
        rejected_error = await _expected_failure(
            client,
            "measurement_import_result_table",
            {"spec": rejected_spec, "idempotency_key": "result-table-example-rejected-import"},
        )
        after_rejected = {
            attempt_id: await _result_list(client, attempt_id, True)
            for attempt_id in (first_attempt, second_attempt)
        }
        if before_rejected != after_rejected:
            raise RuntimeError("A failed mapped import changed a target result ID list")

        original_b_resolution = _find_row(initial_rows, first_attempt, "resolution", "B")
        correction_rows_source = [
            _row(
                first_attempt,
                1,
                "resolution",
                "observed",
                "1",
                "B",
                value=0,
                supersedes_result_id=original_b_resolution["result_id"],
                explanation="Synthetic correction reports coelution as zero resolution.",
                plate="plate-correction",
                notes="Current result explicitly superseded by a caller-declared correction",
            )
        ]
        correction_text = _csv_text(correction_rows_source)
        correction_spec = _table_spec("correction.csv", correction_text)
        correction_import = await _import_and_collect(
            client, correction_spec, "result-table-example-correction-import"
        )
        correction_rows = correction_import["rows"]
        corrected_result_id = correction_rows[0]["result_id"]
        if (
            correction_rows[0]["result"]["supersedes_result_id"]
            != original_b_resolution["result_id"]
        ):
            raise RuntimeError(
                "Correction row did not retain the requested current result identity"
            )
        old_after_correction = await _call(
            client,
            "measurement_get_observation",
            {"observation_id": old_observation["observation_id"]},
        )
        snapshot_after_correction = await _call(
            client,
            "measurement_get_observation_snapshot",
            {"snapshot_id": old_snapshot["snapshot_id"]},
        )
        if old_after_correction != old_observation or snapshot_after_correction != old_snapshot:
            raise RuntimeError("A result correction rewrote old analytical evidence")

        corrected_observation = await _call(
            client,
            "measurement_evaluate_observation",
            {
                "contract_id": contract["contract_id"],
                "attempt_id": first_attempt,
                "expected_attempt_revision": 1,
                "spec": _observation_spec(initial_rows, first_attempt, corrected_result_id),
                "idempotency_key": "result-table-example-observation-corrected",
            },
        )
        if (
            corrected_observation["observation"]["assessment"]["analytical_acceptance"]
            != "not_accepted"
        ):
            raise RuntimeError("The corrected zero-resolution observation was not rejected")

        current_heads = {
            attempt_id: await _result_list(client, attempt_id, False)
            for attempt_id in (first_attempt, second_attempt)
        }
        all_result_ids = initial_result_ids + [corrected_result_id]
        full_results = await _full_results(client, all_result_ids)
        if len(full_results) != 12:
            raise RuntimeError("Expected eleven initial and one corrected full result record")

    async with _server_session(data_dir) as client:
        replay = await _call(
            client,
            "measurement_import_result_table",
            {"spec": initial_spec, "idempotency_key": "result-table-example-initial-import"},
        )
        replay_receipt = await _call(
            client,
            "measurement_get_result_table_import",
            {"import_id": initial_import["receipt"]["import_id"]},
        )
        replay_collected = await collect_result_table_import(
            client, initial_import["receipt"]["import_id"]
        )
        correction_receipt_replay = await _call(
            client,
            "measurement_get_result_table_import",
            {"import_id": correction_import["receipt"]["import_id"]},
        )
        if replay != initial_import["receipt"] or replay_receipt != initial_import["receipt"]:
            raise RuntimeError("Original import replay changed after restart")
        if (
            replay_collected["receipt"] != initial_import["receipt"]
            or replay_collected["csv_text"] != initial_text
            or replay_collected["rows"] != initial_rows
        ):
            raise RuntimeError("Original source or row evidence changed after restart")
        if correction_receipt_replay != correction_import["receipt"]:
            raise RuntimeError("Correction import receipt changed after restart")
        final_old_observation = await _call(
            client,
            "measurement_get_observation",
            {"observation_id": old_observation["observation_id"]},
        )
        final_corrected_observation = await _call(
            client,
            "measurement_get_observation",
            {"observation_id": corrected_observation["observation_id"]},
        )
        final_snapshot = await _call(
            client,
            "measurement_get_observation_snapshot",
            {"snapshot_id": old_snapshot["snapshot_id"]},
        )
        if (
            final_old_observation != old_observation
            or final_corrected_observation != corrected_observation
            or final_snapshot != old_snapshot
        ):
            raise RuntimeError("Restart changed old observation or snapshot")
        final_current_heads = {
            attempt_id: await _result_list(client, attempt_id, False)
            for attempt_id in (first_attempt, second_attempt)
        }
        if final_current_heads != current_heads:
            raise RuntimeError("Restart changed corrected current result heads")
        final_results = await _full_results(client, all_result_ids)
        if final_results != full_results:
            raise RuntimeError("Restart changed a retained full result record")

    initial_status_counts = initial_import["receipt"]["status_counts"]
    pending_unknown_statuses = {"missing", "failed", "infeasible"}
    pending_unknown_rows = [
        row
        for row in initial_rows
        if row["attempt_id"] == second_attempt
        and row["result"]["status"] in pending_unknown_statuses
    ]
    corrected_current_head_separate = corrected_result_id in {
        item["result_id"] for item in current_heads[first_attempt]["items"]
    } and original_b_resolution["result_id"] not in {
        item["result_id"] for item in current_heads[first_attempt]["items"]
    }
    row_bindings = [
        {
            "source": "initial.csv",
            "ordinal": row["ordinal"],
            "attempt_id": row["attempt_id"],
            "metric": row["result"]["metric"],
            "analyte_reference": row["result"]["analyte_reference"],
            "status": row["result"]["status"],
            "result_id": row["result_id"],
            "result_sha256": row["result_sha256"],
            "row_sha256": row["row_sha256"],
        }
        for row in initial_rows
    ] + [
        {
            "source": "correction.csv",
            "ordinal": row["ordinal"],
            "attempt_id": row["attempt_id"],
            "metric": row["result"]["metric"],
            "analyte_reference": row["result"]["analyte_reference"],
            "status": row["result"]["status"],
            "result_id": row["result_id"],
            "result_sha256": row["result_sha256"],
            "row_sha256": row["row_sha256"],
        }
        for row in correction_rows
    ]
    return {
        "profile": PROFILE,
        "schema_version": "1.0",
        "method": {
            "method_id": method["method_id"],
            "declaration": METHOD_DECLARATION,
            "evaluation": method_evaluation,
        },
        "contract": {
            "contract_id": contract["contract_id"],
            "spec": contract["spec"],
        },
        "experiment": {
            "experiment_id": experiment["experiment_id"],
            "attempt_ids": [first_attempt, second_attempt],
            "attempts": attempts,
        },
        "ids": {
            "method_id": method["method_id"],
            "contract_id": contract["contract_id"],
            "experiment_id": experiment["experiment_id"],
            "attempt_ids": [first_attempt, second_attempt],
            "initial_import_id": initial_import["receipt"]["import_id"],
            "correction_import_id": correction_import["receipt"]["import_id"],
            "old_observation_id": old_observation["observation_id"],
            "corrected_observation_id": corrected_observation["observation_id"],
            "snapshot_id": old_snapshot["snapshot_id"],
        },
        "counts": {
            "initial_rows": len(initial_rows),
            "corrected_rows": len(correction_rows),
            "retained_result_records": len(full_results),
            "attempts": len(attempts),
            "outcome_states": len(initial_status_counts),
        },
        "initial_import": {
            "receipt": initial_import["receipt"],
            "preview": initial_import["preview"],
            "rows": initial_rows,
            "collector": initial_import["collector"],
        },
        "correction_import": {
            "receipt": correction_import["receipt"],
            "preview": correction_import["preview"],
            "rows": correction_rows,
            "collector": correction_import["collector"],
        },
        "source_texts": {
            "initial.csv": initial_text,
            "correction.csv": correction_text,
            "rejected.csv": rejected_text,
        },
        "rejected_import": {
            "error": rejected_error,
            "rows_requested": len(rejected_rows_source),
            "persisted": False,
            "result_id_lists_before": before_rejected,
            "result_id_lists_after": after_rejected,
            "result_id_lists_unchanged": before_rejected == after_rejected,
        },
        "results": {
            "records": full_results,
            "initial_result_ids": initial_result_ids,
            "corrected_result_id": corrected_result_id,
            "retained_result_ids": all_result_ids,
            "current_heads": current_heads,
        },
        "observations": {
            "old": old_observation,
            "corrected": corrected_observation,
            "old_after_correction": old_after_correction,
            "snapshot_after_correction": snapshot_after_correction,
        },
        "snapshot": old_snapshot,
        "row_bindings": row_bindings,
        "verification": {
            "rejected_import_persisted": False,
            "old_snapshot_unchanged": snapshot_after_correction == old_snapshot,
            "old_assessment": old_observation["observation"]["assessment"]["analytical_acceptance"],
            "corrected_assessment": corrected_observation["observation"]["assessment"][
                "analytical_acceptance"
            ],
            "replay_receipt_exact": replay == initial_import["receipt"],
            "replay_rows_exact": replay_collected["rows"] == initial_rows,
            "corrected_current_head_separate": corrected_current_head_separate,
            "old_pending_unknowns_not_zero": bool(pending_unknown_rows)
            and all(row["result"]["value"] is None for row in pending_unknown_rows),
            "source_encoding": "UTF-8",
            "source_line_endings": "CRLF",
            "r_backend_used": False,
            "instrument_execution": False,
            "optimizer_execution": False,
            "physical_qualification": False,
        },
        "scope": (
            "Synthetic scalar outcomes and caller-declared A/B labels demonstrate "
            "mapped-source identity, explicit correction, and analytical evidence history; "
            "they are not physical, identity, purity, instrument, or optimizer qualification."
        ),
    }


def _outcome_rows_csv(state: dict[str, Any]) -> bytes:
    rows = []
    for source, source_rows in (
        ("initial.csv", state["initial_import"]["rows"]),
        ("correction.csv", state["correction_import"]["rows"]),
    ):
        for row in source_rows:
            result = row["result"]
            rows.append(
                {
                    "source": source,
                    "ordinal": row["ordinal"],
                    "attempt_id": row["attempt_id"],
                    "expected_attempt_revision": row["expected_attempt_revision"],
                    "record_number": row["record_number"],
                    "line_start": row["line_start"],
                    "line_end": row["line_end"],
                    "metric": result["metric"],
                    "analyte_reference": result["analyte_reference"],
                    "status": result["status"],
                    "unit": result["unit"],
                    "value": result["value"],
                    "lower_bound": result["lower_bound"],
                    "upper_bound": result["upper_bound"],
                    "supersedes_result_id": result["supersedes_result_id"],
                    "explanation": result["explanation"],
                    "result_id": row["result_id"],
                    "result_sha256": row["result_sha256"],
                    "row_sha256": row["row_sha256"],
                }
            )
    headers = tuple(rows[0])
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _html_document(state: dict[str, Any]) -> bytes:
    def esc(value: Any) -> str:
        return html.escape("" if value is None else str(value), quote=True)

    counts = state["counts"]
    initial_receipt = state["initial_import"]["receipt"]
    correction_receipt = state["correction_import"]["receipt"]
    status_rows = "".join(
        f'<tr><th scope="row">{esc(status)}</th><td>{count}</td></tr>'
        for status, count in sorted(initial_receipt["status_counts"].items())
    )
    outcome_rows = []
    for source, source_rows in (
        ("initial.csv", state["initial_import"]["rows"]),
        ("correction.csv", state["correction_import"]["rows"]),
    ):
        for row in source_rows:
            result = row["result"]
            result_id = row["result_id"]
            outcome_rows.append(
                "<tr>"
                f"<td>{row['ordinal']}</td><td>{esc(source)}</td>"
                f"<td>{esc(row['attempt_id'])}</td><td>{esc(result['metric'])}</td>"
                f"<td>{esc(result['analyte_reference'])}</td><td>{esc(result['status'])}</td>"
                f"<td>{esc(result['value'] if result['value'] is not None else 'not reported')}</td>"
                f"<td>{esc(result['unit'])}</td>"
                f"<td>{esc(result['lower_bound'] if result['lower_bound'] is not None else '—')}</td>"
                f"<td>{esc(result['upper_bound'] if result['upper_bound'] is not None else '—')}</td>"
                f"<td>{esc(result_id)}</td>"
                f'<td><a href="results.json" aria-label="Full result record {esc(result_id)}">JSON</a></td>'
                "</tr>"
            )
    links = "".join(
        f'<li><a href="{esc(name)}">{esc(label)}</a></li>'
        for name, label in (
            ("initial.csv", "Initial retained source CSV"),
            ("correction.csv", "Correction source CSV"),
            ("rejected.csv", "Rejected two-row source CSV"),
            ("initial-import.json", "Initial import receipt and rows"),
            ("correction-import.json", "Correction import receipt and rows"),
            ("results.json", "All twelve full result records"),
            ("observations.json", "Original and corrected observations"),
            ("snapshot.json", "Frozen original observation snapshot"),
            ("outcome-rows.csv", "Per-row coordinates and result links"),
            ("workflow_receipt.json", "Machine-readable workflow receipt"),
            ("manifest.json", "Artifact hashes and byte sizes"),
        )
    )
    html_text = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mapped outcome-table evidence</title>
<style>
:root {{ color-scheme: light; --ink:#1d2a35; --muted:#5e6d76; --teal:#176b68; --paper:#fbfaf6; --line:#d8e0dc; --wash:#edf5f1; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--paper); color:var(--ink); font:16px/1.55 system-ui,-apple-system,sans-serif; }}
main {{ max-width:1180px; margin:auto; padding:2.5rem 1.25rem 4rem; }} h1,h2 {{ line-height:1.15; color:#102f3a; }} h1 {{ font-size:clamp(2rem,5vw,3.4rem); max-width:16ch; }} h2 {{ margin-top:2.5rem; }}
.lead {{ max-width:72ch; color:var(--muted); font-size:1.08rem; }} .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:.8rem; margin:1.5rem 0; }}
.card {{ background:var(--wash); border:1px solid var(--line); border-radius:.8rem; padding:1rem; }} .card strong {{ display:block; font-size:1.7rem; color:var(--teal); }}
.table-wrap {{ overflow:auto; max-width:100%; border:1px solid var(--line); border-radius:.7rem; background:white; }} .table-wrap:focus {{ outline:3px solid #79b8b0; outline-offset:3px; }}
table {{ border-collapse:collapse; min-width:760px; width:100%; }} caption {{ text-align:left; padding:.9rem 1rem; font-weight:700; color:#102f3a; }} th,td {{ border-bottom:1px solid var(--line); padding:.6rem .75rem; text-align:left; vertical-align:top; }} thead th {{ background:#e5f0ec; position:sticky; top:0; }}
code {{ overflow-wrap:anywhere; }} a {{ color:var(--teal); }} .note {{ border-left:4px solid var(--teal); padding:.75rem 1rem; background:#f0f6f3; }} ul {{ padding-left:1.25rem; }}
@media (max-width:600px) {{ main {{ padding:1.5rem .85rem 3rem; }} h1 {{ font-size:2.25rem; }} .cards {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .card {{ padding:.75rem; }} }}
</style>
</head>
<body><main>
<p><a href="#outcomes">Skip to outcome rows</a></p>
<h1>Mapped outcome-table evidence</h1>
<p class="lead">A public-MCP, R-free synthetic workflow retains an exact UTF-8 source table, its generated result identities, and an explicit analytical evidence history across a correction and a server restart.</p>
<section aria-labelledby="summary"><h2 id="summary">Workflow summary</h2>
<div class="cards">
<div class="card"><strong>{counts["initial_rows"]}</strong>initial rows</div>
<div class="card"><strong>{counts["corrected_rows"]}</strong>correction row</div>
<div class="card"><strong>{counts["retained_result_records"]}</strong>retained results</div>
<div class="card"><strong>{counts["attempts"]}</strong>technical replicates</div>
<div class="card"><strong>{counts["outcome_states"]}</strong>explicit states</div>
<div class="card"><strong>{esc(state["verification"]["old_assessment"])} → {esc(state["verification"]["corrected_assessment"])}</strong>assessment history</div>
</div></section>
<p class="note">The original accepted observation and snapshot remain immutable. The corrected zero-resolution result receives a new assessment that is not accepted. Caller-declared synthetic values and labels do not qualify an instrument, physical identity, purity, or optimizer.</p>
<section aria-labelledby="sources"><h2 id="sources">Retained sources and receipts</h2><ul>{links}</ul>
<div class="table-wrap" role="region" aria-label="Source receipt summary" tabindex="0"><table><caption>Source identity and generated result bindings</caption><thead><tr><th scope="col">Source</th><th scope="col">Rows</th><th scope="col">Bytes</th><th scope="col">Source SHA-256</th><th scope="col">Binding SHA-256</th><th scope="col">Unmapped columns</th></tr></thead><tbody>
<tr><th scope="row"><a href="initial.csv">initial.csv</a></th><td>{initial_receipt["row_count"]}</td><td>{initial_receipt["source_bytes"]}</td><td><code>{esc(initial_receipt["source_sha256"])}</code></td><td><code>{esc(initial_receipt["result_bindings_sha256"])}</code></td><td>plate, notes</td></tr>
<tr><th scope="row"><a href="correction.csv">correction.csv</a></th><td>{correction_receipt["row_count"]}</td><td>{correction_receipt["source_bytes"]}</td><td><code>{esc(correction_receipt["source_sha256"])}</code></td><td><code>{esc(correction_receipt["result_bindings_sha256"])}</code></td><td>plate, notes</td></tr>
</tbody></table></div></section>
<section aria-labelledby="states"><h2 id="states">Initial outcome states</h2><div class="table-wrap" role="region" aria-label="Initial outcome status counts" tabindex="0"><table><caption>Status counts are retained declarations, not inferred values.</caption><thead><tr><th scope="col">Status</th><th scope="col">Rows</th></tr></thead><tbody>{status_rows}</tbody></table></div></section>
<section id="outcomes" aria-labelledby="outcome-heading"><h2 id="outcome-heading">Per-row result identities</h2><div class="table-wrap" role="region" aria-label="Per-row outcome result links; scroll horizontally" tabindex="0"><table><caption>Values and censoring bounds retain their declared units. “Not reported” is unavailable; — means no bound supplied. The correction is a separate immutable result.</caption><thead><tr><th scope="col">Ordinal</th><th scope="col">Source</th><th scope="col">Attempt</th><th scope="col">Metric</th><th scope="col">Analyte</th><th scope="col">Status</th><th scope="col">Value</th><th scope="col">Unit</th><th scope="col">Lower bound</th><th scope="col">Upper bound</th><th scope="col">Result ID</th><th scope="col">Full record</th></tr></thead><tbody>{"".join(outcome_rows)}</tbody></table></div></section>
<section aria-labelledby="method"><h2 id="method">Method and interpretation</h2><p>Method identity <code>{esc(state["method"]["method_id"])}</code> declares a seven-minute isocratic program. The observation contract uses resolution definition <code>caller-declared/1</code> for required labels A and B. The workflow uses no R backend, instrument, optimizer, or automatic observation evaluation.</p><p>{esc(state["scope"])}</p></section>
</main></body></html>
"""
    return html_text.encode("utf-8")


def _artifact_bytes(state: dict[str, Any]) -> dict[str, bytes]:
    receipt = {
        "profile": state["profile"],
        "schema_version": state["schema_version"],
        "method": state["method"],
        "contract": state["contract"],
        "experiment": state["experiment"],
        "ids": state["ids"],
        "counts": state["counts"],
        "initial_status_counts": state["initial_import"]["receipt"]["status_counts"],
        "initial_import": {
            "import_id": state["initial_import"]["receipt"]["import_id"],
            "input_sha256": state["initial_import"]["receipt"]["input_sha256"],
            "source_sha256": state["initial_import"]["receipt"]["source_sha256"],
            "result_bindings_sha256": state["initial_import"]["receipt"]["result_bindings_sha256"],
            "row_bindings": state["row_bindings"][:11],
        },
        "correction_import": {
            "import_id": state["correction_import"]["receipt"]["import_id"],
            "input_sha256": state["correction_import"]["receipt"]["input_sha256"],
            "source_sha256": state["correction_import"]["receipt"]["source_sha256"],
            "result_bindings_sha256": state["correction_import"]["receipt"][
                "result_bindings_sha256"
            ],
            "row_bindings": state["row_bindings"][11:],
        },
        "sources": {
            name: {
                "bytes": len(text.encode("utf-8")),
                "sha256": _sha256_bytes(text.encode("utf-8")),
                "encoding": "UTF-8",
                "line_endings": "CRLF",
                "unmapped_columns": ["plate", "notes"],
            }
            for name, text in state["source_texts"].items()
        },
        "results": {
            "initial_result_ids": state["results"]["initial_result_ids"],
            "corrected_result_id": state["results"]["corrected_result_id"],
            "retained_result_ids": state["results"]["retained_result_ids"],
            "current_heads": state["results"]["current_heads"],
        },
        "rejected_import": state["rejected_import"],
        "observations": {
            "old_observation_id": state["observations"]["old"]["observation_id"],
            "corrected_observation_id": state["observations"]["corrected"]["observation_id"],
            "snapshot_id": state["snapshot"]["snapshot_id"],
        },
        "verification": state["verification"],
        "row_bindings": state["row_bindings"],
        "scope": state["scope"],
    }
    return {
        "initial.csv": state["source_texts"]["initial.csv"].encode("utf-8"),
        "correction.csv": state["source_texts"]["correction.csv"].encode("utf-8"),
        "rejected.csv": state["source_texts"]["rejected.csv"].encode("utf-8"),
        "initial-import.json": _json_bytes(state["initial_import"]),
        "correction-import.json": _json_bytes(state["correction_import"]),
        "results.json": _json_bytes(state["results"]),
        "observations.json": _json_bytes(state["observations"]),
        "snapshot.json": _json_bytes({"snapshot": state["snapshot"]}),
        "outcome-rows.csv": _outcome_rows_csv(state),
        "workflow_receipt.json": _json_bytes(receipt),
        "index.html": _html_document(state),
    }


def _write_artifacts(output_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    artifacts = _artifact_bytes(state)
    for name in OUTPUT_NAMES[:-1]:
        if name not in artifacts:
            raise RuntimeError(f"Missing planned artifact {name}")
        _write_exclusive(output_dir / name, artifacts[name])
    entries = [
        {
            "path": name,
            "bytes": (output_dir / name).stat().st_size,
            "sha256": _sha256_bytes((output_dir / name).read_bytes()),
        }
        for name in OUTPUT_NAMES[:-1]
    ]
    manifest = {
        "profile": f"{PROFILE}/manifest",
        "schema_version": "1.0",
        "files": entries,
        "excludes": ["manifest.json", "EXAMPLE_INCOMPLETE"],
    }
    manifest_bytes = _json_bytes(manifest)
    _write_exclusive(output_dir / "manifest.json", manifest_bytes)
    return manifest


def run(output_dir: Path, *, data_dir: str | None = None) -> dict[str, Any]:
    """Run the public-MCP workflow and write a new, self-contained output directory."""

    output_path = Path(output_dir)
    owned_data = data_dir is None
    data_path = (
        Path(tempfile.gettempdir()) / f"measure-result-table-example-data-{uuid.uuid4().hex}"
        if owned_data
        else Path(data_dir)
    )
    paths = _preflight_new_paths(output_path, data_path)
    _reserve_paths(paths)
    marker = paths["output"] / "EXAMPLE_INCOMPLETE"
    try:
        _write_exclusive(marker, b"This output is incomplete until the workflow finishes.\n")
        state = asyncio.run(_workflow(paths["data"]))
        _write_artifacts(paths["output"], state)
        marker.unlink()
        receipt = json.loads(
            (paths["output"] / "workflow_receipt.json").read_text(encoding="utf-8")
        )
        return receipt
    finally:
        if owned_data and paths["data"].is_dir() and not paths["data"].is_symlink():
            shutil.rmtree(paths["data"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--data-dir", type=Path)
    arguments = parser.parse_args(argv)
    try:
        receipt = run(
            arguments.output_dir,
            data_dir=None if arguments.data_dir is None else str(arguments.data_dir),
        )
    except Exception as error:
        print(f"result-table example failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
