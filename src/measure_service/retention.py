"""Immutable declarations and predictions for the ideal retention reference.

The service binds strict, owner-scoped model inputs to an owned canonical
method and delegates all retention calculations to ``measure_methods.retention``.
Predictions are continuous numerical references; they do not qualify fitted
parameters, physical measurements, chromatographic signals or hardware.
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

from measure_methods.retention import RetentionParameters, predict_retention_time, retention_factor

from .method_backend import method_backend_info
from .storage import canonical, now, registry_connection, registry_mutation, sha256

SCHEMA_VERSION = "1.0"
PROFILE = "ideal-lc-retention-zeta/1"
MAX_ANALYTES = 32
MAX_RECORD_BYTES = 64_000
ABSOLUTE_TOLERANCE_MIN = 1e-7
MAX_EVALUATIONS_PER_ANALYTE = 20_000
_NUMBER = StrictFloat | StrictInt
_SOURCE_FIT_RANGES = {
    "ln_k0": (2.0, 20.0),
    "S1": (5.0, 250.0),
    "S2": (0.5, 10.0),
}


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


class AnalyteRetentionParameters(StrictModel):
    analyte_id: StrictStr = Field(min_length=1, max_length=100)
    model: Literal["linear", "quadratic", "neue-kuss"]
    ln_k0: _NUMBER = Field(
        description="Natural logarithm (base e) of dimensionless k0 at strong-solvent fraction zero; no log10 conversion is applied."
    )
    S1: _NUMBER = Field(
        description="Nonnegative dimensionless coefficient for the declared natural-log law and solvent fraction; upstream fitting bounds are advisory only."
    )
    S2: _NUMBER | None = Field(
        default=None,
        description="Nonnegative quadratic coefficient required for quadratic and neue-kuss; omitted for linear.",
    )

    @field_validator("analyte_id")
    @classmethod
    def analyte_id_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("analyte_id must be nonblank")
        return value

    @model_validator(mode="after")
    def validate_parameters(self) -> AnalyteRetentionParameters:
        _finite_number(self.ln_k0, "ln_k0")
        s1 = _finite_number(self.S1, "S1")
        if s1 < 0:
            raise ValueError("S1 must be nonnegative")
        if self.model == "linear":
            if self.S2 is not None:
                raise ValueError("S2 must be None for the linear model")
        else:
            if self.S2 is None:
                raise ValueError(f"S2 is required for the {self.model} model")
            s2 = _finite_number(self.S2, "S2")
            if s2 < 0:
                raise ValueError("S2 must be nonnegative")
        return self


class RetentionModelSpec(StrictModel):
    profile: Literal["ideal-lc-retention-zeta/1"] = PROFILE
    gradient_coordinate: Literal["column_inlet_at_analyte_entry"] = Field(
        description="Caller declares that canonical method points are already at the column inlet, with time zero at analyte entry; no delay is inferred or corrected."
    )
    analytes: list[AnalyteRetentionParameters] = Field(min_length=1, max_length=MAX_ANALYTES)
    column_length_m: _NUMBER = Field(description="Declared column length in metres.")
    effective_area_m2: _NUMBER = Field(
        description="Declared effective mobile-phase cross-section in square metres; no porosity is inferred."
    )
    post_column_volume_ml: _NUMBER = Field(
        default=0.0,
        description="Column-outlet-to-detector transport volume in millilitres; not an executable method phase.",
    )
    strong_solvent: StrictStr = Field(
        min_length=1,
        max_length=32,
        description="Solvent identifier whose fraction is supplied to the model, as a fraction in [0, 1].",
    )
    parameter_origin: Literal["synthetic", "caller_supplied"] = Field(
        description="Declared provenance category; it does not validate physical origin or parameter fit quality."
    )
    parameter_reference: StrictStr = Field(
        min_length=1,
        max_length=500,
        description="Caller-declared source or note. The service does not fetch or execute referenced content.",
    )
    training_snapshot_id: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Optional owned observation-snapshot lineage; it does not fit or validate the model.",
    )

    @field_validator("strong_solvent", "parameter_reference")
    @classmethod
    def declaration_text_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("strong_solvent and parameter_reference must be nonblank")
        return value

    @model_validator(mode="after")
    def validate_model(self) -> RetentionModelSpec:
        for name, value in (
            ("column_length_m", self.column_length_m),
            ("effective_area_m2", self.effective_area_m2),
        ):
            if _finite_number(value, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if _finite_number(self.post_column_volume_ml, "post_column_volume_ml") < 0:
            raise ValueError("post_column_volume_ml must be nonnegative")
        analyte_ids = [item.analyte_id for item in self.analytes]
        if len(set(analyte_ids)) != len(analyte_ids):
            raise ValueError("analyte IDs must be unique")
        return self


def _bounded(value: Any) -> Any:
    if len(canonical(value).encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("Retention registry record exceeds the 64,000-byte limit")
    return value


def _source_hash(function: Any) -> str:
    source = inspect.getsourcefile(function)
    if source is None:
        raise RuntimeError("Retention implementation source identity is unavailable")
    return hashlib.sha256(Path(source).read_bytes()).hexdigest()


def _implementation_provenance() -> dict[str, Any]:
    return {
        "measure_service": method_backend_info(),
        "service_adapter": {
            "module": "measure_service.retention",
            "source_sha256": _source_hash(predict_retention),
        },
        "retention_kernel": {
            "module": "measure_methods.retention",
            "source_sha256": _source_hash(predict_retention_time),
        },
    }


def _parameter_object(item: AnalyteRetentionParameters) -> RetentionParameters:
    return RetentionParameters(model=item.model, ln_k0=item.ln_k0, S1=item.S1, S2=item.S2)


def _fit_range_warnings(item: AnalyteRetentionParameters) -> list[dict[str, Any]]:
    values = {"ln_k0": item.ln_k0, "S1": item.S1}
    if item.S2 is not None:
        values["S2"] = item.S2
    warnings = []
    for name, value in values.items():
        lower, upper = _SOURCE_FIT_RANGES[name]
        if value < lower or value > upper:
            warnings.append(
                {
                    "code": "outside_source_fitting_range",
                    "parameter": name,
                    "value": value,
                    "source_fitting_range": [lower, upper],
                    "effect": "warning_only; value is retained and used without clipping",
                }
            )
    return warnings


def _validate_kernel_parameter_domain(spec: RetentionModelSpec) -> None:
    for item in spec.analytes:
        parameters = _parameter_object(item)
        try:
            for composition in (0.0, 0.5, 1.0):
                retention_factor(parameters=parameters, composition=composition)
        except (OverflowError, TypeError, ValueError) as error:
            raise ValueError(
                f"Retention parameters for analyte {item.analyte_id!r} are outside the finite kernel domain: {error}"
            ) from error


def _model_row(con: sqlite3.Connection, owner: str, model_id: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM retention_models WHERE id=? AND owner=?", (model_id, owner)
    ).fetchone()
    if row is None:
        raise ValueError("Retention model was not found or is not accessible")
    return row


def _model_response(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload"])
    identity = {"title": row["title"], "spec": payload["spec"]}
    if sha256(identity) != row["input_digest"]:
        raise ValueError("Stored retention model input digest does not match its contents")
    return _bounded(
        {
            "schema_version": SCHEMA_VERSION,
            "model_id": row["id"],
            "title": row["title"],
            "spec": payload["spec"],
            "training_snapshot": payload["training_snapshot"],
            "source_fit_range_warnings": payload["source_fit_range_warnings"],
            "implementation_provenance": payload["implementation_provenance"],
            "input_sha256": row["input_digest"],
            "created_at": row["created_at"],
        }
    )


def _snapshot_link(
    con: sqlite3.Connection,
    owner: str,
    snapshot_id: str | None,
    analyte_ids: set[str],
) -> dict[str, Any] | None:
    if snapshot_id is None:
        return None
    from .observations import ObservationContractSpec

    row = con.execute(
        "SELECT * FROM observation_snapshots WHERE id=? AND owner=?", (snapshot_id, owner)
    ).fetchone()
    if row is None:
        raise ValueError("Training observation snapshot was not found or is not accessible")
    payload = json.loads(row["payload"])
    if sha256(payload) != row["digest"]:
        raise ValueError("Training observation snapshot digest does not match its contents")
    contract_id = payload.get("contract_id")
    contract_digest = payload.get("contract_input_sha256")
    if contract_id != row["contract_id"] or contract_digest != row["contract_input_digest"]:
        raise ValueError("Training observation snapshot contract link is stale")
    contract_row = con.execute(
        "SELECT * FROM observation_contracts WHERE id=? AND owner=?", (contract_id, owner)
    ).fetchone()
    if contract_row is None or contract_row["input_digest"] != contract_digest:
        raise ValueError("Training observation contract was not found or has changed")
    contract_payload = json.loads(contract_row["payload"])
    contract_identity = {"title": contract_row["title"], "spec": contract_payload["spec"]}
    if sha256(contract_identity) != contract_digest:
        raise ValueError("Training observation contract input digest does not match its contents")
    contract_spec = ObservationContractSpec.model_validate(contract_payload["spec"])
    required = set(contract_spec.required_analytes)
    if analyte_ids - required:
        raise ValueError("Retention model analytes must be covered by the snapshot contract cohort")

    actual_members = [
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
    if actual_members != payload.get("members"):
        raise ValueError("Training snapshot membership does not match its immutable payload")
    return {
        "snapshot_id": row["id"],
        "sha256": row["digest"],
        "contract_id": contract_id,
        "contract_input_sha256": contract_digest,
        "required_analytes": contract_spec.required_analytes,
        "membership_count": len(actual_members),
        "interpretation": "caller-declared lineage only; this registry does not fit the model or validate calibration quality",
    }


def register_retention_model(
    owner: str, title: str, spec: RetentionModelSpec | dict[str, Any], key: str
) -> dict[str, Any]:
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("A retention model title of 1 to 200 characters is required")
    if not isinstance(spec, RetentionModelSpec):
        spec = RetentionModelSpec.model_validate(spec)
    requested = spec.model_dump(mode="json")
    identity = {"title": title, "spec": requested}
    request = {"operation": "register_retention_model", **identity}

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        _validate_kernel_parameter_domain(spec)
        analyte_ids = {item.analyte_id for item in spec.analytes}
        retained = {
            "spec": requested,
            "training_snapshot": _snapshot_link(con, owner, spec.training_snapshot_id, analyte_ids),
            "source_fit_range_warnings": {
                item.analyte_id: _fit_range_warnings(item) for item in spec.analytes
            },
            "implementation_provenance": _implementation_provenance(),
        }
        _bounded(retained)
        model_id, created = str(uuid.uuid4()), now()
        digest = sha256(identity)
        con.execute(
            "INSERT INTO retention_models VALUES (?, ?, ?, ?, ?, ?)",
            (model_id, owner, title, canonical(retained), digest, created),
        )
        return _model_response(_model_row(con, owner, model_id))

    return registry_mutation(owner, key, request, write)


def get_retention_model(owner: str, model_id: str) -> dict[str, Any]:
    with registry_connection() as con:
        return _model_response(_model_row(con, owner, model_id))


def _method_row(
    con: sqlite3.Connection, owner: str, method_id: str
) -> tuple[sqlite3.Row, dict[str, Any]]:
    from .method_registry import _owned_method, _record

    row = _owned_method(con, owner, method_id)
    return row, _record(row)


def _method_gradient(method: dict[str, Any], strong_solvent: str) -> dict[str, Any]:
    evaluation = method.get("evaluation")
    declaration = evaluation.get("declaration") if isinstance(evaluation, dict) else None
    program = evaluation.get("canonical") if isinstance(evaluation, dict) else None
    if not isinstance(declaration, dict) or not isinstance(program, dict):
        raise ValueError("Executable method has no canonical program")
    solvents = declaration.get("solvents")
    if not isinstance(solvents, list) or len(solvents) != 2 or len(set(solvents)) != 2:
        raise ValueError(
            "Retention prediction requires a method with exactly two declared solvents"
        )
    if strong_solvent not in solvents:
        raise ValueError("Declared strong_solvent is not present in the executable method")
    if program.get("pre_acquisition") is not None:
        raise ValueError(
            "Retention prediction does not support a pre_acquisition phase or inferred delay"
        )
    points = program.get("points")
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError("Canonical method must contain at least two analytical gradient points")
    times, fractions, flows = [], [], []
    for point in points:
        time = point.get("time_min")
        flow = point.get("flow_ml_min")
        solvent_fractions = point.get("solvent_fractions")
        if not isinstance(solvent_fractions, dict) or set(solvent_fractions) != set(solvents):
            raise ValueError("Canonical gradient solvent identities do not match its declaration")
        if strong_solvent not in solvent_fractions:
            raise ValueError("Canonical gradient is missing the declared strong solvent fraction")
        for name, value in (
            ("gradient time", time),
            ("flow", flow),
            ("strong-solvent fraction", solvent_fractions[strong_solvent]),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"Canonical {name} must be finite numeric")
        if flow <= 0:
            raise ValueError("Canonical gradient flow must be positive")
        if not 0 <= solvent_fractions[strong_solvent] <= 1:
            raise ValueError("Canonical strong-solvent composition must be between zero and one")
        times.append(time)
        fractions.append(solvent_fractions[strong_solvent])
        flows.append(flow)
    if any(flow != flows[0] for flow in flows[1:]):
        raise ValueError("Retention prediction requires constant flow across all canonical points")
    duration = program.get("acquisition_duration_min")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration <= 0
        or not math.isfinite(float(duration))
    ):
        raise ValueError("Canonical acquisition duration must be finite and positive")
    if times[0] != 0 or times[-1] != duration:
        raise ValueError("Canonical gradient points must span the acquisition duration from zero")
    return {
        "gradient_times_min": times,
        "gradient_fractions": fractions,
        "flow_rate_ml_min": flows[0],
        "acquisition_duration_min": duration,
        "canonical_program": program,
        "method_post_acquisition_ignored": program.get("post_acquisition"),
    }


def predict_retention(owner: str, model_id: str, method_id: str, key: str) -> dict[str, Any]:
    request = {
        "operation": "predict_retention",
        "model_id": model_id,
        "method_id": method_id,
    }

    def write(con: sqlite3.Connection) -> dict[str, Any]:
        model_row = _model_row(con, owner, model_id)
        model_record = _model_response(model_row)
        spec = RetentionModelSpec.model_validate(model_record["spec"])
        _, method = _method_row(con, owner, method_id)
        method_identity = {
            "method_id": method_id,
            "input_sha256": method["input_sha256"],
            "canonical_program_sha256": method["canonical_program_sha256"],
        }
        gradient = _method_gradient(method, spec.strong_solvent)
        if sha256(gradient["canonical_program"]) != method["canonical_program_sha256"]:
            raise ValueError(
                "Canonical program digest does not match the executable method identity"
            )

        analyte_results = []
        for item in spec.analytes:
            parameters = _parameter_object(item)
            result = predict_retention_time(
                parameters=parameters,
                gradient_times_min=gradient["gradient_times_min"],
                gradient_fractions=gradient["gradient_fractions"],
                flow_rate_ml_min=gradient["flow_rate_ml_min"],
                column_length_m=spec.column_length_m,
                effective_area_m2=spec.effective_area_m2,
                acquisition_duration_min=gradient["acquisition_duration_min"],
                post_column_volume_ml=spec.post_column_volume_ml,
                absolute_tolerance_min=ABSOLUTE_TOLERANCE_MIN,
                max_evaluations=MAX_EVALUATIONS_PER_ANALYTE,
            )
            analyte_results.append(
                {
                    "analyte_id": item.analyte_id,
                    "parameters": item.model_dump(mode="json"),
                    "source_fit_range_warnings": model_record["source_fit_range_warnings"][
                        item.analyte_id
                    ],
                    "result": result,
                }
            )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "profile": PROFILE,
            "model": {
                "model_id": model_id,
                "input_sha256": model_row["input_digest"],
                "gradient_coordinate": spec.gradient_coordinate,
                "training_snapshot": model_record["training_snapshot"],
            },
            "method": {
                **method_identity,
                "canonical_program": gradient["canonical_program"],
                "gradient_coordinate": spec.gradient_coordinate,
                "gradient_coordinate_interpretation": "caller-declared column-inlet profile and analyte-entry time origin; no delay is inferred or corrected",
                "strong_solvent": spec.strong_solvent,
                "flow_rate_ml_min": gradient["flow_rate_ml_min"],
                "post_acquisition_phase_ignored": gradient["method_post_acquisition_ignored"],
                "post_acquisition_is_not_post_column_volume": True,
            },
            "geometry": {
                "column_length_m": spec.column_length_m,
                "effective_area_m2": spec.effective_area_m2,
                "post_column_volume_ml": spec.post_column_volume_ml,
            },
            "solver": {
                "absolute_tolerance_min": ABSOLUTE_TOLERANCE_MIN,
                "max_evaluations_per_analyte": MAX_EVALUATIONS_PER_ANALYTE,
            },
            "analytes": analyte_results,
            "qualification_scope": {
                "numeric_input_validation": "strict_schema_and_kernel_domain_checks_passed",
                "parameter_fit_quality_assessed": False,
                "physical_retention_qualification_established": False,
                "hardware_qualification_established": False,
                "peak_width_or_resolution_calculated": False,
                "utility_score_calculated": False,
            },
            "implementation_provenance": _implementation_provenance(),
        }
        _bounded(payload)
        prediction_id, created = str(uuid.uuid4()), now()
        input_digest = sha256(request)
        output_digest = sha256(payload)
        con.execute(
            """INSERT INTO retention_predictions (
              id, owner, retention_model_id, model_input_digest, method_id,
              method_input_digest, canonical_program_digest, payload, input_digest,
              output_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                prediction_id,
                owner,
                model_id,
                model_row["input_digest"],
                method_id,
                method["input_sha256"],
                method["canonical_program_sha256"],
                canonical(payload),
                input_digest,
                output_digest,
                created,
            ),
        )
        return _prediction_response(
            con.execute(
                "SELECT * FROM retention_predictions WHERE id=?", (prediction_id,)
            ).fetchone()
        )

    return registry_mutation(owner, key, request, write)


