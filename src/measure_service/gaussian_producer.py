"""Owner-scoped bindings from ideal retention predictions to latent Gaussian results.

The producer consumes an immutable retention prediction and explicit synthetic
FWHM assumptions. It stores model-derived centroid and resolution evidence; it
does not create a trace, identify chemicals, or qualify an instrument.
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

from measure_methods.chromatography import gaussian_pairwise_resolutions
from measure_methods.gaussian_observations import (
    RESOLUTION_DEFINITION,
    GaussianAnalyte,
    simulate_gaussian_centroids,
)

from .method_backend import method_backend_info
from .storage import canonical, now, registry_connection, registry_mutation, sha256

SCHEMA_VERSION = "1.0"
PROFILE = "ideal-gaussian-centroid/1"
MAX_ANALYTES = 32
MAX_RECORD_BYTES = 64_000
RETENTION_TIME_METRIC = "retention_time"
RESOLUTION_METRIC = "resolution"
_NUMBER = StrictFloat | StrictInt
_UNSUPPORTED_REASON = "no_adjacent_finite_centroid"
_CENSORED_REASON = "retention_not_eluted_within_acquisition_duration"


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, validate_default=True
    )

    @field_validator("*")
    @classmethod
    def reject_whitespace_only_text(cls, value: Any) -> Any:
        if isinstance(value, str) and value and not value.strip():
            raise ValueError("Text fields must not consist only of whitespace")
        return value


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite numeric value")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite and representable") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


class GaussianWidth(StrictModel):
    """One explicitly declared synthetic Gaussian full width at half maximum."""

    analyte_id: StrictStr = Field(min_length=1, max_length=100)
    fwhm_min: _NUMBER = Field(
        description="Declared synthetic full width at half maximum, in minutes."
    )

    @field_validator("analyte_id")
    @classmethod
    def analyte_id_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("analyte_id must be nonblank")
        return value

    @model_validator(mode="after")
    def validate_width(self) -> GaussianWidth:
        if _finite_number(self.fwhm_min, "fwhm_min") <= 0:
            raise ValueError("fwhm_min must be positive")
        return self


class GaussianProducerSpec(StrictModel):
    """Fixed ideal-centroid profile and a complete cohort of synthetic widths."""

    profile: Literal["ideal-gaussian-centroid/1"] = PROFILE
    resolution_definition: Literal["gaussian-baseline-4sigma-finite-centroids/1"] = (
        RESOLUTION_DEFINITION
    )
    widths: list[GaussianWidth] = Field(min_length=1, max_length=MAX_ANALYTES)
    width_origin: Literal["synthetic_assumption"]
    width_reference: StrictStr = Field(min_length=1, max_length=500)

    @field_validator("width_reference")
    @classmethod
    def width_reference_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("width_reference must be nonblank")
        return value

    @model_validator(mode="after")
    def validate_cohort(self) -> GaussianProducerSpec:
        identifiers = [item.analyte_id for item in self.widths]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Gaussian width analyte IDs must be unique")
        return self


def _bounded(value: Any) -> Any:
    if len(canonical(value).encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("Gaussian producer record exceeds the 64,000-byte limit")
    return value


def _source_hash(function: Any) -> str:
    source = inspect.getsourcefile(function)
    if source is None:
        raise RuntimeError("Gaussian producer implementation source identity is unavailable")
    return hashlib.sha256(Path(source).read_bytes()).hexdigest()


def _implementation_provenance() -> dict[str, Any]:
    return {
        "measure_service": method_backend_info(),
        "service_adapter": {
            "module": "measure_service.gaussian_producer",
            "source_sha256": _source_hash(record_gaussian_observation),
        },
        "gaussian_kernel": {
            "module": "measure_methods.gaussian_observations",
            "source_sha256": _source_hash(simulate_gaussian_centroids),
        },
        "resolution_helper": {
            "module": "measure_methods.chromatography",
            "source_sha256": _source_hash(gaussian_pairwise_resolutions),
        },
    }


def _producer_row(con: sqlite3.Connection, owner: str, producer_id: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM gaussian_producers WHERE id=? AND owner=?", (producer_id, owner)
    ).fetchone()
    if row is None:
        raise ValueError("Gaussian producer was not found or is not accessible")
    return row


def _producer_request(row: sqlite3.Row, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": "register_gaussian_producer",
        "title": row["title"],
        "retention_model_id": row["retention_model_id"],
        "spec": spec,
    }


def _producer_response(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload"])
    spec = payload.get("spec")
    model_link = payload.get("retention_model")
    if not isinstance(spec, dict) or not isinstance(model_link, dict):
        raise ValueError("Stored Gaussian producer payload is malformed")
    declared_spec = GaussianProducerSpec.model_validate(spec).model_dump(mode="json")
    if declared_spec != spec:
        raise ValueError("Stored Gaussian producer specification is not canonical")
    if sha256(_producer_request(row, spec)) != row["input_digest"]:
        raise ValueError("Stored Gaussian producer input digest does not match its contents")
    output_payload = {key: value for key, value in payload.items() if key != "output_sha256"}
    output_digest = sha256(output_payload)
    if payload.get("output_sha256") != output_digest:
        raise ValueError("Stored Gaussian producer output digest does not match its contents")
    if (
        model_link.get("model_id") != row["retention_model_id"]
        or model_link.get("input_sha256") != row["retention_model_input_digest"]
    ):
        raise ValueError("Stored Gaussian producer retention-model identity is inconsistent")
    return _bounded(
        {
            "schema_version": SCHEMA_VERSION,
            "producer_id": row["id"],
            "title": row["title"],
            "spec": spec,
            "retention_model": model_link,
            "implementation_provenance": payload["implementation_provenance"],
            "input_sha256": row["input_digest"],
            "output_sha256": output_digest,
            "created_at": row["created_at"],
        }
    )


def _validate_model_cohort(spec: GaussianProducerSpec, model_spec: dict[str, Any]) -> None:
    model_ids = [item["analyte_id"] for item in model_spec["analytes"]]
    width_ids = [item.analyte_id for item in spec.widths]
    if set(model_ids) != set(width_ids) or len(model_ids) != len(width_ids):
        raise ValueError("Synthetic Gaussian widths must exactly match the retention-model cohort")


def register_gaussian_producer(
    owner: str,
    title: str,
    retention_model_id: str,
    spec: GaussianProducerSpec | dict[str, Any],
    key: str,
) -> dict[str, Any]:
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("A Gaussian producer title of 1 to 200 characters is required")
    if not isinstance(spec, GaussianProducerSpec):
        spec = GaussianProducerSpec.model_validate(spec)
    requested = spec.model_dump(mode="json")
    request = {
        "operation": "register_gaussian_producer",
        "title": title,
        "retention_model_id": retention_model_id,
        "spec": requested,
    }

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        from . import retention

        model_row = retention._model_row(con, owner, retention_model_id)
        model = retention._model_response(model_row)
        model_spec = model["spec"]
        if model_spec.get("parameter_origin") != "synthetic":
            raise ValueError("Gaussian latent production requires a synthetic retention model")
        _validate_model_cohort(spec, model_spec)
        retained = {
            "spec": requested,
            "retention_model": {
                "model_id": model_row["id"],
                "input_sha256": model_row["input_digest"],
            },
            "implementation_provenance": _implementation_provenance(),
        }
        retained["output_sha256"] = sha256(retained)
        _bounded(retained)
        producer_id = str(uuid.uuid4())
        created = now()
        input_digest = sha256(request)
        con.execute(
            "INSERT INTO gaussian_producers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                producer_id,
                owner,
                title,
                model_row["id"],
                model_row["input_digest"],
                canonical(retained),
                input_digest,
                created,
            ),
        )
        return _producer_response(_producer_row(con, owner, producer_id))

    return registry_mutation(owner, key, request, write)


def get_gaussian_producer(owner: str, producer_id: str) -> dict[str, Any]:
    with registry_connection() as con:
        return _producer_response(_producer_row(con, owner, producer_id))


def gaussian_producer_link(
    con: sqlite3.Connection, owner: str, producer_id: str, contract_id: str
) -> dict[str, str]:
    """Resolve and cohort-check an immutable producer before an experiment starts."""
    from . import observations

    producer_row = _producer_row(con, owner, producer_id)
    producer = _producer_response(producer_row)
    contract_row = observations._contract(con, owner, contract_id)
    contract = observations._contract_response(contract_row)
    producer_spec = GaussianProducerSpec.model_validate(producer["spec"])
    required = contract["spec"]["required_analytes"]
    width_ids = [item.analyte_id for item in producer_spec.widths]
    if set(required) != set(width_ids) or len(required) != len(width_ids):
        raise ValueError(
            "Gaussian producer widths must exactly match the observation contract cohort"
        )
    if contract["spec"]["resolution_definition"] != producer_spec.resolution_definition:
        raise ValueError(
            "Gaussian producer resolution definition must match the observation contract"
        )
    if producer_row["retention_model_id"]:
        from . import retention

        model_row = retention._model_row(con, owner, producer_row["retention_model_id"])
        if model_row["input_digest"] != producer_row["retention_model_input_digest"]:
            raise ValueError("Gaussian producer retention-model link is stale")
    return {
        "producer_id": producer_row["id"],
        "input_sha256": producer_row["input_digest"],
        "output_sha256": producer["output_sha256"],
    }


def _validate_producer_experiment(
    con: sqlite3.Connection, owner: str, experiment_id: str, producer_id: str
) -> tuple[sqlite3.Row, dict[str, Any]]:
    from .experiments import _owned

    experiment_row = _owned(con, "experiments", owner, experiment_id)
    payload = json.loads(experiment_row["payload"])
    contract_id = payload.get("observation_contract_id")
    link = payload.get("observation_producer")
    if (
        payload.get("observation_producer_id") != producer_id
        or not isinstance(link, dict)
        or link.get("producer_id") != producer_id
    ):
        raise ValueError("Experiment is not bound to the selected Gaussian producer")
    producer = _producer_response(_producer_row(con, owner, producer_id))
    if link.get("input_sha256") != producer["input_sha256"]:
        raise ValueError("Experiment Gaussian-producer link is stale")
    if not isinstance(contract_id, str) or not contract_id:
        raise ValueError("Gaussian-producer experiments require an observation contract")
    expected = gaussian_producer_link(con, owner, producer_id, contract_id)
    if link != expected:
        raise ValueError("Experiment Gaussian-producer link does not match its contract")
    return experiment_row, producer


def _finite_nonnegative(value: object, name: str) -> float:
    number = _finite_number(value, name)
    if number < 0:
        raise ValueError(f"{name} must be nonnegative")
    return number


def _validate_kernel_output(
    output: dict[str, Any],
    *,
    duration: float,
    retention_by_id: dict[str, dict[str, Any]],
    widths_by_id: dict[str, float],
) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise ValueError("Gaussian kernel returned a malformed output")
    if (
        output.get("profile") != PROFILE
        or output.get("resolution_definition") != RESOLUTION_DEFINITION
        or output.get("acquisition_duration_min") != duration
        or output.get("fwhm_per_sigma") != 2.3548
    ):
        raise ValueError("Gaussian kernel profile, units or numerical convention is inconsistent")
    analytes = output.get("analytes")
    pairs = output.get("pairs")
    if not isinstance(analytes, list) or not isinstance(pairs, list):
        raise ValueError("Gaussian kernel output must include analyte and pair arrays")
    scope = output.get("scope")
    if (
        not isinstance(scope, dict)
        or scope.get("adjacency")
        != "adjacent_finite_eluted_centroids_ordered_by_retention_time_then_analyte_id"
        or scope.get("analyte_resolution") != "minimum_of_incident_adjacent_pair_resolutions"
        or scope.get("widths")
        != "caller_supplied_synthetic_fwhm; fwhm_per_sigma_approximation_2.3548"
        or scope.get("interpretation")
        != "ideal_latent_model_only_not_trace_simulation_or_physical_qualification"
    ):
        raise ValueError("Gaussian kernel interpretation scope is inconsistent")
    expected_ids = set(retention_by_id)
    observed_rows: dict[str, dict[str, Any]] = {}
    for item in analytes:
        if not isinstance(item, dict):
            raise ValueError("Gaussian kernel analyte row is malformed")
        analyte_id = item.get("analyte_id")
        if not isinstance(analyte_id, str) or analyte_id in observed_rows:
            raise ValueError("Gaussian kernel analyte IDs are missing or duplicated")
        if analyte_id not in expected_ids:
            raise ValueError("Gaussian kernel returned an analyte outside the registered cohort")
        retention = retention_by_id[analyte_id]
        result = retention.get("result")
        if not isinstance(result, dict):
            raise ValueError("Stored retention prediction analyte result is malformed")
        expected_retention_status = result.get("status")
        expected_retention_time = result.get("retention_time_min")
        expected_status = "eluted" if expected_retention_status == "eluted" else "not_eluted"
        if item.get("retention_status") != expected_status:
            raise ValueError("Gaussian kernel retention status differs from its stored prediction")
        if item.get("retention_time_min") != expected_retention_time:
            raise ValueError("Gaussian kernel centroid differs from its stored prediction")
        if item.get("fwhm_min") != widths_by_id[analyte_id]:
            raise ValueError(
                "Gaussian kernel width differs from the registered synthetic assumption"
            )
        resolution_status = item.get("resolution_status")
        resolution = item.get("resolution")
        reason = item.get("resolution_reason")
        censor = item.get("right_censor_bound_min")
        if resolution_status == "observed":
            _finite_nonnegative(resolution, "Gaussian resolution")
            if reason is not None or censor is not None or expected_status != "eluted":
                raise ValueError("Observed Gaussian resolution has invalid reason or censor bound")
        elif resolution_status == "unsupported":
            if (
                resolution is not None
                or reason != _UNSUPPORTED_REASON
                or censor is not None
                or expected_status != "eluted"
            ):
                raise ValueError("Unsupported Gaussian resolution row is inconsistent")
        elif resolution_status == "censored":
            if (
                resolution is not None
                or reason != _CENSORED_REASON
                or censor != duration
                or expected_status != "not_eluted"
            ):
                raise ValueError("Censored Gaussian resolution row is inconsistent")
        else:
            raise ValueError("Gaussian kernel returned an unsupported resolution status")
        observed_rows[analyte_id] = item
    if set(observed_rows) != expected_ids:
        raise ValueError("Gaussian kernel omitted a registered analyte")

    ordered_eluted = sorted(
        (item for item in observed_rows.values() if item["retention_status"] == "eluted"),
        key=lambda item: (item["retention_time_min"], item["analyte_id"]),
    )
    expected_pairs = {
        tuple(sorted((left["analyte_id"], right["analyte_id"])))
        for left, right in zip(ordered_eluted, ordered_eluted[1:])
    }
    if len(pairs) > max(0, len(expected_ids) - 1):
        raise ValueError("Gaussian kernel returned too many adjacent centroid pairs")
    pair_keys: set[tuple[str, str]] = set()
    incident: dict[str, list[float]] = {item: [] for item in expected_ids}
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("Gaussian kernel pair row is malformed")
        identifiers = pair.get("analyte_ids")
        if (
            not isinstance(identifiers, list)
            or len(identifiers) != 2
            or identifiers[0] == identifiers[1]
            or identifiers != sorted(identifiers)
            or any(item not in expected_ids for item in identifiers)
            or pair.get("resolution_status") != "observed"
            or pair.get("right_censor_bound_min") is not None
        ):
            raise ValueError("Gaussian kernel pair identity or status is inconsistent")
        pair_key = (identifiers[0], identifiers[1])
        if pair_key in pair_keys:
            raise ValueError("Gaussian kernel returned a duplicate pair")
        pair_keys.add(pair_key)
        value = _finite_nonnegative(pair.get("resolution"), "Gaussian pair resolution")
        incident[identifiers[0]].append(value)
        incident[identifiers[1]].append(value)
    for analyte_id, values in incident.items():
        row = observed_rows[analyte_id]
        if values:
            if row["resolution_status"] != "observed" or row["resolution"] != min(values):
                raise ValueError("Per-analyte resolution does not match its incident pair rows")
        elif row["resolution_status"] == "observed":
            raise ValueError("An observed analyte resolution has no adjacent finite pair")
        elif row["resolution_status"] == "unsupported" and row["retention_status"] != "eluted":
            raise ValueError("Unsupported resolution is only valid for an eluted analyte")
    if pair_keys != expected_pairs:
        raise ValueError("Gaussian kernel pair rows do not match adjacent finite centroids")
    return output


def _attempt_method_identity(
    con: sqlite3.Connection, owner: str, attempt: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .method_registry import _owned_method, _record

    method_id = attempt["spec"].get("executable_method_id")
    method_link = attempt.get("executable_method")
    if not isinstance(method_id, str) or not isinstance(method_link, dict):
        raise ValueError("Gaussian latent production requires the attempt's executable method")
    row = _owned_method(con, owner, method_id)
    method = _record(row)
    canonical_program = method.get("evaluation", {}).get("canonical")
    if (
        not isinstance(canonical_program, dict)
        or sha256(canonical_program) != method["canonical_program_sha256"]
    ):
        raise ValueError("Executable method canonical-program digest does not match its contents")
    if (
        method_link.get("method_id") != method_id
        or method_link.get("input_sha256") != method["input_sha256"]
        or method_link.get("canonical_program_sha256") != method["canonical_program_sha256"]
    ):
        raise ValueError("Attempt executable-method identity is missing or stale")
    return row, method


def _current_result_conflicts(
    con: sqlite3.Connection, owner: str, attempt_id: str, analyte_ids: list[str]
) -> None:
    for metric in (RETENTION_TIME_METRIC, RESOLUTION_METRIC):
        for analyte_id in analyte_ids:
            row = con.execute(
                """SELECT r.id FROM experiment_results r WHERE r.owner=? AND r.attempt_id=?
                AND r.metric=? AND r.analyte_reference=?
                AND NOT EXISTS(SELECT 1 FROM experiment_results next WHERE next.supersedes_id=r.id)""",
                (owner, attempt_id, metric, analyte_id),
            ).fetchone()
            if row is not None:
                raise ValueError(
                    "A current result already exists for this Gaussian producer metric/analyte"
                )


def _result_specs(
    kernel_output: dict[str, Any], fidelity_role: str
) -> tuple[list[Any], list[dict[str, Any]], dict[str, Any]]:
    from .experiments import ResultSpec
    from .observations import ObservationEvidenceSpec

    specs: list[ResultSpec] = []
    links: list[dict[str, Any]] = []
    result_ids_by_analyte: dict[str, dict[str, str]] = {}
    duration = kernel_output["acquisition_duration_min"]
    for analyte in kernel_output["analytes"]:
        analyte_id = analyte["analyte_id"]
        if analyte["retention_status"] == "eluted":
            retention = ResultSpec(
                metric=RETENTION_TIME_METRIC,
                analyte_reference=analyte_id,
                status="observed",
                unit="min",
                value=analyte["retention_time_min"],
                explanation="",
            )
        else:
            retention = ResultSpec(
                metric=RETENTION_TIME_METRIC,
                analyte_reference=analyte_id,
                status="censored",
                unit="min",
                lower_bound=duration,
                explanation="Ideal retention prediction was not eluted within the declared acquisition duration.",
            )
        if analyte["resolution_status"] == "observed":
            resolution = ResultSpec(
                metric=RESOLUTION_METRIC,
                analyte_reference=analyte_id,
                status="observed",
                unit="1",
                value=analyte["resolution"],
                explanation="",
            )
        else:
            resolution = ResultSpec(
                metric=RESOLUTION_METRIC,
                analyte_reference=analyte_id,
                status="missing",
                unit="1",
                explanation=(
                    "Ideal Gaussian resolution is unavailable: "
                    f"{analyte['resolution_status']} ({analyte['resolution_reason']})."
                ),
            )
        result_ids_by_analyte[analyte_id] = {}
        for metric, spec in (
            (RETENTION_TIME_METRIC, retention),
            (RESOLUTION_METRIC, resolution),
        ):
            result_id = str(uuid.uuid4())
            specs.append(spec)
            result_ids_by_analyte[analyte_id][metric] = result_id
            links.append(
                {
                    "result_id": result_id,
                    "metric": metric,
                    "analyte_reference": analyte_id,
                    "outcome": spec.model_dump(mode="json"),
                }
            )
    evidence_spec = {
        "fidelity_role": fidelity_role,
        "observation_origin": "simulator_latent",
        "analytes": [
            {
                "analyte_id": analyte["analyte_id"],
                "assignment": "confirmed",
                "assignment_evidence": (
                    "Synthetic latent model analyte identity; this is not a verified chemical identity."
                ),
                "retention_result_id": result_ids_by_analyte[analyte["analyte_id"]][
                    RETENTION_TIME_METRIC
                ],
                "resolution_result_id": result_ids_by_analyte[analyte["analyte_id"]][
                    RESOLUTION_METRIC
                ],
            }
            for analyte in kernel_output["analytes"]
        ],
    }
    return (
        specs,
        links,
        ObservationEvidenceSpec.model_validate(evidence_spec).model_dump(mode="json"),
    )


def _observation_request(
    producer_id: str, prediction_id: str, attempt_id: str, attempt_revision: int
) -> dict[str, Any]:
    return {
        "operation": "record_gaussian_observation",
        "producer_id": producer_id,
        "prediction_id": prediction_id,
        "attempt_id": attempt_id,
        "expected_attempt_revision": attempt_revision,
    }


def _observation_response(
    row: sqlite3.Row, con: sqlite3.Connection | None = None
) -> dict[str, Any]:
    payload = json.loads(row["payload"])
    artifact = payload.get("artifact")
    if not isinstance(artifact, dict) or sha256(artifact) != row["output_digest"]:
        raise ValueError("Stored Gaussian observation output digest does not match its contents")
    if payload.get("output_sha256") != row["output_digest"]:
        raise ValueError("Stored Gaussian observation output identity is inconsistent")
    request = _observation_request(
        row["producer_id"],
        row["retention_prediction_id"],
        row["attempt_id"],
        row["attempt_revision"],
    )
    if sha256(request) != row["input_digest"]:
        raise ValueError("Stored Gaussian observation input digest does not match its contents")
    producer_link = artifact.get("producer")
    prediction_link = artifact.get("prediction")
    attempt_link = artifact.get("attempt")
    method_link = artifact.get("method")
    if not all(
        isinstance(item, dict)
        for item in (producer_link, prediction_link, attempt_link, method_link)
    ):
        raise ValueError("Stored Gaussian observation identity links are malformed")
    identity_checks = (
        (producer_link.get("producer_id"), row["producer_id"]),
        (producer_link.get("input_sha256"), row["producer_input_digest"]),
        (prediction_link.get("prediction_id"), row["retention_prediction_id"]),
        (prediction_link.get("input_sha256"), row["prediction_input_digest"]),
        (prediction_link.get("output_sha256"), row["prediction_output_digest"]),
        (attempt_link.get("attempt_id"), row["attempt_id"]),
        (attempt_link.get("revision"), row["attempt_revision"]),
        (attempt_link.get("input_sha256"), row["attempt_digest"]),
        (method_link.get("method_id"), row["executable_method_id"]),
        (method_link.get("input_sha256"), row["method_input_digest"]),
        (method_link.get("canonical_program_sha256"), row["canonical_program_digest"]),
    )
    if any(actual != expected for actual, expected in identity_checks):
        raise ValueError("Stored Gaussian observation identities do not match their contents")
    if con is not None:
        from . import retention

        producer = _producer_response(_producer_row(con, row["owner"], row["producer_id"]))
        prediction_row = con.execute(
            "SELECT * FROM retention_predictions WHERE id=? AND owner=?",
            (row["retention_prediction_id"], row["owner"]),
        ).fetchone()
        if prediction_row is None:
            raise ValueError("Stored Gaussian observation prediction is missing")
        prediction = retention._prediction_response(prediction_row)
        from .experiments import _attempt

        attempt = _attempt(con, row["owner"], row["attempt_id"], row["attempt_revision"])
        experiment_row, _ = _validate_producer_experiment(
            con, row["owner"], row["experiment_id"], row["producer_id"]
        )
        method_row, method = _attempt_method_identity(con, row["owner"], attempt)
        if (
            producer["input_sha256"] != row["producer_input_digest"]
            or producer["output_sha256"] != producer_link.get("output_sha256")
            or prediction["input_sha256"] != row["prediction_input_digest"]
            or prediction["output_sha256"] != row["prediction_output_digest"]
            or attempt["input_sha256"] != row["attempt_digest"]
            or attempt["experiment_id"] != experiment_row["id"]
            or method_row["id"] != row["executable_method_id"]
            or method["input_sha256"] != row["method_input_digest"]
            or method["canonical_program_sha256"] != row["canonical_program_digest"]
        ):
            raise ValueError("Stored Gaussian observation source identities are stale")
    return _bounded(
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": row["id"],
            "producer_id": row["producer_id"],
            "retention_prediction_id": row["retention_prediction_id"],
            "experiment_id": row["experiment_id"],
            "attempt_id": row["attempt_id"],
            "attempt_revision": row["attempt_revision"],
            "executable_method_id": row["executable_method_id"],
            "observation": artifact["kernel_output"],
            "evidence_spec": artifact["evidence_spec"],
            "generated_results": artifact["generated_results"],
            "producer": artifact["producer"],
            "prediction": artifact["prediction"],
            "attempt": artifact["attempt"],
            "method": artifact["method"],
            "implementation_provenance": artifact["implementation_provenance"],
            "input_sha256": row["input_digest"],
            "output_sha256": row["output_digest"],
            "created_at": row["created_at"],
        }
    )


def record_gaussian_observation(
    owner: str,
    producer_id: str,
    prediction_id: str,
    attempt_id: str,
    expected_attempt_revision: int,
    key: str,
) -> dict[str, Any]:
    if (
        isinstance(expected_attempt_revision, bool)
        or not isinstance(expected_attempt_revision, int)
        or expected_attempt_revision < 1
    ):
        raise ValueError("expected_attempt_revision must be a positive integer")
    request = _observation_request(
        producer_id, prediction_id, attempt_id, expected_attempt_revision
    )

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        from . import experiments, retention

        producer_row = _producer_row(con, owner, producer_id)
        producer = _producer_response(producer_row)
        producer_spec = GaussianProducerSpec.model_validate(producer["spec"])
        model_row = retention._model_row(con, owner, producer_row["retention_model_id"])
        model = retention._model_response(model_row)
        if (
            model_row["input_digest"] != producer_row["retention_model_input_digest"]
            or model["spec"].get("parameter_origin") != "synthetic"
        ):
            raise ValueError("Gaussian producer is not linked to its original synthetic model")
        _validate_model_cohort(producer_spec, model["spec"])

        prediction_row = con.execute(
            "SELECT * FROM retention_predictions WHERE id=? AND owner=?",
            (prediction_id, owner),
        ).fetchone()
        if prediction_row is None:
            raise ValueError("Retention prediction was not found or is not accessible")
        prediction_record = retention._prediction_response(prediction_row)
        prediction = prediction_record["prediction"]
        if (
            prediction_row["retention_model_id"] != producer_row["retention_model_id"]
            or prediction_row["model_input_digest"] != producer_row["retention_model_input_digest"]
        ):
            raise ValueError("Retention prediction does not use the producer's bound model")

        attempt = experiments._attempt(con, owner, attempt_id)
        if attempt["revision"] != expected_attempt_revision:
            raise ValueError(
                "Attempt revision conflict; retrieve the current revision before producing an observation"
            )
        if attempt["spec"].get("status") != "completed":
            raise ValueError("Gaussian latent production requires a completed simulator attempt")
        if attempt["spec"].get("evaluator_kind") != "simulator":
            raise ValueError("Gaussian latent production requires a simulator attempt")
        fidelity = attempt["spec"].get("fidelity")
        if fidelity is None:
            raise ValueError("Gaussian latent production requires a declared fidelity coordinate")
        fidelity = _finite_number(fidelity, "attempt fidelity")
        fidelity_role = "high" if fidelity >= 0.99 else "low"
        experiment_row, _ = _validate_producer_experiment(
            con, owner, attempt["experiment_id"], producer_id
        )
        attempt_method_row, method = _attempt_method_identity(con, owner, attempt)
        if (
            prediction_row["method_id"] != attempt_method_row["id"]
            or prediction_row["method_input_digest"] != method["input_sha256"]
            or prediction_row["canonical_program_digest"] != method["canonical_program_sha256"]
        ):
            raise ValueError("Retention prediction method differs from the actual attempt method")
        prediction_method = prediction.get("method", {})
        canonical_program = method.get("evaluation", {}).get("canonical", {})
        duration = canonical_program.get("acquisition_duration_min")
        if (
            prediction_method.get("canonical_program_sha256") != method["canonical_program_sha256"]
            or prediction_method.get("canonical_program") != canonical_program
            or prediction_method.get("method_id") != attempt_method_row["id"]
        ):
            raise ValueError("Retention prediction canonical method identity is stale")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or duration <= 0
            or prediction_method.get("canonical_program", {}).get("acquisition_duration_min")
            != duration
        ):
            raise ValueError("Actual method has no valid canonical acquisition duration")
        duration = float(duration)

        expected_ids = {item.analyte_id for item in producer_spec.widths}
        width_by_id = {item.analyte_id: float(item.fwhm_min) for item in producer_spec.widths}
        predicted_rows = prediction.get("analytes")
        if not isinstance(predicted_rows, list):
            raise ValueError("Retention prediction has no analyte results")
        retention_by_id: dict[str, dict[str, Any]] = {}
        for item in predicted_rows:
            if not isinstance(item, dict) or item.get("analyte_id") in retention_by_id:
                raise ValueError("Retention prediction analyte rows are malformed or duplicated")
            retention_by_id[item.get("analyte_id")] = item
        if set(retention_by_id) != expected_ids:
            raise ValueError("Retention prediction cohort differs from the Gaussian producer")

        gaussian_inputs = []
        for analyte_id in sorted(expected_ids):
            item = retention_by_id[analyte_id]
            result = item.get("result")
            if not isinstance(result, dict) or result.get("profile") != "ideal-lc-retention-zeta/1":
                raise ValueError("Gaussian producer requires stored ideal-retention results")
            status = result.get("status")
            if status == "eluted":
                retention_time = _finite_nonnegative(
                    result.get("retention_time_min"), "predicted retention time"
                )
                if retention_time > duration:
                    raise ValueError(
                        "Stored retention centroid exceeds the method acquisition duration"
                    )
            elif status == "not_eluted":
                retention_time = None
                if result.get("right_censor_bound_min") != duration:
                    raise ValueError(
                        "Stored retention censor bound differs from the method duration"
                    )
            else:
                raise ValueError("Stored retention prediction has an unsupported event status")
            gaussian_inputs.append(
                GaussianAnalyte(
                    analyte_id=analyte_id,
                    retention_status=status,
                    retention_time_min=retention_time,
                    fwhm_min=width_by_id[analyte_id],
                )
            )
        kernel_output = _validate_kernel_output(
            simulate_gaussian_centroids(
                analytes=gaussian_inputs,
                acquisition_duration_min=duration,
            ),
            duration=duration,
            retention_by_id=retention_by_id,
            widths_by_id=width_by_id,
        )

        prior_run = con.execute(
            """SELECT id FROM gaussian_observations
            WHERE owner=? AND producer_id=? AND attempt_id=? AND attempt_revision=?""",
            (owner, producer_id, attempt_id, expected_attempt_revision),
        ).fetchone()
        if prior_run is not None:
            raise ValueError(
                "A Gaussian producer observation already exists for this attempt revision"
            )
        _current_result_conflicts(con, owner, attempt_id, sorted(expected_ids))

        run_id = str(uuid.uuid4())
        input_digest = sha256(request)
        specs, generated_results, evidence_spec = _result_specs(kernel_output, fidelity_role)
        result_ids = [item["result_id"] for item in generated_results]
        implementation = _implementation_provenance()
        producer_link = {
            "producer_id": producer_row["id"],
            "input_sha256": producer_row["input_digest"],
            "output_sha256": producer["output_sha256"],
        }
        prediction_link = {
            "prediction_id": prediction_row["id"],
            "input_sha256": prediction_row["input_digest"],
            "output_sha256": prediction_row["output_digest"],
            "model_id": prediction_row["retention_model_id"],
            "model_input_sha256": prediction_row["model_input_digest"],
        }
        attempt_link = {
            "attempt_id": attempt_id,
            "revision": expected_attempt_revision,
            "input_sha256": attempt["input_sha256"],
            "fidelity_coordinate": fidelity,
            "fidelity_role": fidelity_role,
            "evaluator_kind": attempt["spec"]["evaluator_kind"],
        }
        method_link = {
            "method_id": method["method_id"],
            "input_sha256": method["input_sha256"],
            "canonical_program_sha256": method["canonical_program_sha256"],
        }
        artifact = {
            "profile": PROFILE,
            "producer": producer_link,
            "prediction": prediction_link,
            "attempt": attempt_link,
            "method": method_link,
            "kernel_output": kernel_output,
            "evidence_spec": evidence_spec,
            "generated_results": generated_results,
            "implementation_provenance": implementation,
            "interpretation_scope": {
                "retention_centroids": "stored ideal retention prediction values, not observed measurements",
                "widths": "synthetic assumptions; not fit, measured, or physically qualified",
                "assignment": "latent model analyte label; not verified chemical identity",
                "instrument_qualification_established": False,
                "physical_width_qualification_established": False,
                "trace_simulated": False,
            },
        }
        output_digest = sha256(artifact)
        _bounded({"artifact": artifact, "output_sha256": output_digest})
        trusted_provenance = {
            "producer_run": {
                "run_id": run_id,
                "input_sha256": input_digest,
                "output_sha256": output_digest,
            },
        }
        experiments._insert_result_records(
            con,
            owner,
            attempt_id,
            expected_attempt_revision,
            specs,
            trusted_provenance=trusted_provenance,
            result_ids=result_ids,
        )
        con.execute(
            """INSERT INTO gaussian_observations (
              id, owner, producer_id, producer_input_digest, retention_prediction_id,
              prediction_input_digest, prediction_output_digest, experiment_id,
              attempt_id, attempt_revision, attempt_digest, executable_method_id,
              method_input_digest, canonical_program_digest, payload, input_digest,
              output_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                owner,
                producer_id,
                producer_row["input_digest"],
                prediction_id,
                prediction_row["input_digest"],
                prediction_row["output_digest"],
                experiment_row["id"],
                attempt_id,
                expected_attempt_revision,
                attempt["input_sha256"],
                method["method_id"],
                method["input_sha256"],
                method["canonical_program_sha256"],
                canonical({"artifact": artifact, "output_sha256": output_digest}),
                input_digest,
                output_digest,
                now(),
            ),
        )
        return _observation_response(
            con.execute("SELECT * FROM gaussian_observations WHERE id=?", (run_id,)).fetchone(),
            con,
        )

    return registry_mutation(owner, key, request, write)


