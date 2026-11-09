"""Explicit design replacements retain immutable experiments and evidence bindings."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any

from . import experiments
from .design_lineage_schema import ensure_design_lineage_schema, validate_design_lineage_schema
from .storage import canonical, registry_connection, registry_mutation, sha256

VERSION_PROFILE = "measure-experiment-design-version/1"
SUPERSESSION_PROFILE = "measure-experiment-design-supersession/1"
LINEAGE_PROFILE = "measure-experiment-design-lineage/1"
MAX_VERSIONS = 50
MAX_PAGE = 20
MAX_ENTRY_BYTES = 3_000
MAX_REASON_BYTES = 2_000
MAX_PAGE_BYTES = 64_000
MAX_AGGREGATE_BYTES = 160_000
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ENTRY_FIELDS = {
    "profile",
    "schema_version",
    "root_experiment_id",
    "revision",
    "experiment_id",
    "experiment_input_sha256",
    "supersedes_experiment_id",
    "supersedes_entry_sha256",
    "reason",
    "created_at",
}


def _invalid(detail: str) -> None:
    raise ValueError(f"Stored experiment-design lineage is inconsistent: {detail}")


def _identifier(value: Any) -> None:
    if type(value) is not str or not 1 <= len(value) <= 100 or not value.strip():
        raise ValueError("An experiment ID must contain 1 to 100 characters")
    if "\x00" in value:
        raise ValueError("An experiment ID must not contain NUL")
    value.encode("utf-8")


def _digest(value: Any) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("Expected experiment digest must be 64 lowercase hexadecimal characters")


def _reason(value: Any) -> None:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError("A nonempty design replacement reason is required, without NUL")
    if len(value) > MAX_REASON_BYTES or len(value.encode("utf-8")) > MAX_REASON_BYTES:
        raise ValueError("A design replacement reason must fit 2000 UTF8 bytes")


def _timestamp(value: Any) -> None:
    if type(value) is not str or not 1 <= len(value) <= 100:
        _invalid("creation timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            _invalid("creation timestamp must be timezone aware")
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Stored experiment-design lineage has an invalid timestamp") from error


def _bounded(value: dict[str, Any], maximum: int) -> dict[str, Any]:
    try:
        size = len(canonical(value).encode("utf-8"))
    except (TypeError, ValueError, RecursionError, OverflowError) as error:
        raise ValueError(
            "Experiment-design lineage must be a finite canonical JSON object"
        ) from error
    if size > maximum:
        raise ValueError("Experiment-design lineage exceeds its canonical UTF8 byte limit")
    return value


def _object(text: Any, maximum: int, label: str) -> dict[str, Any]:
    try:
        if type(text) is not str or len(text) > maximum or len(text.encode("utf-8")) > maximum:
            _invalid(label + " byte limit")
        value = json.loads(text)
        if type(value) is not dict or canonical(value) != text:
            _invalid(label + " canonical object")
        return value
    except (TypeError, ValueError, RecursionError, OverflowError) as error:
        raise ValueError("Stored experiment-design lineage is inconsistent: " + label) from error


def _verified_experiment(
    con: sqlite3.Connection, owner: str, experiment_id: str
) -> tuple[sqlite3.Row, dict[str, Any]]:
    row = con.execute(
        "SELECT id, "
        + _text_column("payload", experiments.MAX_RECORD_BYTES)
        + ", "
        + _text_column("digest", 64)
        + ", "
        + _text_column("created_at", 100)
        + " FROM experiments WHERE id=? AND owner=?",
        (experiment_id, owner),
    ).fetchone()
    if row is None:
        raise ValueError("Registry record was not found or is not accessible")
    payload = _object(row["payload"], experiments.MAX_RECORD_BYTES, "experiment payload")
    if sha256(payload) != row["digest"]:
        _invalid("experiment payload digest")
    _digest(row["digest"])
    _timestamp(row["created_at"])
    # Normalize defaults only for semantic no-op comparison. The identity above
    # always hashes the untouched retained payload, including service-built links.
    requested = dict(payload)
    for field in ("executable_method", "observation_contract", "observation_producer"):
        requested.pop(field, None)
    try:
        normalized = experiments._requested_spec(
            experiments.ExperimentSpec.model_validate(requested)
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("Stored experiment-design specification is inconsistent") from error
    return row, normalized


def _text_column(column: str, maximum: int) -> str:
    """Bound trusted SQL column names before text is materialized in Python."""
    if column not in {
        "payload",
        "digest",
        "created_at",
        "experiment_id",
        "root_experiment_id",
        "previous_experiment_id",
        "latest_experiment_id",
        "latest_entry_sha256",
    }:
        raise ValueError("Unsupported lineage column")
    return (
        f"CASE WHEN typeof({column})='text' AND length(CAST({column} AS BLOB))<={maximum} "
        f"THEN {column} END AS {column}"
    )


def _make_entry(
    root_id: str,
    revision: int,
    experiment: sqlite3.Row | dict[str, Any],
    previous: dict[str, Any] | None,
    reason: str,
) -> dict[str, Any]:
    is_record = isinstance(experiment, dict)
    payload = {
        "profile": VERSION_PROFILE,
        "schema_version": "1.0",
        "root_experiment_id": root_id,
        "revision": revision,
        "experiment_id": experiment["experiment_id" if is_record else "id"],
        "experiment_input_sha256": experiment["input_sha256" if is_record else "digest"],
        "supersedes_experiment_id": None if previous is None else previous["experiment_id"],
        "supersedes_entry_sha256": None if previous is None else previous["entry_sha256"],
        "reason": reason,
        "created_at": experiment["created_at"],
    }
    return _bounded({**payload, "entry_sha256": sha256(payload)}, MAX_ENTRY_BYTES)


def _stored_entry(row: sqlite3.Row) -> dict[str, Any]:
    if row["previous_valid"] != 1:
        _invalid("previous experiment column")
    payload = _object(row["payload"], MAX_ENTRY_BYTES, "version payload")
    if set(payload) != _ENTRY_FIELDS or sha256(payload) != row["digest"]:
        _invalid("version payload or digest")
    if payload["profile"] != VERSION_PROFILE or payload["schema_version"] != "1.0":
        _invalid("version profile")
    if type(payload["revision"]) is not int or not 1 <= payload["revision"] <= MAX_VERSIONS:
        _invalid("version revision")
    for field in ("experiment_id", "root_experiment_id"):
        _identifier(payload[field])
    _digest(payload["experiment_input_sha256"])
    _digest(row["digest"])
    _reason(payload["reason"])
    _timestamp(payload["created_at"])
    columns = {
        "experiment_id": "experiment_id",
        "root_experiment_id": "root_experiment_id",
        "revision": "revision",
        "supersedes_experiment_id": "previous_experiment_id",
        "created_at": "created_at",
    }
    if any(
        type(payload[key]) is not type(row[column]) or payload[key] != row[column]
        for key, column in columns.items()
    ):
        _invalid("version lookup columns")
    return _bounded({**payload, "entry_sha256": row["digest"]}, MAX_ENTRY_BYTES)


def _load(
    con: sqlite3.Connection, owner: str, experiment_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    selected, requested = _verified_experiment(con, owner, experiment_id)
    if not validate_design_lineage_schema(con):
        return (
            [_make_entry(experiment_id, 1, selected, None, "Initial immutable experiment")],
            requested,
            False,
        )
    member = con.execute(
        "SELECT "
        + _text_column("root_experiment_id", 400)
        + ", owner=? AS owner_matches FROM design_lineage_versions WHERE experiment_id=?",
        (owner, experiment_id),
    ).fetchone()
    if member is None:
        anchor = con.execute(
            "SELECT 1 FROM design_lineage_roots WHERE root_experiment_id=? OR latest_experiment_id=? LIMIT 1",
            (experiment_id, experiment_id),
        ).fetchone()
        child = con.execute(
            "SELECT 1 FROM design_lineage_versions WHERE previous_experiment_id=? OR root_experiment_id=? LIMIT 1",
            (experiment_id, experiment_id),
        ).fetchone()
        if anchor is not None or child is not None:
            _invalid("missing selected version")
        return (
            [_make_entry(experiment_id, 1, selected, None, "Initial immutable experiment")],
            requested,
            False,
        )
    if member["owner_matches"] != 1:
        _invalid("version owner")
    anchor = con.execute(
        "SELECT "
        + _text_column("root_experiment_id", 400)
        + ", "
        + _text_column("latest_experiment_id", 400)
        + ", "
        + _text_column("latest_entry_sha256", 64)
        + ", CASE WHEN typeof(latest_revision)='integer' THEN latest_revision END AS latest_revision, "
        "owner=? AS owner_matches FROM design_lineage_roots WHERE root_experiment_id=?",
        (owner, member["root_experiment_id"]),
    ).fetchone()
    if anchor is None or anchor["owner_matches"] != 1:
        _invalid("missing or inconsistent root owner")
    if (
        type(anchor["latest_revision"]) is not int
        or not 1 <= anchor["latest_revision"] <= MAX_VERSIONS
    ):
        _invalid("head revision")
    rows = con.execute(
        "SELECT "
        + ", ".join(
            _text_column(name, maximum)
            for name, maximum in (
                ("experiment_id", 400),
                ("root_experiment_id", 400),
                ("previous_experiment_id", 400),
                ("payload", MAX_ENTRY_BYTES),
                ("digest", 64),
                ("created_at", 100),
            )
        )
        + ", CASE WHEN typeof(revision)='integer' THEN revision END AS revision, owner=? AS owner_matches, "
        "(previous_experiment_id IS NULL OR (typeof(previous_experiment_id)='text' "
        "AND length(CAST(previous_experiment_id AS BLOB))<=400)) AS previous_valid "
        "FROM design_lineage_versions WHERE root_experiment_id=? ORDER BY revision LIMIT ?",
        (owner, anchor["root_experiment_id"], MAX_VERSIONS + 1),
    ).fetchall()
    if len(rows) != anchor["latest_revision"]:
        _invalid("version count does not match head anchor")
    entries = []
    for revision, row in enumerate(rows, 1):
        if row["owner_matches"] != 1:
            _invalid("mixed version ownership")
        entry = _stored_entry(row)
        if (
            entry["revision"] != revision
            or entry["root_experiment_id"] != anchor["root_experiment_id"]
        ):
            _invalid("noncontiguous versions or root")
        record, _normalized = _verified_experiment(con, owner, entry["experiment_id"])
        if (
            record["digest"] != entry["experiment_input_sha256"]
            or record["created_at"] != entry["created_at"]
        ):
            _invalid("immutable experiment binding")
        previous = entries[-1] if entries else None
        expected_id = None if previous is None else previous["experiment_id"]
        expected_hash = None if previous is None else previous["entry_sha256"]
        if (
            entry["supersedes_experiment_id"] != expected_id
            or entry["supersedes_entry_sha256"] != expected_hash
        ):
            _invalid("predecessor identity or digest")
        if revision == 1 and (
            entry["experiment_id"] != anchor["root_experiment_id"]
            or entry["reason"] != "Initial immutable experiment"
        ):
            _invalid("initial version")
        entries.append(entry)
    head = entries[-1]
    if (
        head["experiment_id"] != anchor["latest_experiment_id"]
        or head["entry_sha256"] != anchor["latest_entry_sha256"]
    ):
        _invalid("head anchor identity or digest")
    if not any(entry["experiment_id"] == experiment_id for entry in entries):
        _invalid("selected membership")
    # Check inbound successors as well as this root's ordered members. A foreign
    # root must not occupy the unique successor of an otherwise valid tip.
    successors = {
        previous["experiment_id"]: following["experiment_id"]
        for previous, following in zip(entries, entries[1:])
    }
    placeholders = ",".join("?" for _ in entries)
    children = con.execute(
        "SELECT "
        + _text_column("experiment_id", 400)
        + ", "
        + _text_column("root_experiment_id", 400)
        + ", previous_experiment_id, owner=? AS owner_matches FROM design_lineage_versions "
        f"WHERE previous_experiment_id IN ({placeholders})",
        (owner, *(entry["experiment_id"] for entry in entries)),
    ).fetchall()
    if len(children) != len(successors) or any(
        child["owner_matches"] != 1
        or child["root_experiment_id"] != anchor["root_experiment_id"]
        or child["previous_experiment_id"] not in successors
        or successors.get(child["previous_experiment_id"]) != child["experiment_id"]
        for child in children
    ):
        _invalid("inconsistent successor references")
    return entries, requested, True


def _insert_entry(con: sqlite3.Connection, owner: str, entry: dict[str, Any]) -> None:
    payload = {key: value for key, value in entry.items() if key != "entry_sha256"}
    con.execute(
        "INSERT INTO design_lineage_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entry["experiment_id"],
            entry["root_experiment_id"],
            owner,
            entry["revision"],
            entry["supersedes_experiment_id"],
            canonical(payload),
            entry["entry_sha256"],
            entry["created_at"],
        ),
    )


def supersede_experiment(
    owner: str,
    experiment_id: str,
    expected_input_sha256: str,
    spec: experiments.ExperimentSpec,
    reason: str,
    key: str,
) -> dict[str, Any]:
    """Replace only a current design, retaining all original experiment bindings."""
    _identifier(experiment_id)
    _digest(expected_input_sha256)
    _reason(reason)
    requested = experiments._requested_spec(spec)

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        entries, parent_requested, persisted = _load(con, owner, experiment_id)
        head = entries[-1]
        if head["experiment_id"] != experiment_id:
            raise ValueError(
                "Experiment design conflict; only the current design can be superseded"
            )
        if head["experiment_input_sha256"] != expected_input_sha256:
            raise ValueError("Experiment design digest conflict; retrieve the immutable design")
        if canonical(parent_requested) == canonical(requested):
            raise ValueError(
                "A replacement design must change the complete experiment specification"
            )
        if len(entries) >= MAX_VERSIONS:
            raise ValueError("An experiment design lineage is limited to 50 versions")
        ensure_design_lineage_schema(con)
        root_id = entries[0]["experiment_id"]
        if not persisted:
            con.execute(
                "INSERT INTO design_lineage_roots VALUES (?, ?, ?, ?, ?)",
                (root_id, owner, 1, root_id, head["entry_sha256"]),
            )
            _insert_entry(con, owner, head)
        replacement = experiments._insert_experiment_record(con, owner, requested)
        entry = _make_entry(root_id, len(entries) + 1, replacement, head, reason)
        _insert_entry(con, owner, entry)
        advanced = con.execute(
            "UPDATE design_lineage_roots SET latest_revision=?,latest_experiment_id=?,latest_entry_sha256=? "
            "WHERE root_experiment_id=? AND owner=? AND latest_revision=? AND latest_experiment_id=? AND latest_entry_sha256=?",
            (
                entry["revision"],
                entry["experiment_id"],
                entry["entry_sha256"],
                root_id,
                owner,
                head["revision"],
                head["experiment_id"],
                head["entry_sha256"],
            ),
        )
        if advanced.rowcount != 1:
            raise ValueError("Experiment design head conflict")
        return _bounded(
            {"profile": SUPERSESSION_PROFILE, "schema_version": "1.0", "entry": entry},
            MAX_PAGE_BYTES,
        )

    def validate_response(con: sqlite3.Connection, response: Any) -> None:
        if type(response) is not dict or set(response) != {"profile", "schema_version", "entry"}:
            _invalid("supersession receipt envelope")
        if response["profile"] != SUPERSESSION_PROFILE or response["schema_version"] != "1.0":
            _invalid("supersession receipt profile")
        entry = response["entry"]
        if type(entry) is not dict or set(entry) != _ENTRY_FIELDS | {"entry_sha256"}:
            _invalid("supersession receipt entry")
        _bounded(entry, MAX_ENTRY_BYTES)
        _identifier(entry["experiment_id"])
        entries, retained_requested, _persisted = _load(con, owner, entry["experiment_id"])
        position = next(
            i for i, item in enumerate(entries) if item["experiment_id"] == entry["experiment_id"]
        )
        if position == 0 or canonical(entries[position]) != canonical(entry):
            _invalid("supersession receipt historical entry")
        parent = entries[position - 1]
        if (
            parent["experiment_id"] != experiment_id
            or parent["experiment_input_sha256"] != expected_input_sha256
            or entry["reason"] != reason
            or canonical(retained_requested) != canonical(requested)
        ):
            _invalid("supersession receipt request binding")
        _bounded(response, MAX_PAGE_BYTES)

    return registry_mutation(
        owner,
        key,
        {
            "operation": "supersede_experiment",
            "experiment_id": experiment_id,
            "expected_input_sha256": expected_input_sha256,
            "spec": requested,
            "reason": reason,
        },
        write,
        response_validator=validate_response,
    )


def get_experiment_design_lineage(
    owner: str,
    experiment_id: str,
    offset: int = 0,
    limit: int = 10,
) -> dict[str, Any]:
    """Read a bounded verified lineage; its current pointer does not move evidence."""
    _identifier(experiment_id)
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= MAX_PAGE
    ):
        raise ValueError("Lineage offset must be a nonnegative integer and limit must be 1 to 20")
    with registry_connection() as con:
        entries, _requested, _persisted = _load(con, owner, experiment_id)
        selected = next(entry for entry in entries if entry["experiment_id"] == experiment_id)
        head = entries[-1]
        return _bounded(
            {
                "profile": LINEAGE_PROFILE,
                "schema_version": "1.0",
                "root_experiment_id": entries[0]["experiment_id"],
                "selected_experiment_id": experiment_id,
                "selected_revision": selected["revision"],
                "current_experiment_id": head["experiment_id"],
                "current_revision": head["revision"],
                "current_entry_sha256": head["entry_sha256"],
                "count": len(entries),
                "offset": offset,
                "limit": limit,
                "next_offset": offset + limit if offset + limit < len(entries) else None,
                "entries": entries[offset : offset + limit],
            },
            MAX_PAGE_BYTES,
        )
