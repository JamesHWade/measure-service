"""Fixed denominators and fabricated histories for the BO ablation preflight."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from measure_service import optimizer_backend
from measure_service.campaign_protocol import AblationProtocolSpec, parse_campaign_protocol

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "measure_service"
    / "benchmarks"
    / "reference_data"
    / "synthetic-isocratic-ablation-v1.json"
)


def _fixture() -> dict[str, object]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _spec() -> AblationProtocolSpec:
    fixture = _fixture()
    return parse_campaign_protocol(
        {
            "profile": "synthetic-isocratic-ablation/1",
            "optimizer_target": "historical_utility",
            "surrogate_noise_prior": "default",
            "domain": fixture["domain"],
            "flow_ml_min": fixture["flow_ml_min"],
            "constraints": {},
            "tasks": [
                {
                    "task_id": "preflight-task",
                    "producer_id": "preflight-producer",
                    "observation_contract_id": "preflight-contract",
                }
            ],
            "seeds": fixture["seeds"],
            "evaluation_budget": fixture["evaluation_budget"],
            "initial_design_size": fixture["initial_design_size"],
            "evaluation_cost": fixture["evaluation_cost"],
            "evaluation_cost_unit": fixture["evaluation_cost_unit"],
        }
    )


def test_preflight_returns_all_16_fixed_routes_and_common_fabricated_coordinates(monkeypatch):
    calls: list[tuple[dict[str, object], list[dict[str, object]], int]] = []

    def fake_suggest(spec, training_data, seed):
        calls.append((spec.model_dump(mode="json"), training_data, seed))
        return {
            "candidate": {
                "strong_solvent_fraction": 0.5,
                "acquisition_duration_min": 6.0,
            },
            "backend": {
                "runtime": {"elapsed_seconds": 0.125},
                "adapter_profile": "bo-mcp-gp-logei-ablation/1",
                "settings": {"objective": {"name": "unused", "direction": "maximize"}},
            },
        }

    monkeypatch.setattr(optimizer_backend, "suggest_candidate", fake_suggest)
    report = optimizer_backend.preflight_ablation_backend(_spec())

    assert report["schema_version"] == 1
    assert report["profile"] == "measure-service-bo-ablation-preflight/1"
    assert report["fixture_sha256"] == hashlib.sha256(_FIXTURE_PATH.read_bytes()).hexdigest()
    assert report["expected_case_count"] == 16
    assert report["attempted_case_count"] == 16
    assert report["success_count"] == 16
    assert report["failed_count"] == 0
    assert report["qualified"] is True
    assert math.isfinite(report["preflight_elapsed_seconds"])
    assert len(report["cases"]) == len(calls) == 16
    assert {case["history_length"] for case in report["cases"]} == {3, 9}
    assert {case["noise_prior"] for case in report["cases"]} == {"default", "low_noise"}
    assert all(case["attempted"] and case["success"] for case in report["cases"])
    assert all(case["candidate"] is not None for case in report["cases"])
    assert all(case["backend_provenance"] is not None for case in report["cases"])
    assert all(case["proposal_elapsed_seconds"] == 0.125 for case in report["cases"])
    assert [seed for _, _, seed in calls] == [17] * 16

    cases_by_route = {
        (case["target"], case["pattern"], case["history_length"], case["noise_prior"]): case
        for case in report["cases"]
    }
    for target, pattern, n in (
        ("historical_utility", "historical_utility", 3),
        ("historical_utility", "historical_utility", 9),
        ("confirmed_acceptance_reward", "all_zero", 3),
        ("confirmed_acceptance_reward", "all_one", 3),
        ("confirmed_acceptance_reward", "alternating_zero_one", 9),
    ):
        left = cases_by_route[(target, pattern, n, "default")]["request"]["training_data"]
        right = cases_by_route[(target, pattern, n, "low_noise")]["request"]["training_data"]
        assert left == right

    for case in report["cases"]:
        request = case["request"]
        rows = request["training_data"]
        assert set(request) == {
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
        assert len(rows) == case["history_length"]
        assert request["schema"] == optimizer_backend.REQUEST_SCHEMA
        assert request["seed"] == 17
        assert len(request["protocol_sha256"]) == 64
        assert len(request["training_sha256"]) == 64
        assert len(request["adapter_source_sha256"]) == 64
        assert len(request["campaign_protocol_source_sha256"]) == 64
        assert len(request["design_kernel_source_sha256"]) == 64
        assert case["error"] is None


def test_preflight_failed_routes_keep_all_16_in_denominator(monkeypatch):
    def fail_suggest(spec, training_data, seed):
        raise RuntimeError("synthetic model fit failure")

    monkeypatch.setattr(optimizer_backend, "suggest_candidate", fail_suggest)
    report = optimizer_backend.preflight_ablation_backend(_spec())

    assert report["expected_case_count"] == 16
    assert report["attempted_case_count"] == 16
    assert report["success_count"] == 0
    assert report["failed_count"] == 16
    assert report["qualified"] is False
    assert len(report["cases"]) == 16
    assert all(case["attempted"] for case in report["cases"])
    assert all(not case["success"] for case in report["cases"])
    assert all(case["candidate"] is None for case in report["cases"])
    assert all(case["backend_provenance"] is None for case in report["cases"])
    assert all(
        case["error"] == "RuntimeError: synthetic model fit failure" for case in report["cases"]
    )
    assert all(
        len(case["request"]["training_data"]) == case["history_length"] for case in report["cases"]
    )


def test_preflight_fails_closed_when_frozen_fixture_changes(tmp_path, monkeypatch):
    altered = tmp_path / "fixture.json"
    altered.write_bytes(_FIXTURE_PATH.read_bytes() + b"\n")
    monkeypatch.setattr(optimizer_backend, "_PREFLIGHT_FIXTURE_PATH", altered)
    called = False

    def unexpected_suggest(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("suggestion must not run for an unverified fixture")

    monkeypatch.setattr(optimizer_backend, "suggest_candidate", unexpected_suggest)
    with pytest.raises(ValueError, match="fixture digest"):
        optimizer_backend.preflight_ablation_backend(_spec())
    assert called is False


def test_preflight_rejects_protocol_that_does_not_match_study_fixture():
    payload = _spec().model_dump(mode="json")
    payload["evaluation_budget"] = 11
    with pytest.raises(ValueError, match="does not match the frozen preflight study design"):
        optimizer_backend.preflight_ablation_backend(payload)
