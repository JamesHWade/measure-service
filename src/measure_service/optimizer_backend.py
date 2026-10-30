"""Optional subprocess adapter for the public BO-MCP BoTorch backend.

The service process validates and hashes campaign inputs, then sends a bounded
JSON request to an administrator-configured Python sidecar. The sidecar imports
the public ``bo_engine`` API and returns one model-guided suggestion. No optimizer
implementation is duplicated here and no backend state is persisted between
calls.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import logging
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
import warnings as python_warnings
from pathlib import Path
from typing import Any

from . import campaign_protocol as _campaign_protocol
from .campaign_protocol import (
    BO_POLICY_ID,
    CampaignProtocolSpec,
    CandidateCoordinates,
    campaign_protocol_sha256,
    method_declaration,
)
from .storage import canonical, sha256

REQUEST_SCHEMA = "measure-service-bo-request/1"
RESPONSE_SCHEMA = "measure-service-bo-response/1"
BACKEND_ID = "bo-engine/BoTorchBackend"
ADAPTER_PROFILE = "bo-mcp-gp-logei/1"
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_048_576
SIDECAR_TIMEOUT_SECONDS = 60
_CANDIDATE_KEYS = frozenset({"strong_solvent_fraction", "acquisition_duration_min"})
_DESIGN_KERNEL_SOURCE_PATH = (
    Path(__file__).resolve().parent.parent / "measure_methods" / "design.py"
)


class OptimizerBackendError(RuntimeError):
    """The configured BO sidecar could not produce a verified BO suggestion."""


class _ScientificWarningCapture(logging.Handler):
    """Collect bounded warning records from the scientific optimizer stack."""

    _LOGGER_PREFIXES = ("bo_engine", "botorch", "gpytorch", "torch", "scipy")
    _MAX_RECORDS = 32
    _MAX_MESSAGE_CHARS = 1000

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []
        self.omitted_records = 0
        self.truncated_records = 0

    def emit(self, record: logging.LogRecord) -> None:
        logger_name = record.name
        if not any(
            logger_name == prefix or logger_name.startswith(f"{prefix}.")
            for prefix in self._LOGGER_PREFIXES
        ):
            return
        if len(self.records) >= self._MAX_RECORDS:
            self.omitted_records += 1
            return
        try:
            message = record.getMessage()
        except Exception:
            message = "<warning message formatting failed>"
        message = " ".join(str(message).splitlines())
        source = f"logging.{record.levelname} [{logger_name}]: "
        truncation = " [message truncated]"
        max_message = self._MAX_MESSAGE_CHARS - len(source)
        if len(message) > max_message:
            keep = max(0, max_message - len(truncation))
            message = message[:keep] + truncation
            self.truncated_records += 1
        formatted = source + message
        self.records.append(formatted)

    def provenance_warnings(self) -> list[str]:
        result = list(self.records)
        if self.truncated_records:
            result.append(
                "logging.WARNING [capture]: "
                f"{self.truncated_records} scientific warning messages truncated to "
                f"{self._MAX_MESSAGE_CHARS} characters"
            )
        if self.omitted_records:
            result.append(
                "logging.WARNING [capture]: "
                f"{self.omitted_records} additional scientific warning records omitted "
                f"after the {self._MAX_RECORDS}-record limit"
            )
        return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _loads_strict(data: str | bytes, *, max_bytes: int) -> Any:
    if isinstance(data, bytes):
        raw = data
        text = raw.decode("utf-8", errors="strict")
    else:
        text = data
        raw = text.encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError("JSON message exceeds the configured size limit")
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _finite_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite JSON number")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{label} must be a finite JSON number") from error
    if not math.isfinite(normalized):
        raise ValueError(f"{label} must be a finite JSON number")
    return normalized


def _normalize_inputs(
    spec: CampaignProtocolSpec | dict[str, Any],
    training_data: list[dict[str, Any]],
    seed: int,
) -> tuple[CampaignProtocolSpec, list[dict[str, Any]], dict[str, Any]]:
    protocol = CampaignProtocolSpec.model_validate(spec)
    if BO_POLICY_ID not in protocol.policies:
        raise ValueError("the protocol does not enable the BO policy")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed not in protocol.seeds:
        raise ValueError("seed must be a predeclared protocol seed")
    if not isinstance(training_data, list):
        raise ValueError("training_data must be an ordered list")
    if len(training_data) < max(3, protocol.initial_design_size):
        raise ValueError("BO requires the complete initial design and at least three observations")
    if len(training_data) >= protocol.evaluation_budget:
        raise ValueError("the declared evaluation budget is exhausted")

    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(training_data):
        if not isinstance(row, dict) or set(row) != {"candidate", "score"}:
            raise ValueError(f"training_data row {index} must contain candidate and score only")
        candidate = row["candidate"]
        if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_KEYS:
            raise ValueError(f"training_data candidate {index} has an invalid shape")
        coordinates = CandidateCoordinates.model_validate(candidate)
        normalized_candidate = coordinates.model_dump(mode="json")
        strong = protocol.domain.strong_solvent_fraction
        duration = protocol.domain.acquisition_duration_min
        if not strong.lower <= coordinates.strong_solvent_fraction <= strong.upper:
            raise ValueError(f"training_data candidate {index} is outside the declared domain")
        if not duration.lower <= coordinates.acquisition_duration_min <= duration.upper:
            raise ValueError(f"training_data candidate {index} is outside the declared domain")
        method_declaration(protocol, normalized_candidate)
        score = _finite_number(row["score"], label=f"training_data score {index}")
        normalized.append({"candidate": normalized_candidate, "score": score})

    normalized_spec = protocol.model_dump(mode="json")
    request_payload = {
        "schema": REQUEST_SCHEMA,
        "spec": normalized_spec,
        "training_data": normalized,
        "seed": seed,
        "protocol_sha256": campaign_protocol_sha256(protocol),
        "training_sha256": sha256(normalized),
        "adapter_source_sha256": _file_sha256(Path(__file__)),
        "campaign_protocol_source_sha256": _file_sha256(Path(_campaign_protocol.__file__)),
        "design_kernel_source_sha256": _file_sha256(_DESIGN_KERNEL_SOURCE_PATH),
    }
    encoded = canonical(request_payload).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("optimizer request exceeds the configured size limit")
    return protocol, normalized, request_payload


def _runtime_path() -> Path:
    configured = os.environ.get("MEASURE_BO_PYTHON")
    if not configured:
        raise OptimizerBackendError(
            "BO backend is unavailable: configure MEASURE_BO_PYTHON to an installed sidecar interpreter"
        )
    interpreter = Path(configured)
    if (
        not interpreter.is_absolute()
        or not interpreter.is_file()
        or not os.access(interpreter, os.X_OK)
    ):
        raise OptimizerBackendError("MEASURE_BO_PYTHON must name an absolute executable file")
    return interpreter


def _validate_backend_response(
    response_value: object,
    *,
    protocol: CampaignProtocolSpec,
    training_data: list[dict[str, Any]],
    seed: int,
    protocol_digest: str,
    training_digest: str,
    adapter_source_digest: str,
    campaign_protocol_source_digest: str,
    design_kernel_source_digest: str,
) -> dict[str, Any]:
    if not isinstance(response_value, dict) or set(response_value) != {
        "schema",
        "candidate",
        "backend",
    }:
        raise OptimizerBackendError("BO sidecar returned an invalid response shape")
    if response_value["schema"] != RESPONSE_SCHEMA:
        raise OptimizerBackendError("BO sidecar returned an unsupported response schema")
    candidate_value = response_value["candidate"]
    if not isinstance(candidate_value, dict) or set(candidate_value) != _CANDIDATE_KEYS:
        raise OptimizerBackendError("BO sidecar returned an invalid candidate shape")
    try:
        candidate = CandidateCoordinates.model_validate(candidate_value).model_dump(mode="json")
        method_declaration(protocol, candidate)
    except Exception as error:
        raise OptimizerBackendError(
            "BO sidecar returned an invalid or infeasible candidate"
        ) from error

    backend = response_value["backend"]
    required = {
        "backend_id",
        "adapter_profile",
        "backend_name",
        "backend_class",
        "generation_method",
        "model_type",
        "acquisition_method",
        "acquisition_implementation",
        "optimization_strategy",
        "source_revision",
        "source_tree_sha256",
        "adapter_source_sha256",
        "campaign_protocol_source_sha256",
        "design_kernel_source_sha256",
        "distribution_versions",
        "protocol_sha256",
        "training_sha256",
        "training_count",
        "seed",
        "runtime",
        "settings",
        "warnings",
        "suggestion_provenance",
    }
    if not isinstance(backend, dict) or set(backend) != required:
        raise OptimizerBackendError("BO sidecar returned incomplete backend provenance")
    if (
        backend["backend_id"] != BACKEND_ID
        or backend["adapter_profile"] != ADAPTER_PROFILE
        or backend["backend_name"] != "bo-mcp"
        or backend["backend_class"] != "BoTorchBackend"
    ):
        raise OptimizerBackendError("BO sidecar did not report the required public BoTorch backend")
    if backend["generation_method"] != "bo":
        raise OptimizerBackendError("BO sidecar returned an initial-design or fallback suggestion")
    if backend["model_type"] != "SingleTaskGP":
        raise OptimizerBackendError("BO sidecar did not fit the required single-task GP")
    if backend["acquisition_method"] != "expected_improvement":
        raise OptimizerBackendError(
            "BO sidecar did not use the declared expected-improvement acquisition"
        )
    if backend["acquisition_implementation"] != "qLogExpectedImprovement":
        raise OptimizerBackendError("BO sidecar did not report qLogExpectedImprovement")
    if (
        backend["protocol_sha256"] != protocol_digest
        or backend["training_sha256"] != training_digest
    ):
        raise OptimizerBackendError("BO sidecar provenance does not bind the submitted inputs")
    if (
        backend["adapter_source_sha256"] != adapter_source_digest
        or backend["campaign_protocol_source_sha256"] != campaign_protocol_source_digest
        or backend["design_kernel_source_sha256"] != design_kernel_source_digest
    ):
        raise OptimizerBackendError(
            "BO sidecar source files differ from the caller's implementation"
        )
    if (
        type(backend["training_count"]) is not int
        or backend["training_count"] != len(training_data)
        or type(backend["seed"]) is not int
        or backend["seed"] != seed
    ):
        raise OptimizerBackendError(
            "BO sidecar provenance does not bind the submitted history and seed"
        )
    if not _is_sha256(backend["source_tree_sha256"]):
        raise OptimizerBackendError("BO sidecar omitted its source-tree digest")
    if backend["source_revision"] is not None and not _is_git_revision(backend["source_revision"]):
        raise OptimizerBackendError("BO sidecar source revision is malformed")
    versions = backend["distribution_versions"]
    if (
        not isinstance(versions, dict)
        or set(versions) != {"bo-engine", "botorch", "gpytorch", "measure-service", "torch"}
        or any(
            not isinstance(versions[name], str) or not versions[name]
            for name in ("bo-engine", "botorch", "gpytorch", "torch")
        )
        or (
            versions["measure-service"] is not None
            and not isinstance(versions["measure-service"], str)
        )
    ):
        raise OptimizerBackendError("BO sidecar omitted the installed bo-engine version")
    if not isinstance(backend["warnings"], list) or any(
        not isinstance(item, str) for item in backend["warnings"]
    ):
        raise OptimizerBackendError("BO sidecar warnings have an invalid shape")
    settings = backend["settings"]
    expected_setting_keys = {
        "parameters",
        "objective",
        "batch_size",
        "iteration",
        "acquisition_method",
        "acquisition_implementation",
        "input_transform",
        "outcome_transform",
        "kernel",
        "acquisition_optimization",
        "backend_state",
        "replay",
    }
    if not isinstance(settings, dict) or set(settings) != expected_setting_keys:
        raise OptimizerBackendError("BO sidecar settings are incomplete")
    if (
        settings["parameters"] != ["strong_solvent_fraction", "acquisition_duration_min"]
        or settings["objective"] != {"name": "utility", "direction": "maximize"}
        or settings["batch_size"] != 1
        or settings["iteration"] != len(training_data)
        or settings["acquisition_method"] != "expected_improvement"
        or settings["acquisition_implementation"] != "qLogExpectedImprovement"
    ):
        raise OptimizerBackendError("BO sidecar settings do not match the requested protocol")
    if not isinstance(settings["acquisition_optimization"], dict) or set(
        settings["acquisition_optimization"]
    ) != {
        "num_restarts",
        "raw_samples",
        "configured_num_restarts",
        "configured_raw_samples",
    }:
        raise OptimizerBackendError("BO sidecar omitted its acquisition optimization settings")
    if not isinstance(backend["runtime"], dict) or backend["runtime"].get("device") != "cpu":
        raise OptimizerBackendError("BO sidecar settings or runtime provenance is invalid")
    provenance = backend["suggestion_provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "iteration",
        "batch_index",
        "generation_method",
        "acquisition_function",
        "model_type",
        "random_seed",
        "confidence_level",
    }:
        raise OptimizerBackendError("BO sidecar suggestion provenance is invalid")
    if (
        provenance["iteration"] != len(training_data)
        or provenance["batch_index"] != 0
        or provenance["generation_method"] != "bo"
        or provenance["acquisition_function"] != "expected_improvement"
        or provenance["model_type"] != "SingleTaskGP (Gaussian Process)"
        or type(provenance["random_seed"]) is not int
    ):
        raise OptimizerBackendError(
            "BO sidecar suggestion provenance contradicts its reported strategy"
        )
    result = {"candidate": candidate, "backend": backend}
    try:
        canonical(result)
    except (TypeError, ValueError) as error:
        raise OptimizerBackendError(
            "BO sidecar response contains non-JSON or non-finite values"
        ) from error
    return result


def suggest_candidate(
    spec: CampaignProtocolSpec | dict[str, Any],
    training_data: list[dict[str, Any]],
    seed: int,
) -> dict[str, Any]:
    """Ask the configured BO-MCP sidecar for one model-guided candidate.

    The ordered observations are replayed on every call. The sidecar is selected
    only by the administrator's ``MEASURE_BO_PYTHON`` setting; requests cannot
    choose an executable or submit arbitrary backend options.
    """
    protocol, normalized_training, request_payload = _normalize_inputs(spec, training_data, seed)
    interpreter = _runtime_path()
    with tempfile.TemporaryDirectory(prefix="measure-bo-sidecar-") as runtime_directory:
        environment = os.environ.copy()
        environment.update(
            {
                "XDG_CACHE_HOME": runtime_directory,
                "TORCH_HOME": runtime_directory,
                "PYTHONPYCACHEPREFIX": runtime_directory,
                "PYTHONHASHSEED": "0",
                "PYTHONNOUSERSITE": "1",
                "BO_ENGINE_DEVICE": "cpu",
                "BO_ENGINE_DISABLE_GPU": "1",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "VECLIB_MAXIMUM_THREADS": "1",
            }
        )
        environment.pop("PYTHONHOME", None)
        environment.pop("PYTHONPATH", None)
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [str(interpreter), "-m", "measure_service.optimizer_backend"],
                input=canonical(request_payload),
                text=True,
                encoding="utf-8",
                capture_output=True,
                cwd=runtime_directory,
                env=environment,
                timeout=SIDECAR_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise OptimizerBackendError(
                "BO sidecar exceeded the 60-second runtime limit"
            ) from error
        except OSError as error:
            raise OptimizerBackendError("BO sidecar could not be started") from error
        finally:
            elapsed_seconds = time.perf_counter() - started
    if completed.returncode != 0:
        raise OptimizerBackendError(
            f"BO sidecar failed with exit status {completed.returncode}; details are available in its stderr"
        )
    if len(completed.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise OptimizerBackendError("BO sidecar response exceeds the configured size limit")
    try:
        response = _loads_strict(completed.stdout, max_bytes=MAX_RESPONSE_BYTES)
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise OptimizerBackendError("BO sidecar returned malformed JSON") from error
    result = _validate_backend_response(
        response,
        protocol=protocol,
        training_data=normalized_training,
        seed=seed,
        protocol_digest=request_payload["protocol_sha256"],
        training_digest=request_payload["training_sha256"],
        adapter_source_digest=request_payload["adapter_source_sha256"],
        campaign_protocol_source_digest=request_payload["campaign_protocol_source_sha256"],
        design_kernel_source_digest=request_payload["design_kernel_source_sha256"],
    )
    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise OptimizerBackendError("BO sidecar elapsed time was not a finite nonnegative value")
    result["backend"]["runtime"]["elapsed_seconds"] = elapsed_seconds
    try:
        canonical(result)
    except (TypeError, ValueError) as error:
        raise OptimizerBackendError("BO sidecar result contains invalid runtime timing") from error
    return result


def _require_exact_keys(value: object, keys: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} has an invalid shape")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_git_revision(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(character in "0123456789abcdef" for character in value)
    )


def _dist_info(name: str) -> dict[str, Any]:
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return {"version": None, "source_revision": None}
    revision = None
    raw_direct_url = distribution.read_text("direct_url.json")
    if raw_direct_url:
        try:
            direct_url = _loads_strict(raw_direct_url, max_bytes=16_384)
        except (UnicodeError, ValueError, json.JSONDecodeError):
            direct_url = None
        if isinstance(direct_url, dict):
            vcs = direct_url.get("vcs_info")
            if isinstance(vcs, dict):
                candidate = vcs.get("commit_id")
                if isinstance(candidate, str) and len(candidate) in (40, 64):
                    if all(character in "0123456789abcdefABCDEF" for character in candidate):
                        revision = candidate.lower()
    return {"version": distribution.version, "source_revision": revision}


def _bo_source_tree_digest(module: Any) -> str:
    module_path = Path(module.__file__).resolve()
    source_root = module_path.parent
    files = sorted(path for path in source_root.rglob("*.py") if path.is_file())
    if not files:
        raise RuntimeError("the installed bo_engine source tree is empty")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(source_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def _field(value: object, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _sidecar_response(request: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema",
        "spec",
        "training_data",
        "seed",
        "protocol_sha256",
        "training_sha256",
        "adapter_source_sha256",
        "campaign_protocol_source_sha256",
        "design_kernel_source_sha256",
    }
    request = _require_exact_keys(request, expected, label="request")
    if request["schema"] != REQUEST_SCHEMA:
        raise ValueError("unsupported request schema")
    if request["adapter_source_sha256"] != _file_sha256(Path(__file__)):
        raise ValueError("the sidecar adapter source does not match the caller")
    if request["campaign_protocol_source_sha256"] != _file_sha256(
        Path(_campaign_protocol.__file__)
    ):
        raise ValueError("the sidecar campaign protocol source does not match the caller")
    if request["design_kernel_source_sha256"] != _file_sha256(_DESIGN_KERNEL_SOURCE_PATH):
        raise ValueError("the sidecar design kernel source does not match the caller")
    protocol, training, checked_request = _normalize_inputs(
        request["spec"], request["training_data"], request["seed"]
    )
    if (
        request["protocol_sha256"] != checked_request["protocol_sha256"]
        or request["training_sha256"] != checked_request["training_sha256"]
    ):
        raise ValueError("input digests do not match the request content")

    try:
        bo_engine = importlib.import_module("bo_engine")
        backend_module = importlib.import_module("bo_engine.botorch_backend")
        types = importlib.import_module("bo_engine.types")
        backend_class = getattr(backend_module, "BoTorchBackend")
        OptimizationSpec = getattr(types, "OptimizationSpec")
        ParameterSpec = getattr(types, "ParameterSpec")
        ParameterType = getattr(types, "ParameterType")
        ObjectiveSpec = getattr(types, "ObjectiveSpec")
        AcquisitionMethod = getattr(types, "AcquisitionMethod")
        ObservationData = getattr(types, "ObservationData")
    except (ImportError, AttributeError) as error:
        raise RuntimeError("the public bo-engine BoTorch backend is not installed") from error

    strong_bounds = protocol.domain.strong_solvent_fraction
    duration_bounds = protocol.domain.acquisition_duration_min
    optimizer_spec = OptimizationSpec(
        parameters=[
            ParameterSpec(
                name="strong_solvent_fraction",
                type=ParameterType.CONTINUOUS,
                bounds=(strong_bounds.lower, strong_bounds.upper),
            ),
            ParameterSpec(
                name="acquisition_duration_min",
                type=ParameterType.CONTINUOUS,
                bounds=(duration_bounds.lower, duration_bounds.upper),
            ),
        ],
        objectives=[ObjectiveSpec(name="utility", minimize=False)],
        batch_size=1,
        initial_design_size=protocol.initial_design_size,
        random_seed=request["seed"],
        acquisition_method=AcquisitionMethod.EXPECTED_IMPROVEMENT,
    )
    observations = [
        ObservationData(
            parameter_values=row["candidate"],
            objective_values={"utility": row["score"]},
            result_id=f"measure-history-{ordinal:04d}",
        )
        for ordinal, row in enumerate(training)
    ]
    backend = backend_class()
    log_capture = _ScientificWarningCapture()
    root_logger = logging.getLogger()
    root_logger.addHandler(log_capture)
    try:
        with python_warnings.catch_warnings(record=True) as captured_warnings:
            python_warnings.simplefilter("always")
            batch = backend.generate_suggestions(
                optimizer_spec,
                observations,
                batch_size=1,
                iteration=len(observations),
                backend_state=None,
                pending_points=[],
            )
    finally:
        root_logger.removeHandler(log_capture)
        log_capture.close()
    suggestions = _field(batch, "suggestions")
    if not isinstance(suggestions, list) or len(suggestions) != 1:
        raise RuntimeError("the public backend did not return exactly one suggestion")
    suggestion = suggestions[0]
    candidate = _field(suggestion, "parameter_values")
    provenance = _field(suggestion, "provenance")
    method_info = _field(batch, "method_info")
    generation_method = _field(provenance, "generation_method")
    acquisition_method = _field(provenance, "acquisition_function")
    suggestion_model_type = _field(provenance, "model_type")
    reported_model_type = _field(method_info, "model_type")
    reported_acquisition = _field(method_info, "acquisition_function")
    optimization_strategy = _field(method_info, "optimization_strategy")
    if generation_method != "bo":
        raise RuntimeError("the public backend returned a non-BO suggestion")
    if reported_model_type != "SingleTaskGP" or not isinstance(suggestion_model_type, str):
        raise RuntimeError("the public backend did not report a single-task GP")
    if acquisition_method != AcquisitionMethod.EXPECTED_IMPROVEMENT.value:
        raise RuntimeError("the public backend did not use expected improvement")
    if reported_acquisition != AcquisitionMethod.EXPECTED_IMPROVEMENT.value:
        raise RuntimeError("the public backend method report does not match expected improvement")
    if not isinstance(optimization_strategy, str) or not optimization_strategy:
        raise RuntimeError("the public backend did not report its optimization strategy")
    if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_KEYS:
        raise RuntimeError("the public backend returned an invalid candidate shape")
    candidate = CandidateCoordinates.model_validate(candidate).model_dump(mode="json")
    method_declaration(protocol, candidate)

    distribution_names = {
        "bo-engine": "bo-engine",
        "botorch": "botorch",
        "gpytorch": "gpytorch",
        "torch": "torch",
    }
    versions = {
        key: _dist_info(dist_name)["version"] for key, dist_name in distribution_names.items()
    }
    if any(version is None for version in versions.values()):
        raise RuntimeError("the public BoTorch backend distribution versions are unavailable")
    direct = _dist_info("bo-engine")
    try:
        service_dist = _dist_info("measure-service")
    except Exception:
        service_dist = {"version": None, "source_revision": None}
    warnings = _field(batch, "warnings")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise RuntimeError("the public backend returned malformed warnings")
    method_warnings = _field(method_info, "warnings")
    if not isinstance(method_warnings, list) or any(
        not isinstance(item, str) for item in method_warnings
    ):
        raise RuntimeError("the public backend method report returned malformed warnings")
    captured = [
        f"{warning.category.__name__}: {str(warning.message)[:2000]}"
        for warning in captured_warnings[:100]
    ]
    if len(captured_warnings) > 100:
        captured.append("additional backend runtime warnings omitted after the first 100")
    warnings = list(
        dict.fromkeys([*warnings, *method_warnings, *captured, *log_capture.provenance_warnings()])
    )
    effective_restarts, effective_raw_samples = optimizer_spec.acquisition_optimization.resolve(2)
    normalized_provenance = {
        "iteration": _field(provenance, "iteration"),
        "batch_index": _field(provenance, "batch_index"),
        "generation_method": generation_method,
        "acquisition_function": acquisition_method,
        "model_type": suggestion_model_type,
        "random_seed": _field(provenance, "random_seed"),
        "confidence_level": _field(provenance, "confidence_level"),
    }
    response_backend = {
        "backend_id": BACKEND_ID,
        "adapter_profile": ADAPTER_PROFILE,
        "backend_name": "bo-mcp",
        "backend_class": type(backend).__name__,
        "generation_method": generation_method,
        "model_type": reported_model_type,
        "acquisition_method": reported_acquisition,
        "acquisition_implementation": "qLogExpectedImprovement",
        "optimization_strategy": optimization_strategy,
        "source_revision": direct["source_revision"],
        "source_tree_sha256": _bo_source_tree_digest(bo_engine),
        "adapter_source_sha256": _file_sha256(Path(__file__)),
        "campaign_protocol_source_sha256": _file_sha256(Path(_campaign_protocol.__file__)),
        "design_kernel_source_sha256": _file_sha256(_DESIGN_KERNEL_SOURCE_PATH),
        "distribution_versions": {
            **versions,
            "measure-service": service_dist["version"],
        },
        "protocol_sha256": checked_request["protocol_sha256"],
        "training_sha256": checked_request["training_sha256"],
        "training_count": len(training),
        "seed": request["seed"],
        "runtime": {
            "python_version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": sys.platform,
            "device": "cpu",
        },
        "settings": {
            "parameters": ["strong_solvent_fraction", "acquisition_duration_min"],
            "objective": {"name": "utility", "direction": "maximize"},
            "batch_size": 1,
            "iteration": len(training),
            "acquisition_method": "expected_improvement",
            "acquisition_implementation": "qLogExpectedImprovement",
            "input_transform": "BoTorch SingleTaskGP default input normalization",
            "outcome_transform": "BoTorch SingleTaskGP default outcome standardization",
            "kernel": (
                "RBF with automatic relevance determination (ARD); BoTorch SingleTaskGP "
                "default, no custom covar_module"
            ),
            "acquisition_optimization": {
                "num_restarts": effective_restarts,
                "raw_samples": effective_raw_samples,
                "configured_num_restarts": optimizer_spec.acquisition_optimization.num_restarts,
                "configured_raw_samples": optimizer_spec.acquisition_optimization.raw_samples,
            },
            "backend_state": "stateless; no prior backend state supplied",
            "replay": "ordered training history is supplied on every call",
        },
        "warnings": list(warnings),
        "suggestion_provenance": normalized_provenance,
    }
    canonical(response_backend)
    return {"schema": RESPONSE_SCHEMA, "candidate": candidate, "backend": response_backend}


def _main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        request = _loads_strict(raw, max_bytes=MAX_REQUEST_BYTES)
        response = _sidecar_response(request)
        encoded = canonical(response).encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds the configured size limit")
    except Exception as error:
        # Do not echo request values, filesystem paths or arbitrary backend
        # exception text to stderr. The service caller gets a stable failure.
        print(f"BO sidecar failed ({type(error).__name__})", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
