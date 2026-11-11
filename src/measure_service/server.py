"""A small, constrained MCP service for measurement and separations analysis.

This prototype deliberately accepts structured, bounded traces rather than
arbitrary server paths or executable code. Connect deployment requires the
explicit proxy/identity configuration documented in docs/deployment.md.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field, StrictInt, StrictStr, model_validator

from . import (
    analysis_evidence,
    analysis_runtime,
    batch_exports,
    batches,
    campaign_protocol_registry,
    campaign_suggestion_attempts,
    campaigns,
    connect_hosting,
    design_lineage,
    gaussian_producer,
    method_registry,
    observations,
    result_tables,
    retention,
)
from . import experiments as registry
from .analysis_models import (
    MAX_DATASET_POINTS,
    MAX_POINTS,
    MAX_TRACES,
    ChromatogramMethod,
    CSVChannel,
    CSVGroup,
    Dataset,
    StrictModel,
    Trace,
    TraceMetadata,
)
from .batch_models import (
    MAX_BATCH_ITEMS,
    MAX_BATCH_POINTS,
    MAX_MANIFEST_BYTES,
    BatchSpec,
)
from .batch_models import (
    PROFILE as BATCH_PROFILE,
)
from .campaign_protocol import (
    ABLATION_CAMPAIGN_PROFILE,
    CAMPAIGN_PROFILE,
    REPLICATION_CAMPAIGN_PROFILE,
    SUPPORTED_OPTIMIZER_TARGETS,
    SUPPORTED_SURROGATE_NOISE_PRIORS,
    ProtocolSpec,
)
from .experiments import AttemptSpec, ExperimentSpec, ResultSpec
from .gaussian_producer import GaussianProducerSpec
from .http_authority import http_transport_security, wrap_authority_headers
from .http_operations import wrap_http
from .methods import (
    MAX_CONSTRAINTS_BYTES,
    MAX_GRADIENT_POINTS,
    MAX_SOLVENTS,
    MAX_VIOLATIONS,
    MethodConstraints,
    MethodDeclaration,
    evaluate_method,
)
from .observations import ObservationContractSpec, ObservationEvidenceSpec
from .operational_readiness import storage_ready
from .result_table_models import ResultTableSpec
from .result_table_parser import preview_result_table
from .retention import RetentionModelSpec
from .storage import canonical, connect, db_path, now, sha256

ROOT = Path(__file__).resolve().parent
MAX_PAYLOAD_BYTES = 4_000_000
MAX_PAGE = 1000
MAX_EVIDENCE_SNAPSHOTS = 4
MAX_EVIDENCE_CACHE_BYTES = 64 * 1024 * 1024
_EVIDENCE_SNAPSHOTS: OrderedDict[tuple, Any] = OrderedDict()
_EVIDENCE_SNAPSHOT_BYTES = 0
_EVIDENCE_SNAPSHOT_LOCK = threading.Lock()


class Standard(StrictModel):
    location: float
    molecular_weight_g_mol: float = Field(gt=0)


class SECMethod(StrictModel):
    standards: list[Standard] = Field(min_length=2, max_length=100)
    fit_type: Literal["linear", "quadratic", "cubic"] = "linear"
    extrapolation: Literal["none", "linear", "warn"] = "none"
    integration_range: tuple[float, float] | None = Field(
        default=None,
        description="Inclusive limits on the original elution axis; uses sampled points without interpolating boundaries.",
    )
    standard_description: str = Field(
        min_length=1,
        max_length=300,
        description="Polymer/standard identity and relevant conditions for interpreting relative calibration.",
    )
    baseline_corrected: Literal[True]
    signal_proportional_to_weight_concentration: Literal[True]

    @model_validator(mode="after")
    def validate_method(self) -> SECMethod:
        required = {"linear": 2, "quadratic": 3, "cubic": 4}[self.fit_type]
        locations = [s.location for s in self.standards]
        if len(locations) < required or len(set(locations)) != len(locations):
            raise ValueError(
                f"{self.fit_type} calibration requires at least {required} distinct standard locations"
            )
        if len({s.molecular_weight_g_mol for s in self.standards}) < 2:
            raise ValueError(
                "calibration standards must contain at least two distinct molecular weights"
            )
        if self.integration_range and self.integration_range[0] >= self.integration_range[1]:
            raise ValueError("integration_range must contain increasing limits")
        return self


class ResponseStandard(StrictModel):
    concentration: float = Field(
        ge=0, description="Known concentration in the method's concentration_unit."
    )
    response: float = Field(
        description="Integrated peak area measured using the same response basis as the unknown."
    )


class PeakQuantitationMethod(StrictModel):
    analyte: str = Field(
        min_length=1,
        max_length=200,
        description="Caller-supplied analyte identity; not verified by the service.",
    )
    concentration_unit: str = Field(min_length=1, max_length=40)
    standards: list[ResponseStandard] = Field(min_length=3, max_length=100)
    weighting: Literal["none", "1/x", "1/x2"] = "none"
    force_origin: bool = False
    peak_assigned_to_analyte: Literal[True] = Field(
        description="Declare that the selected peak and standards correspond to this analyte."
    )
    standards_use_same_area_basis: Literal[True] = Field(
        description="Declare that standards and unknown use comparable integration, detector and response units."
    )

    @model_validator(mode="after")
    def validate_standards(self) -> PeakQuantitationMethod:
        concentrations = [s.concentration for s in self.standards]
        if len(set(concentrations)) < 3:
            raise ValueError("At least three distinct concentration levels are required")
        if self.weighting != "none" and any(x == 0 for x in concentrations):
            raise ValueError(
                "Inverse-concentration weighting requires positive standard concentrations"
            )
        return self


def identity(ctx: Context) -> str:
    """Use a local stdio principal, or an explicitly trusted Connect GUID."""
    mode = os.environ.get("MEASURE_MCP_IDENTITY", "local")
    request = ctx.request_context.request
    if mode == "local":
        if request is not None:
            raise ValueError("Local identity is permitted only over stdio")
        return "local"
    if mode != "connect" or os.environ.get("MEASURE_MCP_TRUST_CONNECT_HEADERS") != "1":
        raise ValueError("A trusted identity mode must be configured")
    if request is None:
        raise ValueError("Connect identity requires an authenticated HTTP request")
    if connect_hosting.user_claim() != "guid":
        # The hosting adapter resolved the proxy's username or session token
        # to a GUID before dispatch. The credentials header is never read as a
        # GUID in these modes.
        resolved = request.scope.get(connect_hosting.RESOLVED_GUID_SCOPE_KEY)
        if not isinstance(resolved, str):
            raise ValueError(
                request.scope.get(connect_hosting.IDENTITY_ERROR_SCOPE_KEY)
                or "Connect identity is not available for this request"
            )
        return "connect:" + str(uuid.UUID(resolved))
    headers = request.headers.getlist("rstudio-connect-credentials")
    if len(headers) != 1:
        raise ValueError("Connect credentials are missing or ambiguous")
    try:
        credentials = json.loads(headers[0])
        principal = str(uuid.UUID(credentials["user"]))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("Connect credentials must contain a unique user GUID") from exc
    return "connect:" + principal


def dataset_for(owner: str, dataset_id: str) -> tuple[dict, str]:
    with connect() as con:
        row = con.execute(
            "SELECT payload, digest FROM datasets WHERE id=? AND owner=?", (dataset_id, owner)
        ).fetchone()
    if row is None:
        raise ValueError("Dataset was not found or is not accessible")
    return json.loads(row[0]), row[1]


def source_for(owner: str, dataset_id: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            """SELECT s.kind, s.digest, s.content, s.mapping FROM dataset_sources s
            JOIN datasets d ON s.dataset_id=d.id WHERE d.id=? AND d.owner=?""",
            (dataset_id, owner),
        ).fetchone()
    if row is None:
        return None
    return {
        "kind": row[0],
        "source_sha256": row[1],
        "content": row[2],
        "mapping": json.loads(row[3]),
    }


def register_dataset(
    owner: str, dataset: Dataset, source: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload = dataset.model_dump(mode="json")
    serialized = canonical(payload)
    if len(serialized.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("Dataset exceeds the request-size limit")
    dataset_id, digest, created = str(uuid.uuid4()), sha256(payload), now()
    with connect() as con:
        con.execute(
            "INSERT INTO datasets VALUES (?, ?, ?, ?, ?)",
            (dataset_id, owner, digest, serialized, created),
        )
        if source is not None:
            con.execute(
                "INSERT INTO dataset_sources VALUES (?, ?, ?, ?, ?)",
                (
                    dataset_id,
                    source["kind"],
                    source["source_sha256"],
                    source["content"],
                    canonical(source["mapping"]),
                ),
            )
    return {
        "dataset_id": dataset_id,
        "input_sha256": digest,
        "source_sha256": None if source is None else source["source_sha256"],
        "created_at": created,
        "trace_count": len(dataset.traces),
        "point_count": sum(len(t.axis) for t in dataset.traces),
    }


def parse_csv(
    csv_text: str, axis_column: str, channels: list[CSVChannel], delimiter: str
) -> tuple[list[Trace], dict[str, Any]]:
    raw = csv_text.encode("utf-8")
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError("CSV exceeds the request-size limit")
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"), newline=""), delimiter=delimiter)
    fields = reader.fieldnames or []
    columns = [channel.column for channel in channels]
    if (
        len(set(fields)) != len(fields)
        or len(set(columns)) != len(columns)
        or axis_column in columns
        or any(c not in fields for c in [axis_column, *columns])
    ):
        raise ValueError("CSV needs unique headers and distinct existing axis and signal columns")
    axis: list[float] = []
    signals: list[list[float]] = [[] for _ in channels]
    for row in reader:
        if len(axis) >= MAX_POINTS or (len(axis) + 1) * len(channels) > MAX_DATASET_POINTS:
            raise ValueError("CSV exceeds the point limit")
        if None in row:
            raise ValueError("CSV contains a row with extra fields")
        try:
            axis.append(float(row[axis_column]))
            for channel, values in zip(channels, signals):
                values.append(float(row[channel.column]))
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError("CSV rows must have numeric values in all selected columns") from exc
    traces = [
        Trace.model_validate({**channel.metadata.model_dump(), "axis": axis, "signal": signal})
        for channel, signal in zip(channels, signals)
    ]
    source = {
        "kind": "delimited_text",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "content": csv_text,
        "mapping": {
            "delimiter": delimiter,
            "axis_column": axis_column,
            "signal_columns": [
                {"column": c.column, "trace_id": c.metadata.trace_id} for c in channels
            ],
        },
    }
    return traces, source


def parse_long_csv(
    csv_text: str,
    axis_column: str,
    signal_column: str,
    group_columns: list[str],
    groups: list[CSVGroup],
    delimiter: str,
) -> tuple[list[Trace], dict[str, Any]]:
    raw = csv_text.encode("utf-8")
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError("CSV exceeds the request-size limit")
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"), newline=""), delimiter=delimiter)
    fields = reader.fieldnames or []
    selected = [axis_column, signal_column, *group_columns]
    if (
        len(set(fields)) != len(fields)
        or len(set(selected)) != len(selected)
        or any(c not in fields for c in selected)
    ):
        raise ValueError(
            "CSV needs unique headers and distinct existing axis, signal and group columns"
        )
    keys = [tuple(group.values) for group in groups]
    if any(len(key) != len(group_columns) for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("Each group needs one unique value per group column")
    indices = {key: i for i, key in enumerate(keys)}
    axes: list[list[float]] = [[] for _ in groups]
    signals: list[list[float]] = [[] for _ in groups]
    total = 0
    for row in reader:
        if None in row:
            raise ValueError("CSV contains a row with extra fields")
        key = tuple(row.get(column) for column in group_columns)
        if key not in indices:
            raise ValueError("CSV contains an unmapped group; provide metadata for every group")
        index = indices[key]
        if len(axes[index]) >= MAX_POINTS or total >= MAX_DATASET_POINTS:
            raise ValueError("CSV exceeds the point limit")
        try:
            axes[index].append(float(row[axis_column]))
            signals[index].append(float(row[signal_column]))
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError(
                "CSV rows must have numeric values in the selected axis and signal columns"
            ) from exc
        total += 1
    traces = [
        Trace.model_validate({**group.metadata.model_dump(), "axis": axis, "signal": signal})
        for group, axis, signal in zip(groups, axes, signals)
    ]
    source = {
        "kind": "delimited_text",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "content": csv_text,
        "mapping": {
            "delimiter": delimiter,
            "axis_column": axis_column,
            "signal_column": signal_column,
            "group_columns": group_columns,
            "groups": [
                {"values": group.values, "trace_id": group.metadata.trace_id} for group in groups
            ],
        },
    }
    return traces, source


def trace_for(dataset: dict, trace_id: str) -> dict[str, Any]:
    for trace in dataset["traces"]:
        if trace["trace_id"] == trace_id:
            return trace
    raise ValueError("Trace was not found in the dataset")


def analysis_for(owner: str, analysis_id: str) -> dict[str, Any]:
    with connect() as con:
        row = con.execute(
            "SELECT dataset_id, trace_id, kind, method, result, provenance, created_at FROM analyses WHERE id=? AND owner=?",
            (analysis_id, owner),
        ).fetchone()
    if row is None:
        raise ValueError("Analysis was not found or is not accessible")
    return {
        "analysis_id": analysis_id,
        "dataset_id": row[0],
        "trace_id": row[1],
        "kind": row[2],
        "method": json.loads(row[3]),
        "result": json.loads(row[4]),
        "provenance": json.loads(row[5]),
        "created_at": row[6],
    }


def run_worker(payload: dict) -> dict[str, Any]:
    return analysis_runtime.run_worker(payload, worker_path=ROOT / "worker.R")


def save_analysis(
    owner: str, dataset_id: str, trace_id: str, kind: str, method: dict, result: dict, digest: str
) -> dict[str, Any]:
    source = source_for(owner, dataset_id)
    record = analysis_runtime.make_analysis_record(
        dataset_id,
        trace_id,
        kind,
        method,
        result,
        digest,
        None if source is None else source["source_sha256"],
        worker_path=ROOT / "worker.R",
    )
    with connect() as con:
        analysis_runtime.insert_analysis(con, owner, record)
    return analysis_runtime.compact_analysis(record)


INSTRUCTIONS = (
    "Analytical trace processing and experiment-result tracking. Every dataset, method, "
    "experiment, result and analysis belongs to the account that created it. Records are "
    "not shared between accounts: another signed-in person cannot list, read or link "
    "yours, and you cannot see theirs. There is no sharing, project or delete operation, "
    "so do not store data you could not keep here. Start with "
    "measurement_registry_capabilities; numerical analysis additionally needs the R "
    "backend reported by measurement_capabilities. A tool failure is reported in the "
    "result's isError field, not as an HTTP error."
)

mcp = FastMCP(
    "measure-separations",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
    transport_security=http_transport_security(),
)
READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
RETRY_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# Writes no durable record, but every call returns a newly identified result.
READ_CAPTURE = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
RequestKey = Annotated[str, Field(min_length=1, max_length=100)]
RecordId = Annotated[str, Field(min_length=1, max_length=100)]


@mcp.tool(annotations=READ, structured_output=True)
def measurement_registry_capabilities(ctx: Context) -> dict[str, Any]:
    """Describe the local experiment registry without requiring an R backend."""
    identity(ctx)
    return {
        "schema_version": registry.SCHEMA_VERSION,
        "qualification": "experimental",
        "storage": "single_node_sqlite",
        "experiment_specs": "immutable",
        "design_lineage": {
            "profile": design_lineage.LINEAGE_PROFILE,
            "replacement": "new_immutable_experiment_with_complete_explicit_specification",
            "concurrency": "current_tip_and_expected_immutable_input_sha256",
            "maximum_versions": design_lineage.MAX_VERSIONS,
            "page_entries": design_lineage.MAX_PAGE,
            "entry_bytes": design_lineage.MAX_ENTRY_BYTES,
            "page_bytes": design_lineage.MAX_PAGE_BYTES,
            "reason_utf8_bytes": design_lineage.MAX_REASON_BYTES,
            "historical_attempts": "explicit_old_design_ids_remain_valid",
            "current_pointer": "discovery_only_without_execution_or_evidence_migration",
        },
        "attempts": "append_only_revisions",
        "results": "immutable_with_explicit_supersession",
        "result_tables": {
            "profile": result_tables.IMPORT_PROFILE,
            "mapping": "explicit_columns_and_constants_without_conversion",
            "source_bytes": 64_000,
            "outcomes_per_import": 50,
            "columns": 64,
            "preview": "pure_parse_without_resolving_target_ownership_or_current_revision",
            "atomicity": "all_attempts_results_source_rows_and_retry_receipt",
            "corrections": "explicit_current_result_ids_in_source_order",
            "source_page_bytes": result_tables.MAX_SOURCE_PAGE_BYTES,
            "source_access": "owned_raw_utf8_bytes_without_server_paths",
            "observation_evaluation": "separate_explicit_operation",
            "optimizer_consumption": "separate_explicit_operation",
        },
        "conditions": ["numeric_with_units", "categorical", "boolean"],
        "outcome_statuses": [
            "observed",
            "not_detected",
            "below_limit",
            "censored",
            "missing",
            "failed",
            "infeasible",
        ],
        "idempotency": "required_key_per_owner_for_registry_writes",
        "bo_suggestions": True,
        "instrument_actuation": False,
        "executable_method_links": "owned_immutable_methods_with_digests",
        "observation_contracts": "predeclared_immutable_analytes_and_acceptance_rules",
        "analytical_observations": "exact_attempt_and_result_evidence_with_three_valued_acceptance",
        "observation_snapshots": "immutable_explicit_membership_without_duplicate_attempts",
        "retention_models": "explicit_ideal_reference_inputs_with_immutable_predictions",
        "gaussian_observation_producer": "predeclared_synthetic_widths_and_exact_prediction_bound_results",
        "chromatogram_batches": {
            "profile": BATCH_PROFILE,
            "items": MAX_BATCH_ITEMS,
            "total_points": MAX_BATCH_POINTS,
            "manifest_bytes": MAX_MANIFEST_BYTES,
            "items_per_run": 2,
            "r_workers_per_process": analysis_runtime.MAX_R_WORKERS,
            "claim_lease_seconds": batches.LEASE_SECONDS,
            "attempts_per_item": batches.MAX_ATTEMPTS,
            "replay": "same_key_reads_retained_run_without_execution",
            "recovery": "explicit_expired_claim_interruption_then_selected_retry",
            "cancellation": "pending_items_stop_running_work_may_finish",
            "portable_source_bundle_restore": True,
            "historical_execution_authority_restore": False,
            "instrument_actuation": False,
        },
        "chromatogram_evidence": {
            "profile": analysis_evidence.PROFILE,
            "retrieval": "owned_stored_results_without_reanalysis",
            "data": "complete_raw_and_processed_recorded_grids_with_peak_rows",
            "point_limit": analysis_evidence.MAX_POINTS,
            "peak_limit": analysis_evidence.MAX_PEAKS,
            "page_rows": analysis_evidence.MAX_PAGE,
            "page_bytes": analysis_evidence.MAX_PAGE_BYTES,
            "document_bytes": analysis_evidence.MAX_BUNDLE_BYTES,
            "paging": "process_local_immutable_capture_by_owner_store_analysis_and_digest",
            "capture_limit": MAX_EVIDENCE_SNAPSHOTS,
            "capture_serialized_bytes": MAX_EVIDENCE_CACHE_BYTES,
            "managed_artifact_upload": False,
        },
        "batch_snapshots": {
            "profile": "measure-chromatogram-batch-snapshot/1",
            "modes": ["references", "self_contained"],
            "snapshot_bytes": 64 * 1024 * 1024,
            "raw_points_in_complete_dataset_closure": 500_000,
            "control_events": 1024,
            "export_page_bytes": batch_exports.MAX_EXPORT_PAGE,
            "process_cache_bytes": batch_exports.MAX_EXPORT_BYTES,
            "process_capture_count": batch_exports.MAX_EXPORTS,
            "capture": "owned_coherent_revision_without_running_claims",
            "restoration": "local_operator_new_store_import_and_fresh_pending_batch",
            "historical_execution_authority": "not_portable",
            "numerical_reproduction": "explicit_fresh_execution_separate_from_import",
            "server_paths_or_archive_upload": False,
            "mocca2_interoperability": False,
        },
        "synthetic_campaigns": {
            "profile": "synthetic-isocratic-paired/1",
            "supported_profiles": [
                CAMPAIGN_PROFILE,
                ABLATION_CAMPAIGN_PROFILE,
                REPLICATION_CAMPAIGN_PROFILE,
            ],
            "duplicate_candidates": {
                "legacy_profiles": "reject",
                "explicit_profile": REPLICATION_CAMPAIGN_PROFILE,
                "required_policy": ["reject", "replicate"],
                "equality": "exact_normalized_candidate_sha256",
                "replicate_evidence": "fresh_execution_and_consumption_counted_per_ordinal",
            },
            "execution": "sequential_single_fidelity_simulator_latent",
            "policies": ["random", "bo_mcp_gp_logei"],
            "supported_optimizer_targets": list(SUPPORTED_OPTIMIZER_TARGETS),
            "supported_surrogate_noise_priors": list(SUPPORTED_SURROGATE_NOISE_PRIORS),
            "surrogate_capabilities": {
                "aggregate_scalar": True,
                "compoundwise": False,
                "rbf_ard": True,
                "matern_5_2": False,
            },
            "bo_runtime": "optional_administrator_configured_MEASURE_BO_PYTHON",
            "bo_runtime_configured": bool(os.environ.get("MEASURE_BO_PYTHON")),
            "outcome_consumption": "exact_owned_immutable_observation_snapshot",
            "suggestion_attempts": {
                "profile": "campaign-suggestion-attempt/1",
                "retry": "same_key_replays_without_recomputation",
                "recovery": "explicit_abandoned_unknown_with_late_result_fencing",
                "historical_missing_timing": "unavailable_not_backfilled",
            },
            "generic_bo_service": False,
        },
        "limits": {
            "record_bytes": registry.MAX_RECORD_BYTES,
            "page_size": registry.MAX_REGISTRY_PAGE,
            "conditions_per_set": 64,
            "measurements_per_attempt": 32,
            "results_per_batch": 50,
            "suggestion_attempt_record_bytes": campaign_suggestion_attempts.MAX_PUBLIC_BYTES,
            "suggestion_attempt_page_size": 20,
            "campaign_composite_response_bytes": campaign_suggestion_attempts.MAX_SUCCESS_RESPONSE_BYTES,
        },
    }


@mcp.tool(annotations=READ, structured_output=True)
def measurement_method_capabilities(ctx: Context) -> dict[str, Any]:
    """Describe executable LC method validation and storage without an R backend."""
    identity(ctx)
    return {
        "schema_version": "1.0",
        "qualification": "experimental",
        "programs": ["isocratic", "gradient_absolute", "gradient_duration"],
        "interpolation": "piecewise_linear_flow_and_solvent_fractions",
        "canonical_units": {"time": "min", "flow": "mL/min", "volume": "mL"},
        "solvent_use": "exact_integral_of_linear_flow_times_linear_fraction",
        "overheads": "separate_constant_pre_and_post_acquisition_phases",
        "equipment_compatibility": "caller_declared_references_and_allowed_values",
        "records": "immutable_owner_scoped_with_retry_receipts",
        "optimizer_search_space": False,
        "pressure_prediction": False,
        "instrument_actuation": False,
        "hardware_qualification": False,
        "limits": {
            "record_bytes": method_registry.MAX_METHOD_BYTES,
            "page_size": method_registry.MAX_METHOD_PAGE,
            "solvents": MAX_SOLVENTS,
            "gradient_points": MAX_GRADIENT_POINTS,
            "constraint_bytes": MAX_CONSTRAINTS_BYTES,
            "violations": MAX_VIOLATIONS,
        },
    }


@mcp.tool(annotations=READ, structured_output=True)
def measurement_validate_method(
    declaration: MethodDeclaration,
    ctx: Context,
    constraints: MethodConstraints | None = None,
) -> dict[str, Any]:
    """Normalize a declared LC method, calculate time and solvent use, and report feasibility; performs no execution or storage."""
    identity(ctx)
    evaluation = evaluate_method(declaration, constraints)
    return evaluation.model_dump(mode="json")


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_register_method(
    title: Annotated[str, Field(min_length=1, max_length=200)],
    declaration: MethodDeclaration,
    idempotency_key: RequestKey,
    ctx: Context,
    constraints: MethodConstraints | None = None,
) -> dict[str, Any]:
    """Retain an immutable method that meets its declared constraints, with original units, canonical program and digests; does not run an instrument."""
    return method_registry.register_method(
        identity(ctx), title, declaration, constraints, idempotency_key
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_method(method_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Retrieve an owned immutable executable method and its declared limits."""
    return method_registry.get_method(identity(ctx), method_id)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_list_methods(
    ctx: Context,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """List compact method identities in a bounded owner-scoped page."""
    return method_registry.list_methods(identity(ctx), offset, limit)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_create_experiment(
    spec: ExperimentSpec, idempotency_key: RequestKey, ctx: Context
) -> dict[str, Any]:
    """Record an immutable experiment design with intended conditions; does not execute it or create an optimizer campaign."""
    return registry.create_experiment(identity(ctx), spec, idempotency_key)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_experiment(experiment_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Retrieve an experiment's intended conditions and caller-declared origin."""
    return registry.get_experiment(identity(ctx), experiment_id)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_supersede_experiment(
    experiment_id: RecordId,
    expected_input_sha256: Annotated[
        StrictStr, Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    ],
    spec: ExperimentSpec,
    reason: Annotated[StrictStr, Field(min_length=1, max_length=2000)],
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Replace only a current owned design with a complete new immutable experiment and exact expected digest; historical attempts, observations, snapshots and campaign evidence keep their original IDs."""
    return design_lineage.supersede_experiment(
        identity(ctx), experiment_id, expected_input_sha256, spec, reason, idempotency_key
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_experiment_design_lineage(
    experiment_id: RecordId,
    ctx: Context,
    offset: Annotated[StrictInt, Field(ge=0)] = 0,
    limit: Annotated[StrictInt, Field(ge=1, le=20)] = 10,
) -> dict[str, Any]:
    """Read verified ordered design versions and a separate current pointer without moving historical evidence or creating lineage state for a standalone design."""
    return design_lineage.get_experiment_design_lineage(identity(ctx), experiment_id, offset, limit)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_list_experiments(
    ctx: Context,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """List a bounded page of the current owner's experiments."""
    return registry.list_experiments(identity(ctx), offset, limit)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_record_attempt(
    experiment_id: RecordId, spec: AttemptSpec, idempotency_key: RequestKey, ctx: Context
) -> dict[str, Any]:
    """Record one attempted experiment, its reported status/actual conditions and owned dataset/trace links; no instrument action occurs."""
    return registry.record_attempt(identity(ctx), experiment_id, spec, idempotency_key)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_revise_attempt(
    attempt_id: RecordId,
    expected_revision: Annotated[StrictInt, Field(ge=1)],
    spec: AttemptSpec,
    reason: Annotated[str, Field(min_length=1, max_length=2000)],
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Append a corrected or updated reported attempt snapshot with an explanation and optimistic version check; earlier revisions remain available."""
    return registry.revise_attempt(
        identity(ctx), attempt_id, expected_revision, spec, reason, idempotency_key
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_attempt(
    attempt_id: RecordId, ctx: Context, revision: Annotated[int | None, Field(ge=1)] = None
) -> dict[str, Any]:
    """Read the latest or a specific historical attempt revision, including sample/injection metadata from its linked traces."""
    return registry.get_attempt(identity(ctx), attempt_id, revision)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_list_attempts(
    experiment_id: RecordId,
    ctx: Context,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """List a bounded page of attempts and latest reported statuses for an owned experiment."""
    return registry.list_attempts(identity(ctx), experiment_id, offset, limit)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_record_results(
    attempt_id: RecordId,
    expected_attempt_revision: Annotated[StrictInt, Field(ge=1)],
    results: Annotated[list[ResultSpec], Field(min_length=1, max_length=50)],
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Atomically record outcomes against an exact attempt revision; copy numeric analysis-summary values or record explicit caller-reported outcomes. Units are caller-declared."""
    return registry.record_results(
        identity(ctx), attempt_id, expected_attempt_revision, results, idempotency_key
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_result(result_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Retrieve an immutable outcome with evidence provenance, attempt revision and any later superseding result."""
    return registry.get_result(identity(ctx), result_id)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_list_results(
    attempt_id: RecordId,
    ctx: Context,
    include_superseded: bool = False,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """List compact current outcome heads or the full result history for an owned attempt; no missing outcome is converted to zero."""
    return registry.list_results(identity(ctx), attempt_id, include_superseded, offset, limit)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_preview_result_table(
    spec: ResultTableSpec,
    ctx: Context,
    offset: Annotated[StrictInt, Field(ge=0)] = 0,
    limit: Annotated[StrictInt, Field(ge=1, le=20)] = 10,
) -> dict[str, Any]:
    """Parse explicitly mapped CSV outcomes without writing or resolving target records; preview does not reserve current attempt revisions. No values or statuses are inferred."""
    identity(ctx)
    return preview_result_table(spec, offset, limit)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_import_result_table(
    spec: ResultTableSpec, idempotency_key: RequestKey, ctx: Context
) -> dict[str, Any]:
    """Atomically import mapped outcomes across owned exact attempt revisions with retained source-row provenance and explicit corrections; no observation is evaluated or consumed automatically."""
    return result_tables.import_result_table(identity(ctx), spec, idempotency_key)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_result_table_import(import_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Read an owned immutable table-import receipt and verify its source, result bindings and historical outcome evidence without R."""
    return result_tables.get_result_table_import(identity(ctx), import_id)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_read_result_table_rows(
    import_id: RecordId,
    ctx: Context,
    offset: Annotated[StrictInt, Field(ge=0)] = 0,
    limit: Annotated[StrictInt, Field(ge=1, le=20)] = 20,
) -> dict[str, Any]:
    """Page immutable source-record coordinates and exact result identities; later corrections do not replace these import members."""
    return result_tables.read_result_table_rows(identity(ctx), import_id, offset, limit)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_read_result_table_source(
    import_id: RecordId,
    ctx: Context,
    offset: Annotated[StrictInt, Field(ge=0)] = 0,
    limit: Annotated[StrictInt, Field(ge=1, le=12_000)] = 12_000,
) -> dict[str, Any]:
    """Page exact retained UTF8 source bytes as base64 using byte offsets, including original quoting, line endings and unmapped columns; no filesystem path is read."""
    return result_tables.read_result_table_source(identity(ctx), import_id, offset, limit)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_register_observation_contract(
    title: Annotated[str, Field(min_length=1, max_length=200)],
    spec: ObservationContractSpec,
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Declare an immutable required analyte cohort, analytical acceptance and separate feasibility/runtime preferences before creating experiments."""
    return observations.register_observation_contract(identity(ctx), title, spec, idempotency_key)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_observation_contract(contract_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Read an owned observation contract and its exact input digest; declarations do not qualify the measurement producer."""
    return observations.get_observation_contract(identity(ctx), contract_id)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_evaluate_observation(
    contract_id: RecordId,
    attempt_id: RecordId,
    expected_attempt_revision: Annotated[int, Field(ge=1)],
    spec: ObservationEvidenceSpec,
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Assess exact owned retention/resolution result records under the experiment's predeclared contract; retain immutable evidence, explicit unknowns and separate fidelity/origin declarations."""
    return observations.evaluate_observation(
        identity(ctx), contract_id, attempt_id, expected_attempt_revision, spec, idempotency_key
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_observation(observation_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Retrieve an immutable analytical observation, source evidence and decision even after later corrections."""
    return observations.get_observation(identity(ctx), observation_id)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_create_observation_snapshot(
    contract_id: RecordId,
    observation_ids: Annotated[list[RecordId], Field(min_length=1, max_length=50)],
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Freeze explicitly selected observation identities and digests for one contract, retaining at most one observation per physical attempt; does not generate optimizer suggestions."""
    return observations.create_observation_snapshot(
        identity(ctx), contract_id, observation_ids, idempotency_key
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_observation_snapshot(snapshot_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Read immutable owned snapshot membership; later result corrections or attempt revisions cannot change its contents."""
    return observations.get_observation_snapshot(identity(ctx), snapshot_id)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_register_retention_model(
    title: Annotated[str, Field(min_length=1, max_length=200)],
    spec: RetentionModelSpec,
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Retain a declared ideal retention reference with natural-log coefficients, explicit geometry/clock/units and optional immutable training lineage; does not fit or physically validate parameters."""
    return retention.register_retention_model(identity(ctx), title, spec, idempotency_key)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_retention_model(model_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Read an owned immutable retention model declaration, input checks, warnings and source evidence."""
    return retention.get_retention_model(identity(ctx), model_id)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_predict_retention(
    model_id: RecordId, method_id: RecordId, idempotency_key: RequestKey, ctx: Context
) -> dict[str, Any]:
    """Calculate and retain ideal retention times for an owned constant-flow binary inlet program; missing within-window events remain censored, with no peak-width, resolution or instrument claim."""
    return retention.predict_retention(identity(ctx), model_id, method_id, idempotency_key)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_retention_prediction(prediction_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Retrieve an immutable ideal prediction and its exact model, method, solver and backend evidence."""
    return retention.get_retention_prediction(identity(ctx), prediction_id)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_register_gaussian_producer(
    title: Annotated[str, Field(min_length=1, max_length=200)],
    retention_model_id: RecordId,
    spec: GaussianProducerSpec,
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Declare immutable synthetic Gaussian widths for an owned synthetic retention model before an experiment; does not fit physical peak widths or identify chemicals."""
    return gaussian_producer.register_gaussian_producer(
        identity(ctx), title, retention_model_id, spec, idempotency_key
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_gaussian_producer(producer_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Retrieve the owned immutable latent-producer declaration, assumed FWHM widths and retention-model identity."""
    return gaussian_producer.get_gaussian_producer(identity(ctx), producer_id)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_record_gaussian_observation(
    producer_id: RecordId,
    prediction_id: RecordId,
    attempt_id: RecordId,
    expected_attempt_revision: Annotated[int, Field(ge=1)],
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Atomically retain latent Gaussian results for a prebound completed simulator attempt and its exact actual-method prediction; returns result IDs for separate analytical assessment and snapshot selection."""
    return gaussian_producer.record_gaussian_observation(
        identity(ctx),
        producer_id,
        prediction_id,
        attempt_id,
        expected_attempt_revision,
        idempotency_key,
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_gaussian_observation(run_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Read an immutable owned Gaussian producer run and its exact prediction, assumed widths and generated result identities."""
    return gaussian_producer.get_gaussian_observation(identity(ctx), run_id)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_register_campaign_protocol(
    title: Annotated[str, Field(min_length=1, max_length=200)],
    spec: ProtocolSpec,
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Freeze a bounded matched synthetic isocratic protocol and owned model, Gaussian producer and observation-contract identities before campaign execution; initialization counts against each arm's budget."""
    return campaign_protocol_registry.register_campaign_protocol(
        identity(ctx), title, spec, idempotency_key
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_campaign_protocol(protocol_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Read an owned immutable matched protocol, complete task bindings, budgets, seeds and provenance."""
    return campaign_protocol_registry.get_campaign_protocol(identity(ctx), protocol_id)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_create_campaign(
    protocol_id: RecordId,
    task_id: RecordId,
    seed: Annotated[StrictInt, Field(ge=0, le=2**31 - 1)],
    policy: Literal["random", "bo_mcp_gp_logei"],
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Create one owned sequential synthetic task/seed/policy arm from a frozen protocol; all initialization consumes its declared evaluation budget."""
    return campaigns.create_campaign(
        identity(ctx), protocol_id, task_id, seed, policy, idempotency_key
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_campaign(campaign_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Inspect an owned synthetic campaign's revision, pending proposal, budget and compact immutable consumption history."""
    return campaigns.get_campaign(identity(ctx), campaign_id)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_campaign_suggestion_attempt(
    suggestion_attempt_id: RecordId, ctx: Context
) -> dict[str, Any]:
    """Read an owned suggestion claim, immutable terminal or late evidence, exact requested training and available timing; a claim does not prove the optimizer started."""
    return campaigns.get_campaign_suggestion_attempt(identity(ctx), suggestion_attempt_id)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_list_campaign_suggestion_attempts(
    campaign_id: RecordId,
    ctx: Context,
    limit: Annotated[StrictInt, Field(ge=1, le=20)] = 10,
    after: RecordId | None = None,
) -> dict[str, Any]:
    """List bounded owned suggestion-attempt summaries in stable order; after is a suggestion-attempt ID from the same campaign, and historical missing telemetry stays absent."""
    return campaigns.list_campaign_suggestion_attempts(identity(ctx), campaign_id, limit, after)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_abandon_campaign_suggestion_attempt(
    suggestion_attempt_id: RecordId,
    expected_start_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")],
    reason: Annotated[str, Field(min_length=1, max_length=500)],
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Explicitly reconcile an unresolved owned suggestion as abandoned_unknown; this does not assert failure, kill a process or invent timing, and any late computation cannot create a proposal."""
    return campaigns.abandon_campaign_suggestion_attempt(
        identity(ctx), suggestion_attempt_id, expected_start_sha256, reason, idempotency_key
    )


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_suggest_campaign(
    campaign_id: RecordId,
    expected_revision: Annotated[StrictInt, Field(ge=1)],
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Durably claim and finalize one synthetic suggestion with exact training evidence and failure timing; same-key retries never rerun a claimed computation, and no fallback replaces a failed candidate."""
    return campaigns.suggest_campaign(
        identity(ctx), campaign_id, expected_revision, idempotency_key
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_campaign_proposal(proposal_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Read an owned immutable proposal with its executable method, exact consumed training references and actual policy/backend provenance."""
    return campaigns.get_campaign_proposal(identity(ctx), proposal_id)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_consume_campaign_snapshot(
    campaign_id: RecordId,
    proposal_id: RecordId,
    snapshot_id: RecordId,
    expected_revision: Annotated[StrictInt, Field(ge=1)],
    idempotency_key: RequestKey,
    ctx: Context,
) -> dict[str, Any]:
    """Consume exactly one frozen complete generated observation for the pending proposal; preserve analytical unknowns and source-profile utility separately, with atomic budget/revision updates and retry receipts."""
    return campaigns.consume_campaign_snapshot(
        identity(ctx), campaign_id, proposal_id, snapshot_id, expected_revision, idempotency_key
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_campaign_consumption(consumption_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Retrieve the owned immutable consumption record, exact source digests, raw metrics, utility, analytical acceptance and declared expenditure."""
    return campaigns.get_campaign_consumption(identity(ctx), consumption_id)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_capabilities(ctx: Context) -> dict[str, Any]:
    """Report installed R backends and the experimental operations available."""
    identity(ctx)
    result = run_worker({"operation": "capabilities"})
    packages = result.get("packages", {})
    return {
        "service": "measure-separations",
        "qualification": "experimental",
        "backend": result,
        "input_formats": [
            "structured_traces",
            "delimited_single_trace",
            "delimited_wide",
            "delimited_long",
        ],
        "techniques": {
            "generic_1d_trace": bool(packages.get("measure") and packages.get("recipes")),
            "linear_response_quantitation": bool(
                packages.get("measure") and packages.get("recipes")
            ),
            "relative_sec": bool(
                result.get("sec_calibrated_curve_support")
                and packages.get("measure")
                and packages.get("recipes")
            ),
            "relative_sec_requires_corrected_development_revision": True,
        },
        "limits": {
            "traces_per_dataset": MAX_TRACES,
            "points_per_trace": MAX_POINTS,
            "points_per_dataset": MAX_DATASET_POINTS,
            "payload_bytes": MAX_PAYLOAD_BYTES,
            "page_size": MAX_PAGE,
        },
    }


@mcp.tool(annotations=WRITE, structured_output=True)
def measurement_register_dataset(dataset: Dataset, ctx: Context) -> dict[str, Any]:
    """Register bounded one-dimensional traces and their sample, injection, detector and unit metadata."""
    return register_dataset(identity(ctx), dataset)


@mcp.tool(annotations=WRITE, structured_output=True)
def measurement_register_csv(
    title: str,
    csv_text: str,
    axis_column: str,
    signal_column: str,
    metadata: TraceMetadata,
    ctx: Context,
    delimiter: Literal[",", "\t", ";"] = ",",
) -> dict[str, Any]:
    """Register one explicitly mapped numeric trace from bounded CSV/TSV text; metadata are the same as for a Trace, excluding axis and signal."""
    owner = identity(ctx)
    traces, source = parse_csv(
        csv_text, axis_column, [CSVChannel(column=signal_column, metadata=metadata)], delimiter
    )
    return register_dataset(owner, Dataset(title=title, traces=traces), source)


@mcp.tool(annotations=WRITE, structured_output=True)
def measurement_register_wide_csv(
    title: str,
    csv_text: str,
    axis_column: str,
    channels: Annotated[list[CSVChannel], Field(min_length=1, max_length=MAX_TRACES)],
    ctx: Context,
    delimiter: Literal[",", "\t", ";"] = ",",
) -> dict[str, Any]:
    """Register multiple explicitly mapped detector columns that share one numeric axis; retain the source text and mapping."""
    owner = identity(ctx)
    traces, source = parse_csv(csv_text, axis_column, channels, delimiter)
    return register_dataset(owner, Dataset(title=title, traces=traces), source)


@mcp.tool(annotations=WRITE, structured_output=True)
def measurement_register_long_csv(
    title: str,
    csv_text: str,
    axis_column: str,
    signal_column: str,
    group_columns: Annotated[list[str], Field(min_length=1, max_length=4)],
    groups: Annotated[list[CSVGroup], Field(min_length=1, max_length=MAX_TRACES)],
    ctx: Context,
    delimiter: Literal[",", "\t", ";"] = ",",
) -> dict[str, Any]:
    """Register row-oriented traces grouped by explicit column values, with a separate axis and metadata for each group."""
    owner = identity(ctx)
    traces, source = parse_long_csv(
        csv_text, axis_column, signal_column, group_columns, groups, delimiter
    )
    return register_dataset(owner, Dataset(title=title, traces=traces), source)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_inspect_dataset(dataset_id: str, ctx: Context) -> dict[str, Any]:
    """Inspect available traces, sample/injection identities, axes and units without returning raw arrays."""
    owner = identity(ctx)
    dataset, digest = dataset_for(owner, dataset_id)
    source = source_for(owner, dataset_id)
    traces = []
    for trace in dataset["traces"]:
        summary = {k: v for k, v in trace.items() if k not in ("axis", "signal")}
        summary.update(
            {
                "point_count": len(trace["axis"]),
                "axis_range": [trace["axis"][0], trace["axis"][-1]],
                "signal_range": [min(trace["signal"]), max(trace["signal"])],
            }
        )
        traces.append(summary)
    source_info = None if source is None else {k: v for k, v in source.items() if k != "content"}
    return {
        "dataset_id": dataset_id,
        "input_sha256": digest,
        "title": dataset["title"],
        "traces": traces,
        "source": source_info,
    }


@mcp.tool(annotations=READ, structured_output=True)
def measurement_read_source(
    dataset_id: str,
    ctx: Context,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE)] = 1000,
) -> dict[str, Any]:
    """Read a bounded character page of the retained original CSV/TSV text and its import mapping."""
    owner = identity(ctx)
    dataset_for(owner, dataset_id)
    source = source_for(owner, dataset_id)
    if source is None:
        raise ValueError("This dataset has no retained text source")
    content = source.pop("content")
    return {
        "dataset_id": dataset_id,
        **source,
        "offset": offset,
        "count": len(content),
        "next_offset": offset + limit if offset + limit < len(content) else None,
        "text": content[offset : offset + limit],
    }


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_create_chromatogram_batch(
    batch: BatchSpec, idempotency_key: RequestKey, ctx: Context
) -> dict[str, Any]:
    """Freeze owned trace selections, caller roles, import identities and versioned chromatogram settings; does not start analysis or verify assignments."""
    return batches.create_batch(identity(ctx), batch, idempotency_key)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_chromatogram_batch(batch_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Read an owned batch's frozen metadata manifest, dataset references, current revision and per-state counts without R; this response omits raw arrays."""
    return batches.get_batch(identity(ctx), batch_id)


@mcp.tool(annotations=READ_CAPTURE, structured_output=True)
async def measurement_export_chromatogram_batch(
    batch_id: RecordId,
    expected_revision: Annotated[StrictInt, Field(ge=1)],
    mode: Literal["references", "self_contained"],
    ctx: Context,
) -> dict[str, Any]:
    """Capture owned quiescent batch evidence at a revision without R; explicitly choose metadata references or self-contained source assets, then read immutable byte pages."""
    owner = identity(ctx)
    return await asyncio.to_thread(
        batch_exports.export_batch, owner, batch_id, expected_revision, mode
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_read_chromatogram_batch_export(
    snapshot_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    ctx: Context,
    offset: Annotated[StrictInt, Field(ge=0)] = 0,
    limit: Annotated[StrictInt, Field(ge=1, le=48_000)] = 48_000,
) -> dict[str, Any]:
    """Read an owner/store-bound immutable export byte page as base64; restart or eviction requires a new capture, without silently changing the snapshot."""
    return batch_exports.read_export(identity(ctx), snapshot_sha256, offset, limit)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_analytical_import(import_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Read the owned receipt mapping an imported evidence snapshot to new local datasets, historical analysis copies and a fresh pending batch; import is not fresh computation."""
    from .import_origins import get_import_receipt
    from .storage import registry_connection

    owner = identity(ctx)
    with registry_connection() as con:
        receipt = get_import_receipt(con, owner, import_id)
    if receipt is None:
        raise ValueError("Analytical import was not found for this principal")
    return receipt


@mcp.tool(annotations=READ, structured_output=True)
def measurement_read_chromatogram_batch_items(
    batch_id: RecordId,
    ctx: Context,
    offset: Annotated[StrictInt, Field(ge=0)] = 0,
    limit: Annotated[StrictInt, Field(ge=1, le=20)] = 20,
) -> dict[str, Any]:
    """Read bounded owned batch-item metadata, states and retained analysis links; does not return raw arrays or execution tokens."""
    return batches.read_items(identity(ctx), batch_id, offset, limit)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_read_chromatogram_batch_attempts(
    batch_id: RecordId,
    ordinal: Annotated[StrictInt, Field(ge=0, lt=MAX_BATCH_ITEMS)],
    ctx: Context,
    offset: Annotated[StrictInt, Field(ge=0)] = 0,
    limit: Annotated[StrictInt, Field(ge=1, le=20)] = 20,
) -> dict[str, Any]:
    """Read one owned item's attempt history with failures, interruption and observed or unavailable runtime evidence; ordinals start at zero."""
    return batches.read_attempts(identity(ctx), batch_id, ordinal, offset, limit)


@mcp.tool(annotations=READ, structured_output=True)
def measurement_read_chromatogram_batch_control_events(
    batch_id: RecordId,
    ctx: Context,
    offset: Annotated[StrictInt, Field(ge=0)] = 0,
    limit: Annotated[StrictInt, Field(ge=1, le=20)] = 20,
) -> dict[str, Any]:
    """Read owned immutable cancellation, recovery and selected-retry outcomes, including controls that affected no items; history survives later retries."""
    return batches.read_control_events(identity(ctx), batch_id, offset, limit)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
async def measurement_run_chromatogram_batch(
    batch_id: RecordId,
    idempotency_key: RequestKey,
    expected_revision: Annotated[StrictInt, Field(ge=1)],
    ctx: Context,
    max_items: Annotated[StrictInt, Field(ge=1, le=2)] = 2,
) -> dict[str, Any]:
    """Run at most two pending items with shared bounded R capacity and atomic result retention; replaying the same request key never executes more items."""
    owner = identity(ctx)
    from .batch_execution import run_batch

    return await asyncio.to_thread(
        run_batch, owner, batch_id, idempotency_key, expected_revision, max_items
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_chromatogram_batch_run(run_id: RecordId, ctx: Context) -> dict[str, Any]:
    """Read a retained run's fixed item membership and current durable outcomes without execution or R; claim tokens remain private."""
    return batches.get_run(identity(ctx), run_id)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_cancel_chromatogram_batch(
    batch_id: RecordId,
    idempotency_key: RequestKey,
    expected_revision: Annotated[StrictInt, Field(ge=1)],
    ctx: Context,
) -> dict[str, Any]:
    """Cancel pending items using a revision check; preserve successes and allow already running numerical work to finish."""
    return batches.cancel_batch(identity(ctx), batch_id, idempotency_key, expected_revision)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_recover_chromatogram_batch(
    batch_id: RecordId,
    idempotency_key: RequestKey,
    expected_revision: Annotated[StrictInt, Field(ge=1)],
    ctx: Context,
) -> dict[str, Any]:
    """Mark expired owned claims interrupted and fence late results; does not kill a process, repeat analysis or automatically retry."""
    return batches.recover_batch(identity(ctx), batch_id, idempotency_key, expected_revision)


@mcp.tool(annotations=RETRY_WRITE, structured_output=True)
def measurement_retry_chromatogram_batch_items(
    batch_id: RecordId,
    ordinals: Annotated[list[StrictInt], Field(min_length=1, max_length=MAX_BATCH_ITEMS)],
    idempotency_key: RequestKey,
    expected_revision: Annotated[StrictInt, Field(ge=1)],
    ctx: Context,
) -> dict[str, Any]:
    """Reset explicitly selected failed, interrupted or cancelled items to pending with a revision check; preserve attempt history and never reset completed items."""
    return batches.retry_items(
        identity(ctx), batch_id, idempotency_key, expected_revision, ordinals
    )


@mcp.tool(annotations=WRITE, structured_output=True)
def measurement_analyze_chromatogram(
    dataset_id: str, trace_id: str, method: ChromatogramMethod, ctx: Context
) -> dict[str, Any]:
    """Process one trace and detect/integrate peaks using measure; peak areas are not compound identities or concentrations."""
    owner = identity(ctx)
    dataset, digest = dataset_for(owner, dataset_id)
    trace = trace_for(dataset, trace_id)
    spec = method.model_dump(mode="json")
    result = analysis_runtime.process_chromatogram(trace, spec, worker=run_worker)
    return save_analysis(owner, dataset_id, trace_id, "chromatogram", spec, result, digest)


@mcp.tool(annotations=WRITE, structured_output=True)
def measurement_analyze_sec(
    dataset_id: str, trace_id: str, method: SECMethod, ctx: Context
) -> dict[str, Any]:
    """Calculate relative SEC molecular-weight moments from a baseline-corrected concentration trace and explicit standards."""
    owner = identity(ctx)
    dataset, digest = dataset_for(owner, dataset_id)
    trace = trace_for(dataset, trace_id)
    if trace["separation"] != "SEC" or trace["axis_unit"] not in ("min", "mL"):
        raise ValueError("SEC analysis requires an SEC trace with an axis in min or mL")
    spec = method.model_dump(mode="json")
    result = run_worker({"operation": "sec", "trace": trace, "method": spec})
    return save_analysis(owner, dataset_id, trace_id, "sec", spec, result, digest)


@mcp.tool(annotations=WRITE, structured_output=True)
def measurement_quantify_peak(
    analysis_id: str, peak_id: int, method: PeakQuantitationMethod, ctx: Context
) -> dict[str, Any]:
    """Quantify an explicitly assigned chromatogram peak with supplied linear response standards; does not establish peak identity."""
    owner = identity(ctx)
    source = analysis_for(owner, analysis_id)
    if source["kind"] != "chromatogram":
        raise ValueError("Peak quantitation requires a chromatogram analysis")
    peaks = source["result"].get("peaks", [])
    matches = [peak for peak in peaks if peak.get("peak_id") == peak_id]
    if len(matches) != 1 or not isinstance(matches[0].get("area"), (int, float)):
        raise ValueError("Peak was not found or does not have a finite integrated area")
    dataset, digest = dataset_for(owner, source["dataset_id"])
    trace = trace_for(dataset, source["trace_id"])
    spec = method.model_dump(mode="json") | {
        "source_analysis_id": analysis_id,
        "peak_id": peak_id,
        "response_unit": f"{trace['signal_unit']} * {trace['axis_unit']}",
    }
    result = run_worker(
        {"operation": "quantitation", "method": spec, "response": matches[0]["area"]}
    )
    return save_analysis(
        owner, source["dataset_id"], source["trace_id"], "peak_quantitation", spec, result, digest
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_chromatogram_evidence(analysis_id: str, ctx: Context) -> dict[str, Any]:
    """Capture owned stored chromatogram evidence and return its manifest/digest for bounded immutable paging; does not reanalyze or return large arrays."""
    owner = identity(ctx)
    store_identity = _evidence_store_identity()
    snapshot = analysis_evidence.capture_evidence(_owned_chromatogram_evidence(owner, analysis_id))
    manifest = analysis_evidence.manifest_for(snapshot)
    _cache_evidence_snapshot(
        owner, analysis_id, manifest["evidence_sha256"], snapshot, store_identity
    )
    return manifest


def _owned_chromatogram_evidence(owner: str, analysis_id: str) -> dict[str, Any]:
    record = analysis_for(owner, analysis_id)
    dataset, digest = dataset_for(owner, record["dataset_id"])
    return analysis_evidence.build_evidence(record, dataset, digest)


def _evidence_store_identity() -> tuple:
    path = db_path().resolve()
    try:
        stat = path.stat()
    except OSError as error:
        raise ValueError(
            "Evidence snapshot was not found or is not accessible; request a manifest"
        ) from error
    return str(path), stat.st_dev, stat.st_ino


def _cache_evidence_snapshot(
    owner: str, analysis_id: str, digest: str, snapshot: Any, store_identity: tuple
) -> None:
    global _EVIDENCE_SNAPSHOT_BYTES
    size = snapshot.serialized_size_bytes
    if size > MAX_EVIDENCE_CACHE_BYTES:
        raise ValueError("Evidence capture exceeds the process-local cache limit")
    if _evidence_store_identity() != store_identity:
        raise ValueError(
            "Database identity changed during evidence capture; request a new manifest"
        )
    key = (*store_identity, owner, analysis_id, digest)
    with _EVIDENCE_SNAPSHOT_LOCK:
        previous = _EVIDENCE_SNAPSHOTS.pop(key, None)
        if previous is not None:
            _EVIDENCE_SNAPSHOT_BYTES -= previous.serialized_size_bytes
        while _EVIDENCE_SNAPSHOTS and (
            len(_EVIDENCE_SNAPSHOTS) >= MAX_EVIDENCE_SNAPSHOTS
            or _EVIDENCE_SNAPSHOT_BYTES + size > MAX_EVIDENCE_CACHE_BYTES
        ):
            _, evicted = _EVIDENCE_SNAPSHOTS.popitem(last=False)
            _EVIDENCE_SNAPSHOT_BYTES -= evicted.serialized_size_bytes
        _EVIDENCE_SNAPSHOTS[key] = snapshot
        _EVIDENCE_SNAPSHOT_BYTES += size


def _cached_evidence_snapshot(owner: str, analysis_id: str, digest: str) -> Any:
    key = (*_evidence_store_identity(), owner, analysis_id, digest)
    with _EVIDENCE_SNAPSHOT_LOCK:
        snapshot = _EVIDENCE_SNAPSHOTS.get(key)
        if snapshot is None:
            raise ValueError(
                "Evidence snapshot was not found or is not accessible; request a manifest"
            )
        _EVIDENCE_SNAPSHOTS.move_to_end(key)
    return snapshot


@mcp.tool(annotations=READ, structured_output=True)
def measurement_read_chromatogram_evidence(
    analysis_id: str,
    kind: Literal["trace", "peaks"],
    ctx: Context,
    evidence_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    offset: Annotated[StrictInt, Field(ge=0)] = 0,
    limit: Annotated[StrictInt, Field(ge=1, le=1000)] = 1000,
) -> dict[str, Any]:
    """Read a bounded page from an owned immutable capture selected by its manifest digest; request a new manifest after cache eviction or server restart."""
    return analysis_evidence.page_for(
        _cached_evidence_snapshot(identity(ctx), analysis_id, evidence_sha256), kind, offset, limit
    )


@mcp.tool(annotations=READ, structured_output=True)
def measurement_get_analysis(analysis_id: str, ctx: Context) -> dict[str, Any]:
    """Retrieve a compact analysis record, method, diagnostics and provenance."""
    from .import_origins import get_import_origin
    from .storage import registry_connection

    owner = identity(ctx)
    record = analysis_for(owner, analysis_id)
    with registry_connection() as con:
        origin = get_import_origin(con, owner, "analysis", analysis_id)
        input_origin = get_import_origin(con, owner, "dataset", record["dataset_id"])
    result = record.pop("result")
    record.update(
        {
            "status": "completed_with_warnings" if result.get("warnings") else "completed",
            "qualification": "experimental",
            "summary": result.get("summary", {"peak_count": result.get("peak_count")}),
            "diagnostics": result.get("diagnostics"),
            "warnings": result.get("warnings", []),
            "notes": result.get("notes", []),
        }
    )
    if origin is not None:
        record["import_origin"] = origin
    if input_origin is not None:
        record["input_import_origin"] = input_origin
    return record


@mcp.tool(annotations=READ, structured_output=True)
def measurement_compare_sec(
    analysis_ids: Annotated[list[str], Field(min_length=2, max_length=8)], ctx: Context
) -> dict[str, Any]:
    """Compare stored SEC moments to the first analysis; report method and detector differences without assigning pass/fail."""
    if len(set(analysis_ids)) != len(analysis_ids):
        raise ValueError("Provide distinct analysis IDs")
    owner = identity(ctx)
    records = [analysis_for(owner, analysis_id) for analysis_id in analysis_ids]
    if any(record["kind"] != "sec" for record in records):
        raise ValueError("All selected analyses must be SEC analyses")
    traces = [
        trace_for(dataset_for(owner, record["dataset_id"])[0], record["trace_id"])
        for record in records
    ]
    reference = records[0]["result"]["summary"]
    fields = ("mn_g_mol", "mw_g_mol", "mz_g_mol", "mp_g_mol", "dispersity")
    rows = []
    for record in records:
        summary = record["result"]["summary"]
        changes = {}
        for field in fields:
            value, baseline = summary.get(field), reference.get(field)
            changes[field] = {
                "value": value,
                "difference": None if value is None or baseline is None else value - baseline,
                "percent_difference": None
                if value is None or not baseline
                else 100 * (value - baseline) / baseline,
            }
        rows.append(
            {
                "analysis_id": record["analysis_id"],
                "trace_id": record["trace_id"],
                "values": changes,
                "warnings": record["result"].get("warnings", []),
                "calibration_r_squared": (record["result"].get("diagnostics") or {}).get(
                    "r_squared"
                ),
            }
        )
    method_matches = all(
        r["provenance"]["method_sha256"] == records[0]["provenance"]["method_sha256"]
        for r in records
    )
    fingerprint = records[0]["provenance"].get("installed_r_code_md5") or {}
    fingerprints_match = bool(
        fingerprint.get("measure") and fingerprint.get("measure.sec")
    ) and all(r["provenance"].get("installed_r_code_md5") == fingerprint for r in records)
    detector_matches = all(
        (t["detector"], t["channel"], t["axis_unit"], t["signal_unit"])
        == (
            traces[0]["detector"],
            traces[0]["channel"],
            traces[0]["axis_unit"],
            traces[0]["signal_unit"],
        )
        for t in traces
    )
    return {
        "qualification": "experimental",
        "reference_analysis_id": analysis_ids[0],
        "results": rows,
        "comparability": {
            "same_method": method_matches,
            "same_backend_r_code_fingerprints": fingerprints_match,
            "same_detector_channel_and_units": detector_matches,
        },
        "notes": [
            "Comparability flags are checks, not proof of equivalent samples, methods, or instrument conditions.",
            "Differences are relative to the first analysis and do not establish statistical significance.",
        ],
        "artifact": True,
    }


@mcp.tool(annotations=READ, structured_output=True)
def measurement_read_analysis_data(
    analysis_id: str,
    kind: Literal["peaks", "processed_trace", "distribution"],
    ctx: Context,
    offset: int = 0,
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE)] = 100,
) -> dict[str, Any]:
    """Read a bounded page of peaks, processed trace points, or SEC distribution points."""
    if offset < 0:
        raise ValueError("offset must be nonnegative")
    record = analysis_for(identity(ctx), analysis_id)
    result = record["result"]
    if kind not in result:
        raise ValueError("This analysis does not contain the requested data")
    data = result[kind]
    if isinstance(data, list):
        count, page = len(data), data[offset : offset + limit]
    elif isinstance(data, dict):
        lengths = {len(v) for v in data.values() if isinstance(v, list)}
        if len(lengths) != 1:
            raise ValueError("Stored analysis data have inconsistent lengths")
        count = next(iter(lengths))
        page = {k: v[offset : offset + limit] for k, v in data.items()}
    else:
        raise ValueError("Stored analysis data have an unsupported shape")
    return {
        "analysis_id": analysis_id,
        "kind": kind,
        "offset": offset,
        "count": count,
        "next_offset": offset + limit if offset + limit < count else None,
        "data": page,
    }


app = wrap_http(
    wrap_authority_headers(connect_hosting.wrap_identity(mcp.streamable_http_app())),
    readiness_check=storage_ready,
)


def main() -> None:
    """Run the local MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
