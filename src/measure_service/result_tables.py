"""Atomic mapped outcome imports and immutable, owned source-row evidence."""

from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from . import experiments
from .result_table_models import ResultTableSpec
from .result_table_parser import ResultTablePlan, ResultTableRow, parse_result_table
from .result_table_schema import ensure_result_table_schema, validate_result_table_schema
from .storage import canonical, now, registry_connection, registry_mutation, sha256

IMPORT_PROFILE = "measure-result-table-import/1"
ROW_PROFILE = "measure-result-table-row/1"
ROWS_PROFILE = "measure-result-table-rows/1"
SOURCE_PROFILE = "measure-result-table-source/1"
MAX_SOURCE_PAGE_BYTES = 12_000


def _invalid(reason: str) -> None:
    raise ValueError(f"Stored result-table evidence is inconsistent: {reason}")


def _source(plan: ResultTablePlan, row: ResultTableRow, import_id: str) -> dict[str, Any]:
    return {
        "profile": ROW_PROFILE,
        "import_id": import_id,
        "input_sha256": plan.input_sha256,
        "source_sha256": plan.source_sha256,
        "mapping_sha256": plan.mapping_sha256,
        "ordinal": row.ordinal,
        "record_number": row.record_number,
        "line_start": row.line_start,
        "line_end": row.line_end,
        "row_sha256": row.row_sha256,
    }


def _receipt(
    plan: ResultTablePlan, import_id: str, created_at: str, bindings_sha256: str
) -> dict[str, Any]:
    return experiments.bounded(
        {
            **plan.metadata(),
            "profile": IMPORT_PROFILE,
            "import_id": import_id,
            "input_sha256": plan.input_sha256,
            "created_at": created_at,
            "result_bindings_sha256": bindings_sha256,
        }
    )


def import_result_table(owner: str, spec: ResultTableSpec, key: str) -> dict[str, Any]:
    """Commit every row, its raw source and the retry receipt in one transaction."""
    plan = parse_result_table(spec)

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        ensure_result_table_schema(con)
        import_id, created_at = str(uuid.uuid4()), now()
        specification = {
            "source_name": plan.source_name,
            "delimiter": plan.delimiter,
            "mapping": json.loads(plan.mapping_json),
        }
        con.execute(
            "INSERT INTO result_table_imports VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                import_id,
                owner,
                plan.input_sha256,
                plan.source_bytes.decode("utf-8"),
                canonical(specification),
                canonical(plan.metadata()),
                created_at,
            ),
        )
        bindings = []
        verified_attempts = set()
        for row in plan.rows:
            coordinates = (row.attempt_id, row.expected_attempt_revision)
            if coordinates not in verified_attempts:
                _verified_attempt(con, owner, *coordinates)
                verified_attempts.add(coordinates)
            # Source order is part of the frozen table identity. All calls
            # share this transaction, including corrections across attempts.
            (result,) = experiments._insert_result_records(
                con,
                owner,
                row.attempt_id,
                row.expected_attempt_revision,
                [row.result_spec()],
                table_sources=[_source(plan, row, import_id)],
            )
            con.execute(
                "INSERT INTO result_table_rows VALUES (?, ?, ?, ?, ?)",
                (
                    import_id,
                    row.ordinal,
                    result["result_id"],
                    result["input_sha256"],
                    row.row_sha256,
                ),
            )
            bindings.append(
                {
                    "ordinal": row.ordinal,
                    "row_sha256": row.row_sha256,
                    "result_id": result["result_id"],
                    "result_sha256": result["input_sha256"],
                }
            )
        bindings_sha256 = sha256(bindings)
        receipt = _receipt(plan, import_id, created_at, bindings_sha256)
        con.execute(
            "UPDATE result_table_imports SET metadata=? WHERE id=? AND owner=?",
            (
                canonical(
                    {
                        **plan.metadata(),
                        "result_bindings_sha256": bindings_sha256,
                        "receipt_sha256": sha256(receipt),
                    }
                ),
                import_id,
                owner,
            ),
        )
        return receipt

    # A digest binds every raw byte, mapping, declared name and normalized
    # row. It keeps the existing bounded global receipt namespace without
    # duplicating source text and normalized rows in the request envelope.
    return registry_mutation(
        owner,
        key,
        {"operation": "import_result_table", "input_sha256": plan.input_sha256},
        write,
    )


def _canonical_object(text: Any, label: str) -> dict[str, Any]:
    if type(text) is not str:
        _invalid(label)
    if (
        len(text) > experiments.MAX_RECORD_BYTES
        or len(text.encode("utf-8")) > experiments.MAX_RECORD_BYTES
    ):
        _invalid(f"{label} byte limit")
    try:
        value = json.loads(text)
        if type(value) is not dict or canonical(value) != text:
            _invalid(label)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError(f"Stored result-table evidence is inconsistent: {label}") from error
    return value


def _verified_attempt(
    con: sqlite3.Connection, owner: str, attempt_id: str, revision: int
) -> dict[str, Any]:
    experiments._owned(con, "experiment_attempts", owner, attempt_id)
    raw = con.execute(
        "SELECT payload, digest FROM attempt_revisions WHERE attempt_id=? AND revision=?",
        (attempt_id, revision),
    ).fetchone()
    if raw is None:
        raise ValueError("Attempt revision was not found")
    payload = _canonical_object(raw["payload"], "historical attempt payload")
    if sha256(payload) != raw["digest"]:
        _invalid("historical attempt digest")
    return experiments._attempt(con, owner, attempt_id, revision)


