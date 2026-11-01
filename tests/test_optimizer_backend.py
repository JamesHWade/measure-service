"""Contract tests for the isolated public BO-MCP backend adapter."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import subprocess
import sys
import types
from enum import Enum
from pathlib import Path

import pytest

from measure_service import optimizer_backend
from measure_service.campaign_protocol import (
    AblationProtocolSpec,
    CampaignProtocolSpec,
    campaign_protocol_sha256,
    optimizer_configuration,
    parse_campaign_protocol,
)
from measure_service.optimizer_backend import (
    MAX_REQUEST_BYTES,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    OptimizerBackendError,
    _loads_strict,
    suggest_candidate,
    suggestion_request_evidence,
)
from measure_service.storage import canonical, sha256


def _spec() -> CampaignProtocolSpec:
    return CampaignProtocolSpec.model_validate(
        {
            "domain": {
                "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
                "acquisition_duration_min": {"lower": 1.0, "upper": 10.0},
            },
            "flow_ml_min": 0.5,
            "constraints": {},
            "tasks": [
                {
                    "task_id": "task-a",
                    "producer_id": "producer-a",
                    "observation_contract_id": "contract-a",
                }
            ],
            "seeds": [7],
            "evaluation_budget": 6,
            "initial_design_size": 3,
            "evaluation_cost": 1.0,
            "evaluation_cost_unit": "candidate-evaluation",
        }
    )


def _ablation_spec(
    *,
    target: str = "historical_utility",
    noise_prior: str = "default",
) -> AblationProtocolSpec:
    payload = _spec().model_dump(mode="json")
    payload.update(
        {
            "profile": "synthetic-isocratic-ablation/1",
            "optimizer_target": target,
            "surrogate_noise_prior": noise_prior,
        }
    )
    return parse_campaign_protocol(payload)


def _history() -> list[dict[str, object]]:
    return [
        {
            "candidate": {
                "strong_solvent_fraction": strong,
                "acquisition_duration_min": duration,
            },
            "score": score,
        }
        for strong, duration, score in ((0.2, 2.0, 0.1), (0.5, 5.0, 0.7), (0.8, 9.0, 0.4))
    ]


def _install_fake_public_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    generation_method: str = "bo",
    log_warning: str | None = None,
) -> list[object]:
    package = types.ModuleType("bo_engine")
    package.__file__ = "/synthetic/bo_engine/__init__.py"
    package.__path__ = []
    backend_module = types.ModuleType("bo_engine.botorch_backend")
    types_module = types.ModuleType("bo_engine.types")
    constants_module = types.ModuleType("bo_engine.constants")
    constants_module.NOISE_PRIOR_MIN_INFERRED = 1e-4
    optimizer_specs: list[object] = []

    class ParameterType(str, Enum):
        CONTINUOUS = "continuous"

    class AcquisitionMethod(str, Enum):
        EXPECTED_IMPROVEMENT = "expected_improvement"

    class ParameterSpec:
        def __init__(self, *, name, type, bounds):
            self.name = name
            self.type = type
            self.bounds = bounds

    class ObjectiveSpec:
        def __init__(self, *, name, minimize):
            self.name = name
            self.minimize = minimize

    class AcquisitionOptimizationConfig:
        num_restarts = None
        raw_samples = None

        def resolve(self, dimensions):
            assert dimensions == 2
            return 14, 512

    class OptimizationSpec:
        def __init__(self, **values):
            self.__dict__.update(values)
            self.acquisition_optimization = AcquisitionOptimizationConfig()
            optimizer_specs.append(self)

    class ObservationData:
        def __init__(self, **values):
            self.__dict__.update(values)

    class BoTorchBackend:
        def generate_suggestions(
            self,
            spec,
            observations,
            *,
            batch_size,
            iteration,
            backend_state,
            pending_points,
        ):
            assert [item.name for item in spec.parameters] == [
                "strong_solvent_fraction",
                "acquisition_duration_min",
            ]
            assert len(spec.objectives) == 1 and not spec.objectives[0].minimize
            objective_name = spec.objectives[0].name
            assert all(set(row.objective_values) == {objective_name} for row in observations)
            assert spec.acquisition_method is AcquisitionMethod.EXPECTED_IMPROVEMENT
            assert spec.random_seed == 7
            assert len(observations) == 3
            assert [row.result_id for row in observations] == [
                "measure-history-0000",
                "measure-history-0001",
                "measure-history-0002",
            ]
            assert batch_size == 1 and backend_state is None and pending_points == []
            if log_warning is not None:
                logging.getLogger("bo_engine.acquisition").warning("%s", log_warning)
            candidate = {
                name: (parameter.bounds[0] + parameter.bounds[1]) / 2
                for name, parameter in zip(
                    ("strong_solvent_fraction", "acquisition_duration_min"), spec.parameters
                )
            }
            provenance = {
                "iteration": iteration,
                "batch_index": 0,
                "generation_method": generation_method,
                "acquisition_function": "expected_improvement",
                "model_type": "SingleTaskGP (Gaussian Process)",
                "random_seed": 7,
                "confidence_level": "medium",
            }
            return {
                "suggestions": [{"parameter_values": candidate, "provenance": provenance}],
                "method_info": {
                    "model_type": "SingleTaskGP",
                    "acquisition_function": "expected_improvement",
                    "optimization_strategy": "L-BFGS-B with random restarts",
                    "warnings": ["sample warning"],
                },
                "warnings": [],
            }

    types_module.ParameterType = ParameterType
    types_module.AcquisitionMethod = AcquisitionMethod
    types_module.ParameterSpec = ParameterSpec
    types_module.ObjectiveSpec = ObjectiveSpec
    types_module.OptimizationSpec = OptimizationSpec
    types_module.ObservationData = ObservationData
    backend_module.BoTorchBackend = BoTorchBackend
    monkeypatch.setitem(sys.modules, "bo_engine", package)
    monkeypatch.setitem(sys.modules, "bo_engine.constants", constants_module)
    monkeypatch.setitem(sys.modules, "bo_engine.botorch_backend", backend_module)
    monkeypatch.setitem(sys.modules, "bo_engine.types", types_module)
    monkeypatch.setattr(optimizer_backend, "_bo_source_tree_digest", lambda _: "a" * 64)
    monkeypatch.setattr(
        optimizer_backend,
        "_dist_info",
        lambda name: {
            "version": {
                "bo-engine": "0.1.0",
                "botorch": "0.16.1",
                "gpytorch": "1.15",
                "torch": "2.9.1",
                "measure-service": "0.1.0.dev0",
            }[name],
            "source_revision": "b" * 40 if name == "bo-engine" else None,
        },
    )
    return optimizer_specs


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expected_failure: bool = False,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def run(args, **kwargs):
        calls.append({"args": args, **kwargs})
        assert args == [sys.executable, "-m", "measure_service.optimizer_backend"]
        assert kwargs["timeout"] == 60
        assert kwargs["env"].get("PYTHONPATH") is None
        assert kwargs["env"].get("PYTHONHOME") is None
        assert kwargs["env"].get("HOME") == os.environ.get("HOME")
        assert kwargs["env"]["BO_ENGINE_DEVICE"] == "cpu"
        assert kwargs["text"] is False
        assert isinstance(kwargs["input"], bytes)
        assert kwargs["cwd"] != str(Path(__file__).resolve().parents[1])
        request = _loads_strict(kwargs["input"], max_bytes=MAX_REQUEST_BYTES)
        assert request["schema"] == REQUEST_SCHEMA
        assert len(request["adapter_source_sha256"]) == 64
        assert len(request["campaign_protocol_source_sha256"]) == 64
        assert len(request["design_kernel_source_sha256"]) == 64
        try:
            response = optimizer_backend._sidecar_response(request)
        except Exception as error:
            if expected_failure:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    b"",
                    f"BO sidecar failed ({type(error).__name__})\n".encode("utf-8"),
                )
            raise
        return subprocess.CompletedProcess(args, 0, (canonical(response) + "\n").encode(), b"")

    monkeypatch.setattr(optimizer_backend.subprocess, "run", run)
    return calls


def _assert_failure_evidence(
    error: OptimizerBackendError,
    request: dict[str, object],
    *,
    phase: str,
    category: str,
    elapsed: float | None,
    returncode: int | None = None,
    response: bytes | None = None,
    stderr: bytes | None = None,
) -> None:
    evidence = error.diagnostics
    assert evidence is not None
    assert evidence["profile"] == "measure-service-bo-suggestion-failure/1"
    assert evidence["phase"] == phase
    assert evidence["category"] == category
    assert evidence["request_sha256"] == sha256(request)
    assert evidence["protocol_sha256"] == request["protocol_sha256"]
    assert evidence["training_sha256"] == request["training_sha256"]
    assert evidence["source_sha256"] == {
        "optimizer_adapter": request["adapter_source_sha256"],
        "campaign_protocol": request["campaign_protocol_source_sha256"],
        "design_kernel": request["design_kernel_source_sha256"],
    }
    assert evidence["subprocess_elapsed_seconds"] == elapsed
    assert evidence["returncode"] == returncode
    assert evidence["response_bytes"] == (None if response is None else len(response))
    assert evidence["stderr_bytes"] == (None if stderr is None else len(stderr))
    assert evidence["response_sha256"] == (
        None if response is None else hashlib.sha256(response).hexdigest()
    )
    assert set(evidence) == {
        "profile",
        "phase",
        "category",
        "request_sha256",
        "protocol_sha256",
        "training_sha256",
        "source_sha256",
        "subprocess_elapsed_seconds",
        "returncode",
        "response_bytes",
        "stderr_bytes",
        "response_sha256",
    }


def test_suggestion_request_evidence_is_exact_normalized_payload_without_runtime_lookup(
    monkeypatch,
):
    protocol = _ablation_spec(target="confirmed_acceptance_reward", noise_prior="low_noise")
    training = _history()
    for row, score in zip(training, (0.0, 1.0, 0.0), strict=True):
        row["score"] = score
    expected = optimizer_backend._normalize_inputs(protocol, training, 7)[2]
    monkeypatch.setattr(
        optimizer_backend,
        "_runtime_path",
        lambda: pytest.fail("request evidence must not resolve a sidecar runtime"),
    )
    monkeypatch.setattr(
        optimizer_backend.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("request evidence must not launch a process"),
    )

    evidence = suggestion_request_evidence(protocol, training, 7)

    assert evidence == expected
    assert evidence["spec"]["optimizer_target"] == "confirmed_acceptance_reward"
    assert evidence["spec"]["surrogate_noise_prior"] == "low_noise"
    assert evidence["training_data"] == expected["training_data"]
    assert all(
        len(evidence[name]) == 64
        for name in (
            "protocol_sha256",
            "training_sha256",
            "adapter_source_sha256",
            "campaign_protocol_source_sha256",
            "design_kernel_source_sha256",
        )
    )


@pytest.mark.parametrize(
    ("configured_runtime", "phase", "category", "message"),
    [
        (
            None,
            "runtime_resolution",
            "runtime_missing",
            "BO backend is unavailable: configure MEASURE_BO_PYTHON to an installed sidecar interpreter",
        ),
        (
            "/missing/bo-python",
            "runtime_resolution",
            "runtime_invalid",
            "MEASURE_BO_PYTHON must name an absolute executable file",
        ),
    ],
)
def test_runtime_resolution_errors_include_request_evidence_without_process(
    monkeypatch, configured_runtime, phase, category, message
):
    if configured_runtime is None:
        monkeypatch.delenv("MEASURE_BO_PYTHON", raising=False)
    else:
        monkeypatch.setenv("MEASURE_BO_PYTHON", configured_runtime)
    calls = []
    monkeypatch.setattr(
        optimizer_backend.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    request = suggestion_request_evidence(_spec(), _history(), 7)

    with pytest.raises(OptimizerBackendError) as captured:
        suggest_candidate(_spec(), _history(), 7)

    assert captured.value.args == (message,)
    assert str(captured.value) == message
    assert calls == []
    _assert_failure_evidence(
        captured.value,
        request,
        phase=phase,
        category=category,
        elapsed=None,
    )


def test_backend_error_keeps_legacy_constructor_and_filters_diagnostics():
    request = suggestion_request_evidence(_spec(), _history(), 7)
    legacy = OptimizerBackendError("legacy failure")
    diagnostics = optimizer_backend._failure_diagnostics(
        request,
        phase="runtime_resolution",
        category="runtime_missing",
    )
    diagnostics.update({"stdout": "secret response", "private_path": "/home/person/file"})
    enriched = OptimizerBackendError("same failure", diagnostics=diagnostics)

    assert legacy.args == ("legacy failure",)
    assert str(legacy) == "legacy failure"
    assert legacy.diagnostics is None
    assert enriched.args == ("same failure",)
    assert "stdout" not in enriched.diagnostics
    assert "private_path" not in enriched.diagnostics
    assert len(canonical(enriched.diagnostics).encode("utf-8")) <= 4096


@pytest.mark.parametrize(
    ("case", "phase", "category", "elapsed", "returncode", "response", "stderr"),
    [
        (
            "launch",
            "process_launch",
            "launch_error",
            None,
            None,
            None,
            None,
        ),
        (
            "timeout",
            "process_timeout",
            "timeout",
            0.75,
            None,
            b"partial-sidecar-output",
            b"private timeout detail",
        ),
        (
            "nonzero",
            "process_exit",
            "nonzero_exit",
            0.25,
            7,
            b"opaque sidecar response",
            b"private sidecar stderr",
        ),
    ],
)
def test_launch_timeout_and_exit_errors_record_bounded_process_evidence(
    monkeypatch, case, phase, category, elapsed, returncode, response, stderr
):
    monkeypatch.setenv("MEASURE_BO_PYTHON", sys.executable)
    request = suggestion_request_evidence(_spec(), _history(), 7)
    output_bytes = response
    stderr_bytes = stderr

    def run(args, **kwargs):
        if case == "launch":
            raise FileNotFoundError("private /home/person/bo-runtime")
        if case == "timeout":
            raise subprocess.TimeoutExpired(
                args, kwargs["timeout"], output=output_bytes, stderr=stderr_bytes
            )
        return subprocess.CompletedProcess(args, returncode, output_bytes, stderr_bytes)

    monkeypatch.setattr(optimizer_backend.subprocess, "run", run)
    ticks = iter((4.0, 4.75) if case == "timeout" else (4.0, 4.25))
    monkeypatch.setattr(optimizer_backend.time, "perf_counter", lambda: next(ticks))

    with pytest.raises(OptimizerBackendError) as captured:
        suggest_candidate(_spec(), _history(), 7)

    assert "/home/person" not in str(captured.value)
    _assert_failure_evidence(
        captured.value,
        request,
        phase=phase,
        category=category,
        elapsed=elapsed,
        returncode=returncode,
        response=response,
        stderr=stderr,
    )


@pytest.mark.parametrize(
    ("stdout", "phase", "category", "message", "elapsed"),
    [
        (
            b"x" * (optimizer_backend.MAX_RESPONSE_BYTES + 1),
            "response_size",
            "oversized_response",
            "response exceeds",
            0.5,
        ),
        (b"\xffinvalid-utf8", "response_decode", "invalid_utf8", "malformed JSON", 0.0),
        (b"{invalid-json", "response_parse", "invalid_json", "malformed JSON", 0.5),
        (
            b'{"schema":"unsupported"}',
            "response_validation",
            "invalid_response",
            "invalid response shape",
            0.5,
        ),
    ],
)
def test_response_failures_record_hashes_and_actual_elapsed_time(
    monkeypatch, stdout, phase, category, message, elapsed
):
    monkeypatch.setenv("MEASURE_BO_PYTHON", sys.executable)
    request = suggestion_request_evidence(_spec(), _history(), 7)
    stderr = b"private response detail"
    monkeypatch.setattr(
        optimizer_backend.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout, stderr),
    )
    ticks = iter((8.0, 8.0 + elapsed))
    monkeypatch.setattr(optimizer_backend.time, "perf_counter", lambda: next(ticks))

    with pytest.raises(OptimizerBackendError, match=message) as captured:
        suggest_candidate(_spec(), _history(), 7)

    _assert_failure_evidence(
        captured.value,
        request,
        phase=phase,
        category=category,
        elapsed=elapsed,
        returncode=0,
        response=stdout,
        stderr=stderr,
    )
    assert "private response detail" not in repr(captured.value.diagnostics)


def test_invalid_subprocess_clock_is_not_serialized_as_elapsed_time(monkeypatch):
    _install_fake_public_backend(monkeypatch)
    monkeypatch.setenv("MEASURE_BO_PYTHON", sys.executable)
    _patch_subprocess(monkeypatch)
    request = suggestion_request_evidence(_spec(), _history(), 7)
    ticks = iter((10.0, 9.0))
    monkeypatch.setattr(optimizer_backend.time, "perf_counter", lambda: next(ticks))

    with pytest.raises(OptimizerBackendError, match="elapsed time") as captured:
        suggest_candidate(_spec(), _history(), 7)

    _assert_failure_evidence(
        captured.value,
        request,
        phase="process_timing",
        category="invalid_elapsed_time",
        elapsed=None,
        returncode=0,
        response=canonical(optimizer_backend._sidecar_response(request)).encode("utf-8") + b"\n",
        stderr=b"",
    )


def test_adapter_uses_public_backend_and_binds_replay_provenance(monkeypatch):
    warning_message = (
        "Acquisition restart dispersion is tight: best=-3.1178 median=-3.1178 "
        "relative_gap=6.58e-08 < 0.05. Restarts may be trapped in widespread local "
        "minima or the acquisition surface may be near-uniform."
    )
    _install_fake_public_backend(monkeypatch, log_warning=warning_message)
    monkeypatch.setenv("MEASURE_BO_PYTHON", sys.executable)
    monkeypatch.setenv("PYTHONPATH", "/host/python/site-packages")
    calls = _patch_subprocess(monkeypatch)
    root_handlers = tuple(logging.getLogger().handlers)

    result = suggest_candidate(_spec(), _history(), 7)

    assert result["candidate"] == {
        "strong_solvent_fraction": 0.5,
        "acquisition_duration_min": 5.5,
    }
    backend = result["backend"]
    assert backend["backend_id"] == "bo-engine/BoTorchBackend"
    assert backend["adapter_profile"] == "bo-mcp-gp-logei/1"
    assert backend["backend_name"] == "bo-mcp"
    assert backend["backend_class"] == "BoTorchBackend"
    assert backend["generation_method"] == "bo"
    assert backend["model_type"] == "SingleTaskGP"
    assert backend["acquisition_implementation"] == "qLogExpectedImprovement"
    assert backend["training_count"] == 3
    assert backend["seed"] == 7
    assert len(backend["protocol_sha256"]) == 64
    assert len(backend["training_sha256"]) == 64
    assert len(backend["source_tree_sha256"]) == 64
    assert len(backend["design_kernel_source_sha256"]) == 64
    assert backend["distribution_versions"]["bo-engine"] == "0.1.0"
    assert backend["settings"]["objective"] == {"name": "utility", "direction": "maximize"}
    assert "noise_prior" not in backend["settings"]
    assert backend["warnings"] == [
        "sample warning",
        f"logging.WARNING [bo_engine.acquisition]: {warning_message}",
    ]
    assert math.isfinite(backend["runtime"]["elapsed_seconds"])
    assert backend["runtime"]["elapsed_seconds"] >= 0
    assert backend["source_revision"] == "b" * 40
    assert len(calls) == 1
    assert tuple(logging.getLogger().handlers) == root_handlers


@pytest.mark.parametrize(
    ("target", "noise_prior", "expected_target", "expected_noise"),
    [
        ("historical_utility", "default", "utility", (1.1, 0.05)),
        ("historical_utility", "low_noise", "utility", (1.1, 100.0)),
        (
            "confirmed_acceptance_reward",
            "default",
            "confirmed_acceptance_reward",
            (1.1, 0.05),
        ),
        (
            "confirmed_acceptance_reward",
            "low_noise",
            "confirmed_acceptance_reward",
            (1.1, 100.0),
        ),
    ],
)
def test_ablation_profile_maps_objective_and_explicit_noise_prior(
    monkeypatch, target, noise_prior, expected_target, expected_noise
):
    optimizer_specs = _install_fake_public_backend(monkeypatch)
    monkeypatch.setenv("MEASURE_BO_PYTHON", sys.executable)
    _patch_subprocess(monkeypatch)
    protocol = _ablation_spec(target=target, noise_prior=noise_prior)
    training = _history()
    if target == "confirmed_acceptance_reward":
        for row, score in zip(training, (0.0, 1.0, 0.0), strict=True):
            row["score"] = score

    result = suggest_candidate(protocol, training, 7)

    configuration = optimizer_configuration(protocol)
    backend = result["backend"]
    settings = backend["settings"]
    assert backend["adapter_profile"] == "bo-mcp-gp-logei-ablation/1"
    assert configuration["target_name"] == expected_target
    assert settings["objective"] == {"name": expected_target, "direction": "maximize"}
    assert settings["noise_prior"] == {
        "family": "Gamma",
        "concentration": expected_noise[0],
        "rate": expected_noise[1],
        "space": "standardized_target_variance",
        "constraint_lower_bound": 1e-4,
        "learned": True,
    }
    assert optimizer_specs[-1].noise_prior_params == expected_noise


def test_ablation_binary_target_rejects_nonbinary_training_scores(monkeypatch):
    monkeypatch.setenv("MEASURE_BO_PYTHON", sys.executable)
    training = _history()
    training[1]["score"] = 0.5

    with pytest.raises(ValueError, match="must be exactly 0 or 1"):
        suggest_candidate(_ablation_spec(target="confirmed_acceptance_reward"), training, 7)


def test_ablation_response_rejects_mismatched_objective_and_prior_provenance(monkeypatch):
    _install_fake_public_backend(monkeypatch)
    protocol = _ablation_spec(target="confirmed_acceptance_reward", noise_prior="low_noise")
    training = _history()
    for row, score in zip(training, (0.0, 1.0, 0.0), strict=True):
        row["score"] = score
    _, normalized, request = optimizer_backend._normalize_inputs(protocol, training, 7)

    response = optimizer_backend._sidecar_response(request)
    response["backend"]["settings"]["objective"]["name"] = "utility"
    with pytest.raises(OptimizerBackendError, match="settings do not match"):
        optimizer_backend._validate_backend_response(
            response,
            protocol=protocol,
            training_data=normalized,
            seed=7,
            protocol_digest=request["protocol_sha256"],
            training_digest=request["training_sha256"],
            adapter_source_digest=request["adapter_source_sha256"],
            campaign_protocol_source_digest=request["campaign_protocol_source_sha256"],
            design_kernel_source_digest=request["design_kernel_source_sha256"],
        )

    response = optimizer_backend._sidecar_response(request)
    response["backend"]["settings"]["noise_prior"]["rate"] = 0.05
    with pytest.raises(OptimizerBackendError, match="noise-prior settings do not match"):
        optimizer_backend._validate_backend_response(
            response,
            protocol=protocol,
            training_data=normalized,
            seed=7,
            protocol_digest=request["protocol_sha256"],
            training_digest=request["training_sha256"],
            adapter_source_digest=request["adapter_source_sha256"],
            campaign_protocol_source_digest=request["campaign_protocol_source_sha256"],
            design_kernel_source_digest=request["design_kernel_source_sha256"],
        )


def test_adapter_rejects_initial_design_fallback(monkeypatch):
    _install_fake_public_backend(monkeypatch, generation_method="initial_design")
    monkeypatch.setenv("MEASURE_BO_PYTHON", sys.executable)
    _patch_subprocess(monkeypatch, expected_failure=True)

    with pytest.raises(OptimizerBackendError, match="exit status 1"):
        suggest_candidate(_spec(), _history(), 7)


def test_sidecar_rejects_stale_design_kernel(tmp_path, monkeypatch):
    request = optimizer_backend._normalize_inputs(_spec(), _history(), 7)[2]
    stale_design = tmp_path / "design.py"
    stale_design.write_text("# stale design kernel\n")
    monkeypatch.setattr(optimizer_backend, "_DESIGN_KERNEL_SOURCE_PATH", stale_design)

    with pytest.raises(ValueError, match="design kernel source does not match"):
        optimizer_backend._sidecar_response(request)


def test_scientific_log_capture_bounds_records_and_reports_truncation():
    capture = optimizer_backend._ScientificWarningCapture()
    root_logger = logging.getLogger()
    original_handlers = tuple(root_logger.handlers)
    root_logger.addHandler(capture)
    try:
        logger = logging.getLogger("bo_engine.acquisition")
        for index in range(capture._MAX_RECORDS + 3):
            logger.warning("%s %s", index, "x" * (capture._MAX_MESSAGE_CHARS + 10))
    finally:
        root_logger.removeHandler(capture)
        capture.close()

    warnings = capture.provenance_warnings()
    assert len(capture.records) == capture._MAX_RECORDS
    assert all(len(message) <= capture._MAX_MESSAGE_CHARS for message in capture.records)
    assert any("32 scientific warning messages truncated" in message for message in warnings)
    assert any("3 additional scientific warning records omitted" in message for message in warnings)
    assert tuple(root_logger.handlers) == original_handlers


def test_adapter_rejects_invalid_host_wall_time(monkeypatch):
    _install_fake_public_backend(monkeypatch)
    monkeypatch.setenv("MEASURE_BO_PYTHON", sys.executable)
    _patch_subprocess(monkeypatch)
    ticks = iter((10.0, 9.0))
    monkeypatch.setattr(optimizer_backend.time, "perf_counter", lambda: next(ticks))

    with pytest.raises(OptimizerBackendError, match="elapsed time"):
        suggest_candidate(_spec(), _history(), 7)


def test_unconfigured_sidecar_and_invalid_history_fail_before_process(monkeypatch):
    monkeypatch.delenv("MEASURE_BO_PYTHON", raising=False)
    with pytest.raises(OptimizerBackendError, match="MEASURE_BO_PYTHON"):
        suggest_candidate(_spec(), _history(), 7)

    monkeypatch.setenv("MEASURE_BO_PYTHON", sys.executable)
    with pytest.raises(ValueError, match="at least three"):
        suggest_candidate(_spec(), _history()[:2], 7)
    with pytest.raises(ValueError, match="predeclared"):
        suggest_candidate(_spec(), _history(), 8)
    with pytest.raises(ValueError, match="candidate 0"):
        bad = _history()
        bad[0]["candidate"]["extra"] = 1
        suggest_candidate(_spec(), bad, 7)
    with pytest.raises(ValueError, match="finite JSON number"):
        bad = _history()
        bad[0]["score"] = float("nan")
        suggest_candidate(_spec(), bad, 7)


def test_strict_json_rejects_duplicate_nonfinite_and_oversized_payloads():
    with pytest.raises(ValueError, match="duplicate"):
        _loads_strict('{"x":1,"x":2}', max_bytes=100)
    with pytest.raises(ValueError, match="non-finite"):
        _loads_strict('{"x":NaN}', max_bytes=100)
    with pytest.raises(ValueError, match="size limit"):
        _loads_strict("x" * (MAX_REQUEST_BYTES + 1), max_bytes=MAX_REQUEST_BYTES)


def test_protocol_and_history_digests_are_stable_and_order_sensitive():
    protocol = _spec()
    rows = _history()
    assert campaign_protocol_sha256(protocol) == campaign_protocol_sha256(
        CampaignProtocolSpec.model_validate(protocol.model_dump(mode="json"))
    )
    assert sha256(rows) != sha256(list(reversed(rows)))
    assert REQUEST_SCHEMA == "measure-service-bo-request/1"
    assert RESPONSE_SCHEMA == "measure-service-bo-response/1"
