"""Contract tests for the isolated public BO-MCP backend adapter."""

from __future__ import annotations

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
from measure_service.campaign_protocol import CampaignProtocolSpec, campaign_protocol_sha256
from measure_service.optimizer_backend import (
    MAX_REQUEST_BYTES,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    OptimizerBackendError,
    _loads_strict,
    suggest_candidate,
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
) -> None:
    package = types.ModuleType("bo_engine")
    package.__file__ = "/synthetic/bo_engine/__init__.py"
    package.__path__ = []
    backend_module = types.ModuleType("bo_engine.botorch_backend")
    types_module = types.ModuleType("bo_engine.types")

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
            assert spec.objectives[0].name == "utility" and not spec.objectives[0].minimize
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
                    args, 1, "", f"BO sidecar failed ({type(error).__name__})\n"
                )
            raise
        return subprocess.CompletedProcess(args, 0, canonical(response) + "\n", "")

    monkeypatch.setattr(optimizer_backend.subprocess, "run", run)
    return calls


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
    assert backend["warnings"] == [
        "sample warning",
        f"logging.WARNING [bo_engine.acquisition]: {warning_message}",
    ]
    assert math.isfinite(backend["runtime"]["elapsed_seconds"])
    assert backend["runtime"]["elapsed_seconds"] >= 0
    assert backend["source_revision"] == "b" * 40
    assert len(calls) == 1
    assert tuple(logging.getLogger().handlers) == root_handlers


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
