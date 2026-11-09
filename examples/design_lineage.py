"""Run an R-free immutable experiment-design replacement workflow over MCP stdio.

The workflow records three complete synthetic designs, keeps attempts and
analytical evidence attached to the design IDs chosen by the caller, and
replays the first replacement after a later design and a fresh server session.
It writes only public MCP responses and bounded local renderings; it does not
read SQLite or claim instrument, optimizer, identity, purity, or physical
qualification.

Run from an installed environment with::

    python examples/design_lineage.py --output-dir /tmp/measure-design-lineage-output

The output and optional data directories must both be new, separate and
non-nested.  The output directory retains ``EXAMPLE_INCOMPLETE`` until every
artifact and its manifest have been written successfully.
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

from measure_service.design_lineage_client import collect_experiment_design_lineage

PROFILE = "measure-experiment-design-example/1"
OUTPUT_NAMES = (
    "designs.json",
    "lineage.json",
    "replacement-v1-v2.json",
    "replacement-v2-v3.json",
    "attempts.json",
    "results.json",
    "observations.json",
    "snapshot.json",
    "history.csv",
    "workflow_receipt.json",
    "index.html",
    "manifest.json",
)
METHOD_FLOW = (0.4, 0.45, 0.5)
METHOD_DURATION = (7, 8, 9)
METHOD_TITLES = (
    "Seven-minute synthetic method",
    "Eight-minute synthetic method",
    "Nine-minute synthetic method",
)
CONTRACT_SPEC = {
    "required_analytes": ["A", "B"],
    "resolution_definition": "caller-declared resolution threshold",
    "resolution_threshold": 1.5,
    "max_acquisition_duration_min": 9,
    "runtime_preference_min": 5,
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_exclusive(path: Path, content: bytes) -> str:
    """Create one artifact without following or replacing an existing target."""

    if os.path.lexists(path):
        raise FileExistsError(f"Design-lineage example artifact already exists: {path}")
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
    """Resolve and reject every target before either directory is created."""

    raw = {"output": output_dir.expanduser(), "data": data_dir.expanduser()}
    existing = [name for name, path in raw.items() if os.path.lexists(path)]
    if existing:
        raise FileExistsError(
            "Design-lineage example paths must be new and non-symlinked: " + ", ".join(existing)
        )
    paths = {name: path.resolve(strict=False) for name, path in raw.items()}
    if (
        paths["output"] == paths["data"]
        or paths["output"] in paths["data"].parents
        or paths["data"] in paths["output"].parents
    ):
        raise ValueError(
            "Design-lineage example output and data paths must be separate and non-nested"
        )
    return paths


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
    """Open the installed public MCP server with R deliberately unavailable."""

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.update(
        MEASURE_MCP_DATA_DIR=str(data_dir),
        MEASURE_MCP_IDENTITY="local",
        MEASURE_MCP_R="/not-used-by-design-lineage-example/R",
        PYTHONDONTWRITEBYTECODE="1",
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


def _method_declaration(duration: int, flow: float) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "time_unit": "min",
        "flow_unit": "mL/min",
        "solvents": ["A", "B"],
        "program": {
            "kind": "isocratic",
            "acquisition_duration": duration,
            "state": {"flow": flow, "solvent_fractions": {"A": 0.8, "B": 0.2}},
        },
    }


def _design_spec(
    method_id: str,
    contract_id: str,
    version: int,
    duration: int,
    flow: float,
) -> dict[str, Any]:
    return {
        "title": f"Synthetic immutable design v{version}",
        "source": "synthetic public example",
        "external_reference": f"design-lineage-example-v{version}",
        "proposal_reference": "design-lineage-example",
        "executable_method_id": method_id,
        "observation_contract_id": contract_id,
        "intended_conditions": [
            {"name": "flow", "value": flow, "unit": "mL/min"},
            {"name": "acquisition_duration", "value": duration, "unit": "min"},
        ],
        "description": (
            "Caller-declared synthetic design replacement; no instrument execution or "
            "optimizer state is implied."
        ),
    }


def _attempt_spec(
    status: str,
    method_id: str,
    flow: float | None = None,
    duration: int | None = None,
    *,
    temperature: float | None = None,
) -> dict[str, Any]:
    actual_conditions = []
    if flow is not None:
        actual_conditions.append({"name": "flow", "value": flow, "unit": "mL/min"})
    if duration is not None:
        actual_conditions.append({"name": "acquisition_duration", "value": duration, "unit": "min"})
    if temperature is not None:
        actual_conditions.append({"name": "sample_temperature", "value": temperature, "unit": "C"})
    return {
        "status": status,
        "evaluator_kind": "manual",
        "fidelity": 1.0,
        "executable_method_id": method_id,
        "actual_conditions": actual_conditions,
    }


def _result_specs(*, b_resolution: float) -> list[dict[str, Any]]:
    return [
        {
            "metric": "retention_time",
            "analyte_reference": "A",
            "status": "observed",
            "unit": "min",
            "value": 2,
        },
        {
            "metric": "resolution",
            "analyte_reference": "A",
            "status": "observed",
            "unit": "1",
            "value": 2,
        },
        {
            "metric": "retention_time",
            "analyte_reference": "B",
            "status": "observed",
            "unit": "min",
            "value": 4,
        },
        {
            "metric": "resolution",
            "analyte_reference": "B",
            "status": "observed",
            "unit": "1",
            "value": b_resolution,
        },
    ]


def _result_id(records: list[dict[str, Any]], metric: str, analyte: str) -> str:
    matches = [
        record["result_id"]
        for record in records
        if record["outcome"]["metric"] == metric
        and record["outcome"]["analyte_reference"] == analyte
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {metric}/{analyte} result, found {len(matches)}")
    return matches[0]


def _attempt_revision_identity(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record[key]
        for key in (
            "schema_version",
            "attempt_id",
            "experiment_id",
            "revision",
            "spec",
            "executable_method",
            "input_sha256",
            "reason",
            "created_at",
        )
    }


def _observation_spec(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "fidelity_role": "high",
        "observation_origin": "manual",
        "analytes": [
            {
                "analyte_id": "A",
                "assignment": "confirmed",
                "assignment_evidence": "Caller-declared synthetic label A.",
                "retention_result_id": _result_id(records, "retention_time", "A"),
                "resolution_result_id": _result_id(records, "resolution", "A"),
            },
            {
                "analyte_id": "B",
                "assignment": "confirmed",
                "assignment_evidence": "Caller-declared synthetic label B.",
                "retention_result_id": _result_id(records, "retention_time", "B"),
                "resolution_result_id": _result_id(records, "resolution", "B"),
            },
        ],
    }


async def _workflow(data_dir: Path) -> dict[str, Any]:
    """Execute the complete public workflow and return only public records."""

    async with _server_session(data_dir) as client:
        methods = []
        for index, (duration, flow, title) in enumerate(
            zip(METHOD_DURATION, METHOD_FLOW, METHOD_TITLES), 1
        ):
            request = {
                "title": title,
                "declaration": _method_declaration(duration, flow),
                "idempotency_key": f"design-lineage-method-{index}",
            }
            method = await _call(client, "measurement_register_method", request)
            if await _call(client, "measurement_register_method", request) != method:
                raise RuntimeError("Method retry changed an immutable method identity")
            if (
                await _call(client, "measurement_get_method", {"method_id": method["method_id"]})
                != method
            ):
                raise RuntimeError("Method retrieval changed an immutable method identity")
            methods.append(method)

        contract = await _call(
            client,
            "measurement_register_observation_contract",
            {
                "title": "Synthetic A/B acceptance contract",
                "spec": CONTRACT_SPEC,
                "idempotency_key": "design-lineage-contract",
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
            raise RuntimeError("Contract retrieval changed an immutable contract identity")

        v1_spec = _design_spec(methods[0]["method_id"], contract["contract_id"], 1, 7, 0.4)
        v1 = await _call(
            client,
            "measurement_create_experiment",
            {"spec": v1_spec, "idempotency_key": "design-lineage-experiment-v1"},
        )
        if (
            await _call(
                client,
                "measurement_create_experiment",
                {"spec": v1_spec, "idempotency_key": "design-lineage-experiment-v1"},
            )
            != v1
        ):
            raise RuntimeError("Experiment retry changed the original design")
        if (
            await _call(
                client,
                "measurement_get_experiment",
                {"experiment_id": v1["experiment_id"]},
            )
            != v1
        ):
            raise RuntimeError("Original design retrieval changed its immutable identity")

        old_attempt = await _call(
            client,
            "measurement_record_attempt",
            {
                "experiment_id": v1["experiment_id"],
                "spec": _attempt_spec("completed", methods[0]["method_id"], 0.395, 7),
                "idempotency_key": "design-lineage-old-attempt",
            },
        )
        old_results_response = await _call(
            client,
            "measurement_record_results",
            {
                "attempt_id": old_attempt["attempt_id"],
                "expected_attempt_revision": 1,
                "results": _result_specs(b_resolution=1.5),
                "idempotency_key": "design-lineage-old-results",
            },
        )
        old_results = old_results_response["results"]
        if len(old_results) != 4:
            raise RuntimeError("Original design did not retain four manual results")
        old_results_before = [
            await _call(client, "measurement_get_result", {"result_id": result["result_id"]})
            for result in old_results
        ]
        old_observation = await _call(
            client,
            "measurement_evaluate_observation",
            {
                "contract_id": contract["contract_id"],
                "attempt_id": old_attempt["attempt_id"],
                "expected_attempt_revision": 1,
                "spec": _observation_spec(old_results),
                "idempotency_key": "design-lineage-observation-v1",
            },
        )
        if old_observation["observation"]["assessment"]["analytical_acceptance"] != "accepted":
            raise RuntimeError("Original synthetic A/B observation was not accepted")
        old_snapshot = await _call(
            client,
            "measurement_create_observation_snapshot",
            {
                "contract_id": contract["contract_id"],
                "observation_ids": [old_observation["observation_id"]],
                "idempotency_key": "design-lineage-snapshot",
            },
        )

        v2_spec = _design_spec(methods[1]["method_id"], contract["contract_id"], 2, 8, 0.45)
        replacement_v1_v2 = await _call(
            client,
            "measurement_supersede_experiment",
            {
                "experiment_id": v1["experiment_id"],
                "expected_input_sha256": v1["input_sha256"],
                "spec": v2_spec,
                "reason": "Declared flow and duration replacement for v2.",
                "idempotency_key": "design-lineage-replace-v1-v2",
            },
        )
        v2 = await _call(
            client,
            "measurement_get_experiment",
            {"experiment_id": replacement_v1_v2["entry"]["experiment_id"]},
        )
        v2_planned_attempt = await _call(
            client,
            "measurement_record_attempt",
            {
                "experiment_id": v2["experiment_id"],
                "spec": _attempt_spec("planned", methods[1]["method_id"]),
                "idempotency_key": "design-lineage-v2-planned-attempt",
            },
        )

        v3_spec = _design_spec(methods[2]["method_id"], contract["contract_id"], 3, 9, 0.5)
        replacement_v2_v3 = await _call(
            client,
            "measurement_supersede_experiment",
            {
                "experiment_id": v2["experiment_id"],
                "expected_input_sha256": v2["input_sha256"],
                "spec": v3_spec,
                "reason": "Declared flow and duration replacement for v3.",
                "idempotency_key": "design-lineage-replace-v2-v3",
            },
        )
        v3 = await _call(
            client,
            "measurement_get_experiment",
            {"experiment_id": replacement_v2_v3["entry"]["experiment_id"]},
        )
        first_replacement_replay = await _call(
            client,
            "measurement_supersede_experiment",
            {
                "experiment_id": v1["experiment_id"],
                "expected_input_sha256": v1["input_sha256"],
                "spec": v2_spec,
                "reason": "Declared flow and duration replacement for v2.",
                "idempotency_key": "design-lineage-replace-v1-v2",
            },
        )
        if first_replacement_replay != replacement_v1_v2:
            raise RuntimeError("The first replacement replay changed after v3")

        experiment_count_before_stale = (
            await _call(client, "measurement_list_experiments", {"offset": 0, "limit": 50})
        )["count"]
        stale_error = await _expected_failure(
            client,
            "measurement_supersede_experiment",
            {
                "experiment_id": v1["experiment_id"],
                "expected_input_sha256": v1["input_sha256"],
                "spec": v3_spec,
                "reason": "Stale v1 caller must not create a fourth design.",
                "idempotency_key": "design-lineage-stale-v1",
            },
        )
        experiment_count_after_stale = (
            await _call(client, "measurement_list_experiments", {"offset": 0, "limit": 50})
        )["count"]
        if experiment_count_before_stale != 3 or experiment_count_after_stale != 3:
            raise RuntimeError("A stale v1 replacement created an unexpected design")

        old_attempt_r1_before = await _call(
            client,
            "measurement_get_attempt",
            {"attempt_id": old_attempt["attempt_id"], "revision": 1},
        )
        old_attempt_r2 = await _call(
            client,
            "measurement_revise_attempt",
            {
                "attempt_id": old_attempt["attempt_id"],
                "expected_revision": 1,
                "spec": _attempt_spec(
                    "completed", methods[0]["method_id"], 0.395, 7, temperature=25
                ),
                "reason": "Added caller-reported sample temperature metadata.",
                "idempotency_key": "design-lineage-old-attempt-revision-2",
            },
        )
        old_attempt_r1_after = await _call(
            client,
            "measurement_get_attempt",
            {"attempt_id": old_attempt["attempt_id"], "revision": 1},
        )
        old_attempt_latest = await _call(
            client,
            "measurement_get_attempt",
            {"attempt_id": old_attempt["attempt_id"]},
        )
        if _attempt_revision_identity(old_attempt_r1_after) != _attempt_revision_identity(
            old_attempt_r1_before
        ):
            raise RuntimeError("Revising old metadata changed its immutable revision 1")
        if old_attempt_latest["revision"] != 2 or old_attempt_r2["revision"] != 2:
            raise RuntimeError("Old attempt did not advance to metadata revision 2")
        if old_attempt_latest["input_sha256"] == old_attempt_r1_before["input_sha256"]:
            raise RuntimeError("Attempt metadata revision did not receive a new digest")
        if old_attempt_r2["executable_method"]["method_id"] != methods[0]["method_id"]:
            raise RuntimeError("Old attempt metadata revision changed its declared method")

        late_old_attempt = await _call(
            client,
            "measurement_record_attempt",
            {
                "experiment_id": v1["experiment_id"],
                "spec": _attempt_spec("completed", methods[0]["method_id"], 0.395, 7),
                "idempotency_key": "design-lineage-late-old-attempt",
            },
        )
        v2_attempt_r1_before = await _call(
            client,
            "measurement_get_attempt",
            {"attempt_id": v2_planned_attempt["attempt_id"], "revision": 1},
        )
        v2_attempt_r2 = await _call(
            client,
            "measurement_revise_attempt",
            {
                "attempt_id": v2_planned_attempt["attempt_id"],
                "expected_revision": 1,
                "spec": _attempt_spec("completed", methods[1]["method_id"], 0.44, 8),
                "reason": "Caller completed the planned v2 design at its reported flow.",
                "idempotency_key": "design-lineage-v2-attempt-revision-2",
            },
        )
        v2_attempt_latest = await _call(
            client,
            "measurement_get_attempt",
            {"attempt_id": v2_planned_attempt["attempt_id"]},
        )
        v2_attempt_r1_after = await _call(
            client,
            "measurement_get_attempt",
            {"attempt_id": v2_planned_attempt["attempt_id"], "revision": 1},
        )
        if v2_attempt_latest != v2_attempt_r2 or v2_attempt_r1_before["revision"] != 1:
            raise RuntimeError("The v2 planned attempt did not retain both revisions")
        if v2_attempt_latest["experiment_id"] != v2["experiment_id"]:
            raise RuntimeError("The completed attempt moved away from its v2 design")
        if v2_attempt_latest["executable_method"]["method_id"] != methods[1]["method_id"]:
            raise RuntimeError("The completed v2 attempt changed its declared method")

        v2_results_response = await _call(
            client,
            "measurement_record_results",
            {
                "attempt_id": v2_planned_attempt["attempt_id"],
                "expected_attempt_revision": 2,
                "results": _result_specs(b_resolution=0),
                "idempotency_key": "design-lineage-v2-results",
            },
        )
        v2_results = v2_results_response["results"]
        v2_observation = await _call(
            client,
            "measurement_evaluate_observation",
            {
                "contract_id": contract["contract_id"],
                "attempt_id": v2_planned_attempt["attempt_id"],
                "expected_attempt_revision": 2,
                "spec": _observation_spec(v2_results),
                "idempotency_key": "design-lineage-observation-v2",
            },
        )
        if v2_observation["observation"]["assessment"]["analytical_acceptance"] != "not_accepted":
            raise RuntimeError("The zero-resolution v2 observation was unexpectedly accepted")

        old_after = await _call(
            client,
            "measurement_get_observation",
            {"observation_id": old_observation["observation_id"]},
        )
        snapshot_after = await _call(
            client,
            "measurement_get_observation_snapshot",
            {"snapshot_id": old_snapshot["snapshot_id"]},
        )
        if old_after != old_observation or snapshot_after != old_snapshot:
            raise RuntimeError("Later design work changed the original observation or snapshot")

        lineage = await collect_experiment_design_lineage(client, v1["experiment_id"])
        if lineage["count"] != 3 or lineage["current_revision"] != 3:
            raise RuntimeError("The public lineage collector did not recover all three designs")
        all_result_ids = [record["result_id"] for record in old_results + v2_results]
        all_results = [
            await _call(client, "measurement_get_result", {"result_id": result_id})
            for result_id in all_result_ids
        ]
        if all_results[:4] != old_results_before:
            raise RuntimeError("Later design work changed an original result record")

    async with _server_session(data_dir) as client:
        replay_after_restart = await _call(
            client,
            "measurement_supersede_experiment",
            {
                "experiment_id": v1["experiment_id"],
                "expected_input_sha256": v1["input_sha256"],
                "spec": v2_spec,
                "reason": "Declared flow and duration replacement for v2.",
                "idempotency_key": "design-lineage-replace-v1-v2",
            },
        )
        lineage_after_restart = await collect_experiment_design_lineage(client, v1["experiment_id"])
        if replay_after_restart != replacement_v1_v2 or lineage_after_restart != lineage:
            raise RuntimeError("Restart changed the retained replacement receipt or lineage")
        designs_after_restart = [
            await _call(client, "measurement_get_experiment", {"experiment_id": experiment_id})
            for experiment_id in (v1["experiment_id"], v2["experiment_id"], v3["experiment_id"])
        ]
        attempts_after_restart = [
            await _call(client, "measurement_get_attempt", {"attempt_id": attempt_id})
            for attempt_id in (
                old_attempt["attempt_id"],
                v2_planned_attempt["attempt_id"],
                late_old_attempt["attempt_id"],
            )
        ]
        observations_after_restart = [
            await _call(
                client,
                "measurement_get_observation",
                {"observation_id": observation_id},
            )
            for observation_id in (
                old_observation["observation_id"],
                v2_observation["observation_id"],
            )
        ]
        snapshot_after_restart = await _call(
            client,
            "measurement_get_observation_snapshot",
            {"snapshot_id": old_snapshot["snapshot_id"]},
        )
        results_after_restart = [
            await _call(client, "measurement_get_result", {"result_id": result_id})
            for result_id in all_result_ids
        ]

    if designs_after_restart != [v1, v2, v3]:
        raise RuntimeError("Restart changed one of the three immutable designs")
    if observations_after_restart != [old_observation, v2_observation]:
        raise RuntimeError("Restart changed one of the explicit observations")
    if snapshot_after_restart != old_snapshot:
        raise RuntimeError("Restart changed the original observation snapshot")
    if results_after_restart != all_results:
        raise RuntimeError("Restart changed a retained result record")
    if old_observation["attempt_id"] != old_attempt["attempt_id"]:
        raise RuntimeError("Original observation lost its v1 attempt binding")
    if v2_observation["attempt_id"] != v2_planned_attempt["attempt_id"]:
        raise RuntimeError("Replacement observation lost its v2 attempt binding")
    if v3["experiment_id"] in {attempt["experiment_id"] for attempt in attempts_after_restart}:
        raise RuntimeError("A current-pointer discovery operation routed an attempt to v3")

    return {
        "profile": PROFILE,
        "schema_version": "1.0",
        "methods": methods,
        "contract": contract,
        "designs": [v1, v2, v3],
        "lineage": lineage,
        "replacement_v1_v2": replacement_v1_v2,
        "replacement_v2_v3": replacement_v2_v3,
        "attempts": {
            "records": [
                old_attempt_r1_after,
                old_attempt_r2,
                v2_attempt_r1_after,
                v2_attempt_r2,
                attempts_after_restart[2],
            ],
            "latest": attempts_after_restart,
            "total_revisions": 5,
            "revision_views": {
                "old_revision_1_before": old_attempt_r1_before,
                "old_revision_1_after": old_attempt_r1_after,
                "v2_revision_1_before": v2_attempt_r1_before,
                "v2_revision_1_after": v2_attempt_r1_after,
            },
        },
        "results": {"records": all_results},
        "observations": {
            "records": [old_observation, v2_observation],
            "old_after_later_work": old_after,
            "snapshot_after_later_work": snapshot_after,
        },
        "snapshot": old_snapshot,
        "verification": {
            "stale_v1_rejected": bool(stale_error),
            "experiment_count_after_stale_v1": experiment_count_after_stale,
            "first_replacement_replay_exact": first_replacement_replay == replacement_v1_v2,
            "restart_replay_exact": replay_after_restart == replacement_v1_v2,
            "lineage_after_restart_exact": lineage_after_restart == lineage,
            "old_attempt_revision_1_unchanged": _attempt_revision_identity(old_attempt_r1_after)
            == _attempt_revision_identity(old_attempt_r1_before),
            "old_observation_unchanged": old_after == old_observation,
            "old_snapshot_unchanged": snapshot_after == old_snapshot,
            "old_results_unchanged": all_results[:4] == old_results_before,
            "results_after_restart_exact": results_after_restart == all_results,
            "old_design_attempts_remain_explicit": all(
                attempt["experiment_id"] == v1["experiment_id"]
                for attempt in attempts_after_restart
                if attempt["attempt_id"]
                in {old_attempt["attempt_id"], late_old_attempt["attempt_id"]}
            ),
            "v2_attempt_remains_explicit": attempts_after_restart[1]["experiment_id"]
            == v2["experiment_id"],
            "v3_has_no_implicit_attempt": v3["experiment_id"]
            not in {attempt["experiment_id"] for attempt in attempts_after_restart},
            "r_backend_used": False,
            "instrument_execution": False,
            "optimizer_execution": False,
            "physical_qualification": False,
        },
        "counts": {
            "designs": 3,
            "attempts": 3,
            "attempt_revisions": 5,
            "results": len(all_results),
            "assessments": 2,
            "snapshots": 1,
        },
        "scope": (
            "Synthetic caller-declared method, attempt, result and A/B observation records "
            "demonstrate immutable design history. They are not instrument, optimizer, "
            "identity, purity, physical, or manuscript qualification."
        ),
    }


def _history_csv(state: dict[str, Any]) -> bytes:
    rows: list[dict[str, Any]] = []
    for entry in state["lineage"]["entries"]:
        rows.append(
            {
                "record_type": "design",
                "record_id": entry["experiment_id"],
                "parent_id": entry["supersedes_experiment_id"],
                "revision": entry["revision"],
                "status": "immutable-design",
                "metric": "",
                "analyte": "",
                "value": "",
                "unit": "",
                "reason": entry["reason"],
                "input_sha256": entry["experiment_input_sha256"],
                "digest": entry["entry_sha256"],
            }
        )
    for attempt in state["attempts"]["records"]:
        spec = attempt["spec"]
        rows.append(
            {
                "record_type": "attempt",
                "record_id": attempt["attempt_id"],
                "parent_id": attempt["experiment_id"],
                "revision": attempt["revision"],
                "status": spec["status"],
                "metric": "",
                "analyte": "",
                "value": "",
                "unit": "",
                "reason": attempt["reason"],
                "input_sha256": attempt["input_sha256"],
                "digest": "",
            }
        )
    for result in state["results"]["records"]:
        outcome = result["outcome"]
        rows.append(
            {
                "record_type": "result",
                "record_id": result["result_id"],
                "parent_id": result["attempt_id"],
                "revision": result["attempt_revision"],
                "status": outcome["status"],
                "metric": outcome["metric"],
                "analyte": outcome["analyte_reference"] or "",
                "value": outcome["value"] if outcome["value"] is not None else "",
                "unit": outcome["unit"],
                "reason": outcome["explanation"],
                "input_sha256": result["input_sha256"],
                "digest": "",
            }
        )
    headers = (
        "record_type",
        "record_id",
        "parent_id",
        "revision",
        "status",
        "metric",
        "analyte",
        "value",
        "unit",
        "reason",
        "input_sha256",
        "digest",
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _html_document(state: dict[str, Any]) -> bytes:
    def esc(value: Any) -> str:
        return html.escape("" if value is None else str(value), quote=True)

    counts = state["counts"]
    lineage_rows = "".join(
        "<tr>"
        f"<td>{entry['revision']}</td><td><code>{esc(entry['experiment_id'])}</code></td>"
        f"<td>{esc(entry['reason'])}</td><td><code>{esc(entry['experiment_input_sha256'])}</code></td>"
        "</tr>"
        for entry in state["lineage"]["entries"]
    )
    attempt_rows = "".join(
        "<tr>"
        f"<td><code>{esc(attempt['attempt_id'])}</code></td>"
        f"<td><code>{esc(attempt['experiment_id'])}</code></td>"
        f"<td>{attempt['revision']}</td><td>{esc(attempt['spec']['status'])}</td>"
        f"<td><code>{esc(attempt['input_sha256'])}</code></td>"
        "</tr>"
        for attempt in state["attempts"]["records"]
    )
    result_rows = "".join(
        "<tr>"
        f"<td><code>{esc(result['result_id'])}</code></td>"
        f"<td><code>{esc(result['attempt_id'])}</code></td>"
        f"<td>{esc(result['outcome']['metric'])}</td>"
        f"<td>{esc(result['outcome']['analyte_reference'])}</td>"
        f"<td>{esc(result['outcome']['status'])}</td>"
        f"<td>{esc(result['outcome']['value'] if result['outcome']['value'] is not None else 'not reported')}</td>"
        f"<td>{esc(result['outcome']['unit'])}</td>"
        f"<td>{esc(result['outcome']['explanation'])}</td>"
        "</tr>"
        for result in state["results"]["records"]
    )
    observation_rows = "".join(
        "<tr>"
        f"<td><code>{esc(observation['observation_id'])}</code></td>"
        f"<td><code>{esc(observation['attempt_id'])}</code></td>"
        f"<td>{esc(observation['observation']['assessment']['analytical_acceptance'])}</td>"
        f"<td>{esc(observation['observation']['evidence']['fidelity_role'])}</td>"
        "</tr>"
        for observation in state["observations"]["records"]
    )
    links = "".join(
        f'<li><a href="{esc(name)}">{esc(label)}</a></li>'
        for name, label in (
            ("workflow_receipt.json", "Complete workflow receipt"),
            ("designs.json", "Three complete design records"),
            ("lineage.json", "Verified ordered lineage"),
            ("replacement-v1-v2.json", "v1 to v2 replacement receipt"),
            ("replacement-v2-v3.json", "v2 to v3 replacement receipt"),
            ("attempts.json", "All attempt revisions"),
            ("results.json", "All eight full result records"),
            ("observations.json", "Both analytical observations"),
            ("snapshot.json", "Original frozen snapshot"),
            ("history.csv", "Tidy design, attempt and result history"),
            ("manifest.json", "Artifact hashes and byte sizes"),
        )
    )
    html_text = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Immutable experiment-design lineage</title>
<style>
:root {{ color-scheme: light; --ink:#1d2a35; --muted:#5d6b74; --teal:#176b68; --paper:#fbfaf6; --line:#d8e0dc; --wash:#edf5f1; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--paper); color:var(--ink); font:16px/1.55 system-ui,-apple-system,sans-serif; }}
main {{ max-width:1200px; margin:auto; padding:2.5rem 1.2rem 4rem; }} h1,h2 {{ color:#102f3a; line-height:1.15; }} h1 {{ font-size:clamp(2rem,5vw,3.5rem); max-width:18ch; }} h2 {{ margin-top:2.6rem; }}
.lead {{ max-width:75ch; color:var(--muted); font-size:1.08rem; }} .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:.8rem; margin:1.5rem 0; }}
.card {{ background:var(--wash); border:1px solid var(--line); border-radius:.8rem; padding:1rem; }} .card strong {{ display:block; color:var(--teal); font-size:1.75rem; overflow-wrap:anywhere; }}
.note {{ border-left:4px solid var(--teal); padding:.8rem 1rem; background:#f0f6f3; }} .table-wrap {{ max-width:100%; overflow:auto; border:1px solid var(--line); border-radius:.7rem; background:white; }}
.table-wrap:focus {{ outline:3px solid #79b8b0; outline-offset:3px; }} table {{ border-collapse:collapse; min-width:760px; width:100%; }} caption {{ color:#102f3a; font-weight:700; padding:.9rem 1rem; text-align:left; }}
th,td {{ border-bottom:1px solid var(--line); padding:.6rem .75rem; text-align:left; vertical-align:top; }} thead th {{ background:#e5f0ec; position:sticky; top:0; }} code {{ overflow-wrap:anywhere; }} a {{ color:var(--teal); }}
@media (max-width:600px) {{ main {{ padding:1.5rem .85rem 3rem; }} h1 {{ font-size:2.3rem; }} .cards {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .card {{ padding:.75rem; }} }}
</style>
</head>
<body><main>
<p><a href="#lineage">Skip to lineage table</a></p>
<h1>Immutable experiment-design lineage</h1>
<p class="lead">A public-MCP, R-free synthetic workflow retains three intended designs across two explicit replacements, preserving the exact IDs, attempts, results, analytical observations and original snapshot selected by the caller.</p>
<section aria-labelledby="summary"><h2 id="summary">Workflow summary</h2>
<div class="cards">
<div class="card"><strong>{counts["designs"]}</strong>designs</div>
<div class="card"><strong>{counts["attempts"]}</strong>attempts</div>
<div class="card"><strong>{counts["attempt_revisions"]}</strong>attempt revisions</div>
<div class="card"><strong>{counts["results"]}</strong>result records</div>
<div class="card"><strong>{counts["assessments"]}</strong>assessments</div>
<div class="card"><strong>{counts["snapshots"]}</strong>snapshot</div>
</div></section>
<p class="note">The current pointer is discovery only. It never routes work or rewrites evidence: the old accepted observation and snapshot remain tied to v1, while the completed replacement attempt and not-accepted observation remain tied to v2. The current v3 design has no recorded attempt or assessment. These are caller-declared synthetic records, not physical or manuscript qualification.</p>
<section aria-labelledby="artifacts"><h2 id="artifacts">Inspect the retained records</h2><ul>{links}</ul></section>
<section id="lineage" aria-labelledby="lineage-heading"><h2 id="lineage-heading">Verified design lineage</h2><div class="table-wrap" role="region" aria-label="Verified design lineage; scroll horizontally" tabindex="0"><table><caption>Oldest first; the current tip is revision {state["lineage"]["current_revision"]}.</caption><thead><tr><th scope="col">Revision</th><th scope="col">Experiment ID</th><th scope="col">Replacement reason</th><th scope="col">Input SHA-256</th></tr></thead><tbody>{lineage_rows}</tbody></table></div></section>
<section aria-labelledby="attempts"><h2 id="attempts">Explicit attempts and revisions</h2><div class="table-wrap" role="region" aria-label="Attempt history; scroll horizontally" tabindex="0"><table><caption>Every attempt keeps the design ID chosen by the caller.</caption><thead><tr><th scope="col">Attempt ID</th><th scope="col">Design ID</th><th scope="col">Revision</th><th scope="col">Status</th><th scope="col">Input SHA-256</th></tr></thead><tbody>{attempt_rows}</tbody></table></div></section>
<section aria-labelledby="results"><h2 id="results">Result records with units</h2><div class="table-wrap" role="region" aria-label="Result records; scroll horizontally" tabindex="0"><table><caption>“Not reported” is unavailable, not zero; explanations remain caller-declared.</caption><thead><tr><th scope="col">Result ID</th><th scope="col">Attempt ID</th><th scope="col">Metric</th><th scope="col">Analyte</th><th scope="col">Status</th><th scope="col">Value</th><th scope="col">Unit</th><th scope="col">Explanation</th></tr></thead><tbody>{result_rows}</tbody></table></div></section>
<section aria-labelledby="observations"><h2 id="observations">Analytical observation history</h2><div class="table-wrap" role="region" aria-label="Observation history; scroll horizontally" tabindex="0"><table><caption>Assessments are explicit evidence records under one predeclared A/B contract.</caption><thead><tr><th scope="col">Observation ID</th><th scope="col">Attempt ID</th><th scope="col">Assessment</th><th scope="col">Fidelity role</th></tr></thead><tbody>{observation_rows}</tbody></table></div></section>
<section aria-labelledby="method"><h2 id="method">Method and limits</h2><p>Three owned isocratic method declarations use {esc(METHOD_FLOW[0])}, {esc(METHOD_FLOW[1])} and {esc(METHOD_FLOW[2])} mL/min at seven, eight and nine minutes. The observation contract requires caller-declared A/B retention and resolution records.</p><p>{esc(state["scope"])}</p></section>
</main></body></html>
"""
    return html_text.encode("utf-8")