def _prediction_response(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload"])
    request = {
        "operation": "predict_retention",
        "model_id": row["retention_model_id"],
        "method_id": row["method_id"],
    }
    if sha256(request) != row["input_digest"]:
        raise ValueError("Stored retention prediction input digest does not match its contents")
    if sha256(payload) != row["output_digest"]:
        raise ValueError("Stored retention prediction output digest does not match its contents")
    model = payload.get("model", {})
    method = payload.get("method", {})
    if (
        model.get("model_id") != row["retention_model_id"]
        or model.get("input_sha256") != row["model_input_digest"]
        or method.get("method_id") != row["method_id"]
        or method.get("input_sha256") != row["method_input_digest"]
        or method.get("canonical_program_sha256") != row["canonical_program_digest"]
    ):
        raise ValueError("Stored retention prediction identities do not match their contents")
    return _bounded(
        {
            "schema_version": SCHEMA_VERSION,
            "prediction_id": row["id"],
            "model_id": row["retention_model_id"],
            "method_id": row["method_id"],
            "prediction": payload,
            "input_sha256": row["input_digest"],
            "output_sha256": row["output_digest"],
            "created_at": row["created_at"],
        }
    )


def get_retention_prediction(owner: str, prediction_id: str) -> dict[str, Any]:
    with registry_connection() as con:
        row = con.execute(
            "SELECT * FROM retention_predictions WHERE id=? AND owner=?", (prediction_id, owner)
        ).fetchone()
        if row is None:
            raise ValueError("Retention prediction was not found or is not accessible")
        return _prediction_response(row)
