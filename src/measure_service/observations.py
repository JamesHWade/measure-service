"""Immutable analytical observation contracts and evidence-bound assessments.

This registry stores caller-declared observation evidence and delegates the
acceptance decision to :mod:`measure_methods.observations`. It does not qualify
measurement producers, execute methods, calculate a utility score, or create
optimizer state.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from measure_methods.moses_objective import moses_score_definition
from measure_methods.observations import AnalyteObservation, assess_analytical_observation

from .method_backend import method_backend_info
from .storage import canonical, now, registry_connection, registry_mutation, sha256

SCHEMA_VERSION = "1.0"
MAX_RECORD_BYTES = 64_000
MAX_ANALYTES = 32
MAX_SNAPSHOT_OBSERVATIONS = 50
CONTRACT_PROFILE = "moses-observed-analytical/1"
UTILITY_PROFILE = "moses-botier-1.0.0-binary64/1"
UTILITY_INPUT_DEFINITION = "finite-resolution-reference/1"
RETENTION_TIME_METRIC = "retention_time"
RESOLUTION_METRIC = "resolution"
_NUMBER = StrictFloat | StrictInt


class ObservationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @field_validator("*")
    @classmethod
    def reject_whitespace_only_text(cls, value: Any) -> Any:
        if isinstance(value, str) and value and not value.strip():
            raise ValueError("Text fields must not consist only of whitespace")
        return value


def _finite_positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


class ObservationContractSpec(ObservationModel):
    """Declared analyte requirements, measurement limits and reference utility."""

    profile: Literal["moses-observed-analytical/1"] = CONTRACT_PROFILE
    required_analytes: list[StrictStr] = Field(min_length=1, max_length=MAX_ANALYTES)
    resolution_definition: StrictStr = Field(min_length=1, max_length=100)
    resolution_threshold: _NUMBER = 1.5
    max_acquisition_duration_min: _NUMBER
    runtime_preference_min: _NUMBER = 5.0
    utility_profile: Literal["moses-botier-1.0.0-binary64/1"] = UTILITY_PROFILE
    utility_input_definition: Literal["finite-resolution-reference/1"] = UTILITY_INPUT_DEFINITION
    count_threshold: _NUMBER = 0.5

    @field_validator("required_analytes")
    @classmethod
    def analyte_ids_are_unique_nonblank(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 100 for value in values):
            raise ValueError("required analyte IDs must contain 1 to 100 nonblank characters")
        if len(set(values)) != len(values):
            raise ValueError("required analyte IDs must be unique")
        return values

    @field_validator("resolution_definition")
    @classmethod
    def resolution_definition_is_bounded(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("resolution_definition must be nonblank")
        return value

    @model_validator(mode="after")
    def validate_limits(self) -> ObservationContractSpec:
        maximum = _finite_positive(
            self.max_acquisition_duration_min, "max_acquisition_duration_min"
        )
        threshold = _finite_positive(self.resolution_threshold, "resolution_threshold")
        preference = _finite_positive(self.runtime_preference_min, "runtime_preference_min")
        count = self.count_threshold
        if isinstance(count, bool) or not isinstance(count, (int, float)):
            raise ValueError("count_threshold must be a finite number between 0 and 1")
        try:
            count_value = float(count)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("count_threshold must be finite") from error
        if not math.isfinite(count_value) or not 0 <= count_value <= 1:
            raise ValueError("count_threshold must be a finite number between 0 and 1")
        if preference > maximum:
            raise ValueError("runtime_preference_min must not exceed max_acquisition_duration_min")
        # The named MoSeS utility profile has a fixed resolution upper bound of 5,
        # and its time criterion requires a threshold of at least one minute.
        if threshold > 5:
            raise ValueError("resolution_threshold must not exceed the utility profile bound of 5")
        if preference < 1:
            raise ValueError(
                "runtime_preference_min must be at least 1 for the declared utility profile"
            )
        if maximum < 1:
            raise ValueError(
                "max_acquisition_duration_min must be at least 1 for the declared utility profile"
            )
        return self


class AnalyteEvidence(ObservationModel):
    analyte_id: StrictStr = Field(min_length=1, max_length=100)
    assignment: Literal["confirmed", "ambiguous", "unassigned"]
    assignment_evidence: StrictStr = Field(min_length=1, max_length=1000)
    retention_result_id: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    resolution_result_id: StrictStr | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("analyte_id", "assignment_evidence")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("analyte IDs and assignment evidence must be nonblank")
        return value


class ObservationEvidenceSpec(ObservationModel):
    fidelity_role: Literal["high", "low"]
    observation_origin: Literal[
        "simulator_latent", "processed_signal", "instrument_reported", "manual", "archived_replay"
    ]
    analytes: list[AnalyteEvidence] = Field(default_factory=list, max_length=MAX_ANALYTES)


def _bounded(value: Any) -> Any:
    if len(canonical(value).encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("Observation record exceeds the 64,000-byte limit")
    return value


def _contract(con: sqlite3.Connection, owner: str, contract_id: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM observation_contracts WHERE id=? AND owner=?", (contract_id, owner)
    ).fetchone()
    if row is None:
        raise ValueError("Observation contract was not found or is not accessible")
    return row


def _contract_response(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload"])
    input_identity = {"title": row["title"], "spec": payload["spec"]}
    if sha256(input_identity) != row["input_digest"]:
        raise ValueError("Stored observation contract input digest does not match its contents")
    return _bounded(
        {
            "schema_version": SCHEMA_VERSION,
            "contract_id": row["id"],
            "title": row["title"],
            "spec": payload["spec"],
            "utility_definition": payload["utility_definition"],
            "implementation_provenance": payload["implementation_provenance"],
            "input_sha256": row["input_digest"],
            "created_at": row["created_at"],
        }
    )


def _utility_definition(spec: ObservationContractSpec) -> dict[str, Any]:
    try:
        definition = moses_score_definition(
            profile=spec.utility_profile,
            num_compounds=len(spec.required_analytes),
            max_run_time_min=spec.max_acquisition_duration_min,
            objective="NumPeaks-CritRes",
            count_threshold=spec.count_threshold,
            resolution_threshold=spec.resolution_threshold,
            run_time_threshold_min=spec.runtime_preference_min,
        )
        return {
            **definition,
            "input_definition": spec.utility_input_definition,
            "parameters": {
                "max_run_time_min": spec.max_acquisition_duration_min,
                "count_threshold": spec.count_threshold,
                "resolution_threshold": spec.resolution_threshold,
                "run_time_threshold_min": spec.runtime_preference_min,
            },
        }
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Contract thresholds are incompatible with the declared utility profile: {error}"
        ) from error


def _implementation_provenance() -> dict[str, Any]:
    source = inspect.getsourcefile(assess_analytical_observation)
    if source is None:
        raise RuntimeError("Analytical acceptance kernel source identity is unavailable")
    digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    return {
        "measure_service": method_backend_info(),
        "acceptance_kernel": {
            "module": "measure_methods.observations",
            "source_sha256": digest,
        },
        "utility_definition_module": "measure_methods.moses_objective",
    }


def register_observation_contract(
    owner: str, title: str, spec: ObservationContractSpec | dict[str, Any], key: str
) -> dict[str, Any]:
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("A contract title of 1 to 200 characters is required")
    if not isinstance(spec, ObservationContractSpec):
        spec = ObservationContractSpec.model_validate(spec)
    requested = spec.model_dump(mode="json")
    request = {"operation": "register_observation_contract", "title": title, "spec": requested}

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        retained = {
            "spec": requested,
            "utility_definition": _utility_definition(spec),
            "implementation_provenance": _implementation_provenance(),
        }
        _bounded(retained)
        record_id = str(uuid.uuid4())
        created = now()
        digest = sha256({"title": title, "spec": requested})
        con.execute(
            "INSERT INTO observation_contracts VALUES (?, ?, ?, ?, ?, ?)",
            (record_id, owner, title, canonical(retained), digest, created),
        )
        return _contract_response(_contract(con, owner, record_id))

    return registry_mutation(owner, key, request, write)


def get_observation_contract(owner: str, contract_id: str) -> dict[str, Any]:
    with registry_connection() as con:
        return _contract_response(_contract(con, owner, contract_id))


def observation_contract_link(
    con: sqlite3.Connection, owner: str, contract_id: str
) -> dict[str, str]:
    """Resolve an immutable contract identity inside the experiment transaction."""
    row = _contract(con, owner, contract_id)
    return {"contract_id": row["id"], "input_sha256": row["input_digest"]}


def _result_record(
    con: sqlite3.Connection,
    owner: str,
    result_id: str,
    *,
    attempt_id: str,
    attempt_revision: int,
    analyte_id: str,
    metric: str,
) -> dict[str, Any]:
    from .experiments import _owned

    row = _owned(con, "experiment_results", owner, result_id)
    if (
        row["attempt_id"] != attempt_id
        or row["attempt_revision"] != attempt_revision
        or row["analyte_reference"] != analyte_id
        or row["metric"] != metric
    ):
        raise ValueError(
            "Result link does not match the selected attempt, revision, analyte and metric"
        )
    successor = con.execute(
        "SELECT id FROM experiment_results WHERE supersedes_id=? AND owner=?", (result_id, owner)
    ).fetchone()
    if successor is not None:
        raise ValueError("Observation evidence must link a current, nonsuperseded result")
    outcome = json.loads(row["payload"])
    if sha256(outcome) != row["digest"]:
        raise ValueError("Stored result digest does not match its contents")
    status = outcome.get("status")
    if status not in {
        "observed",
        "not_detected",
        "below_limit",
        "censored",
        "missing",
        "failed",
        "infeasible",
    }:
        raise ValueError("Stored result has an unsupported outcome status")
    unit = outcome.get("unit")
    if metric == RETENTION_TIME_METRIC and unit not in ("min", "s"):
        raise ValueError("Retention-time results must use min or s")
    if metric == RESOLUTION_METRIC and unit != "1":
        raise ValueError("Resolution results must use unit 1")
    value = outcome.get("value")
    normalized_value: float | None = None
    if status == "observed":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Observed result is missing its stored numeric value")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("Observed result is not representable as a finite number") from error
        if not math.isfinite(number) or number < 0:
            raise ValueError("Observed result must be finite and nonnegative")
        if metric == RETENTION_TIME_METRIC:
            if unit == "min":
                normalized_value = number
            elif unit == "s":
                normalized_value = number / 60.0
            else:
                raise ValueError("Retention-time results must use min or s")
        elif unit == "1":
            normalized_value = number
        else:
            raise ValueError("Resolution results must use unit 1")
    elif value is not None:
        raise ValueError("Non-observed result statuses must not carry an observed value")
    if metric == RESOLUTION_METRIC and status == "not_detected":
        raise ValueError("not_detected is only a valid retention-time result status")
    provenance = outcome.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("Stored result provenance is malformed")
    return {
        "result_id": row["id"],
        "metric": row["metric"],
        "analyte_reference": row["analyte_reference"],
        "attempt_id": row["attempt_id"],
        "attempt_revision": row["attempt_revision"],
        "status": status,
        "raw_unit": outcome["unit"],
        "raw_value": value,
        "normalized_value": normalized_value,
        "input_sha256": row["digest"],
        "outcome": outcome,
    }


def _retention_status(result: dict[str, Any] | None) -> tuple[str, float | None]:
    if result is None:
        return "missing", None
    status = result["status"]
    if status == "observed":
        return "observed", result["normalized_value"]
    if status == "not_detected":
        return "not_detected", None
    if status == "missing":
        return "missing", None
    if status in ("failed", "infeasible"):
        return "failed", None
    if status in ("censored", "below_limit"):
        return "censored", None
    raise ValueError("Result status cannot be interpreted as retention evidence")


def _resolution_status(result: dict[str, Any] | None) -> tuple[str, float | None]:
    if result is None:
        return "missing", None
    status = result["status"]
    if status == "observed":
        return "observed", result["normalized_value"]
    if status == "missing":
        return "missing", None
    if status in ("failed", "infeasible"):
        return "failed", None
    if status in ("censored", "below_limit"):
        return "censored", None
    raise ValueError("Result status cannot be interpreted as resolution evidence")


def _experiment_contract_link(
    con: sqlite3.Connection, owner: str, experiment_id: str, contract_id: str
) -> tuple[str, sqlite3.Row]:
    from .experiments import _owned

    experiment = _owned(con, "experiments", owner, experiment_id)
    payload = json.loads(experiment["payload"])
    link = payload.get("observation_contract")
    if (
        payload.get("observation_contract_id") != contract_id
        or not isinstance(link, dict)
        or link.get("contract_id") != contract_id
    ):
        raise ValueError("Experiment is not bound to the selected observation contract")
    contract = _contract(con, owner, contract_id)
    if link.get("input_sha256") != contract["input_digest"]:
        raise ValueError("Experiment observation-contract link has a stale input digest")
    return experiment["id"], experiment


def evaluate_observation(
    owner: str,
    contract_id: str,
    attempt_id: str,
    expected_attempt_revision: int,
    spec: ObservationEvidenceSpec | dict[str, Any],
    key: str,
) -> dict[str, Any]:
    if (
        isinstance(expected_attempt_revision, bool)
        or not isinstance(expected_attempt_revision, int)
        or expected_attempt_revision < 1
    ):
        raise ValueError("expected_attempt_revision must be a positive integer")
    if not isinstance(spec, ObservationEvidenceSpec):
        spec = ObservationEvidenceSpec.model_validate(spec)
    requested = spec.model_dump(mode="json")
    request = {
        "operation": "evaluate_observation",
        "contract_id": contract_id,
        "attempt_id": attempt_id,
        "expected_attempt_revision": expected_attempt_revision,
        "spec": requested,
    }

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        from . import experiments
        from .method_registry import _owned_method, _record

        contract_row = _contract(con, owner, contract_id)
        contract = _contract_response(contract_row)
        contract_spec = ObservationContractSpec.model_validate(contract["spec"])
        if len({item.analyte_id for item in spec.analytes}) != len(spec.analytes):
            raise ValueError("Evidence must contain at most one row per analyte")
        provided_ids = {item.analyte_id for item in spec.analytes}
        required_ids = set(contract_spec.required_analytes)
        if provided_ids - required_ids:
            raise ValueError("Evidence contains analytes outside the bound contract")

        attempt = experiments._attempt(con, owner, attempt_id)
        if attempt["revision"] != expected_attempt_revision:
            raise ValueError(
                "Attempt revision conflict; retrieve the current revision before evaluation"
            )
        experiment_id, experiment_row = _experiment_contract_link(
            con, owner, attempt["experiment_id"], contract_id
        )
        experiment_spec = json.loads(experiment_row["payload"])
        producer_id = experiment_spec.get("observation_producer_id")

        method_id = attempt["spec"].get("executable_method_id")
        if not isinstance(method_id, str) or not method_id:
            raise ValueError("Observation evaluation requires the attempt's executable method")
        method_row = _owned_method(con, owner, method_id)
        method = _record(method_row)
        method_link = attempt.get("executable_method")
        if (
            not isinstance(method_link, dict)
            or method_link.get("method_id") != method_id
            or method_link.get("input_sha256") != method["input_sha256"]
            or method_link.get("canonical_program_sha256") != method["canonical_program_sha256"]
        ):
            raise ValueError("Attempt executable-method identity is missing or stale")
        canonical_method = method.get("evaluation", {}).get("canonical", {})
        duration = canonical_method.get("acquisition_duration_min")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ValueError("Executable method has no canonical acquisition duration")
        if not math.isfinite(float(duration)) or duration <= 0:
            raise ValueError("Executable method acquisition duration must be finite and positive")

        attempt_fidelity = attempt["spec"].get("fidelity")
        fidelity_check = "unavailable"
        if attempt_fidelity is not None:
            expected_role = "high" if attempt_fidelity >= 0.99 else "low"
            if expected_role != spec.fidelity_role:
                raise ValueError(
                    "Declared fidelity_role conflicts with the attempt fidelity coordinate"
                )
            fidelity_check = "matches_attempt_coordinate"

        evaluator_kind = attempt["spec"].get("evaluator_kind")
        allowed_evaluators = {
            "simulator_latent": {"simulator"},
            "instrument_reported": {"instrument"},
            "manual": {"manual"},
            "archived_replay": {"replay"},
            "processed_signal": {"instrument", "manual"},
        }
        if evaluator_kind not in allowed_evaluators[spec.observation_origin]:
            raise ValueError("Observation origin conflicts with the attempt evaluator kind")

        evidence_rows: list[dict[str, Any]] = []
        kernel_rows: list[AnalyteObservation] = []
        used_result_ids: set[str] = set()
        has_supporting_analysis = False
        for item in spec.analytes:
            retention = None
            resolution = None
            if item.retention_result_id is not None:
                if item.retention_result_id in used_result_ids:
                    raise ValueError("A result may be linked only once in one observation")
                used_result_ids.add(item.retention_result_id)
                retention = _result_record(
                    con,
                    owner,
                    item.retention_result_id,
                    attempt_id=attempt_id,
                    attempt_revision=expected_attempt_revision,
                    analyte_id=item.analyte_id,
                    metric=RETENTION_TIME_METRIC,
                )
            if item.resolution_result_id is not None:
                if item.resolution_result_id in used_result_ids:
                    raise ValueError("A result may be linked only once in one observation")
                used_result_ids.add(item.resolution_result_id)
                resolution = _result_record(
                    con,
                    owner,
                    item.resolution_result_id,
                    attempt_id=attempt_id,
                    attempt_revision=expected_attempt_revision,
                    analyte_id=item.analyte_id,
                    metric=RESOLUTION_METRIC,
                )
            for result in (retention, resolution):
                if result and result["outcome"].get("provenance", {}).get("analysis_id"):
                    has_supporting_analysis = True
            retention_status, retention_time = _retention_status(retention)
            resolution_status, resolution_value = _resolution_status(resolution)
            kernel_rows.append(
                AnalyteObservation(
                    analyte_id=item.analyte_id,
                    assignment=item.assignment,
                    retention_status=retention_status,
                    retention_time_min=retention_time,
                    resolution_status=resolution_status,
                    resolution=resolution_value,
                )
            )
            evidence_rows.append(
                {
                    "analyte_id": item.analyte_id,
                    "assignment": item.assignment,
                    "assignment_evidence": item.assignment_evidence,
                    "retention_result": retention,
                    "resolution_result": resolution,
                }
            )
        if spec.observation_origin == "processed_signal" and not has_supporting_analysis:
            raise ValueError(
                "processed_signal evidence requires a linked supporting analysis result"
            )

        producer_evidence = None
        if producer_id is not None:
            from .gaussian_producer import validate_gaussian_result_evidence

            if spec.observation_origin != "simulator_latent":
                raise ValueError(
                    "The experiment's Gaussian producer requires simulator_latent origin"
                )
            producer_evidence = validate_gaussian_result_evidence(
                con,
                owner,
                producer_id,
                attempt_id,
                expected_attempt_revision,
                [
                    result
                    for row in evidence_rows
                    for result in (row["retention_result"], row["resolution_result"])
                    if result is not None
                ],
            )
            # The immutable producer run retains shared source evidence once.
            # Keep exact result identities and normalized values here without
            # repeating that complete provenance for every analyte and metric.
            # input_sha256 still hashes the full immutable outcome retrievable
            # with measurement_get_result, not this compact reference.
            for row in evidence_rows:
                for field in ("retention_result", "resolution_result"):
                    result = row[field]
                    if result is not None:
                        row[field] = {
                            **{key: value for key, value in result.items() if key != "outcome"},
                            "outcome_included": False,
                        }

        assessment = assess_analytical_observation(
            required_analytes=contract_spec.required_analytes,
            observations=kernel_rows,
            acquisition_duration_min=duration,
            max_acquisition_duration_min=contract_spec.max_acquisition_duration_min,
            resolution_threshold=contract_spec.resolution_threshold,
            runtime_preference_min=contract_spec.runtime_preference_min,
        )
        evidence = {
            "fidelity_role": spec.fidelity_role,
            "attempt_fidelity_coordinate": attempt_fidelity,
            "fidelity_role_check": fidelity_check,
            "observation_origin": spec.observation_origin,
            "attempt_evaluator_kind": evaluator_kind,
            "analytes": evidence_rows,
            "required_analytes_without_declared_evidence": [
                item for item in contract_spec.required_analytes if item not in provided_ids
            ],
        }
        if producer_id is not None:
            evidence["producer_evidence"] = producer_evidence
        payload = {
            "schema_version": SCHEMA_VERSION,
            "contract": {
                "contract_id": contract_id,
                "input_sha256": contract_row["input_digest"],
                "profile": contract_spec.profile,
                "resolution_definition": contract_spec.resolution_definition,
                "utility_profile": contract_spec.utility_profile,
                "utility_input_definition": contract_spec.utility_input_definition,
            },
            "experiment_id": experiment_id,
            "attempt": {
                "attempt_id": attempt_id,
                "experiment_id": experiment_id,
                "revision": attempt["revision"],
                "input_sha256": attempt["input_sha256"],
                "spec": attempt["spec"],
                "executable_method_link": attempt["executable_method"],
            },
            "executable_method": {
                "method_id": method_id,
                "input_sha256": method["input_sha256"],
                "canonical_program_sha256": method["canonical_program_sha256"],
                "acquisition_duration_min": duration,
            },
            "input_spec": requested,
            "evidence": evidence,
            "assessment": assessment,
            "implementation_provenance": _implementation_provenance(),
        }
        _bounded(payload)
        observation_id = str(uuid.uuid4())
        created = now()
        input_digest = sha256(request)
        evidence_digest = sha256(evidence)
        assessment_digest = sha256(assessment)
        con.execute(
            """INSERT INTO analytical_observations (
              id, owner, contract_id, contract_input_digest, experiment_id, attempt_id,
              attempt_revision, executable_method_id, method_input_digest,
              canonical_program_digest, payload, input_digest, evidence_digest,
              assessment_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                observation_id,
                owner,
                contract_id,
                contract_row["input_digest"],
                experiment_id,
                attempt_id,
                attempt["revision"],
                method_id,
                method["input_sha256"],
                method["canonical_program_sha256"],
                canonical(payload),
                input_digest,
                evidence_digest,
                assessment_digest,
                created,
            ),
        )
        return _observation_response(
            con.execute(
                "SELECT * FROM analytical_observations WHERE id=?", (observation_id,)
            ).fetchone()
        )

    return registry_mutation(owner, key, request, write)


