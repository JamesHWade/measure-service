"""Run the fixed paired synthetic campaign and publish its evidence bundle.

The predeclared fixture is immutable input. The runner stores every proposal,
simulator attempt, generated result, analytical assessment, snapshot and
campaign consumption in an isolated service database before summarizing it.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from measure_methods.gaussian_observations import RESOLUTION_DEFINITION
from measure_methods.trajectories import (
    SuccessObservation,
    aggregate_success_trajectories,
    summarize_campaign_success,
)

from .. import (
    campaign_protocol,
    campaign_protocol_registry,
    campaigns,
    experiments,
    gaussian_producer,
    observations,
    retention,
)
from ..method_backend import method_backend_info
from ..storage import canonical, db_path, sha256
from .evidence import file_sha256, staged_bundle

PROFILE = "synthetic-isocratic-paired-comparison/1"
DEFAULT_DOCUMENT = Path(__file__).with_name("reference_data") / (
    "synthetic-isocratic-paired-v1.json"
)
DEFAULT_SHA256 = "0728c6b2e03132d2889fef030c57a5331cae24067ca3fc5e6c40256f1238b38e"
OWNER = "synthetic-campaign-runner"
POLICIES = ("random", "bo_mcp_gp_logei")
STRICT_SUCCESS_SERIES = "strict_analytical_accepted"
SCORE_SUCCESS_SERIES = "finite_resolution_score_ge_2"
SUCCESS_SERIES = (STRICT_SUCCESS_SERIES, SCORE_SUCCESS_SERIES)
PLANNED_CAMPAIGNS_PER_POLICY = 6
PLANNED_CAMPAIGN_COUNT = 12
PLANNED_EVALUATION_COUNT = 120
_STORE_MARKER_TABLE = "measure_synthetic_campaign_store"
_STORE_MARKER_SCHEMA_VERSION = 1
_ALLOWED_DATA_FILES = {
    "measure.sqlite3",
    "measure.sqlite3-shm",
    "measure.sqlite3-wal",
}
_SERVICE_TABLES = {
    "analyses",
    "analytical_observations",
    "attempt_revisions",
    "campaign_consumptions",
    "campaign_proposals",
    "campaign_protocols",
    "campaign_runs",
    "dataset_sources",
    "datasets",
    "executable_methods",
    "experiment_attempts",
    "experiment_results",
    "experiments",
    "gaussian_observations",
    "gaussian_producers",
    "observation_contracts",
    "observation_snapshot_members",
    "observation_snapshots",
    "registry_receipts",
    "retention_models",
    "retention_predictions",
}
_SERVICE_INDEXES = {
    "analytical_observations_contract_attempt",
    "analytical_observations_owner_order",
    "attempts_owner_experiment",
    "campaign_one_pending",
    "campaign_protocols_owner_order",
    "campaign_runs_owner_order",
    "experiments_owner_order",
    "gaussian_observations_owner_attempt",
    "gaussian_observations_owner_order",
    "gaussian_producers_owner_order",
    "methods_owner_order",
    "observation_contracts_owner_order",
    "observation_snapshots_owner_order",
    "results_owner_attempt",
    "retention_models_owner_order",
    "retention_predictions_owner_order",
}
_SERVICE_OWNER_TABLES = _SERVICE_TABLES - {
    "attempt_revisions",
    "dataset_sources",
}
_EVALUATION_CSV_FIELDS = (
    "campaign_id",
    "task_id",
    "seed",
    "policy",
    "ordinal",
    "budget",
    "status",
    "completed_simulator_attempt",
    "consumed_evaluation",
    "proposal_source",
    "strong_solvent_fraction",
    "acquisition_duration_min",
    "proposal_id",
    "candidate_sha256",
    "method_id",
    "method_input_sha256",
    "canonical_program_sha256",
    "experiment_id",
    "attempt_id",
    "attempt_revision",
    "retention_prediction_id",
    "retention_prediction_input_sha256",
    "retention_prediction_output_sha256",
    "gaussian_run_id",
    "gaussian_run_input_sha256",
    "gaussian_run_output_sha256",
    "result_ids_json",
    "observation_id",
    "observation_input_sha256",
    "evidence_sha256",
    "assessment_sha256",
    "snapshot_id",
    "snapshot_sha256",
    "consumption_id",
    "consumption_input_sha256",
    "consumption_output_sha256",
    "analytical_acceptance",
    "strict_analytical_success",
    "utility_status",
    "score",
    "score_ge_2_success",
    "detected_count",
    "unknown_count",
    "confirmed_absent_count",
    "retention_times_min_json",
    "minimum_retention_time_min",
    "mean_retention_time_min",
    "retention_statuses_json",
    "finite_resolution_count",
    "minimum_finite_resolution",
    "mean_finite_resolution",
    "resolution_statuses_json",
    "censored_analyte_count",
    "simulated_acquisition_time_min",
    "runtime_preference_satisfied",
    "feasible",
    "declared_evaluation_cost",
    "declared_evaluation_cost_unit",
    "declared_solvent_use_ml_json",
    "declared_solvent_volume_ml",
    "optimizer_walltime_seconds",
    "failure_step",
    "failure_type",
    "failure_message",
    "acknowledgement_failure_step",
    "acknowledgement_failure_type",
    "acknowledgement_failure_message",
    "proposal_acknowledgement_failure_step",
    "proposal_acknowledgement_failure_type",
    "proposal_acknowledgement_failure_message",
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in predeclared input: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value in predeclared input: {value}")


def _read_default_document(path: str | Path = DEFAULT_DOCUMENT) -> tuple[dict[str, Any], str]:
    """Read only the fixed, hash-verified scientific declaration."""
    source = Path(path)
    try:
        info = source.lstat()
    except OSError as error:
        raise ValueError("the fixed synthetic campaign declaration is unavailable") from error
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("the fixed synthetic campaign declaration must be a regular file")
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != DEFAULT_SHA256:
        raise ValueError("the fixed synthetic campaign declaration failed its SHA256 check")
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("the fixed synthetic campaign declaration is malformed") from error
    if not isinstance(document, dict):
        raise ValueError("the fixed synthetic campaign declaration must be an object")
    if (
        document.get("profile") != campaign_protocol.CAMPAIGN_PROFILE
        or document.get("seeds") != [17, 29, 43]
        or document.get("evaluation_budget") != 10
        or document.get("initial_design_size") != 3
        or document.get("policies") != list(POLICIES)
        or len(document.get("tasks", [])) != 2
        or document.get("scope", {}).get("observation_origin") != "simulator_latent"
        or document.get("scope", {}).get("fidelity") != 1.0
    ):
        raise ValueError("the fixed synthetic campaign declaration has an unsupported profile")
    return document, digest


def _key(default_digest: str, *logical_parts: object) -> str:
    """Derive a stable, bounded registry key from frozen input and identity."""
    suffix = sha256(list(logical_parts))[:24]
    return f"syn-cmp-{default_digest}-{suffix}"


def _error_record(error: Exception, *, private_paths: tuple[Path, ...] = ()) -> dict[str, str]:
    message = str(error) or type(error).__name__
    for path in private_paths:
        message = message.replace(str(path), "<local-path>")
    return {"type": type(error).__name__, "message": message[:500]}


def _preflight_output(output_dir: str | Path) -> Path:
    target = Path(output_dir).expanduser()
    if target.is_symlink():
        raise ValueError("output evidence directory must not be a symlink")
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise ValueError("output evidence directory must be absent or empty")
    return target


def _validate_data_dir(data_dir: str | Path, output_dir: Path) -> Path:
    if data_dir is None:
        raise ValueError("an explicit isolated data_dir is required")
    supplied = Path(data_dir).expanduser()
    if supplied.is_symlink():
        raise ValueError("isolated data_dir must not be a symlink")
    resolved = supplied.resolve()
    shared = (Path.home() / ".local" / "share" / "measure-mcp").resolve()
    if resolved == shared or resolved.is_relative_to(shared):
        raise ValueError("data_dir must be separate from the shared user service store")
    reference_dir = DEFAULT_DOCUMENT.parent.resolve()
    if (
        resolved == reference_dir
        or resolved.is_relative_to(reference_dir)
        or reference_dir.is_relative_to(resolved)
    ):
        raise ValueError("data_dir must be separate from bundled reference data")
    output = output_dir.resolve()
    if resolved == output or resolved.is_relative_to(output) or output.is_relative_to(resolved):
        raise ValueError("output evidence and registry data must use separate directories")
    if supplied.exists() and not supplied.is_dir():
        raise ValueError("data_dir must be a directory")
    if supplied.exists():
        for child in supplied.iterdir():
            if child.name not in _ALLOWED_DATA_FILES or child.is_symlink() or not child.is_file():
                raise ValueError(
                    "data_dir may contain only this runner's SQLite database and its sidecar files"
                )
    database = supplied / "measure.sqlite3"
    if database.is_symlink():
        raise ValueError("the isolated SQLite database must not be a symlink")
    return resolved


def _store_objects(con: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (row[0], row[1])
        for row in con.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    ]


def _store_identity_references(value: Any, ids: set[str], attempt_revisions: set[tuple[str, int]]):
    if isinstance(value, dict):
        attempt_id = value.get("attempt_id")
        revision = value.get("attempt_revision", value.get("revision"))
        if (
            isinstance(attempt_id, str)
            and isinstance(revision, int)
            and not isinstance(revision, bool)
        ):
            attempt_revisions.add((attempt_id, revision))
        for key, item in value.items():
            if key.endswith("_id") and isinstance(item, str):
                ids.add(item)
            elif key.endswith("_ids") and isinstance(item, list):
                ids.update(value for value in item if isinstance(value, str))
            _store_identity_references(item, ids, attempt_revisions)
    elif isinstance(value, list):
        for item in value:
            _store_identity_references(item, ids, attempt_revisions)


def _check_owned_store(con: sqlite3.Connection, input_sha256: str, *, allow_empty: bool) -> bool:
    """Verify the marker and every stored service principal/reference.

    Return False only for an entirely empty SQLite schema that may be claimed.
    """
    objects = _store_objects(con)
    object_names = {name for _, name in objects}
    marker_present = _STORE_MARKER_TABLE in object_names
    if not marker_present:
        if objects:
            raise ValueError("isolated data_dir contains an unmarked SQLite store")
        if allow_empty:
            return False
        raise ValueError("isolated data_dir SQLite ownership marker is missing")

    for object_type, name in objects:
        if object_type == "table":
            if name not in _SERVICE_TABLES and name != _STORE_MARKER_TABLE:
                raise ValueError(f"isolated data_dir contains an unknown SQLite table: {name}")
        elif object_type == "index":
            if not name.startswith("sqlite_autoindex_") and name not in _SERVICE_INDEXES:
                raise ValueError(f"isolated data_dir contains an unknown SQLite index: {name}")
        else:
            raise ValueError(f"isolated data_dir contains an unsupported SQLite object: {name}")

    marker_columns = [row[1] for row in con.execute(f'PRAGMA table_info("{_STORE_MARKER_TABLE}")')]
    if marker_columns != ["marker_id", "schema_version", "profile", "input_sha256", "owner"]:
        raise ValueError("isolated data_dir ownership marker has an unsupported schema")
    marker_rows = con.execute(
        f"SELECT marker_id, schema_version, profile, input_sha256, owner "
        f'FROM "{_STORE_MARKER_TABLE}"'
    ).fetchall()
    if marker_rows != [(1, _STORE_MARKER_SCHEMA_VERSION, PROFILE, input_sha256, OWNER)]:
        raise ValueError("isolated data_dir ownership marker does not match this frozen runner")

    table_names = {name for object_type, name in objects if object_type == "table"}
    for table in sorted(_SERVICE_OWNER_TABLES):
        if table not in table_names:
            continue
        foreign_row = con.execute(
            f'SELECT 1 FROM "{table}" WHERE owner IS NULL OR owner != ? LIMIT 1', (OWNER,)
        ).fetchone()
        if foreign_row is not None:
            raise ValueError(f"isolated data_dir contains a foreign principal in {table}")

    receipt_prefix = f"syn-cmp-{input_sha256}-"
    receipt_ids: set[str] = set()
    attempt_revisions: set[tuple[str, int]] = set()
    if "registry_receipts" in table_names:
        receipts = con.execute(
            "SELECT request_key, response FROM registry_receipts "
            "WHERE owner=? ORDER BY request_key",
            (OWNER,),
        ).fetchall()
        for request_key, response in receipts:
            if not request_key.startswith(receipt_prefix):
                raise ValueError("isolated data_dir contains a receipt outside this frozen runner")
            try:
                decoded = json.loads(response)
            except (TypeError, json.JSONDecodeError) as error:
                raise ValueError(
                    "isolated data_dir contains a malformed registry receipt"
                ) from error
            if not isinstance(decoded, dict):
                raise ValueError("isolated data_dir registry receipt is not an object")
            _store_identity_references(decoded, receipt_ids, attempt_revisions)

    for table in sorted(_SERVICE_OWNER_TABLES - {"observation_snapshot_members"}):
        if table not in table_names:
            continue
        columns = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
        if "id" not in columns:
            continue
        for (record_id,) in con.execute(f'SELECT id FROM "{table}" WHERE owner=?', (OWNER,)):
            if record_id not in receipt_ids:
                raise ValueError(f"isolated data_dir contains an unreceipted record in {table}")

    if "observation_snapshot_members" in table_names:
        members = con.execute(
            "SELECT snapshot_id, observation_id, attempt_id FROM observation_snapshot_members "
            "WHERE owner=?",
            (OWNER,),
        ).fetchall()
        if any(
            snapshot_id not in receipt_ids
            or observation_id not in receipt_ids
            or attempt_id not in receipt_ids
            for snapshot_id, observation_id, attempt_id in members
        ):
            raise ValueError("isolated data_dir contains unreceipted snapshot membership")

    if "attempt_revisions" in table_names:
        revisions = con.execute("SELECT attempt_id, revision FROM attempt_revisions").fetchall()
        if any(
            (attempt_id, revision) not in attempt_revisions for attempt_id, revision in revisions
        ):
            raise ValueError("isolated data_dir contains an unreceipted attempt revision")

    if (
        "dataset_sources" in table_names
        and con.execute("SELECT 1 FROM dataset_sources LIMIT 1").fetchone() is not None
    ):
        raise ValueError("isolated synthetic runner store must not contain dataset source records")
    if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("isolated data_dir contains a broken SQLite foreign-key relationship")
    if con.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise ValueError("isolated data_dir failed SQLite integrity verification")
    return True


def _insert_store_marker(con: sqlite3.Connection, input_sha256: str) -> None:
    con.execute(
        f'CREATE TABLE "{_STORE_MARKER_TABLE}" ('
        "marker_id INTEGER PRIMARY KEY CHECK(marker_id=1), "
        "schema_version INTEGER NOT NULL, profile TEXT NOT NULL, "
        "input_sha256 TEXT NOT NULL, owner TEXT NOT NULL)"
    )
    con.execute(
        f'INSERT INTO "{_STORE_MARKER_TABLE}" '
        "(marker_id, schema_version, profile, input_sha256, owner) VALUES (1, ?, ?, ?, ?)",
        (_STORE_MARKER_SCHEMA_VERSION, PROFILE, input_sha256, OWNER),
    )


def _claim_or_verify_owned_store(data_path: Path, input_sha256: str) -> None:
    """Claim an absent/empty store or validate an existing frozen-run store."""
    database = data_path / "measure.sqlite3"
    sidecars = [data_path / "measure.sqlite3-shm", data_path / "measure.sqlite3-wal"]
    if not database.exists() and any(path.exists() for path in sidecars):
        raise ValueError("isolated data_dir has SQLite sidecars without a database")

    if database.exists():
        try:
            with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as con:
                if _check_owned_store(con, input_sha256, allow_empty=True):
                    return
        except sqlite3.DatabaseError as error:
            raise ValueError("isolated data_dir is not a readable SQLite runner store") from error

    try:
        with sqlite3.connect(str(database), timeout=30) as con:
            con.execute("BEGIN IMMEDIATE")
            if not _check_owned_store(con, input_sha256, allow_empty=True):
                _insert_store_marker(con, input_sha256)
            _check_owned_store(con, input_sha256, allow_empty=False)
            con.commit()
    except sqlite3.DatabaseError as error:
        raise ValueError("isolated data_dir could not be safely initialized") from error


def _verify_owned_store(data_path: Path, input_sha256: str) -> None:
    database = data_path / "measure.sqlite3"
    if not database.is_file() or database.is_symlink():
        raise ValueError("isolated data_dir runner database is unavailable")
    try:
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as con:
            _check_owned_store(con, input_sha256, allow_empty=False)
    except sqlite3.DatabaseError as error:
        raise ValueError("isolated data_dir is not a readable SQLite runner store") from error


@contextmanager
def _scoped_data_dir(path: Path) -> Iterator[None]:
    existed = "MEASURE_MCP_DATA_DIR" in os.environ
    previous = os.environ.get("MEASURE_MCP_DATA_DIR")
    os.environ["MEASURE_MCP_DATA_DIR"] = str(path)
    try:
        yield
    finally:
        if existed:
            assert previous is not None
            os.environ["MEASURE_MCP_DATA_DIR"] = previous
        else:
            os.environ.pop("MEASURE_MCP_DATA_DIR", None)


def _retention_spec(task: dict[str, Any], document: dict[str, Any], digest: str) -> dict:
    geometry = document["geometry"]
    return {
        "gradient_coordinate": "column_inlet_at_analyte_entry",
        "analytes": task["analytes"],
        "column_length_m": geometry["column_length_m"],
        "effective_area_m2": geometry["effective_area_m2"],
        "post_column_volume_ml": geometry["post_column_volume_ml"],
        "strong_solvent": "B",
        "parameter_origin": "synthetic",
        "parameter_reference": (
            f"Predeclared synthetic-isocratic-paired-v1.json sha256={digest}; "
            "constructed equations, not fitted physical parameters."
        ),
    }


def _contract_spec(task: dict[str, Any], document: dict[str, Any]) -> dict:
    limits = document["contract"]
    return {
        "required_analytes": [item["analyte_id"] for item in task["analytes"]],
        "resolution_definition": RESOLUTION_DEFINITION,
        "resolution_threshold": limits["resolution_threshold"],
        "max_acquisition_duration_min": limits["max_acquisition_duration_min"],
        "runtime_preference_min": limits["runtime_preference_min"],
        "count_threshold": limits["count_threshold"],
    }


def _producer_spec(task: dict[str, Any], digest: str) -> dict:
    analyte_ids = [item["analyte_id"] for item in task["analytes"]]
    return {
        "widths": [
            {"analyte_id": analyte_id, "fwhm_min": task["fwhm_min"]} for analyte_id in analyte_ids
        ],
        "width_origin": "synthetic_assumption",
        "width_reference": (
            f"Predeclared synthetic-isocratic-paired-v1.json sha256={digest}; "
            "fixed Gaussian FWHM assumption, not measured or fitted."
        ),
    }


def _prepare_inputs(
    document: dict[str, Any], digest: str, progress: dict[str, Any]
) -> dict[str, Any]:
    task_bindings: list[dict[str, Any]] = []
    for task in document["tasks"]:
        task_id = task["task_id"]
        model = retention.register_retention_model(
            OWNER,
            f"Predeclared synthetic retention {task_id}",
            _retention_spec(task, document, digest),
            _key(digest, "retention_model", task_id),
        )
        producer = gaussian_producer.register_gaussian_producer(
            OWNER,
            f"Predeclared synthetic Gaussian producer {task_id}",
            model["model_id"],
            _producer_spec(task, digest),
            _key(digest, "gaussian_producer", task_id),
        )
        contract = observations.register_observation_contract(
            OWNER,
            f"Predeclared analytical observation contract {task_id}",
            _contract_spec(task, document),
            _key(digest, "observation_contract", task_id),
        )
        binding = {
            "task_id": task_id,
            "retention_model": {
                "model_id": model["model_id"],
                "input_sha256": model["input_sha256"],
                "spec": model["spec"],
                "source_fit_range_warnings": model["source_fit_range_warnings"],
                "implementation_provenance": model["implementation_provenance"],
            },
            "producer": {
                "producer_id": producer["producer_id"],
                "input_sha256": producer["input_sha256"],
                "output_sha256": producer["output_sha256"],
                "spec": producer["spec"],
                "implementation_provenance": producer["implementation_provenance"],
            },
            "contract": {
                "contract_id": contract["contract_id"],
                "input_sha256": contract["input_sha256"],
                "spec": contract["spec"],
                "utility_definition": contract["utility_definition"],
                "implementation_provenance": contract["implementation_provenance"],
            },
        }
        task_bindings.append(binding)
        progress["registered_tasks"] = task_bindings

    protocol_spec = {
        "domain": document["domain"],
        "flow_ml_min": document["flow_ml_min"],
        "constraints": {
            "max_acquisition_duration_min": document["contract"]["max_acquisition_duration_min"]
        },
        "tasks": [
            {
                "task_id": binding["task_id"],
                "producer_id": binding["producer"]["producer_id"],
                "observation_contract_id": binding["contract"]["contract_id"],
            }
            for binding in task_bindings
        ],
        "seeds": document["seeds"],
        "evaluation_budget": document["evaluation_budget"],
        "initial_design_size": document["initial_design_size"],
        "policies": document["policies"],
        "stopping_policy": document["stopping_policy"],
        "evaluation_cost": document["evaluation_cost"],
        "evaluation_cost_unit": document["evaluation_cost_unit"],
    }
    registered = campaign_protocol_registry.register_campaign_protocol(
        OWNER,
        "Predeclared synthetic isocratic paired comparison",
        protocol_spec,
        _key(digest, "campaign_protocol"),
    )
    progress["protocol"] = registered
    return {"protocol": registered, "tasks": task_bindings}


def _empty_evaluation(
    *,
    campaign_id: str | None,
    task_id: str,
    seed: int,
    policy: str,
    ordinal: int,
    budget: int,
    status: str,
) -> dict[str, Any]:
    return {
        "campaign_id": campaign_id,
        "task_id": task_id,
        "seed": seed,
        "policy": policy,
        "ordinal": ordinal,
        "budget": budget,
        "status": status,
    }


def _execute_evaluation(
    *,
    owner: str,
    default_sha256: str,
    campaign_id: str,
    task_id: str,
    seed: int,
    policy: str,
    ordinal: int,
    expected_revision: int,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Execute one proposal pipeline, mutating row as durable steps complete."""
    row["failure_step"] = None
    row["proposal_response"] = None

    def run_step(name: str, function):
        row["failure_step"] = name
        return function()

    suggestion = run_step(
        "suggest_campaign",
        lambda: campaigns.suggest_campaign(
            owner,
            campaign_id,
            expected_revision=expected_revision,
            key=_key(default_sha256, "campaign", task_id, seed, policy, ordinal, "proposal"),
        ),
    )
    proposal = suggestion["proposal"]
    method = proposal["method"]
    row.update(
        {
            "proposal_response": suggestion,
            "proposal": proposal,
            "proposal_id": proposal["proposal_id"],
            "proposal_source": proposal["proposal_source"],
            "backend": proposal.get("backend"),
            "candidate": proposal["candidate"],
            "candidate_sha256": proposal["candidate_sha256"],
            "method": method,
            "method_id": method["method_id"],
            "method_input_sha256": method["input_sha256"],
            "canonical_program_sha256": method["canonical_program_sha256"],
        }
    )
    task_record = suggestion["campaign"]["task"]
    producer = task_record["producer"]
    contract = task_record["contract"]
    candidate = proposal["candidate"]
    method_id = method["method_id"]
    experiment = run_step(
        "create_experiment",
        lambda: experiments.create_experiment(
            owner,
            experiments.ExperimentSpec(
                title=f"Synthetic {task_id} {policy} seed {seed} evaluation {ordinal + 1}",
                source="predeclared_synthetic_campaign_runner",
                proposal_reference=proposal["proposal_id"],
                executable_method_id=method_id,
                observation_contract_id=contract["contract_id"],
                observation_producer_id=producer["producer_id"],
                intended_conditions=[
                    experiments.Condition(
                        name="strong_solvent_fraction",
                        value=candidate["strong_solvent_fraction"],
                        unit="1",
                    ),
                    experiments.Condition(
                        name="acquisition_duration_min",
                        value=candidate["acquisition_duration_min"],
                        unit="min",
                    ),
                ],
                description=(
                    "Synthetic simulator campaign record. The ideal model and Gaussian "
                    "widths are declared assumptions; no instrument was run."
                ),
            ),
            _key(default_sha256, "campaign", task_id, seed, policy, ordinal, "experiment"),
        ),
    )
    row["experiment"] = experiment
    attempt = run_step(
        "record_simulator_attempt",
        lambda: experiments.record_attempt(
            owner,
            experiment["experiment_id"],
            experiments.AttemptSpec(
                status="completed",
                evaluator_kind="simulator",
                evaluator_reference=(
                    "measure-service ideal-retention and Gaussian-centroid simulator"
                ),
                evaluator_revision="synthetic-isocratic-paired-v1",
                fidelity=1.0,
                actual_conditions=[
                    experiments.Condition(
                        name="strong_solvent_fraction",
                        value=candidate["strong_solvent_fraction"],
                        unit="1",
                    ),
                    experiments.Condition(
                        name="acquisition_duration_min",
                        value=candidate["acquisition_duration_min"],
                        unit="min",
                    ),
                ],
                executable_method_id=method_id,
            ),
            _key(default_sha256, "campaign", task_id, seed, policy, ordinal, "attempt"),
        ),
    )
    row["attempt"] = attempt
    prediction = run_step(
        "predict_retention",
        lambda: retention.predict_retention(
            owner,
            producer["retention_model_id"],
            method_id,
            _key(default_sha256, "campaign", task_id, seed, policy, ordinal, "retention"),
        ),
    )
    row["retention_prediction"] = prediction
    gaussian_run = run_step(
        "record_gaussian_observation",
        lambda: gaussian_producer.record_gaussian_observation(
            owner,
            producer["producer_id"],
            prediction["prediction_id"],
            attempt["attempt_id"],
            expected_attempt_revision=attempt["revision"],
            key=_key(default_sha256, "campaign", task_id, seed, policy, ordinal, "gaussian"),
        ),
    )
    row["gaussian_observation"] = gaussian_run
    observation = run_step(
        "evaluate_analytical_observation",
        lambda: observations.evaluate_observation(
            owner,
            contract["contract_id"],
            attempt["attempt_id"],
            expected_attempt_revision=attempt["revision"],
            spec=gaussian_run["evidence_spec"],
            key=_key(default_sha256, "campaign", task_id, seed, policy, ordinal, "assessment"),
        ),
    )
    row["observation"] = observation
    snapshot_response = run_step(
        "create_observation_snapshot",
        lambda: observations.create_observation_snapshot(
            owner,
            contract["contract_id"],
            [observation["observation_id"]],
            _key(default_sha256, "campaign", task_id, seed, policy, ordinal, "snapshot"),
        ),
    )
    snapshot = run_step(
        "read_observation_snapshot",
        lambda: observations.get_observation_snapshot(owner, snapshot_response["snapshot_id"]),
    )
    row["snapshot"] = snapshot
    consumption_response = run_step(
        "consume_campaign_snapshot",
        lambda: campaigns.consume_campaign_snapshot(
            owner,
            campaign_id,
            proposal["proposal_id"],
            snapshot["snapshot_id"],
            expected_revision=expected_revision + 1,
            key=_key(default_sha256, "campaign", task_id, seed, policy, ordinal, "consumption"),
        ),
    )
    row["consumption_response"] = consumption_response
    row["consumption"] = consumption_response["consumption"]
    row["status"] = "consumed"
    row["failure_step"] = None
    row["metrics"] = _evaluation_metrics(row)
    return row


