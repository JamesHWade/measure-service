"""Local SQLite storage shared by analytical records and the experiment registry."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .batch_schema import ensure_batch_schema, validate_batch_schema
from .campaign_schema import ensure_proposal_repetition_schema


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path(*, create: bool = True) -> Path:
    """Return the configured database path, optionally without creating its directory."""
    root = Path(
        os.environ.get(
            "MEASURE_MCP_DATA_DIR", str(Path.home() / ".local" / "share" / "measure-mcp")
        )
    )
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / "measure.sqlite3"


def connect() -> sqlite3.Connection:
    path = db_path()
    if os.path.lexists(path.parent / "INCOMPLETE"):
        raise ValueError("Store contains an incomplete analytical import; inspect it before use")
    return _connect_at(path)


def _connect_at(path: Path) -> sqlite3.Connection:
    """Initialize a caller-selected local database without changing process configuration."""
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        from .design_lineage_schema import validate_design_lineage_schema
        from .import_origins import validate_import_schema
        from .result_table_schema import validate_result_table_schema

        # Optional imported evidence must fail closed before any unrelated DDL.
        # Validation observes an absent component without creating it.
        validate_import_schema(con)
        validate_result_table_schema(con)
        validate_design_lineage_schema(con)
        batch_schema_exists = validate_batch_schema(con)
    except BaseException:
        con.close()
        raise
    con.executescript("""
    CREATE TABLE IF NOT EXISTS datasets (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, digest TEXT NOT NULL,
      payload TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS analyses (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, dataset_id TEXT NOT NULL,
      trace_id TEXT NOT NULL, kind TEXT NOT NULL, method TEXT NOT NULL,
      result TEXT NOT NULL, provenance TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(dataset_id) REFERENCES datasets(id)
    );
    CREATE TABLE IF NOT EXISTS dataset_sources (
      dataset_id TEXT PRIMARY KEY, kind TEXT NOT NULL, digest TEXT NOT NULL,
      content TEXT NOT NULL, mapping TEXT NOT NULL,
      FOREIGN KEY(dataset_id) REFERENCES datasets(id)
    );
    CREATE TABLE IF NOT EXISTS executable_methods (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
      payload TEXT NOT NULL, input_digest TEXT NOT NULL,
      program_digest TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS methods_owner_order
      ON executable_methods(owner, created_at, id);
    CREATE TABLE IF NOT EXISTS experiments (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL,
      digest TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS experiments_owner_order ON experiments(owner, created_at, id);
    CREATE TABLE IF NOT EXISTS experiment_attempts (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, experiment_id TEXT NOT NULL,
      latest_revision INTEGER NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(experiment_id) REFERENCES experiments(id)
    );
    CREATE INDEX IF NOT EXISTS attempts_owner_experiment
      ON experiment_attempts(owner, experiment_id, created_at, id);
    CREATE TABLE IF NOT EXISTS attempt_revisions (
      attempt_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
      digest TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(attempt_id, revision),
      FOREIGN KEY(attempt_id) REFERENCES experiment_attempts(id)
    );
    CREATE TABLE IF NOT EXISTS experiment_results (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, attempt_id TEXT NOT NULL,
      attempt_revision INTEGER NOT NULL, metric TEXT NOT NULL,
      analyte_reference TEXT, supersedes_id TEXT UNIQUE,
      payload TEXT NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(attempt_id, attempt_revision) REFERENCES attempt_revisions(attempt_id, revision),
      FOREIGN KEY(supersedes_id) REFERENCES experiment_results(id)
    );
    CREATE INDEX IF NOT EXISTS results_owner_attempt
      ON experiment_results(owner, attempt_id, metric, analyte_reference, created_at, id);
    CREATE TABLE IF NOT EXISTS registry_receipts (
      owner TEXT NOT NULL, request_key TEXT NOT NULL, digest TEXT NOT NULL,
      response TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(owner, request_key)
    );
    CREATE TABLE IF NOT EXISTS observation_contracts (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
      payload TEXT NOT NULL, input_digest TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS observation_contracts_owner_order
      ON observation_contracts(owner, created_at, id);
    CREATE TABLE IF NOT EXISTS analytical_observations (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, contract_id TEXT NOT NULL,
      contract_input_digest TEXT NOT NULL, experiment_id TEXT NOT NULL,
      attempt_id TEXT NOT NULL, attempt_revision INTEGER NOT NULL,
      executable_method_id TEXT, method_input_digest TEXT, canonical_program_digest TEXT,
      payload TEXT NOT NULL, input_digest TEXT NOT NULL, evidence_digest TEXT NOT NULL,
      assessment_digest TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(contract_id) REFERENCES observation_contracts(id),
      FOREIGN KEY(experiment_id) REFERENCES experiments(id),
      FOREIGN KEY(attempt_id, attempt_revision)
        REFERENCES attempt_revisions(attempt_id, revision),
      FOREIGN KEY(executable_method_id) REFERENCES executable_methods(id)
    );
    CREATE INDEX IF NOT EXISTS analytical_observations_owner_order
      ON analytical_observations(owner, created_at, id);
    CREATE INDEX IF NOT EXISTS analytical_observations_contract_attempt
      ON analytical_observations(owner, contract_id, attempt_id, attempt_revision);
    CREATE TABLE IF NOT EXISTS observation_snapshots (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, contract_id TEXT NOT NULL,
      contract_input_digest TEXT NOT NULL, payload TEXT NOT NULL,
      digest TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(contract_id) REFERENCES observation_contracts(id)
    );
    CREATE INDEX IF NOT EXISTS observation_snapshots_owner_order
      ON observation_snapshots(owner, created_at, id);
    CREATE TABLE IF NOT EXISTS observation_snapshot_members (
      snapshot_id TEXT NOT NULL, owner TEXT NOT NULL, ordinal INTEGER NOT NULL,
      observation_id TEXT NOT NULL, attempt_id TEXT NOT NULL, attempt_revision INTEGER NOT NULL,
      observation_input_digest TEXT NOT NULL, evidence_digest TEXT NOT NULL,
      assessment_digest TEXT NOT NULL, PRIMARY KEY(snapshot_id, ordinal),
      UNIQUE(snapshot_id, observation_id), UNIQUE(snapshot_id, attempt_id),
      FOREIGN KEY(snapshot_id) REFERENCES observation_snapshots(id),
      FOREIGN KEY(observation_id) REFERENCES analytical_observations(id)
    );
    CREATE TABLE IF NOT EXISTS retention_models (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
      payload TEXT NOT NULL, input_digest TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS retention_models_owner_order
      ON retention_models(owner, created_at, id);
    CREATE TABLE IF NOT EXISTS retention_predictions (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, retention_model_id TEXT NOT NULL,
      model_input_digest TEXT NOT NULL, method_id TEXT NOT NULL,
      method_input_digest TEXT NOT NULL, canonical_program_digest TEXT NOT NULL,
      payload TEXT NOT NULL, input_digest TEXT NOT NULL, output_digest TEXT NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY(retention_model_id) REFERENCES retention_models(id),
      FOREIGN KEY(method_id) REFERENCES executable_methods(id)
    );
    CREATE INDEX IF NOT EXISTS retention_predictions_owner_order
      ON retention_predictions(owner, created_at, id);
    CREATE TABLE IF NOT EXISTS gaussian_producers (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
      retention_model_id TEXT NOT NULL, retention_model_input_digest TEXT NOT NULL,
      payload TEXT NOT NULL, input_digest TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(retention_model_id) REFERENCES retention_models(id)
    );
    CREATE INDEX IF NOT EXISTS gaussian_producers_owner_order
      ON gaussian_producers(owner, created_at, id);
    CREATE TABLE IF NOT EXISTS gaussian_observations (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, producer_id TEXT NOT NULL,
      producer_input_digest TEXT NOT NULL, retention_prediction_id TEXT NOT NULL,
      prediction_input_digest TEXT NOT NULL, prediction_output_digest TEXT NOT NULL,
      experiment_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
      attempt_revision INTEGER NOT NULL, attempt_digest TEXT NOT NULL,
      executable_method_id TEXT NOT NULL, method_input_digest TEXT NOT NULL,
      canonical_program_digest TEXT NOT NULL, payload TEXT NOT NULL,
      input_digest TEXT NOT NULL, output_digest TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(owner, attempt_id, attempt_revision),
      FOREIGN KEY(producer_id) REFERENCES gaussian_producers(id),
      FOREIGN KEY(retention_prediction_id) REFERENCES retention_predictions(id),
      FOREIGN KEY(experiment_id) REFERENCES experiments(id),
      FOREIGN KEY(attempt_id, attempt_revision)
        REFERENCES attempt_revisions(attempt_id, revision),
      FOREIGN KEY(executable_method_id) REFERENCES executable_methods(id)
    );
    CREATE INDEX IF NOT EXISTS gaussian_observations_owner_order
      ON gaussian_observations(owner, created_at, id);
    CREATE INDEX IF NOT EXISTS gaussian_observations_owner_attempt
      ON gaussian_observations(owner, attempt_id, attempt_revision);
    CREATE TABLE IF NOT EXISTS campaign_protocols (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
      payload TEXT NOT NULL, input_digest TEXT NOT NULL,
      output_digest TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS campaign_protocols_owner_order
      ON campaign_protocols(owner, created_at, id);
    CREATE TABLE IF NOT EXISTS campaign_runs (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, protocol_id TEXT NOT NULL,
      protocol_input_digest TEXT NOT NULL, task_id TEXT NOT NULL,
      seed INTEGER NOT NULL, policy TEXT NOT NULL, payload TEXT NOT NULL,
      input_digest TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(protocol_id) REFERENCES campaign_protocols(id)
    );
    CREATE INDEX IF NOT EXISTS campaign_runs_owner_order
      ON campaign_runs(owner, created_at, id);
    CREATE TABLE IF NOT EXISTS campaign_proposals (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, campaign_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL, state TEXT NOT NULL, candidate_digest TEXT NOT NULL,
      executable_method_id TEXT NOT NULL, method_input_digest TEXT NOT NULL,
      canonical_program_digest TEXT NOT NULL, payload TEXT NOT NULL,
      input_digest TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(campaign_id, ordinal),
      FOREIGN KEY(campaign_id) REFERENCES campaign_runs(id),
      FOREIGN KEY(executable_method_id) REFERENCES executable_methods(id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS campaign_one_pending
      ON campaign_proposals(owner, campaign_id) WHERE state='pending';
    CREATE TABLE IF NOT EXISTS campaign_consumptions (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, campaign_id TEXT NOT NULL,
      proposal_id TEXT NOT NULL, observation_snapshot_id TEXT NOT NULL,
      snapshot_digest TEXT NOT NULL, observation_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
      attempt_revision INTEGER NOT NULL, observation_input_digest TEXT NOT NULL,
      evidence_digest TEXT NOT NULL, assessment_digest TEXT NOT NULL,
      payload TEXT NOT NULL, input_digest TEXT NOT NULL, output_digest TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(campaign_id, proposal_id), UNIQUE(campaign_id, attempt_id),
      UNIQUE(campaign_id, observation_snapshot_id),
      FOREIGN KEY(campaign_id) REFERENCES campaign_runs(id),
      FOREIGN KEY(proposal_id) REFERENCES campaign_proposals(id),
      FOREIGN KEY(observation_snapshot_id) REFERENCES observation_snapshots(id),
      FOREIGN KEY(observation_id) REFERENCES analytical_observations(id),
      FOREIGN KEY(attempt_id, attempt_revision) REFERENCES attempt_revisions(attempt_id, revision)
    );
    """)
    try:
        ensure_proposal_repetition_schema(con)
        if not batch_schema_exists:
            con.execute("BEGIN IMMEDIATE")
            # Another first-time caller may have initialized while we waited.
            ensure_batch_schema(con)
            con.commit()
    except BaseException:
        con.rollback()
        con.close()
        raise
    return con


def _connect_readonly_at(path: Path) -> sqlite3.Connection:
    """Open an existing store without creating, migrating, or repairing it."""
    root = path.parent
    if root.is_symlink():
        raise ValueError("Store data directory must not be a symlink")
    if not root.is_dir():
        raise _missing_store_error()
    if os.path.lexists(root / "INCOMPLETE"):
        raise ValueError("Store contains an INCOMPLETE marker; inspect or recover it before use")
    if path.is_symlink():
        raise ValueError("Store database must not be a symlink")
    if not path.is_file():
        raise _missing_store_error()

    try:
        con = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=30)
    except (OSError, sqlite3.Error):
        raise ValueError("Store database could not be opened read-only") from None

    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        from .design_lineage_schema import validate_design_lineage_schema
        from .import_origins import validate_import_schema
        from .result_table_schema import validate_result_table_schema

        # Keep optional component checks fail-closed while preserving the
        # read-only contract. These validators only inspect the opened schema.
        validate_import_schema(con)
        validate_result_table_schema(con)
        validate_design_lineage_schema(con)
        validate_batch_schema(con)

        # Reads must not trigger the proposal migration hidden in connect().
        # Reuse that component's validator and report its supported remedy.
        from .campaign_schema import _candidate_index_ready, _schema_state

        try:
            proposal_state = _schema_state(con)
            candidate_index_ready = _candidate_index_ready(con)
        except ValueError as error:
            raise ValueError(f"{_proposal_migration_error()}: {error}") from error
        if proposal_state != "current" or not candidate_index_ready:
            raise ValueError(_proposal_migration_error())
    except BaseException:
        con.close()
        raise
    return con


def _missing_store_error() -> ValueError:
    return ValueError(
        "Store is not initialized; create a new store with measure-service-store initialize "
        "--data-dir /path/to/new-store, then set MEASURE_MCP_DATA_DIR to that directory"
    )


def _proposal_migration_error() -> str:
    return (
        "The store's campaign proposal schema needs a writable migration before read-only "
        "access; run MEASURE_MCP_DATA_DIR=/path/to/store python -c "
        "'from measure_service.storage import connect; connect().close()' and retry"
    )


@contextmanager
def registry_connection(write: bool = False) -> Iterator[sqlite3.Connection]:
    """Open a read-only transaction or an explicit serialized registry write."""
    con = connect() if write else _connect_readonly_at(db_path(create=False))
    con.row_factory = sqlite3.Row
    try:
        if write:
            con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def registry_mutation(
    owner: str,
    key: str,
    request: dict,
    action: Callable[[sqlite3.Connection], dict],
    *,
    response_validator: Callable[[sqlite3.Connection, Any], None] | None = None,
) -> dict[str, Any]:
    """Commit a bounded registry mutation and its retry receipt in one transaction."""
    if not isinstance(key, str) or not 1 <= len(key) <= 100 or not key.strip():
        raise ValueError("A nonempty idempotency key of at most 100 characters is required")
    serialized = canonical(request)
    if len(serialized.encode("utf-8")) > 64_000:
        raise ValueError("Registry record or result batch exceeds the byte limit")
    digest = sha256(request)
    with registry_connection(write=True) as con:
        if response_validator is None:
            receipt = con.execute(
                "SELECT digest, response FROM registry_receipts WHERE owner=? AND request_key=?",
                (owner, key),
            ).fetchone()
        else:
            # Opt-in components reject oversized stored JSON before SQLite
            # materializes it in Python; legacy receipt reads stay unchanged.
            receipt = con.execute(
                "SELECT CASE WHEN typeof(digest)='text' AND length(CAST(digest AS BLOB))=64 "
                "THEN digest END AS digest, "
                "CASE WHEN typeof(response)='text' AND length(CAST(response AS BLOB))<=64000 "
                "THEN response END AS response FROM registry_receipts WHERE owner=? AND request_key=?",
                (owner, key),
            ).fetchone()
        if receipt is not None:
            if receipt["digest"] != digest:
                raise ValueError(
                    "Idempotency key was already used for a different registry request"
                )
            if response_validator is None:
                return json.loads(receipt["response"])
            raw = receipt["response"]
            try:
                if type(raw) is not str or len(raw) > 64_000 or len(raw.encode("utf-8")) > 64_000:
                    raise ValueError("Invalid retry receipt byte limit")
                response = json.loads(raw)
                if canonical(response) != raw:
                    raise ValueError("Retry receipt must retain canonical JSON bytes")
            except (TypeError, ValueError, RecursionError) as error:
                raise ValueError("Stored retry receipt is inconsistent") from error
            response_validator(con, response)
            return response
        # A durable suggestion claim reserves the same namespace as ordinary
        # receipts, including while its external computation is unresolved.
        claims_exist = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_suggestion_attempts'"
        ).fetchone()
        if (
            claims_exist is not None
            and con.execute(
                "SELECT 1 FROM campaign_suggestion_attempts WHERE owner=? AND request_key=?",
                (owner, key),
            ).fetchone()
            is not None
        ):
            raise ValueError("Idempotency key is reserved by a campaign suggestion attempt")
        response = action(con)
        if response_validator is not None:
            response_validator(con, response)
        con.execute(
            "INSERT INTO registry_receipts VALUES (?, ?, ?, ?, ?)",
            (owner, key, digest, canonical(response), now()),
        )
        return response
