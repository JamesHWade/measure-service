"""Recover a complete synthetic service store through public MCP and operator APIs."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from measure_service.store_operations import (
    StoreOperationError,
    backup_store,
    initialize_store,
    inspect_store,
    restore_store,
    verify_backup,
)

PROFILE = "measure-store-recovery-example/1"
STORE_PROFILE = "measure-store/1"
BACKUP_PROFILE = "measure-store-backup/1"
NOT_USED_R = "/not-used-by-store-recovery-example/R"
MARKER = "EXAMPLE_INCOMPLETE"
DATABASE_NAME = "measure.sqlite3"
MANIFEST_NAME = "manifest.json"
CHUNK_SIZE = 1024 * 1024


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _file_digest(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
            byte_count += len(chunk)
    return {"bytes": byte_count, "sha256": digest.hexdigest()}


def _inventory(directory: Path) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("A store operation produced a symbolic link")
        if path.is_file():
            inventory[path.relative_to(directory).as_posix()] = _file_digest(path)
        else:
            raise RuntimeError("A store operation produced an unexpected filesystem entry")
    return inventory


def _private_output_root(value: str) -> tuple[Path, Path]:
    output_dir = Path(value).expanduser()
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("The output directory must be new and must not be a symlink")
    output_dir.mkdir(mode=0o700, parents=False)
    if os.name == "posix":
        os.chmod(output_dir, 0o700)
    marker = output_dir / MARKER
    with marker.open("x", encoding="utf-8") as stream:
        stream.write("This example has not completed successfully.\n")
    if os.name == "posix":
        os.chmod(marker, 0o600)
    return output_dir, marker


def _server_parameters(data_dir: Path) -> StdioServerParameters:
    environment = dict(os.environ)
    environment["MEASURE_MCP_DATA_DIR"] = str(data_dir)
    environment["MEASURE_MCP_IDENTITY"] = "local"
    environment["MEASURE_MCP_R"] = NOT_USED_R
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "measure_service"],
        env=environment,
    )


async def _record_call(
    client: ClientSession,
    calls: list[dict[str, Any]],
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    response = await client.call_tool(name, arguments)
    if response.isError or not isinstance(response.structuredContent, dict):
        raise RuntimeError("A public MCP operation did not return a structured response")
    record = {
        "tool": name,
        "arguments": arguments,
        "response": response.structuredContent,
    }
    calls.append(record)
    return record["response"]


async def _record_original_session(
    client: ClientSession,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    calls: list[dict[str, Any]] = []
    first_spec = {
        "title": "Synthetic LC scouting design v1",
        "source": "manual",
        "description": "Initial synthetic scouting design.",
        "intended_conditions": [
            {"name": "flow", "value": 0.4, "unit": "mL/min"},
            {"name": "column_temperature", "value": 25, "unit": "degC"},
        ],
    }
    create_request = {
        "spec": first_spec,
        "idempotency_key": "store-recovery-create-v1",
    }
    design = await _record_call(client, calls, "measurement_create_experiment", create_request)
    old_experiment_id = design["experiment_id"]

    attempt_request = {
        "experiment_id": old_experiment_id,
        "spec": {
            "status": "completed",
            "actual_conditions": [
                {"name": "flow", "value": 0.395, "unit": "mL/min"},
                {"name": "column_temperature", "value": 25, "unit": "degC"},
            ],
            "evaluator_kind": "manual",
            "explanation": "Synthetic caller-reported completed attempt.",
        },
        "idempotency_key": "store-recovery-attempt-v1",
    }
    attempt = await _record_call(client, calls, "measurement_record_attempt", attempt_request)

    results_request = {
        "attempt_id": attempt["attempt_id"],
        "expected_attempt_revision": attempt["revision"],
        "results": [
            {
                "metric": "blank_response",
                "status": "observed",
                "value": 0,
                "unit": "mAU",
                "explanation": "Synthetic caller-reported observed zero.",
            },
            {
                "metric": "impurity_concentration",
                "status": "below_limit",
                "upper_bound": 0.05,
                "unit": "mg/L",
                "explanation": "Synthetic caller reports a result below the reporting limit.",
            },
        ],
        "idempotency_key": "store-recovery-results-v1",
    }
    saved_results = await _record_call(client, calls, "measurement_record_results", results_request)

    replacement_spec = {
        **first_spec,
        "title": "Synthetic LC scouting design v2",
        "description": "Replacement synthetic scouting design with a changed flow.",
        "intended_conditions": [
            {"name": "flow", "value": 0.42, "unit": "mL/min"},
            {"name": "column_temperature", "value": 25, "unit": "degC"},
        ],
    }
    supersede_request = {
        "experiment_id": old_experiment_id,
        "expected_input_sha256": design["input_sha256"],
        "spec": replacement_spec,
        "reason": "Update the synthetic intended flow condition.",
        "idempotency_key": "store-recovery-supersede-v1-v2",
    }
    replacement = await _record_call(
        client, calls, "measurement_supersede_experiment", supersede_request
    )
    replacement_experiment_id = replacement["entry"]["experiment_id"]

    retrieval_start = len(calls)
    await _record_call(
        client,
        calls,
        "measurement_get_experiment",
        {"experiment_id": old_experiment_id},
    )
    await _record_call(
        client,
        calls,
        "measurement_get_experiment",
        {"experiment_id": replacement_experiment_id},
    )
    await _record_call(
        client,
        calls,
        "measurement_get_attempt",
        {"attempt_id": attempt["attempt_id"]},
    )
    result_ids = [record["result_id"] for record in saved_results["results"]]
    for result_id in result_ids:
        await _record_call(client, calls, "measurement_get_result", {"result_id": result_id})
    await _record_call(
        client,
        calls,
        "measurement_get_experiment_design_lineage",
        {"experiment_id": old_experiment_id},
    )
    identities = {
        "initial_experiment_id": old_experiment_id,
        "replacement_experiment_id": replacement_experiment_id,
        "attempt_id": attempt["attempt_id"],
        "result_ids": result_ids,
    }
    return calls, identities, retrieval_start


async def run(output_value: str) -> dict[str, Any]:
    output_dir, marker = _private_output_root(output_value)
    source_dir = output_dir / "source"
    backup_dir = output_dir / "backup"
    restored_dir = output_dir / "restored"

    initialize_receipt = await asyncio.to_thread(initialize_store, str(source_dir))
    original_calls: list[dict[str, Any]] = []
    identities: dict[str, Any] = {}
    retrieval_start = 0
    backup_receipt: dict[str, Any] = {}
    backup_inventory: dict[str, dict[str, Any]] = {}

    # The online backup runs in a worker thread while this original stdio
    # server and MCP session remain open.
    async with stdio_client(_server_parameters(source_dir)) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            original_calls, identities, retrieval_start = await _record_original_session(client)
            backup_receipt = await asyncio.to_thread(backup_store, str(source_dir), str(backup_dir))
            backup_inventory = _inventory(backup_dir)

    # The original server is closed before verification, restore, or any
    # connection to the restored store.
    verification_receipt = await asyncio.to_thread(verify_backup, str(backup_dir))
    manifest_path = backup_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_manifest_fields = {
        "profile",
        "database",
        "database_bytes",
        "database_sha256",
        "table_counts",
    }
    if set(manifest) != expected_manifest_fields or manifest.get("profile") != BACKUP_PROFILE:
        raise RuntimeError("The verified backup manifest has an unexpected profile")
    database_name = manifest.get("database")
    if not isinstance(database_name, str) or database_name != DATABASE_NAME:
        raise RuntimeError("The backup manifest database name is invalid")
    if set(backup_inventory) != {MANIFEST_NAME, database_name}:
        raise RuntimeError("The verified backup has an unexpected file inventory")
    backup_database = backup_dir / database_name
    backup_digest = _file_digest(backup_database)
    if backup_digest["bytes"] != manifest.get("database_bytes") or backup_digest[
        "sha256"
    ] != manifest.get("database_sha256"):
        raise RuntimeError("The backup database does not match its manifest")

    source_inspection_before = await asyncio.to_thread(inspect_store, str(source_dir))
    source_database_before = _file_digest(source_dir / database_name)
    if (
        source_inspection_before.get("profile") != STORE_PROFILE
        or source_inspection_before.get("status") != "valid"
        or source_inspection_before.get("table_counts") != manifest.get("table_counts")
    ):
        raise RuntimeError("The closed source inspection does not match the backup manifest")

    restore_receipt = await asyncio.to_thread(restore_store, str(backup_dir), str(restored_dir))
    restored_database_before_connect = _file_digest(restored_dir / database_name)
    exact_initial_restore = restored_database_before_connect == backup_digest
    if not exact_initial_restore:
        raise RuntimeError("The restored database bytes differ from the verified backup")

    restored_inventory_before_retry = _inventory(restored_dir)
    second_restore_rejected = False
    try:
        await asyncio.to_thread(restore_store, str(backup_dir), str(restored_dir))
    except StoreOperationError:
        second_restore_rejected = True
    restored_inventory_after_retry = _inventory(restored_dir)
    if (
        not second_restore_rejected
        or restored_inventory_after_retry != restored_inventory_before_retry
    ):
        raise RuntimeError("A second restore changed an existing restored store")

    restored_calls: list[dict[str, Any]] = []
    replay_matches = True
    async with stdio_client(_server_parameters(restored_dir)) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            for original in original_calls:
                replayed = await _record_call(
                    client, restored_calls, original["tool"], original["arguments"]
                )
                if _json_bytes(replayed) != _json_bytes(original["response"]):
                    replay_matches = False
            if not replay_matches:
                raise RuntimeError("Restored MCP receipts or historical retrievals changed")

            new_design_request = {
                "spec": {
                    "title": "Synthetic design created after recovery",
                    "source": "manual",
                    "description": "One new design created only in the restored store.",
                    "intended_conditions": [
                        {"name": "flow", "value": 0.44, "unit": "mL/min"},
                        {"name": "column_temperature", "value": 25, "unit": "degC"},
                    ],
                },
                "idempotency_key": "store-recovery-restored-only-design",
            }
            new_design = await _record_call(
                client,
                restored_calls,
                "measurement_create_experiment",
                new_design_request,
            )

    source_inspection_after = await asyncio.to_thread(inspect_store, str(source_dir))
    source_database_after = _file_digest(source_dir / database_name)
    backup_inventory_after = _inventory(backup_dir)
    source_counts_before = source_inspection_before["table_counts"]
    source_counts_after = source_inspection_after.get("table_counts")
    source_count_unchanged = (
        source_inspection_after.get("profile") == STORE_PROFILE
        and source_inspection_after.get("status") == "valid"
        and source_counts_before == source_counts_after
    )
    source_unmodified = source_database_before == source_database_after
    backup_unchanged = backup_inventory == backup_inventory_after
    if not source_count_unchanged or not source_unmodified or not backup_unchanged:
        raise RuntimeError("Restored-store work changed the original source or backup")

    write_call_indices = (0, 1, 2, 3)
    original_receipt_replay_exact = all(
        _json_bytes(original_calls[index]["response"])
        == _json_bytes(restored_calls[index]["response"])
        for index in write_call_indices
    )
    retrieval_continuity_exact = all(
        _json_bytes(original["response"]) == _json_bytes(replayed["response"])
        for original, replayed in zip(
            original_calls[retrieval_start:],
            restored_calls[retrieval_start : len(original_calls)],
            strict=True,
        )
    )
    if not original_receipt_replay_exact or not retrieval_continuity_exact:
        raise RuntimeError("Restored receipts or historical reads were not byte-equivalent")

    records = {
        "profile": PROFILE,
        "original_calls": original_calls,
        "restored_replay_calls": restored_calls[: len(original_calls)],
        "restored_only_design": {
            "request": new_design_request,
            "response": new_design,
        },
    }
    receipt: dict[str, Any] = {
        "profile": PROFILE,
        "store_profile": STORE_PROFILE,
        "backup_profile": BACKUP_PROFILE,
        "synthetic_records": {
            **identities,
            "restored_only_experiment_id": new_design["experiment_id"],
            "initial_attempt_status": "completed",
            "observed_zero": 0,
            "below_limit_upper_bound": 0.05,
        },
        "source_table_counts_before_restored_write": source_counts_before,
        "source_table_counts_after_restored_write": source_counts_after,
        "operator_receipts": {
            "initialize_source": initialize_receipt,
            "inspect_source_before_restored_write": source_inspection_before,
            "backup": backup_receipt,
            "verify_backup": verification_receipt,
            "restore": restore_receipt,
            "inspect_source_after_restored_write": source_inspection_after,
        },
        "backup_database": {
            "file": database_name,
            **backup_digest,
        },
        "source_database": {
            "file": database_name,
            **source_database_after,
        },
        "backup_inventory_sha256": hashlib.sha256(_json_bytes(backup_inventory)).hexdigest(),
        "verification": {
            "exact_initial_restore_before_mcp_connect": exact_initial_restore,
            "original_receipt_replay_exact": original_receipt_replay_exact,
            "historical_retrieval_continuity_exact": retrieval_continuity_exact,
            "source_count_unchanged_by_restored_work": source_count_unchanged,
            "source_unmodified_by_restored_work": source_unmodified,
            "backup_unchanged": backup_unchanged,
            "second_restore_rejected_without_target_change": second_restore_rejected,
            "r_backend_used": False,
            "optimizer_used": False,
            "instrument_controlled": False,
            "hosted_auth_qualified": False,
        },
        "scope": "Synthetic local stdio and single-owner operator recovery example.",
    }
    _write_json(output_dir / "records.json", records)
    _write_json(output_dir / "workflow_receipt.json", receipt)
    marker.unlink()
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        required=True,
        help="New private directory for the source, backup, restore, and example records",
    )
    arguments = parser.parse_args(argv)

    try:
        receipt = asyncio.run(run(arguments.output_dir))
    except Exception as error:
        # Omit exception details because they may contain a caller's local paths.
        print(
            json.dumps({"profile": PROFILE, "error": type(error).__name__}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