def _completed_simulator_attempt(evaluation: dict[str, Any]) -> bool:
    attempt = evaluation.get("attempt")
    spec = attempt.get("spec", {}) if isinstance(attempt, dict) else {}
    fidelity = spec.get("fidelity") if isinstance(spec, dict) else None
    return (
        isinstance(attempt, dict)
        and isinstance(spec, dict)
        and spec.get("status") == "completed"
        and spec.get("evaluator_kind") == "simulator"
        and isinstance(fidelity, (int, float))
        and not isinstance(fidelity, bool)
        and fidelity == 1.0
    )


def _attempt_acquisition_duration(evaluation: dict[str, Any]) -> float | None:
    attempt = evaluation.get("attempt")
    spec = attempt.get("spec", {}) if isinstance(attempt, dict) else {}
    conditions = spec.get("actual_conditions", []) if isinstance(spec, dict) else []
    for condition in conditions if isinstance(conditions, list) else []:
        if not isinstance(condition, dict) or condition.get("name") != "acquisition_duration_min":
            continue
        value = condition.get("value")
        if (
            condition.get("unit") == "min"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        ):
            return float(value)
    return None


def _evaluation_metrics(evaluation: dict[str, Any]) -> dict[str, Any]:
    consumption = evaluation.get("consumption")
    result = consumption.get("result", {}) if isinstance(consumption, dict) else {}
    raw = result.get("raw_metrics", {}) if isinstance(result, dict) else {}
    observation_response = evaluation.get("observation")
    observation = (
        observation_response.get("observation", {})
        if isinstance(observation_response, dict)
        else {}
    )
    assessment = observation.get("assessment", {})
    completed_attempt = _completed_simulator_attempt(evaluation)
    attempt_duration = _attempt_acquisition_duration(evaluation) if completed_attempt else None
    retention_times = raw.get("retention_times_min", [])
    retention_statuses = raw.get("retention_statuses", [])
    resolutions = raw.get("peak_resolutions", [])
    resolution_statuses = raw.get("resolution_statuses", [])
    if not isinstance(retention_times, list):
        retention_times = []
    if not isinstance(retention_statuses, list):
        retention_statuses = []
    if not isinstance(resolutions, list):
        resolutions = []
    if not isinstance(resolution_statuses, list):
        resolution_statuses = []
    finite_times = [
        float(value)
        for value in retention_times
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    ]
    finite_resolutions = [
        float(value)
        for value in resolutions
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    ]
    acceptance = result.get("analytical_acceptance")
    if acceptance == "accepted":
        strict_success = True
    elif acceptance == "not_accepted":
        strict_success = False
    else:
        strict_success = None
    utility = result.get("utility", {})
    score = utility.get("score") if isinstance(utility, dict) else None
    score_status = utility.get("status") if isinstance(utility, dict) else None
    score_success = (
        score >= 2.0
        if score_status == "scored"
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
        and math.isfinite(score)
        else None
    )
    method = evaluation.get("method", {})
    canonical_program = method.get("canonical_program", {}) if isinstance(method, dict) else {}
    solvent_use = canonical_program.get("total_solvent_use_ml", {})
    if not isinstance(solvent_use, dict):
        solvent_use = {}
    volume_values = [
        float(value)
        for value in solvent_use.values()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ]
    declared_solvent_use = (
        solvent_use
        if completed_attempt and solvent_use and len(volume_values) == len(solvent_use)
        else None
    )
    declared_solvent_volume = math.fsum(volume_values) if declared_solvent_use is not None else None
    simulated_time = result.get("runtime_min")
    if (
        isinstance(simulated_time, bool)
        or not isinstance(simulated_time, (int, float))
        or not math.isfinite(simulated_time)
        or simulated_time < 0
    ):
        simulated_time = attempt_duration
    declared_cost = result.get("evaluation_cost")
    if (
        isinstance(declared_cost, bool)
        or not isinstance(declared_cost, (int, float))
        or not math.isfinite(declared_cost)
        or declared_cost < 0
    ):
        declared_cost = evaluation.get("protocol_evaluation_cost") if completed_attempt else None
    declared_cost_unit = result.get("evaluation_cost_unit") or (
        evaluation.get("protocol_evaluation_cost_unit") if completed_attempt else None
    )
    preference = result.get("runtime_preference_satisfied")
    if not isinstance(preference, bool):
        threshold = evaluation.get("runtime_preference_min")
        preference = (
            simulated_time <= threshold
            if isinstance(simulated_time, (int, float))
            and isinstance(threshold, (int, float))
            and not isinstance(threshold, bool)
            and math.isfinite(threshold)
            else None
        )
    backend = evaluation.get("backend")
    backend_runtime = backend.get("runtime", {}) if isinstance(backend, dict) else {}
    walltime = backend_runtime.get("elapsed_seconds") if isinstance(backend_runtime, dict) else None
    if (
        isinstance(walltime, bool)
        or not isinstance(walltime, (int, float))
        or not math.isfinite(walltime)
        or walltime < 0
    ):
        walltime = None
    return {
        "completed_simulator_attempt": completed_attempt,
        "consumed_evaluation": evaluation.get("status") == "consumed",
        "retention_times_min": retention_times,
        "minimum_retention_time_min": min(finite_times) if finite_times else None,
        "mean_retention_time_min": math.fsum(finite_times) / len(finite_times)
        if finite_times
        else None,
        "retention_statuses": retention_statuses,
        "detected_count": assessment.get("detected_count"),
        "unknown_count": assessment.get("unknown_count"),
        "confirmed_absent_count": assessment.get("confirmed_absent_count"),
        "finite_resolution_count": len(finite_resolutions),
        "finite_resolutions": finite_resolutions,
        "minimum_finite_resolution": min(finite_resolutions) if finite_resolutions else None,
        "mean_finite_resolution": math.fsum(finite_resolutions) / len(finite_resolutions)
        if finite_resolutions
        else None,
        "resolution_statuses": resolution_statuses,
        "censored_analyte_count": sum(
            status in ("not_eluted", "censored") for status in retention_statuses
        ),
        "unsupported_resolution_count": sum(
            status in ("unsupported", "censored") for status in resolution_statuses
        ),
        "resolution_availability_is_detection": False,
        "analytical_acceptance": acceptance,
        "strict_analytical_success": strict_success,
        "utility_status": score_status,
        "score": score,
        "score_ge_2_success": score_success,
        "simulated_acquisition_time_min": simulated_time,
        "runtime_preference_satisfied": preference,
        "feasible": result.get("feasible"),
        "declared_evaluation_cost": declared_cost,
        "declared_evaluation_cost_unit": declared_cost_unit,
        "declared_solvent_use_ml": declared_solvent_use,
        "declared_solvent_volume_ml": declared_solvent_volume,
        "optimizer_walltime_seconds": walltime,
    }