def get_gaussian_observation(owner: str, run_id: str) -> dict[str, Any]:
    with registry_connection() as con:
        row = con.execute(
            "SELECT * FROM gaussian_observations WHERE id=? AND owner=?", (run_id, owner)
        ).fetchone()
        if row is None:
            raise ValueError("Gaussian observation was not found or is not accessible")
        return _observation_response(row, con)


def validate_gaussian_result_evidence(
    con: sqlite3.Connection,
    owner: str,
    producer_id: str,
    attempt_id: str,
    attempt_revision: int,
    results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Verify linked result rows against the exact run from a bound producer.

    Empty selections return ``None`` so the observation kernel can preserve an
    unknown result. Any selected result in a producer-bound experiment must be
    a result emitted by this one stored run.
    """
    from . import experiments, observations, retention

    attempt = experiments._attempt(con, owner, attempt_id)
    experiment_row, producer = _validate_producer_experiment(
        con, owner, attempt["experiment_id"], producer_id
    )
    if attempt["revision"] != attempt_revision:
        raise ValueError("Attempt revision conflict while validating Gaussian producer evidence")
    row = con.execute(
        """SELECT * FROM gaussian_observations WHERE owner=? AND producer_id=?
        AND attempt_id=? AND attempt_revision=?""",
        (owner, producer_id, attempt_id, attempt_revision),
    ).fetchone()
    if not results:
        return None
    if row is None:
        raise ValueError(
            "Producer-bound observation evidence requires a stored Gaussian producer run"
        )
    run = _observation_response(row, con)
    prediction_row = con.execute(
        "SELECT * FROM retention_predictions WHERE id=? AND owner=?",
        (row["retention_prediction_id"], owner),
    ).fetchone()
    if prediction_row is None:
        raise ValueError("Stored Gaussian producer prediction is missing")
    prediction = retention._prediction_response(prediction_row)
    if (
        row["producer_input_digest"] != producer["input_sha256"]
        or row["experiment_id"] != experiment_row["id"]
        or row["prediction_input_digest"] != prediction["input_sha256"]
        or row["prediction_output_digest"] != prediction["output_sha256"]
    ):
        raise ValueError("Stored Gaussian producer run identity is stale")
    expected = {item["result_id"]: item for item in run["generated_results"]}
    for result in results:
        result_id = result.get("result_id")
        link = expected.get(result_id)
        if link is None:
            raise ValueError(
                "Result evidence is not from the experiment's bound Gaussian producer run"
            )
        verified = observations._result_record(
            con,
            owner,
            result_id,
            attempt_id=attempt_id,
            attempt_revision=attempt_revision,
            analyte_id=link["analyte_reference"],
            metric=link["metric"],
        )
        if verified != result:
            raise ValueError("Gaussian producer evidence differs from its stored current result")
        outcome = result.get("outcome")
        if not isinstance(outcome, dict) or sha256(outcome) != result.get("input_sha256"):
            raise ValueError("Gaussian producer result digest does not match its stored outcome")
        if (
            result.get("attempt_id") != attempt_id
            or result.get("attempt_revision") != attempt_revision
            or result.get("metric") != link["metric"]
            or result.get("analyte_reference") != link["analyte_reference"]
            or {k: v for k, v in outcome.items() if k != "provenance"} != link["outcome"]
        ):
            raise ValueError(
                "Gaussian producer result differs from its immutable generated outcome"
            )
        provenance = outcome.get("provenance")
        required = {
            "value_origin": "stored_ideal_gaussian_producer",
            "producer_run": {
                "run_id": row["id"],
                "input_sha256": row["input_digest"],
                "output_sha256": row["output_digest"],
            },
        }
        if not isinstance(provenance, dict) or any(
            provenance.get(name) != value for name, value in required.items()
        ):
            raise ValueError("Gaussian producer result provenance does not match its immutable run")
    return {
        "producer_id": row["producer_id"],
        "producer_input_sha256": row["producer_input_digest"],
        "run_id": row["id"],
        "run_input_sha256": row["input_digest"],
        "run_output_sha256": row["output_digest"],
        "prediction_id": row["retention_prediction_id"],
        "prediction_input_sha256": row["prediction_input_digest"],
        "prediction_output_sha256": row["prediction_output_digest"],
    }
