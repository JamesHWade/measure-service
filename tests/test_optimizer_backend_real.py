"""Actual optional BO-MCP execution through the installed public MCP workflow."""

import json
import math
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.bo_backend,
    pytest.mark.skipif(
        not os.environ.get("MEASURE_BO_PYTHON"), reason="optional BO-MCP runtime is not configured"
    ),
]


def _base_protocol():
    from measure_service.campaign_protocol import CampaignProtocolSpec

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


def _history(scores=(0.1, 0.7, 0.4)):
    return [
        {
            "candidate": {
                "strong_solvent_fraction": strong,
                "acquisition_duration_min": duration,
            },
            "score": score,
        }
        for strong, duration, score in (
            (0.2, 2.0, scores[0]),
            (0.5, 5.0, scores[1]),
            (0.8, 9.0, scores[2]),
        )
    ]


def _model_capture_sidecar(tmp_path: Path) -> tuple[Path, Path]:
    """Create a test-only interpreter wrapper that inspects actual fitted GPs."""
    configured_interpreter = Path(os.environ["MEASURE_BO_PYTHON"]).absolute()
    shim = tmp_path / "sidecar-shim"
    shim.mkdir()
    capture_path = tmp_path / "actual-models.jsonl"
    capture_path.unlink(missing_ok=True)
    shim.joinpath("sitecustomize.py").write_text(
        """import importlib
import json
import os
from pathlib import Path

factory_module = importlib.import_module('bo_engine.suggestions_single_objective')
original = factory_module.create_and_fit_single_task_model

def checked_factory(*args, **kwargs):
    model = original(*args, **kwargs)
    noise = model.likelihood.noise_covar
    prior = noise.noise_prior
    lower = float(noise.raw_noise_constraint.lower_bound.detach().cpu().item())
    covariance = model.covar_module
    kernel_names = [type(covariance).__name__]
    base = getattr(covariance, 'base_kernel', None)
    if base is not None:
        kernel_names.append(type(base).__name__)
    payload = {
        'prior_class': type(prior).__name__,
        'concentration': float(prior.concentration.detach().cpu().item()),
        'rate': float(prior.rate.detach().cpu().item()),
        'concentration_dtype': str(prior.concentration.dtype),
        'rate_dtype': str(prior.rate.dtype),
        'constraint_dtype': str(noise.raw_noise_constraint.lower_bound.dtype),
        'constraint_lower_bound': lower,
        'kernel_names': kernel_names,
    }
    with Path(os.environ['MEASURE_BO_MODEL_CAPTURE']).open('a', encoding='utf-8') as out:
        out.write(json.dumps(payload, sort_keys=True) + '\\n')
    return model

factory_module.create_and_fit_single_task_model = checked_factory
""",
        encoding="utf-8",
    )
    wrapper = tmp_path / "python-with-model-capture"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH={shlex.quote(str(shim))} exec {shlex.quote(str(configured_interpreter))} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return wrapper, capture_path


def test_actual_bo_campaign_replays_across_fresh_stores_and_server_restart(tmp_path):
    example = Path(__file__).resolve().parents[1] / "examples" / "synthetic_campaign.py"
    outputs = []
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, str(example), "--policy", "bo_mcp_gp_logei"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result["campaign"]["state"] == "budget_complete"
        assert result["campaign"]["consumed_evaluations"] == 4
        assert result["campaign"]["evaluation_cost_total"] == 4
        assert result["records"]["proposal_sources"] == ["common_initial_design"] * 3 + [
            "bo_mcp_gp_logei"
        ]
        assert result["assertions"]["bo_backend_used"] is True
        assert (
            result["assertions"]["first_proposal_retry_stable_after_progress_and_restart"] is True
        )
        assert (
            result["assertions"]["first_consumption_retry_stable_after_progress_and_restart"]
            is True
        )
        backend = result["records"]["backend_provenance"][-1]
        assert backend["backend_id"] == "bo-engine/BoTorchBackend"
        assert backend["generation_method"] == "bo"
        assert backend["model_type"] == "SingleTaskGP"
        assert backend["acquisition_implementation"] == "qLogExpectedImprovement"
        assert backend["training_count"] == 3
        assert all(
            backend["distribution_versions"][name]
            for name in ("bo-engine", "botorch", "gpytorch", "torch")
        )
        for key in (
            "source_tree_sha256",
            "adapter_source_sha256",
            "campaign_protocol_source_sha256",
            "design_kernel_source_sha256",
            "training_sha256",
        ):
            assert len(backend[key]) == 64
        assert math.isfinite(backend["runtime"]["elapsed_seconds"])
        assert backend["runtime"]["elapsed_seconds"] >= 0
        outputs.append(result)
    assert outputs[0]["records"]["candidates"] == outputs[1]["records"]["candidates"]
    assert outputs[0]["records"]["method_ids"] != outputs[1]["records"]["method_ids"]
    left, right = [result["records"]["backend_provenance"][-1] for result in outputs]
    assert left["training_sha256"] == right["training_sha256"]
    assert left["source_tree_sha256"] == right["source_tree_sha256"]