def _campaign_summaries(evaluations: list[dict[str, Any]], budget: int) -> dict[str, Any]:
    consumed = [row for row in evaluations if row.get("status") == "consumed"]
    summaries: dict[str, Any] = {}
    for series, metric_name in (
        (STRICT_SUCCESS_SERIES, "strict_analytical_success"),
        (SCORE_SUCCESS_SERIES, "score_ge_2_success"),
    ):
        observations_for_series = [
            SuccessObservation(
                sequence=index,
                fidelity_role="high",
                satisfied=row["metrics"].get(metric_name),
            )
            for index, row in enumerate(consumed, 1)
        ]
        summaries[series] = (
            summarize_campaign_success(observations_for_series, budget=budget)
            if observations_for_series
            else None
        )
    return summaries


def _recover_completed_attempt(owner: str, evaluation: dict[str, Any]) -> bool:
    """Read the exact completed attempt after a response acknowledgement was lost."""
    if _completed_simulator_attempt(evaluation):
        return True
    experiment = evaluation.get("experiment")
    experiment_id = experiment.get("experiment_id") if isinstance(experiment, dict) else None
    if not isinstance(experiment_id, str):
        return False
    page = experiments.list_attempts(owner, experiment_id, limit=20)
    items = page.get("items", [])
    if page.get("count") != 1 or len(items) != 1:
        return False
    attempt = experiments.get_attempt(owner, items[0]["attempt_id"], items[0]["revision"])
    spec = attempt.get("spec", {})
    if (
        attempt.get("experiment_id") != experiment_id
        or spec.get("status") != "completed"
        or spec.get("evaluator_kind") != "simulator"
        or spec.get("fidelity") != 1.0
        or spec.get("executable_method_id") != evaluation.get("method_id")
    ):
        return False
    evaluation["attempt"] = attempt
    evaluation["attempt_recovery"] = {
        "source": "owned_experiment_attempt_read",
        "attempt_id": attempt["attempt_id"],
        "revision": attempt["revision"],
        "completed_simulator_attempt": True,
    }
    return True