def _observation_response(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload"])
    input_request = {
        "operation": "evaluate_observation",
        "contract_id": row["contract_id"],
        "attempt_id": row["attempt_id"],
        "expected_attempt_revision": row["attempt_revision"],
        "spec": payload["input_spec"],
    }
    if sha256(input_request) != row["input_digest"]:
        raise ValueError("Stored observation input digest does not match its contents")
    if sha256(payload["evidence"]) != row["evidence_digest"]:
        raise ValueError("Stored observation evidence digest does not match its contents")
    if sha256(payload["assessment"]) != row["assessment_digest"]:
        raise ValueError("Stored observation assessment digest does not match its contents")
    return _bounded(
        {
            "schema_version": SCHEMA_VERSION,
            "observation_id": row["id"],
            "contract_id": row["contract_id"],
            "experiment_id": row["experiment_id"],
            "attempt_id": row["attempt_id"],
            "attempt_revision": row["attempt_revision"],
            "observation": payload,
            "input_sha256": row["input_digest"],
            "evidence_sha256": row["evidence_digest"],
            "assessment_sha256": row["assessment_digest"],
            "created_at": row["created_at"],
        }
    )


def get_observation(owner: str, observation_id: str) -> dict[str, Any]:
    with registry_connection() as con:
        row = con.execute(
            "SELECT * FROM analytical_observations WHERE id=? AND owner=?",
            (observation_id, owner),
        ).fetchone()
        if row is None:
            raise ValueError("Analytical observation was not found or is not accessible")
        return _observation_response(row)


def create_observation_snapshot(
    owner: str, contract_id: str, observation_ids: list[str], key: str
) -> dict[str, Any]:
    if (
        not isinstance(observation_ids, list)
        or not 1 <= len(observation_ids) <= MAX_SNAPSHOT_OBSERVATIONS
    ):
        raise ValueError("A snapshot must select between 1 and 50 analytical observations")
    if any(not isinstance(value, str) or not value.strip() for value in observation_ids):
        raise ValueError("Snapshot observation IDs must be nonblank strings")
    # Canonicalize order and repeated references before hashing the mutation request.
    selected_ids = sorted(set(observation_ids))
    request = {
        "operation": "create_observation_snapshot",
        "contract_id": contract_id,
        "observation_ids": selected_ids,
    }

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        contract_row = _contract(con, owner, contract_id)
        members = []
        seen_attempts = set()
        for observation_id in selected_ids:
            row = con.execute(
                "SELECT * FROM analytical_observations WHERE id=? AND owner=?",
                (observation_id, owner),
            ).fetchone()
            if row is None:
                raise ValueError("Snapshot observation was not found or is not accessible")
            if (
                row["contract_id"] != contract_id
                or row["contract_input_digest"] != contract_row["input_digest"]
            ):
                raise ValueError("Snapshot members must use the selected immutable contract")
            if row["attempt_id"] in seen_attempts:
                raise ValueError(
                    "A snapshot may include at most one observation per physical attempt"
                )
            seen_attempts.add(row["attempt_id"])
            members.append(
                {
                    "observation_id": row["id"],
                    "attempt_id": row["attempt_id"],
                    "attempt_revision": row["attempt_revision"],
                    "input_sha256": row["input_digest"],
                    "evidence_sha256": row["evidence_digest"],
                    "assessment_sha256": row["assessment_digest"],
                }
            )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "contract_id": contract_id,
            "contract_input_sha256": contract_row["input_digest"],
            "members": members,
        }
        _bounded(payload)
        snapshot_id, created = str(uuid.uuid4()), now()
        digest = sha256(payload)
        con.execute(
            "INSERT INTO observation_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                owner,
                contract_id,
                contract_row["input_digest"],
                canonical(payload),
                digest,
                created,
            ),
        )
        for ordinal, member in enumerate(members):
            con.execute(
                """INSERT INTO observation_snapshot_members (
                  snapshot_id, owner, ordinal, observation_id, attempt_id, attempt_revision,
                  observation_input_digest, evidence_digest, assessment_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    owner,
                    ordinal,
                    member["observation_id"],
                    member["attempt_id"],
                    member["attempt_revision"],
                    member["input_sha256"],
                    member["evidence_sha256"],
                    member["assessment_sha256"],
                ),
            )
        return _bounded(
            {
                "schema_version": SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "contract_id": contract_id,
                "members": members,
                "sha256": digest,
                "created_at": created,
            }
        )

    return registry_mutation(owner, key, request, write)


def get_observation_snapshot(owner: str, snapshot_id: str) -> dict[str, Any]:
    with registry_connection() as con:
        row = con.execute(
            "SELECT * FROM observation_snapshots WHERE id=? AND owner=?", (snapshot_id, owner)
        ).fetchone()
        if row is None:
            raise ValueError("Observation snapshot was not found or is not accessible")
        payload = json.loads(row["payload"])
        if sha256(payload) != row["digest"]:
            raise ValueError("Stored observation snapshot digest does not match its contents")
        members = [
            {
                "observation_id": member["observation_id"],
                "attempt_id": member["attempt_id"],
                "attempt_revision": member["attempt_revision"],
                "input_sha256": member["observation_input_digest"],
                "evidence_sha256": member["evidence_digest"],
                "assessment_sha256": member["assessment_digest"],
            }
            for member in con.execute(
                "SELECT * FROM observation_snapshot_members WHERE snapshot_id=? AND owner=? ORDER BY ordinal",
                (snapshot_id, owner),
            )
        ]
        if members != payload.get("members"):
            raise ValueError("Stored observation snapshot membership does not match its contents")
        return _bounded(
            {
                "schema_version": SCHEMA_VERSION,
                "snapshot_id": row["id"],
                "contract_id": row["contract_id"],
                "members": members,
                "sha256": row["digest"],
                "created_at": row["created_at"],
            }
        )