def _read(
    con: sqlite3.Connection, owner: str, import_id: str
) -> tuple[ResultTablePlan, dict[str, Any], list[dict[str, Any]]]:
    if not validate_result_table_schema(con):
        raise ValueError("Result-table import was not found or is not accessible")
    record = con.execute(
        "SELECT * FROM result_table_imports WHERE id=? AND owner=?", (import_id, owner)
    ).fetchone()
    if record is None:
        raise ValueError("Result-table import was not found or is not accessible")
    specification = _canonical_object(record["specification"], "source specification")
    if set(specification) != {"source_name", "delimiter", "mapping"}:
        _invalid("source specification fields")
    try:
        plan = parse_result_table(ResultTableSpec(**specification, csv_text=record["source_text"]))
    except (TypeError, ValueError) as error:
        raise ValueError("Stored result-table source is not a valid mapped table") from error
    if record["input_sha256"] != plan.input_sha256:
        _invalid("input identity")
    metadata = _canonical_object(record["metadata"], "source metadata")
    bindings_sha256 = metadata.pop("result_bindings_sha256", None)
    receipt_sha256 = metadata.pop("receipt_sha256", None)
    if canonical(metadata) != canonical(plan.metadata()):
        _invalid("source metadata differs from parsed source")
    try:
        if type(record["created_at"]) is not str or len(record["created_at"]) > 100:
            _invalid("creation timestamp")
        timestamp = datetime.fromisoformat(record["created_at"])
        if timestamp.utcoffset() is None:
            _invalid("creation timestamp")
    except (TypeError, ValueError) as error:
        raise ValueError("Stored result-table creation timestamp is invalid") from error
    members = con.execute(
        "SELECT * FROM result_table_rows WHERE import_id=? ORDER BY ordinal", (import_id,)
    ).fetchall()
    if len(members) != len(plan.rows):
        _invalid("row membership count")
    rows = []
    attempts = {}
    for member, row in zip(members, plan.rows):
        if member["ordinal"] != row.ordinal or member["row_sha256"] != row.row_sha256:
            _invalid("row membership identity")
        result = con.execute(
            "SELECT * FROM experiment_results WHERE id=? AND owner=?",
            (member["result_id"], owner),
        ).fetchone()
        if result is None:
            _invalid("result ownership or membership")
        requested = row.result_spec()
        if (
            result["attempt_id"] != row.attempt_id
            or result["attempt_revision"] != row.expected_attempt_revision
            or result["metric"] != requested.metric
            or result["analyte_reference"] != requested.analyte_reference
            or result["supersedes_id"] != requested.supersedes_result_id
        ):
            _invalid("result and source-row coordinates")
        payload = _canonical_object(result["payload"], "result payload")
        if sha256(payload) != result["digest"] or result["digest"] != member["result_sha256"]:
            _invalid("result digest")
        coordinates = (row.attempt_id, row.expected_attempt_revision)
        if coordinates not in attempts:
            attempts[coordinates] = _verified_attempt(con, owner, *coordinates)
        attempt = attempts[coordinates]
        expected = experiments._outcome(con, owner, attempt, requested)
        expected["provenance"]["table_source"] = _source(plan, row, import_id)
        if canonical(payload) != canonical(expected):
            _invalid("result provenance or retained value")
        rows.append(
            {
                **row.as_dict(),
                "row_sha256": row.row_sha256,
                "result_id": member["result_id"],
                "result_sha256": member["result_sha256"],
            }
        )
    bindings = [
        {key: row[key] for key in ("ordinal", "row_sha256", "result_id", "result_sha256")}
        for row in rows
    ]
    if bindings_sha256 != sha256(bindings):
        _invalid("result assignment digest")
    receipt = _receipt(plan, import_id, record["created_at"], bindings_sha256)
    if receipt_sha256 != sha256(receipt):
        _invalid("receipt identity")
    return plan, receipt, rows


def get_result_table_import(owner: str, import_id: str) -> dict[str, Any]:
    """Read an immutable owned import receipt, checking source and result closure."""
    with registry_connection() as con:
        _, receipt, _ = _read(con, owner, import_id)
        return receipt


def _page(offset: int, limit: int, maximum: int) -> None:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError(
            f"Use a nonnegative integer offset and an integer limit from 1 to {maximum}"
        )


def read_result_table_rows(
    owner: str, import_id: str, offset: int = 0, limit: int = 20
) -> dict[str, Any]:
    """Page immutable source-row/result membership rather than mutable result heads."""
    _page(offset, limit, 20)
    with registry_connection() as con:
        plan, _, rows = _read(con, owner, import_id)
        selected = rows[offset : offset + limit]
        end = offset + len(selected)
        return experiments.bounded(
            {
                "profile": ROWS_PROFILE,
                "import_id": import_id,
                "input_sha256": plan.input_sha256,
                "offset": offset,
                "limit": limit,
                "total_count": len(rows),
                "next_offset": end if end < len(rows) else None,
                "rows": selected,
            }
        )


def read_result_table_source(
    owner: str, import_id: str, offset: int = 0, limit: int = MAX_SOURCE_PAGE_BYTES
) -> dict[str, Any]:
    """Page exact UTF8 source bytes without filesystem access or line re-encoding."""
    _page(offset, limit, MAX_SOURCE_PAGE_BYTES)
    with registry_connection() as con:
        plan, _, _ = _read(con, owner, import_id)
        if offset > len(plan.source_bytes):
            raise ValueError("Source byte offset exceeds the retained source length")
        content = plan.source_bytes[offset : offset + limit]
        end = offset + len(content)
        return experiments.bounded(
            {
                "profile": SOURCE_PROFILE,
                "import_id": import_id,
                "input_sha256": plan.input_sha256,
                "source_sha256": plan.source_sha256,
                "offset": offset,
                "total_bytes": len(plan.source_bytes),
                "next_offset": end if end < len(plan.source_bytes) else None,
                "encoding": "base64",
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        )