def _recover_pending_proposal(
    owner: str, campaign_id: str, ordinal: int, evaluation: dict[str, Any]
) -> dict[str, Any] | None:
    """Read a committed pending proposal when its mutation acknowledgement was lost."""
    if isinstance(evaluation.get("proposal"), dict):
        return None
    campaign = campaigns.get_campaign(owner, campaign_id)
    pending = campaign.get("pending_proposal")
    if not isinstance(pending, dict) or pending.get("ordinal") != ordinal:
        return None
    proposal = campaigns.get_campaign_proposal(owner, pending["proposal_id"])
    if proposal.get("campaign_id") != campaign_id or proposal.get("ordinal") != ordinal:
        raise ValueError("Stored pending proposal does not match its campaign ordinal")
    method = proposal["method"]
    evaluation.update(
        {
            "proposal": proposal,
            "proposal_id": proposal["proposal_id"],
            "proposal_source": proposal["proposal_source"],
            "backend": proposal.get("backend"),
            "candidate": proposal["candidate"],
            "candidate_sha256": proposal["candidate_sha256"],
            "method": method,
            "method_id": method["method_id"],
            "method_input_sha256": method["input_sha256"],
            "canonical_program_sha256": method["canonical_program_sha256"],
        }
    )
    recovery = {
        "source": "owned_campaign_pending_proposal_read",
        "proposal_id": proposal["proposal_id"],
        "campaign_revision": campaign["revision"],
    }
    evaluation["proposal_recovery"] = recovery
    return recovery