def test_actual_ablation_prior_routes_match_the_public_fitted_model(tmp_path, monkeypatch):
    from measure_service.campaign_protocol import parse_campaign_protocol
    from measure_service.optimizer_backend import suggest_candidate

    wrapper, capture_path = _model_capture_sidecar(tmp_path)
    monkeypatch.setenv("MEASURE_BO_PYTHON", str(wrapper))
    monkeypatch.setenv("MEASURE_BO_MODEL_CAPTURE", str(capture_path))

    base = _base_protocol()
    training = _history()
    legacy = suggest_candidate(base, training, 7)
    default_protocol = parse_campaign_protocol(
        base.model_dump(mode="json")
        | {
            "profile": "synthetic-isocratic-ablation/1",
            "optimizer_target": "historical_utility",
            "surrogate_noise_prior": "default",
        }
    )
    explicit_default = suggest_candidate(default_protocol, training, 7)
    low_noise_protocol = parse_campaign_protocol(
        default_protocol.model_dump(mode="json") | {"surrogate_noise_prior": "low_noise"}
    )
    explicit_low_noise = suggest_candidate(low_noise_protocol, training, 7)

    assert legacy["candidate"] == explicit_default["candidate"]
    assert legacy["backend"]["settings"]["objective"] == {
        "name": "utility",
        "direction": "maximize",
    }
    assert "noise_prior" not in legacy["backend"]["settings"]
    assert explicit_default["backend"]["settings"]["noise_prior"]["rate"] == 0.05
    assert explicit_low_noise["backend"]["settings"]["noise_prior"]["rate"] == 100.0

    fitted_models = [json.loads(line) for line in capture_path.read_text().splitlines()]
    assert len(fitted_models) == 3
    for record, expected_rate in zip(fitted_models, (0.05, 0.05, 100.0), strict=True):
        assert record["prior_class"] == "GammaPrior"
        assert math.isclose(record["concentration"], 1.1, rel_tol=1e-6, abs_tol=0.0)
        assert math.isclose(record["rate"], expected_rate, rel_tol=1e-6, abs_tol=0.0)
        assert math.isclose(record["constraint_lower_bound"], 1e-4, rel_tol=1e-6, abs_tol=0.0)
        assert record["concentration_dtype"].startswith("torch.")
        assert record["rate_dtype"].startswith("torch.")
        assert record["constraint_dtype"].startswith("torch.")
        assert "RBFKernel" in record["kernel_names"]


def test_actual_fixed_ablation_preflight_retains_all_routes(tmp_path, monkeypatch):
    from measure_service import campaign_protocol
    from measure_service.benchmarks import synthetic_ablations as ablations
    from measure_service.optimizer_backend import preflight_ablation_backend

    document, study_digest = ablations._read_study_document()
    task_bindings = [
        {
            "task_id": task["task_id"],
            "producer": {"producer_id": f"preflight-producer-{task['task_id']}"},
            "contract": {"contract_id": f"preflight-contract-{task['task_id']}"},
        }
        for task in document["tasks"]
    ]
    protocol = ablations._protocol_spec(document, task_bindings, document["arms"][0])

    wrapper, capture_path = _model_capture_sidecar(tmp_path)
    monkeypatch.setenv("MEASURE_BO_PYTHON", str(wrapper))
    monkeypatch.setenv("MEASURE_BO_MODEL_CAPTURE", str(capture_path))
    report = preflight_ablation_backend(protocol)
    protocol_sha256 = campaign_protocol.campaign_protocol_sha256(protocol)

    assert isinstance(report, dict)
    assert report["study_protocol_sha256"] == protocol_sha256
    route_spec = campaign_protocol.parse_campaign_protocol(report["cases"][0]["request"]["spec"])
    base_spec_values = route_spec.model_dump(mode="json")
    base_spec_values.update(
        {"optimizer_target": "historical_utility", "surrogate_noise_prior": "default"}
    )
    base_spec = campaign_protocol.parse_campaign_protocol(base_spec_values)
    assert campaign_protocol.campaign_design_sha256(base_spec) == (
        campaign_protocol.campaign_design_sha256(protocol)
    )
    assert campaign_protocol.campaign_protocol_sha256(base_spec) == protocol_sha256
    qualified, validation_error = ablations._preflight_report_status(
        report,
        document,
        study_digest,
        protocol_sha256,
    )
    assert qualified, validation_error or "backend preflight report failed the runner gate"

    report_path = tmp_path / "preflight-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert report["expected_case_count"] == 16
    assert report["attempted_case_count"] == 16
    assert report["success_count"] == 16
    assert report["failed_count"] == 0
    assert report["qualified"] is True
    fitted_models = [json.loads(line) for line in capture_path.read_text().splitlines()]
    assert len(fitted_models) == 16
    for case, record in zip(report["cases"], fitted_models, strict=True):
        if case["success"]:
            assert case["error"] is None
            assert case["backend_provenance"]["generation_method"] == "bo"
            assert math.isfinite(case["candidate"]["strong_solvent_fraction"])
            assert math.isfinite(case["candidate"]["acquisition_duration_min"])
            assert (
                document["domain"]["strong_solvent_fraction"]["lower"]
                <= case["candidate"]["strong_solvent_fraction"]
                <= document["domain"]["strong_solvent_fraction"]["upper"]
            )
            assert (
                document["domain"]["acquisition_duration_min"]["lower"]
                <= case["candidate"]["acquisition_duration_min"]
                <= document["domain"]["acquisition_duration_min"]["upper"]
            )
            expected_rate = 0.05 if case["noise_prior"] == "default" else 100.0
            assert record["prior_class"] == "GammaPrior"
            assert math.isclose(record["concentration"], 1.1, rel_tol=1e-6, abs_tol=0.0)
            assert math.isclose(record["rate"], expected_rate, rel_tol=1e-6, abs_tol=0.0)
            assert math.isclose(record["constraint_lower_bound"], 1e-4, rel_tol=1e-6, abs_tol=0.0)
            assert "RBFKernel" in record["kernel_names"]
        else:
            assert case["candidate"] is None
            assert case["backend_provenance"] is None
            assert isinstance(case["error"], str) and case["error"]