def _artifact_bytes(state: dict[str, Any]) -> dict[str, bytes]:
    return {
        "designs.json": _json_bytes({"records": state["designs"]}),
        "lineage.json": _json_bytes(state["lineage"]),
        "replacement-v1-v2.json": _json_bytes(state["replacement_v1_v2"]),
        "replacement-v2-v3.json": _json_bytes(state["replacement_v2_v3"]),
        "attempts.json": _json_bytes(state["attempts"]),
        "results.json": _json_bytes(state["results"]),
        "observations.json": _json_bytes(state["observations"]),
        "snapshot.json": _json_bytes({"snapshot": state["snapshot"]}),
        "history.csv": _history_csv(state),
        "workflow_receipt.json": _json_bytes(state),
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
    _write_exclusive(output_dir / "manifest.json", _json_bytes(manifest))
    return manifest


def run(output_dir: Path, *, data_dir: str | None = None) -> dict[str, Any]:
    """Run the public-MCP workflow and write a new inspectable output directory."""

    output_path = Path(output_dir)
    owned_data = data_dir is None
    data_path = (
        Path(tempfile.gettempdir()) / f"measure-design-lineage-data-{uuid.uuid4().hex}"
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
        return json.loads((paths["output"] / "workflow_receipt.json").read_text(encoding="utf-8"))
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
        print(f"design-lineage example failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