def _recover_durable_consumption(
    owner: str, campaign_id: str, evaluation: dict[str, Any]
) -> dict[str, Any] | None:
    """Reconcile an exact stored consumption through owner-scoped campaign reads."""
    proposal_id = evaluation.get("proposal_id")
    snapshot = evaluation.get("snapshot")
    snapshot_id = snapshot.get("snapshot_id") if isinstance(snapshot, dict) else None
    if not isinstance(proposal_id, str) or not isinstance(snapshot_id, str):
        return None
    campaign = campaigns.get_campaign(owner, campaign_id)
    matches = [
        item
        for item in campaign.get("history", [])
        if item.get("proposal_id") == proposal_id and item.get("snapshot_id") == snapshot_id
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Campaign history contains duplicate exact proposal/snapshot consumptions")
    history = matches[0]
    consumption = campaigns.get_campaign_consumption(owner, history["consumption_id"])
    expected = {
        "campaign_id": campaign_id,
        "proposal_id": proposal_id,
        "snapshot_id": snapshot_id,
        "observation_id": (
            evaluation["observation"].get("observation_id")
            if isinstance(evaluation.get("observation"), dict)
            else None
        ),
    }
    if any(consumption.get(key) != value for key, value in expected.items()):
        raise ValueError("Stored consumption does not match the exact proposal and snapshot")
    attempt = evaluation.get("attempt", {})
    if isinstance(attempt, dict) and attempt.get("attempt_id") is not None:
        if consumption.get("attempt_id") != attempt.get("attempt_id") or consumption.get(
            "attempt_revision"
        ) != attempt.get("revision"):
            raise ValueError("Stored consumption does not match the exact completed attempt")
    evaluation["consumption"] = consumption
    recovery = {
        "source": "owned_campaign_history_and_consumption_read",
        "consumption_id": consumption["consumption_id"],
        "proposal_id": proposal_id,
        "snapshot_id": snapshot_id,
        "campaign_revision": campaign["revision"],
    }
    evaluation["consumption_recovery"] = recovery
    evaluation["status"] = "consumed"
    evaluation["failure_step"] = None
    evaluation["metrics"] = _evaluation_metrics(evaluation)
    return recovery


def _execute_campaign(
    *,
    owner: str,
    protocol_id: str,
    task_id: str,
    seed: int,
    policy: str,
    default_sha256: str,
    planned_budget: int = 10,
    private_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Run or resume one campaign using its original request receipts."""
    evaluations: list[dict[str, Any]] = []
    recovered_interruptions: list[dict[str, Any]] = []
    campaign_id = None
    failure = None
    current = None
    try:
        registered_protocol = campaign_protocol_registry.get_campaign_protocol(owner, protocol_id)
        protocol_spec = registered_protocol["spec"]
        task_link = next(
            item for item in registered_protocol["tasks"] if item["task_id"] == task_id
        )
        if protocol_spec["evaluation_budget"] != planned_budget:
            raise ValueError("registered campaign budget differs from the frozen input")
        created = campaigns.create_campaign(
            owner,
            protocol_id,
            task_id,
            seed,
            policy,
            _key(default_sha256, "campaign", task_id, seed, policy, "create"),
        )
        campaign_id = created["campaign_id"]
        if created["evaluation_budget"] != planned_budget:
            raise ValueError("registered campaign budget differs from the frozen input")
    except Exception as error:
        failure = {
            "ordinal": 0,
            "step": "create_campaign",
            **_error_record(error, private_paths=private_paths),
        }
        evaluations = [
            _empty_evaluation(
                campaign_id=None,
                task_id=task_id,
                seed=seed,
                policy=policy,
                ordinal=ordinal,
                budget=planned_budget,
                status="campaign_setup_failed" if ordinal == 0 else "not_attempted_unknown",
            )
            for ordinal in range(planned_budget)
        ]
        return {
            "campaign_id": None,
            "task_id": task_id,
            "seed": seed,
            "policy": policy,
            "status": "partial_failure",
            "failure": failure,
            "evaluation_budget": planned_budget,
            "consumed_evaluations": 0,
            "budget_remaining": planned_budget,
            "completed_simulator_attempt_count": 0,
            "unconsumed_completed_simulator_attempt_count": 0,
            "recovered_interruptions": [],
            "evaluations": evaluations,
            "history": [],
            "trajectory_summaries": _campaign_summaries(evaluations, planned_budget),
        }

    for ordinal in range(planned_budget):
        evaluation = _empty_evaluation(
            campaign_id=campaign_id,
            task_id=task_id,
            seed=seed,
            policy=policy,
            ordinal=ordinal,
            budget=planned_budget,
            status="not_attempted_unknown",
        )
        evaluation["protocol_evaluation_cost"] = protocol_spec["evaluation_cost"]
        evaluation["protocol_evaluation_cost_unit"] = protocol_spec["evaluation_cost_unit"]
        evaluation["runtime_preference_min"] = task_link["contract"]["spec"][
            "runtime_preference_min"
        ]
        try:
            _execute_evaluation(
                owner=owner,
                default_sha256=default_sha256,
                campaign_id=campaign_id,
                task_id=task_id,
                seed=seed,
                policy=policy,
                ordinal=ordinal,
                expected_revision=2 * ordinal + 1,
                row=evaluation,
            )
            evaluations.append(evaluation)
        except Exception as error:
            step = evaluation.get("failure_step") or "campaign_evaluation"
            error_data = _error_record(error, private_paths=private_paths)
            proposal_recovery = None
            try:
                proposal_recovery = _recover_pending_proposal(
                    owner, campaign_id, ordinal, evaluation
                )
            except Exception as recovery_error:
                evaluation["proposal_recovery_error"] = _error_record(
                    recovery_error, private_paths=private_paths
                )
            if proposal_recovery is not None and step == "suggest_campaign":
                evaluation["proposal_acknowledgement_failure"] = {
                    "step": step,
                    **error_data,
                }
                recovered_interruptions.append(
                    {
                        "ordinal": ordinal,
                        "step": step,
                        "failure": error_data,
                        "recovery": proposal_recovery,
                    }
                )
            attempt_recovery_error = None
            try:
                _recover_completed_attempt(owner, evaluation)
            except Exception as recovery_error:
                attempt_recovery_error = _error_record(recovery_error, private_paths=private_paths)
            if attempt_recovery_error is not None:
                evaluation["attempt_recovery_error"] = attempt_recovery_error

            recovered_consumption = None
            if step == "consume_campaign_snapshot" or evaluation.get("status") == "consumed":
                try:
                    recovered_consumption = _recover_durable_consumption(
                        owner, campaign_id, evaluation
                    )
                except Exception as recovery_error:
                    evaluation["consumption_recovery_error"] = _error_record(
                        recovery_error, private_paths=private_paths
                    )
            if recovered_consumption is not None:
                evaluation["acknowledgement_failure"] = {"step": step, **error_data}
                recovered_interruptions.append(
                    {
                        "ordinal": ordinal,
                        "step": step,
                        "failure": error_data,
                        "recovery": recovered_consumption,
                    }
                )
                evaluations.append(evaluation)
                continue

            if evaluation.get("status") != "consumed":
                evaluation["status"] = "partial_failure"
            evaluation["failure_step"] = step
            evaluation["failure"] = error_data
            try:
                evaluation["metrics"] = _evaluation_metrics(evaluation)
            except Exception as metrics_error:
                evaluation["metrics_error"] = _error_record(
                    metrics_error, private_paths=private_paths
                )
            evaluations.append(evaluation)
            failure = {"ordinal": ordinal, "step": step, **error_data}
            break

    if failure is not None:
        failed_ordinal = failure["ordinal"]
        for ordinal in range(failed_ordinal + 1, planned_budget):
            evaluations.append(
                _empty_evaluation(
                    campaign_id=campaign_id,
                    task_id=task_id,
                    seed=seed,
                    policy=policy,
                    ordinal=ordinal,
                    budget=planned_budget,
                    status="not_attempted_unknown",
                )
            )
    try:
        current = campaigns.get_campaign(owner, campaign_id)
    except Exception as error:
        if failure is None:
            failure = {
                "ordinal": len(evaluations),
                "step": "get_campaign",
                **_error_record(error, private_paths=private_paths),
            }
    consumed_count = (
        current["consumed_evaluations"]
        if isinstance(current, dict)
        else sum(row.get("status") == "consumed" for row in evaluations)
    )
    if current is not None and consumed_count != sum(
        row.get("status") == "consumed" for row in evaluations
    ):
        failure = failure or {
            "ordinal": consumed_count,
            "step": "campaign_history_reconciliation",
            "type": "ValueError",
            "message": "campaign registry history differs from reconstructed evaluation receipts",
        }
    summaries = _campaign_summaries(evaluations, planned_budget)
    complete = failure is None and current is not None and current["state"] == "budget_complete"
    completed_attempt_count = sum(_completed_simulator_attempt(row) for row in evaluations)
    unconsumed_completed_attempt_count = sum(
        _completed_simulator_attempt(row) and row.get("status") != "consumed" for row in evaluations
    )
    return {
        "campaign_id": campaign_id,
        "task_id": task_id,
        "seed": seed,
        "policy": policy,
        "status": "budget_complete" if complete else "partial_failure",
        "failure": failure,
        "evaluation_budget": planned_budget,
        "consumed_evaluations": consumed_count,
        "budget_remaining": planned_budget - consumed_count,
        "completed_simulator_attempt_count": completed_attempt_count,
        "unconsumed_completed_simulator_attempt_count": unconsumed_completed_attempt_count,
        "recovered_interruptions": recovered_interruptions,
        "pending_proposal": None if current is None else current["pending_proposal"],
        "campaign_revision": None if current is None else current["revision"],
        "campaign_state": None if current is None else current["state"],
        "history": [] if current is None else current["history"],
        "implementation_provenance": (
            None if current is None else current["implementation_provenance"]
        ),
        "evaluations": evaluations,
        "trajectory_summaries": summaries,
    }


def _campaign_placeholder(
    *, task_id: str, seed: int, policy: str, budget: int, error: dict[str, str]
) -> dict[str, Any]:
    evaluations = [
        _empty_evaluation(
            campaign_id=None,
            task_id=task_id,
            seed=seed,
            policy=policy,
            ordinal=ordinal,
            budget=budget,
            status="campaign_setup_failed" if ordinal == 0 else "not_attempted_unknown",
        )
        for ordinal in range(budget)
    ]
    return {
        "campaign_id": None,
        "task_id": task_id,
        "seed": seed,
        "policy": policy,
        "status": "partial_failure",
        "failure": {"ordinal": 0, "step": "prepare_campaign_inputs", **error},
        "evaluation_budget": budget,
        "consumed_evaluations": 0,
        "budget_remaining": budget,
        "completed_simulator_attempt_count": 0,
        "unconsumed_completed_simulator_attempt_count": 0,
        "recovered_interruptions": [],
        "pending_proposal": None,
        "campaign_revision": None,
        "campaign_state": None,
        "history": [],
        "implementation_provenance": None,
        "evaluations": evaluations,
        "trajectory_summaries": {series: None for series in SUCCESS_SERIES},
    }


def _build_trajectory_rows(campaign_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for policy in POLICIES:
        group = [row for row in campaign_records if row["policy"] == policy]
        for series in SUCCESS_SERIES:
            summaries = [
                campaign["trajectory_summaries"][series]
                for campaign in group
                if campaign["trajectory_summaries"].get(series) is not None
            ]
            points = aggregate_success_trajectories(
                summaries,
                planned_count=PLANNED_CAMPAIGNS_PER_POLICY,
                budget=10,
            )
            rows.extend({"policy": policy, "series": series, **point} for point in points)
    return rows


def _paired_comparison_rows(campaign_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = {
        (record["task_id"], record["seed"], record["policy"]): record for record in campaign_records
    }
    rows = []
    task_seeds = sorted({(record["task_id"], record["seed"]) for record in campaign_records})
    for task_id, seed in task_seeds:
        random_record = indexed.get((task_id, seed, "random"))
        bo_record = indexed.get((task_id, seed, "bo_mcp_gp_logei"))
        for series in SUCCESS_SERIES:
            random_summary = (
                None if random_record is None else random_record["trajectory_summaries"].get(series)
            )
            bo_summary = (
                None if bo_record is None else bo_record["trajectory_summaries"].get(series)
            )
            random_state = (
                "unknown"
                if random_summary is None
                else random_summary["trajectory"][-1]["success_state"]
            )
            bo_state = (
                "unknown" if bo_summary is None else bo_summary["trajectory"][-1]["success_state"]
            )
            random_first = (
                None
                if random_summary is None or random_summary["first_confirmed_success"] is None
                else random_summary["first_confirmed_success"]["high_fidelity_index"]
            )
            bo_first = (
                None
                if bo_summary is None or bo_summary["first_confirmed_success"] is None
                else bo_summary["first_confirmed_success"]["high_fidelity_index"]
            )
            rows.append(
                {
                    "task_id": task_id,
                    "seed": seed,
                    "series": series,
                    "planned_policy_pair": "random_vs_bo_mcp_gp_logei",
                    "random_campaign_id": (
                        None if random_record is None else random_record["campaign_id"]
                    ),
                    "bo_campaign_id": None if bo_record is None else bo_record["campaign_id"],
                    "random_state_at_budget": random_state,
                    "bo_state_at_budget": bo_state,
                    "state_changed": random_state != bo_state,
                    "random_first_confirmed_index": random_first,
                    "bo_first_confirmed_index": bo_first,
                    "candidate_minus_random_first_index": (
                        None
                        if random_first is None or bo_first is None
                        else bo_first - random_first
                    ),
                    "comparison_scope": (
                        "descriptive matched task-seed pair; not a population estimate"
                    ),
                }
            )
    return rows


def _policy_metrics(campaign_records: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for policy in POLICIES:
        campaign_evaluations = [
            evaluation
            for campaign in campaign_records
            for evaluation in campaign["evaluations"]
            if campaign["policy"] == policy
        ]
        consumed = [
            evaluation
            for evaluation in campaign_evaluations
            if evaluation.get("status") == "consumed"
        ]
        completed_attempts = [
            evaluation
            for evaluation in campaign_evaluations
            if _completed_simulator_attempt(evaluation)
        ]
        consumed_metrics = [
            evaluation["metrics"]
            for evaluation in consumed
            if isinstance(evaluation.get("metrics"), dict)
        ]
        attempt_metrics = [evaluation.get("metrics", {}) for evaluation in completed_attempts]
        all_evaluation_metrics = [
            evaluation["metrics"]
            for evaluation in campaign_evaluations
            if isinstance(evaluation.get("metrics"), dict)
        ]
        retentions = [
            value
            for item in consumed_metrics
            for value in item["retention_times_min"]
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
        ]
        resolutions = [value for item in consumed_metrics for value in item["finite_resolutions"]]
        costs = [
            item["declared_evaluation_cost"]
            for item in attempt_metrics
            if isinstance(item.get("declared_evaluation_cost"), (int, float))
            and not isinstance(item["declared_evaluation_cost"], bool)
            and math.isfinite(item["declared_evaluation_cost"])
        ]
        acquisition_times = [
            item["simulated_acquisition_time_min"]
            for item in attempt_metrics
            if isinstance(item.get("simulated_acquisition_time_min"), (int, float))
            and not isinstance(item["simulated_acquisition_time_min"], bool)
            and math.isfinite(item["simulated_acquisition_time_min"])
        ]
        walltimes = [
            item["optimizer_walltime_seconds"]
            for item in all_evaluation_metrics
            if item.get("optimizer_walltime_seconds") is not None
        ]
        solvent_totals: dict[str, list[float]] = {}
        for item in attempt_metrics:
            declared_solvent_use = item.get("declared_solvent_use_ml")
            if not isinstance(declared_solvent_use, dict):
                continue
            for solvent, volume in declared_solvent_use.items():
                if (
                    isinstance(volume, (int, float))
                    and not isinstance(volume, bool)
                    and math.isfinite(volume)
                    and volume >= 0
                ):
                    solvent_totals.setdefault(solvent, []).append(float(volume))
        detected_values = [
            item["detected_count"]
            for item in consumed_metrics
            if isinstance(item["detected_count"], int)
            and not isinstance(item["detected_count"], bool)
        ]
        runtime_preferences = [item.get("runtime_preference_satisfied") for item in attempt_metrics]
        consumed_preferences = [
            item.get("runtime_preference_satisfied") for item in consumed_metrics
        ]
        known_cost_unit = next(
            (
                item["declared_evaluation_cost_unit"]
                for item in attempt_metrics
                if isinstance(item.get("declared_evaluation_cost_unit"), str)
            ),
            None,
        )
        result[policy] = {
            "planned_campaign_count": PLANNED_CAMPAIGNS_PER_POLICY,
            "recorded_campaign_count": sum(
                campaign["policy"] == policy for campaign in campaign_records
            ),
            "budget_complete_campaign_count": sum(
                campaign["policy"] == policy and campaign["status"] == "budget_complete"
                for campaign in campaign_records
            ),
            "partial_failure_campaign_count": sum(
                campaign["policy"] == policy and campaign["status"] != "budget_complete"
                for campaign in campaign_records
            ),
            "planned_evaluation_count": PLANNED_CAMPAIGNS_PER_POLICY * 10,
            "consumed_evaluation_count": len(consumed),
            "completed_simulator_attempt_count": len(completed_attempts),
            "unconsumed_completed_simulator_attempt_count": sum(
                evaluation.get("status") != "consumed" for evaluation in completed_attempts
            ),
            "finite_retention_time_count": len(retentions),
            "minimum_retention_time_min": min(retentions) if retentions else None,
            "mean_retention_time_min": math.fsum(retentions) / len(retentions)
            if retentions
            else None,
            "detected_count_total": (
                sum(detected_values)
                if consumed_metrics and len(detected_values) == len(consumed_metrics)
                else None
            ),
            "detected_count_known_total": sum(detected_values) if detected_values else None,
            "detected_count_unknown_evaluation_count": len(consumed_metrics) - len(detected_values),
            "detected_count_mean_per_consumed_evaluation": (
                math.fsum(detected_values) / len(consumed_metrics)
                if consumed_metrics and len(detected_values) == len(consumed_metrics)
                else None
            ),
            "analytical_acceptance_counts": {
                "accepted": sum(
                    item["analytical_acceptance"] == "accepted" for item in consumed_metrics
                ),
                "not_accepted": sum(
                    item["analytical_acceptance"] == "not_accepted" for item in consumed_metrics
                ),
                "unknown": sum(
                    item["analytical_acceptance"] not in ("accepted", "not_accepted")
                    for item in consumed_metrics
                ),
            },
            "finite_resolution_count": sum(
                item["finite_resolution_count"] for item in consumed_metrics
            ),
            "minimum_finite_resolution": min(resolutions) if resolutions else None,
            "mean_finite_resolution": math.fsum(resolutions) / len(resolutions)
            if resolutions
            else None,
            "resolution_count_is_detection_count": False,
            "censored_analyte_count": sum(
                item["censored_analyte_count"] for item in consumed_metrics
            ),
            "runtime_preference_satisfied_consumed_evaluation_count": sum(
                value is True for value in consumed_preferences
            ),
            "runtime_preference_known_consumed_evaluation_count": sum(
                isinstance(value, bool) for value in consumed_preferences
            ),
            "runtime_preference_satisfied_completed_attempt_count": sum(
                value is True for value in runtime_preferences
            ),
            "runtime_preference_known_completed_attempt_count": sum(
                isinstance(value, bool) for value in runtime_preferences
            ),
            "declared_evaluation_cost_total_completed_attempts": (
                math.fsum(float(value) for value in costs) if costs else None
            ),
            "declared_evaluation_cost_unit": known_cost_unit,
            "declared_evaluation_cost_known_completed_attempt_count": len(costs),
            "declared_evaluation_cost_unknown_completed_attempt_count": (
                len(completed_attempts) - len(costs)
            ),
            "simulated_acquisition_time_total_min_completed_attempts": (
                math.fsum(float(value) for value in acquisition_times)
                if acquisition_times
                else None
            ),
            "simulated_acquisition_time_known_completed_attempt_count": len(acquisition_times),
            "simulated_acquisition_time_unknown_completed_attempt_count": (
                len(completed_attempts) - len(acquisition_times)
            ),
            "declared_solvent_use_ml_total_by_solvent_completed_attempts": {
                solvent: math.fsum(values) for solvent, values in sorted(solvent_totals.items())
            },
            "declared_solvent_volume_total_ml_completed_attempts": (
                math.fsum(math.fsum(values) for values in solvent_totals.values())
                if solvent_totals
                else None
            ),
            "declared_solvent_volume_known_completed_attempt_count": sum(
                isinstance(item.get("declared_solvent_use_ml"), dict) for item in attempt_metrics
            ),
            "declared_solvent_volume_unknown_completed_attempt_count": len(completed_attempts)
            - sum(
                isinstance(item.get("declared_solvent_use_ml"), dict) for item in attempt_metrics
            ),
            "optimizer_walltime_sample_count": len(walltimes),
            "optimizer_walltime_total_seconds": math.fsum(walltimes) if walltimes else None,
            "optimizer_walltime_mean_seconds": math.fsum(walltimes) / len(walltimes)
            if walltimes
            else None,
            "timing_scope": (
                "Persisted proposal walltime includes optional sidecar launch time and includes "
                "proposals whose later observation pipeline failed. Simulated acquisition time "
                "and declared method solvent volume cover completed synthetic simulator attempts, "
                "including those not consumed as observations; none are physical instrument, "
                "solvent-spend or CPU-time measurements."
            ),
        }
    return result


def _evaluation_csv_row(evaluation: dict[str, Any]) -> dict[str, Any]:
    metrics = evaluation.get("metrics", {})
    proposal = evaluation.get("proposal", {})
    experiment = evaluation.get("experiment", {})
    attempt = evaluation.get("attempt", {})
    prediction = evaluation.get("retention_prediction", {})
    gaussian = evaluation.get("gaussian_observation", {})
    observation = evaluation.get("observation", {})
    snapshot = evaluation.get("snapshot", {})
    consumption = evaluation.get("consumption", {})
    result_refs = gaussian.get("generated_results", []) if isinstance(gaussian, dict) else []
    failure = evaluation.get("failure", {})
    acknowledgement_failure = evaluation.get("acknowledgement_failure", {})
    proposal_acknowledgement_failure = evaluation.get("proposal_acknowledgement_failure", {})
    values = {
        "campaign_id": evaluation.get("campaign_id"),
        "task_id": evaluation.get("task_id"),
        "seed": evaluation.get("seed"),
        "policy": evaluation.get("policy"),
        "ordinal": evaluation.get("ordinal"),
        "budget": evaluation.get("budget"),
        "status": evaluation.get("status"),
        "completed_simulator_attempt": metrics.get("completed_simulator_attempt"),
        "consumed_evaluation": metrics.get("consumed_evaluation"),
        "proposal_source": proposal.get("proposal_source"),
        "strong_solvent_fraction": evaluation.get("candidate", {}).get("strong_solvent_fraction"),
        "acquisition_duration_min": evaluation.get("candidate", {}).get("acquisition_duration_min"),
        "proposal_id": evaluation.get("proposal_id"),
        "candidate_sha256": evaluation.get("candidate_sha256"),
        "method_id": evaluation.get("method_id"),
        "method_input_sha256": evaluation.get("method_input_sha256"),
        "canonical_program_sha256": evaluation.get("canonical_program_sha256"),
        "experiment_id": experiment.get("experiment_id"),
        "attempt_id": attempt.get("attempt_id"),
        "attempt_revision": attempt.get("revision"),
        "retention_prediction_id": prediction.get("prediction_id"),
        "retention_prediction_input_sha256": prediction.get("input_sha256"),
        "retention_prediction_output_sha256": prediction.get("output_sha256"),
        "gaussian_run_id": gaussian.get("run_id"),
        "gaussian_run_input_sha256": gaussian.get("input_sha256"),
        "gaussian_run_output_sha256": gaussian.get("output_sha256"),
        "result_ids_json": [item.get("result_id") for item in result_refs],
        "observation_id": observation.get("observation_id"),
        "observation_input_sha256": observation.get("input_sha256"),
        "evidence_sha256": observation.get("evidence_sha256"),
        "assessment_sha256": observation.get("assessment_sha256"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_sha256": snapshot.get("sha256"),
        "consumption_id": consumption.get("consumption_id"),
        "consumption_input_sha256": consumption.get("input_sha256"),
        "consumption_output_sha256": consumption.get("output_sha256"),
        "analytical_acceptance": metrics.get("analytical_acceptance"),
        "strict_analytical_success": metrics.get("strict_analytical_success"),
        "utility_status": metrics.get("utility_status"),
        "score": metrics.get("score"),
        "score_ge_2_success": metrics.get("score_ge_2_success"),
        "detected_count": metrics.get("detected_count"),
        "unknown_count": metrics.get("unknown_count"),
        "confirmed_absent_count": metrics.get("confirmed_absent_count"),
        "retention_times_min_json": metrics.get("retention_times_min"),
        "minimum_retention_time_min": metrics.get("minimum_retention_time_min"),
        "mean_retention_time_min": metrics.get("mean_retention_time_min"),
        "retention_statuses_json": metrics.get("retention_statuses"),
        "finite_resolution_count": metrics.get("finite_resolution_count"),
        "minimum_finite_resolution": metrics.get("minimum_finite_resolution"),
        "mean_finite_resolution": metrics.get("mean_finite_resolution"),
        "resolution_statuses_json": metrics.get("resolution_statuses"),
        "censored_analyte_count": metrics.get("censored_analyte_count"),
        "simulated_acquisition_time_min": metrics.get("simulated_acquisition_time_min"),
        "runtime_preference_satisfied": metrics.get("runtime_preference_satisfied"),
        "feasible": metrics.get("feasible"),
        "declared_evaluation_cost": metrics.get("declared_evaluation_cost"),
        "declared_evaluation_cost_unit": metrics.get("declared_evaluation_cost_unit"),
        "declared_solvent_use_ml_json": metrics.get("declared_solvent_use_ml"),
        "declared_solvent_volume_ml": metrics.get("declared_solvent_volume_ml"),
        "optimizer_walltime_seconds": metrics.get("optimizer_walltime_seconds"),
        "failure_step": evaluation.get("failure_step"),
        "failure_type": failure.get("type"),
        "failure_message": failure.get("message"),
        "acknowledgement_failure_step": acknowledgement_failure.get("step"),
        "acknowledgement_failure_type": acknowledgement_failure.get("type"),
        "acknowledgement_failure_message": acknowledgement_failure.get("message"),
        "proposal_acknowledgement_failure_step": proposal_acknowledgement_failure.get("step"),
        "proposal_acknowledgement_failure_type": proposal_acknowledgement_failure.get("type"),
        "proposal_acknowledgement_failure_message": proposal_acknowledgement_failure.get("message"),
    }
    return {field: values.get(field) for field in _EVALUATION_CSV_FIELDS}


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.write_text("".join(canonical(value) + "\n" for value in values), encoding="utf-8")


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return canonical(value)
    return value


def _write_csv(path: Path, fields: tuple[str, ...] | list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=list(fields), extrasaction="raise")
        writer.writeheader()
        writer.writerows({key: _csv_value(value) for key, value in row.items()} for row in rows)


def _backup_database(destination: Path) -> None:
    source = sqlite3.connect(str(db_path()))
    target = sqlite3.connect(str(destination))
    try:
        source.backup(target)
        target.commit()
        result = target.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise ValueError("SQLite evidence backup failed its integrity check")
    finally:
        target.close()
        source.close()


def _trajectory_final_rows(trajectory_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in trajectory_rows if row["high_fidelity_budget"] == 10]


def _render_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Matched synthetic campaign comparison",
        "",
        f"Run status: **{'complete' if report['completed'] else 'partial'}**.",
        "",
        f"Predeclared input SHA256: {report['predeclared_input']['sha256']}.",
        "",
        "The comparison includes two constructed three-analyte tasks, seeds 17, 29 and 43, "
        "three shared initial-design evaluations and a full budget of ten per campaign. "
        "The planned denominator is six campaigns per policy (12 total), with 120 planned "
        "evaluation rows including initialization.",
        "",
        "## Policy summaries",
        "",
        "| Policy | Planned | Complete | Partial | Completed attempts | Consumed | Mean detected | "
        "Finite Rs | Simulated time (min) | Declared cost | Optimizer walltime (s) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for policy, item in report["policy_metrics"].items():
        simulated_time = item["simulated_acquisition_time_total_min_completed_attempts"]
        declared_cost = item["declared_evaluation_cost_total_completed_attempts"]
        lines.append(
            "| {policy} | {planned} | {complete} | {partial} | {attempts} | {consumed} | "
            "{detected} | {finite_rs} | {simulated} | {cost} | {wall} |".format(
                policy=policy,
                planned=item["planned_campaign_count"],
                complete=item["budget_complete_campaign_count"],
                partial=item["partial_failure_campaign_count"],
                attempts=item["completed_simulator_attempt_count"],
                consumed=item["consumed_evaluation_count"],
                detected=(
                    "unknown"
                    if item["detected_count_mean_per_consumed_evaluation"] is None
                    else f"{item['detected_count_mean_per_consumed_evaluation']:.4g}"
                ),
                finite_rs=item["finite_resolution_count"],
                simulated=("unknown" if simulated_time is None else f"{simulated_time:.6g}"),
                cost="unknown" if declared_cost is None else f"{declared_cost:.6g}",
                wall=(
                    "unavailable"
                    if item["optimizer_walltime_total_seconds"] is None
                    else f"{item['optimizer_walltime_total_seconds']:.6g}"
                ),
            )
        )
    lines.extend(
        [
            "",
            "Retention-time summaries, strict analytical acceptance, runtime preference, "
            "censoring, solvent use and optimizer versions/settings are in report.json and "
            "the per-evaluation evidence. Finite-resolution availability is not a detection "
            "count. success-trajectories.csv reports strict analytical acceptance separately "
            "from the finite-resolution score threshold of 2.0. Both use the existing three-valued "
            "campaign kernel and the full planned denominator; missing future evaluations remain "
            "unknown.",
            "",
            "Declared protocol evaluation cost, simulated acquisition time and declared method "
            "solvent volume are summarized over completed synthetic simulator attempts, including "
            "attempts whose observations were not consumed. Coverage counts accompany the totals. "
            "These declared values are not monetary, physical solvent-use, instrument-time or CPU-time "
            "measurements. Persisted optimizer proposal walltime is reported separately and can "
            "be present when the later observation pipeline failed.",
            "",
            "## Paired interpretation",
            "",
            "The six task-seed pairs are descriptive. The comparison reports matched final "
            "states and first confirmed budget indices; it does not estimate a population effect "
            "or support a confidence interval. Synthetic acquisition time is kept separate from "
            "optional optimizer proposal walltime.",
            "",
            "## Scientific scope",
            "",
            "These are noiseless ideal retention equations with declared synthetic Gaussian FWHM "
            "assumptions and latent simulator observations. They do not reproduce the manuscript "
            "campaign, validate fitted physical parameters, qualify signal processing or qualify "
            "an instrument. The BO policy is reported only when its optional sidecar actually "
            "returned a verified BoTorch SingleTaskGP and qLogExpectedImprovement suggestion. "
            "See report.json for the actual model, acquisition, versions, settings and warnings.",
            "",
        ]
    )
    failures = [
        campaign for campaign in report["campaigns"] if campaign["status"] != "budget_complete"
    ]
    if failures:
        lines.extend(["## Partial campaigns", ""])
        for campaign in failures:
            failure = campaign.get("failure") or {}
            lines.append(
                f"- {campaign['task_id']} / seed {campaign['seed']} / {campaign['policy']}: "
                f"{campaign['consumed_evaluations']} of {campaign['evaluation_budget']} consumed; "
                f"failure at {failure.get('step', 'unresolved')} "
                f"({failure.get('type', 'unknown')}: {failure.get('message', 'no detail')})."
            )
        lines.append("")
    return "\n".join(lines)


def _backend_observations(evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = {}
    for evaluation in evaluations:
        backend = evaluation.get("backend")
        if not isinstance(backend, dict):
            continue
        identity = sha256(backend)
        seen.setdefault(identity, {"backend_sha256": identity, **backend})
    return [seen[key] for key in sorted(seen)]


def _validate_counts(
    campaign_records: list[dict[str, Any]], evaluation_rows: list[dict[str, Any]]
) -> None:
    if len(campaign_records) != PLANNED_CAMPAIGN_COUNT:
        raise ValueError("campaign accounting does not cover all twelve predeclared campaigns")
    if len(evaluation_rows) != PLANNED_EVALUATION_COUNT:
        raise ValueError("evaluation accounting does not cover all 120 planned evaluations")
    campaign_keys = [(row["task_id"], row["seed"], row["policy"]) for row in campaign_records]
    if len(set(campaign_keys)) != PLANNED_CAMPAIGN_COUNT:
        raise ValueError("campaign accounting contains duplicate task-seed-policy identities")
    eval_keys = [
        (row["task_id"], row["seed"], row["policy"], row["ordinal"]) for row in evaluation_rows
    ]
    if len(set(eval_keys)) != PLANNED_EVALUATION_COUNT:
        raise ValueError("evaluation accounting contains duplicate planned evaluation identities")
    for campaign in campaign_records:
        identity = (campaign["task_id"], campaign["seed"], campaign["policy"])
        members = [
            row
            for row in evaluation_rows
            if (row["task_id"], row["seed"], row["policy"]) == identity
        ]
        if len(members) != campaign["evaluation_budget"]:
            raise ValueError("campaign and evaluation rows disagree on the planned budget")
        if sorted(row["ordinal"] for row in members) != list(range(campaign["evaluation_budget"])):
            raise ValueError("campaign evaluations do not cover each planned ordinal exactly once")
        if any(row["campaign_id"] != campaign["campaign_id"] for row in members):
            raise ValueError("campaign and evaluation rows disagree on campaign identity")
        if campaign["consumed_evaluations"] != sum(row["status"] == "consumed" for row in members):
            raise ValueError("campaign consumption count differs from evaluation evidence rows")


def run_synthetic_campaigns(output_dir: Path, *, data_dir: Path) -> dict[str, Any]:
    """Run or resume all twelve fixed campaigns and atomically publish evidence.

    data_dir is an explicit, dedicated registry location. It is never replaced
    by or merged with the service's normal per-user database. Existing SQLite
    data in this directory resumes original idempotent campaign requests.
    """
    document, input_digest = _read_default_document()
    target = _preflight_output(output_dir)
    data_path = _validate_data_dir(data_dir, target)
    data_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _claim_or_verify_owned_store(data_path, input_digest)
    task_ids = [task["task_id"] for task in document["tasks"]]
    plans = [
        (task_id, seed, policy)
        for task_id in task_ids
        for seed in document["seeds"]
        for policy in document["policies"]
    ]
    if len(plans) != PLANNED_CAMPAIGN_COUNT:
        raise ValueError("frozen input does not produce the expected twelve campaign identities")
    progress: dict[str, Any] = {"registered_tasks": [], "protocol": None}
    campaign_records: list[dict[str, Any]] = []
    with staged_bundle(target, input_dirs=(DEFAULT_DOCUMENT.parent, data_path)) as stage:
        with _scoped_data_dir(data_path):
            prepared = None
            preparation_error = None
            try:
                prepared = _prepare_inputs(document, input_digest, progress)
            except Exception as error:
                preparation_error = _error_record(
                    error, private_paths=(data_path, DEFAULT_DOCUMENT.parent)
                )
            for task_id, seed, policy in plans:
                if prepared is None:
                    record = _campaign_placeholder(
                        task_id=task_id,
                        seed=seed,
                        policy=policy,
                        budget=document["evaluation_budget"],
                        error=preparation_error
                        or {"type": "RuntimeError", "message": "campaign inputs were unavailable"},
                    )
                else:
                    record = _execute_campaign(
                        owner=OWNER,
                        protocol_id=prepared["protocol"]["protocol_id"],
                        task_id=task_id,
                        seed=seed,
                        policy=policy,
                        default_sha256=input_digest,
                        planned_budget=document["evaluation_budget"],
                        private_paths=(data_path, DEFAULT_DOCUMENT.parent),
                    )
                campaign_records.append(record)
            evaluation_rows = [
                evaluation
                for campaign in campaign_records
                for evaluation in campaign["evaluations"]
            ]
            _validate_counts(campaign_records, evaluation_rows)
            trajectory_rows = _build_trajectory_rows(campaign_records)
            paired_rows = _paired_comparison_rows(campaign_records)
            policy_metrics = _policy_metrics(campaign_records)
            evaluation_csv_rows = [_evaluation_csv_row(row) for row in evaluation_rows]
            protocol_document = {
                "profile": PROFILE,
                "predeclared_input": document,
                "predeclared_input_sha256": input_digest,
                "registered_protocol": progress["protocol"],
                "registered_task_inputs": progress["registered_tasks"],
                "registration_failure": preparation_error,
                "campaign_idempotency_key_scheme": (
                    "syn-cmp-{full predeclared SHA256}-{first 24 hex characters of canonical "
                    "logical operation/task/seed/policy/ordinal identity SHA256}"
                ),
            }
            backend_observations = _backend_observations(evaluation_rows)
            complete = all(row["status"] == "budget_complete" for row in campaign_records)
            failures = [
                {
                    "task_id": row["task_id"],
                    "seed": row["seed"],
                    "policy": row["policy"],
                    "campaign_id": row["campaign_id"],
                    "consumed_evaluations": row["consumed_evaluations"],
                    "failure": row.get("failure"),
                }
                for row in campaign_records
                if row["status"] != "budget_complete"
            ]
            report: dict[str, Any] = {
                "schema_version": 1,
                "profile": PROFILE,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "completed": complete,
                "status": "budget_complete" if complete else "partial_failure",
                "predeclared_input": {
                    "bundled_path": "reference_data/synthetic-isocratic-paired-v1.json",
                    "sha256": input_digest,
                    "selection": document["selection"],
                    "scope": document["scope"],
                },
                "planned_denominators": {
                    "campaigns_total": PLANNED_CAMPAIGN_COUNT,
                    "campaigns_per_policy": PLANNED_CAMPAIGNS_PER_POLICY,
                    "evaluations_total_including_initial_design": PLANNED_EVALUATION_COUNT,
                    "evaluations_per_campaign_including_initial_design": document[
                        "evaluation_budget"
                    ],
                    "initial_design_size_per_campaign": document["initial_design_size"],
                    "stopping_policy": document["stopping_policy"],
                },
                "protocol": {
                    "protocol_id": None
                    if progress["protocol"] is None
                    else progress["protocol"]["protocol_id"],
                    "input_sha256": None
                    if progress["protocol"] is None
                    else progress["protocol"]["input_sha256"],
                    "output_sha256": None
                    if progress["protocol"] is None
                    else progress["protocol"]["output_sha256"],
                    "registration_failure": preparation_error,
                },
                "policy_metrics": policy_metrics,
                "trajectory_final_counts": _trajectory_final_rows(trajectory_rows),
                "paired_comparison_summary": [
                    {
                        "series": series,
                        "pair_count": sum(row["series"] == series for row in paired_rows),
                        "changed_final_state_count": sum(
                            row["series"] == series and row["state_changed"] for row in paired_rows
                        ),
                        "both_confirmed_by_budget_count": sum(
                            row["series"] == series
                            and row["random_state_at_budget"] == "confirmed_success"
                            and row["bo_state_at_budget"] == "confirmed_success"
                            for row in paired_rows
                        ),
                        "scope": "descriptive matched task-seed pairs; no population inference",
                    }
                    for series in SUCCESS_SERIES
                ],
                "backend_observations": backend_observations,
                "campaigns": campaign_records,
                "failures": failures,
                "scientific_scope": {
                    "utility_profile": "moses-botier-1.0.0-binary64/1",
                    "utility_input_definition": "finite-resolution-reference/1",
                    "analytical_success_series": "separate strict observation-contract acceptance",
                    "score_success_series": "finite-resolution utility score >= 2.0",
                    "finite_resolution_count_is_analytical_detection": False,
                    "noise": document["scope"]["noise"],
                    "retention_models_are_constructed_equations_not_physical_fits": True,
                    "fwhm_values_are_declared_synthetic_assumptions": True,
                    "trace_simulation_or_signal_processing_qualified": document["scope"][
                        "signal_processing_qualified"
                    ],
                    "instrument_qualified": document["scope"]["instrument_qualified"],
                    "manuscript_protocol_match": document["scope"]["manuscript_protocol_match"],
                    "multi_fidelity": False,
                    "physical_model_fit": document["scope"]["physical_model_fit"],
                },
                "implementation_provenance": {
                    "measure_service": method_backend_info(),
                    "campaign_protocol": (
                        None
                        if progress["protocol"] is None
                        else progress["protocol"]["implementation_provenance"]
                    ),
                    "input_sha256": input_digest,
                    "runner_module_sha256": file_sha256(__file__),
                },
                "evidence_files": [
                    "protocol.json",
                    "evaluations.jsonl",
                    "campaigns.jsonl",
                    "evaluations.csv",
                    "success-trajectories.csv",
                    "paired-comparisons.csv",
                    "measure.sqlite3",
                    "report.json",
                    "report.md",
                ],
            }
            _write_json(stage / "protocol.json", protocol_document)
            _write_jsonl(stage / "evaluations.jsonl", evaluation_rows)
            _write_jsonl(stage / "campaigns.jsonl", campaign_records)
            _write_csv(stage / "evaluations.csv", _EVALUATION_CSV_FIELDS, evaluation_csv_rows)
            trajectory_fields = (
                "policy",
                "series",
                "high_fidelity_budget",
                "cohort_size",
                "recorded_campaign_count",
                "observed_at_budget_count",
                "success_count",
                "unknown_success_count",
                "observed_no_success_count",
                "success_fraction",
                "success_fraction_upper_bound",
            )
            _write_csv(stage / "success-trajectories.csv", trajectory_fields, trajectory_rows)
            paired_fields = (
                "task_id",
                "seed",
                "series",
                "planned_policy_pair",
                "random_campaign_id",
                "bo_campaign_id",
                "random_state_at_budget",
                "bo_state_at_budget",
                "state_changed",
                "random_first_confirmed_index",
                "bo_first_confirmed_index",
                "candidate_minus_random_first_index",
                "comparison_scope",
            )
            _write_csv(stage / "paired-comparisons.csv", paired_fields, paired_rows)
            _verify_owned_store(data_path, input_digest)
            _backup_database(stage / "measure.sqlite3")
            report["database_backup"] = {
                "filename": "measure.sqlite3",
                "sha256": file_sha256(stage / "measure.sqlite3"),
                "size_bytes": (stage / "measure.sqlite3").stat().st_size,
                "scope": (
                    "SQLite online backup of the isolated synthetic-run registry after all "
                    "connections closed"
                ),
            }
            _write_json(stage / "report.json", report)
            (stage / "report.md").write_text(_render_report_markdown(report), encoding="utf-8")
    report["output_dir"] = str(target)
    return report
