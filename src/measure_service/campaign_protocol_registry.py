"""Owned immutable declarations for matched synthetic optimizer campaigns."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from measure_methods import design

from . import gaussian_producer, observations, retention
from .campaign_protocol import (
    AblationProtocolSpec,
    ProtocolSpec,
    optimizer_configuration,
    parse_campaign_protocol,
)
from .method_backend import method_backend_info
from .storage import canonical, now, registry_connection, registry_mutation, sha256


def _bounded(value: Any) -> Any:
    if len(canonical(value).encode("utf-8")) > 64_000:
        raise ValueError("Campaign protocol exceeds the 64,000-byte limit")
    return value


def _protocol_row(con: sqlite3.Connection, owner: str, protocol_id: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM campaign_protocols WHERE id=? AND owner=?", (protocol_id, owner)
    ).fetchone()
    if row is None:
        raise ValueError("Campaign protocol was not found or is not accessible")
    return row


def _protocol_response(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload"])
    spec = parse_campaign_protocol(payload["spec"])
    normalized = spec.model_dump(mode="json")
    request = {"operation": "register_campaign_protocol", "title": row["title"], "spec": normalized}
    if payload["spec"] != normalized or sha256(request) != row["input_digest"]:
        raise ValueError("Stored campaign protocol input digest does not match its contents")
    if isinstance(spec, AblationProtocolSpec):
        if payload.get("optimizer_configuration") != optimizer_configuration(spec):
            raise ValueError("Stored campaign optimizer configuration does not match its protocol")
    elif "optimizer_configuration" in payload:
        raise ValueError("Stored V1 campaign protocol has unsupported optimizer configuration")
    if sha256(payload) != row["output_digest"]:
        raise ValueError("Stored campaign protocol output digest does not match its contents")
    return _bounded(
        {
            "schema_version": "1.0",
            "protocol_id": row["id"],
            "title": row["title"],
            **payload,
            "input_sha256": row["input_digest"],
            "output_sha256": row["output_digest"],
            "created_at": row["created_at"],
        }
    )


def _task_links(con: sqlite3.Connection, owner: str, spec: ProtocolSpec) -> list[dict]:
    tasks = []
    for task in spec.tasks:
        producer = gaussian_producer._producer_response(
            gaussian_producer._producer_row(con, owner, task.producer_id)
        )
        model = retention._model_response(
            retention._model_row(con, owner, producer["retention_model"]["model_id"])
        )
        contract = observations._contract_response(
            observations._contract(con, owner, task.observation_contract_id)
        )
        if model["input_sha256"] != producer["retention_model"]["input_sha256"]:
            raise ValueError("Task producer retention-model identity is inconsistent")
        if (
            model["spec"]["parameter_origin"] != "synthetic"
            or model["spec"]["strong_solvent"] != "B"
        ):
            raise ValueError(
                "Campaign tasks require synthetic retention models with strong solvent B"
            )
        cohort = sorted(item["analyte_id"] for item in model["spec"]["analytes"])
        if len(cohort) < 2:
            raise ValueError("Matched campaign tasks require at least two analytes")
        if cohort != sorted(contract["spec"]["required_analytes"]) or cohort != sorted(
            item["analyte_id"] for item in producer["spec"]["widths"]
        ):
            raise ValueError(
                "Task model, producer and observation contract cohorts must match exactly"
            )
        if contract["spec"]["resolution_definition"] != producer["spec"]["resolution_definition"]:
            raise ValueError("Task contract and producer resolution definitions must match")
        if (
            contract["spec"]["max_acquisition_duration_min"]
            != spec.domain.acquisition_duration_min.upper
        ):
            raise ValueError(
                "Task contract maximum acquisition duration must equal the declared domain maximum"
            )
        tasks.append(
            {
                "task_id": task.task_id,
                "producer": {
                    "producer_id": producer["producer_id"],
                    "input_sha256": producer["input_sha256"],
                    "output_sha256": producer["output_sha256"],
                    "retention_model_id": model["model_id"],
                    "retention_model_input_sha256": model["input_sha256"],
                },
                "contract": {
                    "contract_id": contract["contract_id"],
                    "input_sha256": contract["input_sha256"],
                    "spec": contract["spec"],
                    "utility_definition": contract["utility_definition"],
                },
                "analyte_ids": cohort,
            }
        )
    return tasks


def register_campaign_protocol(
    owner: str, title: str, spec: ProtocolSpec | dict[str, Any], key: str
) -> dict[str, Any]:
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("A campaign protocol title of 1 to 200 characters is required")
    spec = parse_campaign_protocol(spec)
    requested = spec.model_dump(mode="json")
    request = {"operation": "register_campaign_protocol", "title": title, "spec": requested}

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        import measure_service.campaign_protocol as protocol_module

        payload = {
            "spec": requested,
            "tasks": _task_links(con, owner, spec),
            "implementation_provenance": {
                "measure_service": method_backend_info(),
                "protocol_module_sha256": hashlib.sha256(
                    Path(protocol_module.__file__).read_bytes()
                ).hexdigest(),
                "registry_module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "design_kernel_module_sha256": hashlib.sha256(
                    Path(design.__file__).read_bytes()
                ).hexdigest(),
            },
        }
        if isinstance(spec, AblationProtocolSpec):
            payload["optimizer_configuration"] = optimizer_configuration(spec)
        _bounded(payload)
        protocol_id, created = str(uuid.uuid4()), now()
        con.execute(
            "INSERT INTO campaign_protocols VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                protocol_id,
                owner,
                title,
                canonical(payload),
                sha256(request),
                sha256(payload),
                created,
            ),
        )
        return _protocol_response(_protocol_row(con, owner, protocol_id))

    return registry_mutation(owner, key, request, write)


def get_campaign_protocol(owner: str, protocol_id: str) -> dict[str, Any]:
    with registry_connection() as con:
        return _protocol_response(_protocol_row(con, owner, protocol_id))
