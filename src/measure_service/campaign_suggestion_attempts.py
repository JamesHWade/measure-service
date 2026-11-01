"""Bounded durable claims and terminal evidence for campaign suggestions.

This module owns only the storage lifecycle for a suggestion computation. It
does not decide campaign readiness, generate a candidate, or interpret backend
diagnostics. Callers provide a transaction and authoritative campaign context.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import sqlite3
import uuid
from typing import Any

from .storage import canonical, now, sha256

SCHEMA_VERSION = "1.0"
PROFILE = "campaign-suggestion-attempt/1"
MAX_REQUEST_BYTES = 64_000
MAX_CONTEXT_BYTES = 128_000
MAX_OUTCOME_BYTES = 64_000
MAX_PUBLIC_BYTES = 256_000
MAX_LIST_SUMMARY_BYTES = 64_000
_CLAIM_STATUSES = {"claimed"}
_TERMINAL_STATUSES = {"accepted", "rejected", "failed", "abandoned_unknown"}
_ALL_STATUSES = _CLAIM_STATUSES | _TERMINAL_STATUSES


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create suggestion-attempt tables within the caller's transaction."""
    campaign_table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_runs'"
    ).fetchone()
    if campaign_table is None:
        raise ValueError("Campaign storage must exist before suggestion-attempt storage")
    statements = (
        """CREATE TABLE IF NOT EXISTS campaign_suggestion_attempts (
          id TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          request_key TEXT NOT NULL,
          campaign_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          expected_revision INTEGER NOT NULL,
          execution_token TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN (
            'claimed', 'accepted', 'rejected', 'failed', 'abandoned_unknown'
          )),
          payload TEXT NOT NULL,
          input_digest TEXT NOT NULL,
          context_digest TEXT NOT NULL,
          record_digest TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(owner, request_key),
          FOREIGN KEY(campaign_id) REFERENCES campaign_runs(id)
        )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS campaign_suggestion_one_active
          ON campaign_suggestion_attempts(owner, campaign_id) WHERE status='claimed'""",
        """CREATE INDEX IF NOT EXISTS campaign_suggestion_owner_campaign_order
          ON campaign_suggestion_attempts(owner, campaign_id, created_at, id)""",
        """CREATE TABLE IF NOT EXISTS campaign_suggestion_attempt_outcomes (
          suggestion_attempt_id TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN (
            'accepted', 'rejected', 'failed', 'abandoned_unknown'
          )),
          payload TEXT NOT NULL,
          output_digest TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(suggestion_attempt_id) REFERENCES campaign_suggestion_attempts(id)
        )""",
        """CREATE TABLE IF NOT EXISTS campaign_suggestion_attempt_late_results (
          suggestion_attempt_id TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          execution_token_digest TEXT NOT NULL,
          start_digest TEXT NOT NULL,
          payload TEXT NOT NULL,
          output_digest TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(suggestion_attempt_id) REFERENCES campaign_suggestion_attempts(id)
        )""",
    )
    for statement in statements:
        con.execute(statement)


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _validate_json(value: Any, label: str, limit: int) -> tuple[Any, str]:
    """Require JSON-native data and enforce a byte bound before persistence."""

    def visit(item: Any) -> None:
        if item is None or isinstance(item, (str, bool)):
            return
        if isinstance(item, int) and not isinstance(item, bool):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{label} must contain only finite numbers")
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError(f"{label} object keys must be strings")
            for child in item.values():
                visit(child)
            return
        raise ValueError(f"{label} must contain strict JSON values")

    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    visit(value)
    try:
        serialized = canonical(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be canonical finite JSON") from error
    if len(serialized.encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds its {limit}-byte limit")
    return value, serialized


def _request_and_key(owner: str, key: str, request: Any) -> tuple[dict[str, Any], str, str]:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("Owner must be nonempty text")
    if not isinstance(key, str) or not 1 <= len(key) <= 100 or not key.strip():
        raise ValueError("A nonempty idempotency key of at most 100 characters is required")
    request, serialized = _validate_json(request, "Suggestion request", MAX_REQUEST_BYTES)
    if set(request) != {"operation", "campaign_id", "expected_revision"}:
        raise ValueError("Suggestion request must contain exactly the campaign operation fields")
    if request.get("operation") != "suggest_campaign":
        raise ValueError("Suggestion request operation must be suggest_campaign")
    if not isinstance(request.get("campaign_id"), str) or not request["campaign_id"].strip():
        raise ValueError("Suggestion request campaign_id must be nonempty text")
    _positive_integer(request.get("expected_revision"), "expected_revision")
    return request, sha256(request), serialized


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validate_context(
    context: Any, *, campaign_id: str, expected_revision: int
) -> tuple[dict[str, Any], str]:
    context, serialized = _validate_json(context, "Suggestion context", MAX_CONTEXT_BYTES)
    campaign = context.get("campaign")
    if not isinstance(campaign, dict):
        raise ValueError("Suggestion context requires a campaign identity object")
    if (
        campaign.get("campaign_id") != campaign_id
        or isinstance(campaign.get("ordinal"), bool)
        or not isinstance(campaign.get("ordinal"), int)
        or campaign["ordinal"] < 0
        or campaign.get("expected_revision") != expected_revision
        or isinstance(campaign.get("expected_revision"), bool)
    ):
        raise ValueError("Suggestion context campaign identity does not match its request")
    return context, sha256(context)


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row(con: sqlite3.Connection, owner: str, suggestion_attempt_id: str) -> sqlite3.Row:
    if not _table_exists(con, "campaign_suggestion_attempts"):
        raise ValueError("Suggestion attempt was not found or is inaccessible")
    row = con.execute(
        "SELECT * FROM campaign_suggestion_attempts WHERE id=? AND owner=?",
        (suggestion_attempt_id, owner),
    ).fetchone()
    if row is None:
        raise ValueError("Suggestion attempt was not found or is inaccessible")
    return row


def _parse_object(serialized: Any, label: str, limit: int) -> dict[str, Any]:
    try:
        value = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Stored {label} is malformed") from error
    _, canonical_value = _validate_json(value, f"Stored {label}", limit)
    if serialized != canonical_value:
        raise ValueError(f"Stored {label} is not canonically encoded")
    return value


def _start_content(start: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in start.items() if key != "start_sha256"}


def _verify_start(row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any]]:
    start = _parse_object(row["payload"], "suggestion-attempt start", MAX_PUBLIC_BYTES)
    expected_fields = {
        "schema_version",
        "profile",
        "suggestion_attempt_id",
        "owner_sha256",
        "campaign_id",
        "ordinal",
        "expected_revision",
        "request",
        "request_sha256",
        "request_key_sha256",
        "execution_token_sha256",
        "context",
        "context_sha256",
        "started_at",
        "start_sha256",
    }
    if (
        set(start) != expected_fields
        or start.get("schema_version") != SCHEMA_VERSION
        or start.get("profile") != PROFILE
        or start.get("suggestion_attempt_id") != row["id"]
        or start.get("owner_sha256") != _key_digest(row["owner"])
        or start.get("campaign_id") != row["campaign_id"]
        or isinstance(start.get("ordinal"), bool)
        or not isinstance(start.get("ordinal"), int)
        or start.get("ordinal") != row["ordinal"]
        or isinstance(start.get("expected_revision"), bool)
        or not isinstance(start.get("expected_revision"), int)
        or start.get("expected_revision") != row["expected_revision"]
        or start.get("started_at") != row["created_at"]
        or start.get("request_sha256") != row["input_digest"]
        or start.get("context_sha256") != row["context_digest"]
        or start.get("request_key_sha256") != _key_digest(row["request_key"])
        or start.get("execution_token_sha256") != _token_digest(row["execution_token"])
        or sha256(start.get("request")) != row["input_digest"]
        or sha256(start.get("context")) != row["context_digest"]
        or start.get("start_sha256") != sha256(_start_content(start))
        or row["record_digest"] != sha256(start)
    ):
        raise ValueError("Stored suggestion-attempt start identity or digest is inconsistent")
    request = start.get("request")
    context = start.get("context")
    if not isinstance(request, dict) or not isinstance(context, dict):
        raise ValueError("Stored suggestion-attempt request or context is malformed")
    _, request_digest, _ = _request_and_key(row["owner"], row["request_key"], request)
    _, context_digest = _validate_context(
        context,
        campaign_id=row["campaign_id"],
        expected_revision=row["expected_revision"],
    )
    if (
        request_digest != row["input_digest"]
        or context_digest != row["context_digest"]
        or request.get("operation") != "suggest_campaign"
        or request.get("campaign_id") != row["campaign_id"]
        or request.get("expected_revision") != row["expected_revision"]
        or context["campaign"].get("ordinal") != row["ordinal"]
    ):
        raise ValueError("Stored suggestion-attempt identity differs from its frozen context")
    return start, context


def _outcome_row(con: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row | None:
    if not _table_exists(con, "campaign_suggestion_attempt_outcomes"):
        return None
    return con.execute(
        "SELECT * FROM campaign_suggestion_attempt_outcomes WHERE suggestion_attempt_id=? AND owner=?",
        (row["id"], row["owner"]),
    ).fetchone()


def _validate_outcome_record(
    con: sqlite3.Connection,
    attempt_row: sqlite3.Row,
    outcome_row: sqlite3.Row,
    start: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    payload = _parse_object(outcome_row["payload"], "suggestion-attempt outcome", MAX_OUTCOME_BYTES)
    expected_fields = {
        "suggestion_attempt_id",
        "start_sha256",
        "outcome",
        "recorded_at",
        "outcome_sha256",
    }
    content = {key: value for key, value in payload.items() if key != "outcome_sha256"}
    digest = sha256(content)
    outcome = payload.get("outcome")
    if (
        set(payload) != expected_fields
        or not isinstance(outcome, dict)
        or outcome_row["status"] not in _TERMINAL_STATUSES
        or outcome_row["status"] != attempt_row["status"]
        or payload.get("start_sha256") != start["start_sha256"]
        or payload.get("suggestion_attempt_id") != attempt_row["id"]
        or payload.get("recorded_at") != outcome_row["created_at"]
        or payload.get("outcome_sha256") != outcome_row["output_digest"]
        or digest != outcome_row["output_digest"]
        or outcome.get("status") != outcome_row["status"]
    ):
        raise ValueError("Stored suggestion-attempt outcome identity or digest is inconsistent")
    _validate_json(outcome, "Stored suggestion-attempt outcome", MAX_OUTCOME_BYTES)
    _validate_stored_outcome(outcome)
    if outcome_row["status"] == "accepted":
        _verify_accepted_binding(con, attempt_row, outcome)
    else:
        receipt = con.execute(
            "SELECT 1 FROM registry_receipts WHERE owner=? AND request_key=?",
            (attempt_row["owner"], attempt_row["request_key"]),
        ).fetchone()
        if receipt is not None:
            raise ValueError("Nonaccepted suggestion attempt unexpectedly has a success receipt")
    return outcome, outcome_row["output_digest"]


def _validate_stored_outcome(outcome: dict[str, Any]) -> None:
    required = {
        "status",
        "phase",
        "candidate",
        "backend",
        "candidate_generation_elapsed_seconds",
        "failure",
        "proposal_id",
    }
    if not required <= set(outcome):
        raise ValueError("Stored suggestion-attempt outcome is incomplete")
    status = outcome.get("status")
    if status not in _TERMINAL_STATUSES:
        raise ValueError("Stored suggestion-attempt outcome has an unsupported status")
    phase = outcome.get("phase")
    if not isinstance(phase, str) or not phase.strip() or len(phase) > 128:
        raise ValueError("Stored suggestion-attempt phase is malformed")
    candidate, backend = outcome.get("candidate"), outcome.get("backend")
    if candidate is not None and not isinstance(candidate, dict):
        raise ValueError("Stored suggestion-attempt candidate is malformed")
    if backend is not None and not isinstance(backend, dict):
        raise ValueError("Stored suggestion-attempt backend is malformed")
    elapsed = outcome.get("candidate_generation_elapsed_seconds")
    if elapsed is not None and (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise ValueError("Stored candidate-generation elapsed time is malformed")
    failure = outcome.get("failure")
    if failure is not None and (
        not isinstance(failure, dict)
        or set(failure) != {"type", "message"}
        or not isinstance(failure.get("type"), str)
        or not failure["type"].strip()
        or len(failure["type"]) > 128
        or not isinstance(failure.get("message"), str)
        or not failure["message"].strip()
        or len(failure["message"]) > 1_000
    ):
        raise ValueError("Stored suggestion-attempt failure details are malformed")
    proposal_id = outcome.get("proposal_id")
    if proposal_id is not None and (not isinstance(proposal_id, str) or not proposal_id.strip()):
        raise ValueError("Stored suggestion-attempt proposal link is malformed")
    if status == "accepted":
        response_digest = outcome.get("response_sha256")
        if (
            not isinstance(response_digest, str)
            or len(response_digest) != 64
            or any(character not in "0123456789abcdef" for character in response_digest)
            or not isinstance(candidate, dict)
            or proposal_id is None
            or failure is not None
        ):
            raise ValueError("Stored accepted suggestion outcome is incomplete")
    elif "response_sha256" in outcome or proposal_id is not None:
        raise ValueError("Nonaccepted suggestion outcome contains a success proposal link")
    if status == "abandoned_unknown" and (
        candidate is not None
        or backend is not None
        or elapsed is not None
        or failure is None
        or failure.get("type") != "abandoned_unknown"
        or not isinstance(outcome.get("reason"), str)
        or not outcome["reason"].strip()
        or len(outcome["reason"]) > 500
        or outcome["reason"] != failure.get("message")
    ):
        raise ValueError("Stored abandoned suggestion outcome is malformed")


def _verify_accepted_binding(
    con: sqlite3.Connection, attempt_row: sqlite3.Row, outcome: dict[str, Any]
) -> None:
    proposal_id = outcome.get("proposal_id")
    proposal = con.execute(
        "SELECT * FROM campaign_proposals WHERE id=? AND owner=?",
        (proposal_id, attempt_row["owner"]),
    ).fetchone()
    if (
        not isinstance(proposal_id, str)
        or proposal is None
        or proposal["campaign_id"] != attempt_row["campaign_id"]
        or proposal["ordinal"] != attempt_row["ordinal"]
    ):
        raise ValueError("Accepted suggestion outcome is not bound to its owned proposal")
    proposal_payload = _parse_object(proposal["payload"], "campaign proposal", MAX_OUTCOME_BYTES)
    candidate = outcome.get("candidate")
    if (
        not isinstance(candidate, dict)
        or proposal_payload.get("candidate") != candidate
        or sha256(candidate) != proposal["candidate_digest"]
    ):
        raise ValueError("Accepted suggestion candidate differs from its stored proposal")
    if proposal["state"] not in {"pending", "consumed"}:
        raise ValueError("Accepted suggestion proposal has an unsupported state")
    receipt = con.execute(
        "SELECT digest, response FROM registry_receipts WHERE owner=? AND request_key=?",
        (attempt_row["owner"], attempt_row["request_key"]),
    ).fetchone()
    if receipt is None or receipt["digest"] != attempt_row["input_digest"]:
        raise ValueError("Accepted suggestion attempt is missing its successful retry receipt")
    response = _parse_object(receipt["response"], "suggestion success receipt", MAX_REQUEST_BYTES)
    if sha256(response) != outcome.get("response_sha256"):
        raise ValueError("Accepted suggestion receipt does not match its outcome digest")
    response_proposal = response.get("proposal")
    if (
        not isinstance(response_proposal, dict)
        or response_proposal.get("proposal_id") != proposal_id
        or response_proposal.get("candidate") != candidate
    ):
        raise ValueError("Accepted suggestion receipt is not bound to its proposal")


def _late_result_row(con: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row | None:
    if not _table_exists(con, "campaign_suggestion_attempt_late_results"):
        return None
    return con.execute(
        """SELECT * FROM campaign_suggestion_attempt_late_results
        WHERE suggestion_attempt_id=?""",
        (row["id"],),
    ).fetchone()


def _validate_late_result(
    row: sqlite3.Row, start: dict[str, Any], late_row: sqlite3.Row
) -> tuple[dict[str, Any], str]:
    payload = _parse_object(late_row["payload"], "late suggestion result", MAX_OUTCOME_BYTES)
    expected_fields = {
        "suggestion_attempt_id",
        "start_sha256",
        "result",
        "recorded_at",
        "late_result_sha256",
    }
    content = {key: value for key, value in payload.items() if key != "late_result_sha256"}
    digest = sha256(content)
    if (
        set(payload) != expected_fields
        or row["status"] != "abandoned_unknown"
        or late_row["owner"] != row["owner"]
        or payload.get("suggestion_attempt_id") != row["id"]
        or payload.get("recorded_at") != late_row["created_at"]
        or late_row["execution_token_digest"] != _token_digest(row["execution_token"])
        or late_row["start_digest"] != start["start_sha256"]
        or payload.get("start_sha256") != start["start_sha256"]
        or payload.get("late_result_sha256") != late_row["output_digest"]
        or digest != late_row["output_digest"]
        or not isinstance(payload.get("result"), dict)
    ):
        raise ValueError("Stored late suggestion evidence identity or digest is inconsistent")
    _validate_json(payload["result"], "Stored late suggestion result", MAX_OUTCOME_BYTES)
    return payload, late_row["output_digest"]


def _public_view(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    if row["status"] not in _ALL_STATUSES:
        raise ValueError("Stored suggestion attempt has an unsupported state")
    start, _ = _verify_start(row)
    outcome_row = _outcome_row(con, row)
    if row["status"] == "claimed":
        if outcome_row is not None:
            raise ValueError("Claimed suggestion attempt unexpectedly has a terminal outcome")
        if _receipt(con, row) is not None:
            raise ValueError("Claimed suggestion attempt unexpectedly has a success receipt")
        outcome = None
        outcome_digest = None
    else:
        if outcome_row is None:
            raise ValueError("Terminal suggestion attempt is missing its immutable outcome")
        outcome, outcome_digest = _validate_outcome_record(con, row, outcome_row, start)
    late_row = _late_result_row(con, row)
    if row["status"] != "abandoned_unknown" and late_row is not None:
        raise ValueError("Late suggestion evidence must follow an abandoned attempt")
    if late_row is None:
        late_result = None
        late_digest = None
    else:
        late_result, late_digest = _validate_late_result(row, start, late_row)
    view = {
        "schema_version": SCHEMA_VERSION,
        "profile": PROFILE,
        "suggestion_attempt_id": row["id"],
        "campaign_id": row["campaign_id"],
        "ordinal": row["ordinal"],
        "expected_revision": row["expected_revision"],
        "state": row["status"],
        "request": start["request"],
        "request_sha256": row["input_digest"],
        "request_key_sha256": start["request_key_sha256"],
        "context": start["context"],
        "context_sha256": row["context_digest"],
        "started_at": row["created_at"],
        "start_sha256": start["start_sha256"],
        "outcome": outcome,
        "outcome_sha256": outcome_digest,
        "late_result": late_result,
        "late_result_sha256": late_digest,
    }
    serialized = canonical(view)
    if len(serialized.encode("utf-8")) > MAX_PUBLIC_BYTES:
        raise ValueError("Public suggestion-attempt record exceeds its 256,000-byte limit")
    return view


def lookup(
    con: sqlite3.Connection, owner: str, key: str, request: dict[str, Any]
) -> dict[str, Any] | None:
    """Find an attempt reservation first, then a legacy successful receipt."""
    request, request_digest, _ = _request_and_key(owner, key, request)
    if _table_exists(con, "campaign_suggestion_attempts"):
        row = con.execute(
            "SELECT * FROM campaign_suggestion_attempts WHERE owner=? AND request_key=?",
            (owner, key),
        ).fetchone()
        if row is not None:
            record = _public_view(con, row)
            if row["input_digest"] != request_digest or record["request"] != request:
                raise ValueError(
                    "Idempotency key was already reserved for a different suggestion request"
                )
            return {
                "kind": "attempt",
                "record": record,
                "execution_token": row["execution_token"],
            }
    if _table_exists(con, "registry_receipts"):
        receipt = con.execute(
            "SELECT digest, response FROM registry_receipts WHERE owner=? AND request_key=?",
            (owner, key),
        ).fetchone()
        if receipt is not None:
            if receipt["digest"] != request_digest:
                raise ValueError(
                    "Idempotency key was already used for a different registry request"
                )
            response = _parse_object(
                receipt["response"], "legacy suggestion receipt", MAX_REQUEST_BYTES
            )
            return {"kind": "legacy", "response": response}
    return None


def claim(
    con: sqlite3.Connection,
    owner: str,
    key: str,
    request: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Reserve an owner/key and one active campaign slot without committing."""
    request, request_digest, _ = _request_and_key(owner, key, request)
    campaign_id = request["campaign_id"]
    expected_revision = request["expected_revision"]
    context, context_digest = _validate_context(
        context,
        campaign_id=campaign_id,
        expected_revision=expected_revision,
    )
    context_campaign = context["campaign"]
    ordinal = context_campaign["ordinal"]
    campaign = con.execute("SELECT owner FROM campaign_runs WHERE id=?", (campaign_id,)).fetchone()
    if campaign is None or campaign["owner"] != owner:
        raise ValueError("Campaign was not found or is not accessible")

    existing = lookup(con, owner, key, request)
    if existing is not None:
        if existing["kind"] == "attempt":
            if existing["record"]["context_sha256"] != context_digest:
                raise ValueError("Idempotency key was already reserved for a different context")
            return {key: value for key, value in existing.items() if key != "kind"}
        raise ValueError("Idempotency key already has a legacy registry receipt")

    ensure_schema(con)
    active_row = con.execute(
        "SELECT id FROM campaign_suggestion_attempts WHERE owner=? AND campaign_id=? AND status='claimed'",
        (owner, campaign_id),
    ).fetchone()
    if active_row is not None:
        raise ValueError("Campaign already has an active suggestion attempt")

    attempt_id = str(uuid.uuid4())
    execution_token = str(uuid.uuid4())
    started_at = now()
    start_without_hash = {
        "schema_version": SCHEMA_VERSION,
        "profile": PROFILE,
        "suggestion_attempt_id": attempt_id,
        "owner_sha256": _key_digest(owner),
        "campaign_id": campaign_id,
        "ordinal": ordinal,
        "expected_revision": expected_revision,
        "request": request,
        "request_sha256": request_digest,
        "request_key_sha256": _key_digest(key),
        "execution_token_sha256": _token_digest(execution_token),
        "context": context,
        "context_sha256": context_digest,
        "started_at": started_at,
    }
    start = {**start_without_hash, "start_sha256": sha256(start_without_hash)}
    _, serialized_start = _validate_json(start, "Suggestion-attempt start", MAX_PUBLIC_BYTES)
    record_digest = sha256(start)
    try:
        con.execute(
            """INSERT INTO campaign_suggestion_attempts (
              id, owner, request_key, campaign_id, ordinal, expected_revision,
              execution_token, status, payload, input_digest, context_digest,
              record_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?, ?)""",
            (
                attempt_id,
                owner,
                key,
                campaign_id,
                ordinal,
                expected_revision,
                execution_token,
                serialized_start,
                request_digest,
                context_digest,
                record_digest,
                started_at,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise ValueError(
            "Campaign suggestion claim conflicts with an existing reservation"
        ) from error
    row = _row(con, owner, attempt_id)
    return {"record": _public_view(con, row), "execution_token": execution_token}


def get(con: sqlite3.Connection, owner: str, suggestion_attempt_id: str) -> dict[str, Any]:
    """Return one verified public attempt record without creating storage tables."""
    return _public_view(con, _row(con, owner, suggestion_attempt_id))


def active(con: sqlite3.Connection, owner: str, campaign_id: str) -> dict[str, Any] | None:
    """Return the one active claim for an owned campaign, if the ledger exists."""
    if not _table_exists(con, "campaign_suggestion_attempts"):
        return None
    rows = con.execute(
        "SELECT * FROM campaign_suggestion_attempts WHERE owner=? AND campaign_id=? AND status='claimed' ORDER BY created_at, id LIMIT 2",
        (owner, campaign_id),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("Campaign has multiple active suggestion attempts")
    return None if not rows else _public_view(con, rows[0])


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    outcome = record["outcome"]
    return {
        "suggestion_attempt_id": record["suggestion_attempt_id"],
        "campaign_id": record["campaign_id"],
        "ordinal": record["ordinal"],
        "expected_revision": record["expected_revision"],
        "state": record["state"],
        "request_sha256": record["request_sha256"],
        "request_key_sha256": record["request_key_sha256"],
        "context_sha256": record["context_sha256"],
        "started_at": record["started_at"],
        "start_sha256": record["start_sha256"],
        "outcome_sha256": record["outcome_sha256"],
        "late_result_sha256": record["late_result_sha256"],
        "proposal_id": None if outcome is None else outcome.get("proposal_id"),
    }


def list_records(
    con: sqlite3.Connection,
    owner: str,
    campaign_id: str,
    limit: int = 10,
    after: str | None = None,
) -> dict[str, Any]:
    """Page bounded attempt summaries in deterministic creation order."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("Suggestion-attempt page limit must be an integer from 1 through 20")
    if after is not None and (not isinstance(after, str) or not after.strip()):
        raise ValueError("Suggestion-attempt cursor must be a nonempty owned record ID")
    if not _table_exists(con, "campaign_suggestion_attempts"):
        return {"records": [], "next_after": None}
    cursor = None
    if after is not None:
        cursor = con.execute(
            "SELECT created_at, campaign_id FROM campaign_suggestion_attempts WHERE id=? AND owner=?",
            (after, owner),
        ).fetchone()
        if cursor is None or cursor["campaign_id"] != campaign_id:
            raise ValueError("Suggestion-attempt cursor is not accessible in this campaign")
    if cursor is None:
        rows = con.execute(
            """SELECT * FROM campaign_suggestion_attempts WHERE owner=? AND campaign_id=?
            ORDER BY created_at, id LIMIT ?""",
            (owner, campaign_id, limit + 1),
        ).fetchall()
    else:
        rows = con.execute(
            """SELECT * FROM campaign_suggestion_attempts WHERE owner=? AND campaign_id=?
            AND (created_at > ? OR (created_at=? AND id>?))
            ORDER BY created_at, id LIMIT ?""",
            (owner, campaign_id, cursor["created_at"], cursor["created_at"], after, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    summaries = [_summary(_public_view(con, row)) for row in page_rows]
    for item in summaries:
        if len(canonical(item).encode("utf-8")) > MAX_LIST_SUMMARY_BYTES:
            raise ValueError("Suggestion-attempt list summary exceeds its byte limit")
    return {
        "records": summaries,
        "next_after": page_rows[-1]["id"] if has_more and page_rows else None,
    }


def _token_matches(row: sqlite3.Row, token: Any) -> bool:
    return (
        isinstance(token, str)
        and bool(token)
        and hmac.compare_digest(row["execution_token"], token)
    )


def _outcome_input(outcome: Any) -> dict[str, Any]:
    outcome, _ = _validate_json(outcome, "Suggestion-attempt outcome", MAX_OUTCOME_BYTES)
    required = {
        "status",
        "phase",
        "candidate",
        "backend",
        "candidate_generation_elapsed_seconds",
        "failure",
        "proposal_id",
    }
    if not required <= set(outcome):
        raise ValueError("Suggestion-attempt outcome is missing required fields")
    if outcome.get("status") not in {"accepted", "rejected", "failed"}:
        raise ValueError("Suggestion-attempt outcome status must be accepted, rejected or failed")
    if set(outcome) & {"response_sha256", "start_sha256"}:
        raise ValueError("Suggestion-attempt outcome cannot supply ledger-owned digest fields")
    phase = outcome.get("phase")
    if not isinstance(phase, str) or not phase.strip() or len(phase) > 128:
        raise ValueError("Suggestion-attempt phase must be nonempty text of at most 128 characters")
    candidate = outcome.get("candidate")
    backend = outcome.get("backend")
    if candidate is not None and not isinstance(candidate, dict):
        raise ValueError("Suggestion-attempt candidate must be an object or null")
    if backend is not None and not isinstance(backend, dict):
        raise ValueError("Suggestion-attempt backend must be an object or null")
    elapsed = outcome.get("candidate_generation_elapsed_seconds")
    if elapsed is not None and (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise ValueError("Candidate-generation elapsed time must be finite and nonnegative or null")
    failure = outcome.get("failure")
    if failure is not None:
        if (
            not isinstance(failure, dict)
            or set(failure) != {"type", "message"}
            or not isinstance(failure.get("type"), str)
            or not failure["type"].strip()
            or len(failure["type"]) > 128
            or not isinstance(failure.get("message"), str)
            or not failure["message"].strip()
            or len(failure["message"]) > 1_000
        ):
            raise ValueError("Failure details must contain bounded type and message text")
    proposal_id = outcome.get("proposal_id")
    if proposal_id is not None and (not isinstance(proposal_id, str) or not proposal_id.strip()):
        raise ValueError("Suggestion-attempt proposal_id must be nonempty text or null")
    return dict(outcome)


def _accepted_response(
    con: sqlite3.Connection,
    row: sqlite3.Row,
    outcome: dict[str, Any],
    successful_response: Any,
) -> tuple[dict[str, Any], str]:
    if not isinstance(successful_response, dict):
        raise ValueError("Accepted suggestion requires a successful response object")
    response, _ = _validate_json(
        successful_response, "Successful suggestion response", MAX_REQUEST_BYTES
    )
    if outcome.get("failure") is not None or outcome.get("proposal_id") is None:
        raise ValueError("Accepted suggestion requires a proposal link and no failure")
    if not isinstance(outcome.get("candidate"), dict):
        raise ValueError("Accepted suggestion requires its generated candidate")
    response_proposal = response.get("proposal")
    if (
        not isinstance(response_proposal, dict)
        or response_proposal.get("proposal_id") != outcome["proposal_id"]
        or response_proposal.get("candidate") != outcome["candidate"]
    ):
        raise ValueError("Successful response proposal link differs from the accepted outcome")
    proposal = con.execute(
        "SELECT * FROM campaign_proposals WHERE id=? AND owner=?",
        (outcome["proposal_id"], row["owner"]),
    ).fetchone()
    if (
        proposal is None
        or proposal["campaign_id"] != row["campaign_id"]
        or proposal["ordinal"] != row["ordinal"]
        or proposal["state"] not in {"pending", "consumed"}
        or proposal["candidate_digest"] != sha256(outcome["candidate"])
    ):
        raise ValueError("Accepted suggestion must bind its owned pending campaign proposal")
    return response, sha256(response)


def _store_outcome(
    con: sqlite3.Connection,
    row: sqlite3.Row,
    outcome: dict[str, Any],
) -> sqlite3.Row:
    start, _ = _verify_start(row)
    occurred_at = now()
    content = {
        "suggestion_attempt_id": row["id"],
        "start_sha256": start["start_sha256"],
        "outcome": outcome,
        "recorded_at": occurred_at,
    }
    outcome_digest = sha256(content)
    payload = {**content, "outcome_sha256": outcome_digest}
    _, serialized = _validate_json(payload, "Suggestion-attempt outcome record", MAX_OUTCOME_BYTES)
    con.execute(
        """INSERT INTO campaign_suggestion_attempt_outcomes
        (suggestion_attempt_id, owner, status, payload, output_digest, created_at)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (row["id"], row["owner"], outcome["status"], serialized, outcome_digest, occurred_at),
    )
    updated = con.execute(
        "UPDATE campaign_suggestion_attempts SET status=? WHERE id=? AND owner=? AND status='claimed'",
        (outcome["status"], row["id"], row["owner"]),
    )
    if updated.rowcount != 1:
        raise ValueError("Suggestion attempt is no longer actively claimed")
    return _row(con, row["owner"], row["id"])


def _receipt(con: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row | None:
    return con.execute(
        "SELECT digest, response FROM registry_receipts WHERE owner=? AND request_key=?",
        (row["owner"], row["request_key"]),
    ).fetchone()


def finalize(
    con: sqlite3.Connection,
    owner: str,
    suggestion_attempt_id: str,
    token: str,
    outcome: dict[str, Any],
    successful_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze an accepted, rejected or failed computation in the caller's transaction."""
    row = _row(con, owner, suggestion_attempt_id)
    if not _token_matches(row, token):
        raise ValueError("Suggestion attempt requires its active execution token")
    if row["status"] == "abandoned_unknown":
        raise ValueError("Abandoned suggestion attempts can accept evidence only as a late result")
    start, _ = _verify_start(row)
    normalized = _outcome_input(outcome)
    if normalized["status"] == "accepted":
        response, response_digest = _accepted_response(con, row, normalized, successful_response)
        normalized["response_sha256"] = response_digest
    elif successful_response is not None or normalized.get("proposal_id") is not None:
        raise ValueError("Rejected or failed suggestion cannot store a success receipt or proposal")
    else:
        response = None

    if row["status"] != "claimed":
        terminal_row = _outcome_row(con, row)
        if terminal_row is None:
            raise ValueError("Terminal suggestion attempt is missing its immutable outcome")
        prior, _ = _validate_outcome_record(con, row, terminal_row, start)
        if prior != normalized:
            raise ValueError("Suggestion attempt already has a different terminal outcome")
        return _public_view(con, row)

    if _outcome_row(con, row) is not None:
        raise ValueError("Claimed suggestion attempt unexpectedly has a terminal outcome")
    receipt = _receipt(con, row)
    if normalized["status"] == "accepted":
        if receipt is not None and (
            receipt["digest"] != row["input_digest"] or receipt["response"] != canonical(response)
        ):
            raise ValueError("Suggestion request key already has a different success receipt")
    elif receipt is not None:
        raise ValueError("Rejected or failed suggestion request key already has a success receipt")
    _store_outcome(con, row, normalized)
    if normalized["status"] == "accepted" and receipt is None:
        con.execute(
            "INSERT INTO registry_receipts VALUES (?, ?, ?, ?, ?)",
            (owner, row["request_key"], row["input_digest"], canonical(response), now()),
        )
    return _public_view(con, _row(con, owner, suggestion_attempt_id))


def abandon(
    con: sqlite3.Connection,
    owner: str,
    suggestion_attempt_id: str,
    expected_start_sha256: str,
    reason: str,
) -> dict[str, Any]:
    """Fence a claimed computation as unknown without asserting failure or success."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise ValueError("Abandonment reason must be nonempty text of at most 500 characters")
    row = _row(con, owner, suggestion_attempt_id)
    start, _ = _verify_start(row)
    if expected_start_sha256 != start["start_sha256"]:
        raise ValueError("Suggestion-attempt start digest does not match")
    if row["status"] != "claimed":
        raise ValueError("Only a claimed suggestion attempt can be abandoned")
    outcome = {
        "status": "abandoned_unknown",
        "phase": "abandon",
        "candidate": None,
        "backend": None,
        "candidate_generation_elapsed_seconds": None,
        "failure": {"type": "abandoned_unknown", "message": reason},
        "proposal_id": None,
        "reason": reason,
    }
    _validate_json(outcome, "Abandonment outcome", MAX_OUTCOME_BYTES)
    if _outcome_row(con, row) is not None or _receipt(con, row) is not None:
        raise ValueError("Claimed suggestion attempt has conflicting terminal evidence")
    _store_outcome(con, row, outcome)
    return _public_view(con, _row(con, owner, suggestion_attempt_id))


def record_late_result(
    con: sqlite3.Connection,
    owner: str,
    suggestion_attempt_id: str,
    token: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Retain one immutable late computation event after abandonment fencing."""
    row = _row(con, owner, suggestion_attempt_id)
    if not _token_matches(row, token):
        raise ValueError("Late suggestion evidence requires its original execution token")
    view = _public_view(con, row)
    if view["state"] != "abandoned_unknown":
        raise ValueError("Late suggestion result is allowed only after abandoned_unknown")
    result, _ = _validate_json(result, "Late suggestion result", MAX_OUTCOME_BYTES)
    existing = _late_result_row(con, row)
    token_digest = _token_digest(row["execution_token"])
    if existing is not None:
        payload, _ = _validate_late_result(row, _verify_start(row)[0], existing)
        if payload.get("result") != result or existing["execution_token_digest"] != token_digest:
            raise ValueError("Suggestion attempt already has a different late result")
        return view
    start, _ = _verify_start(row)
    occurred_at = now()
    content = {
        "suggestion_attempt_id": row["id"],
        "start_sha256": start["start_sha256"],
        "result": result,
        "recorded_at": occurred_at,
    }
    late_digest = sha256(content)
    payload = {**content, "late_result_sha256": late_digest}
    _, serialized = _validate_json(payload, "Late suggestion result record", MAX_OUTCOME_BYTES)
    con.execute(
        """INSERT INTO campaign_suggestion_attempt_late_results
        (suggestion_attempt_id, owner, execution_token_digest, start_digest,
         payload, output_digest, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            row["id"],
            owner,
            token_digest,
            start["start_sha256"],
            serialized,
            late_digest,
            occurred_at,
        ),
    )
    return _public_view(con, _row(con, owner, suggestion_attempt_id))
