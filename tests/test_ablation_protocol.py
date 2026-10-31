"""Versioned optimizer target and surrogate-noise protocol declarations."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from measure_service.campaign_protocol import (
    ABLATION_CAMPAIGN_PROFILE,
    CONFIRMED_ACCEPTANCE_PROFILE,
    UTILITY_PROFILE,
    AblationProtocolSpec,
    CampaignProtocolSpec,
    ProtocolSpec,
    campaign_design_sha256,
    campaign_protocol_sha256,
    initial_candidate,
    optimizer_configuration,
    parse_campaign_protocol,
    random_candidate,
)


def _base_spec(**changes):
    value = {
        "domain": {
            "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
            "acquisition_duration_min": {"lower": 2.0, "upper": 10.0},
        },
        "flow_ml_min": 0.5,
        "constraints": {},
        "tasks": [
            {
                "task_id": "parallel-linear",
                "producer_id": "producer-a",
                "observation_contract_id": "contract-a",
            },
            {
                "task_id": "crossing-linear",
                "producer_id": "producer-b",
                "observation_contract_id": "contract-b",
            },
        ],
        "seeds": [17, 29, 43],
        "evaluation_budget": 10,
        "initial_design_size": 3,
        "evaluation_cost": 1.0,
        "evaluation_cost_unit": "synthetic_evaluation",
    }
    value.update(changes)
    return value


def _ablation_spec(target="historical_utility", prior="default", **changes):
    value = _base_spec(
        profile=ABLATION_CAMPAIGN_PROFILE,
        optimizer_target=target,
        surrogate_noise_prior=prior,
        **changes,
    )
    return parse_campaign_protocol(value)


@pytest.mark.parametrize(
    ("target", "prior", "target_name", "target_profile", "noise_params"),
    [
        (
            "historical_utility",
            "default",
            "utility",
            UTILITY_PROFILE,
            [1.1, 0.05],
        ),
        (
            "historical_utility",
            "low_noise",
            "utility",
            UTILITY_PROFILE,
            [1.1, 100.0],
        ),
        (
            "confirmed_acceptance_reward",
            "default",
            "confirmed_acceptance_reward",
            CONFIRMED_ACCEPTANCE_PROFILE,
            [1.1, 0.05],
        ),
        (
            "confirmed_acceptance_reward",
            "low_noise",
            "confirmed_acceptance_reward",
            CONFIRMED_ACCEPTANCE_PROFILE,
            [1.1, 100.0],
        ),
    ],
)
def test_optimizer_configuration_freezes_each_factor_pair(
    target, prior, target_name, target_profile, noise_params
):
    spec = _ablation_spec(target, prior)

    assert isinstance(spec, AblationProtocolSpec)
    assert optimizer_configuration(spec) == {
        "target_name": target_name,
        "target_profile": target_profile,
        "noise_prior_params": noise_params,
        "adapter_profile": "bo-mcp-gp-logei-ablation/1",
    }


def test_v1_optimizer_configuration_retains_legacy_route():
    spec = CampaignProtocolSpec.model_validate(_base_spec())
    assert optimizer_configuration(spec) == {
        "target_name": "utility",
        "target_profile": UTILITY_PROFILE,
        "noise_prior_params": None,
        "adapter_profile": "bo-mcp-gp-logei/1",
    }


def test_factor_variants_share_design_stream_but_keep_distinct_protocol_hashes():
    specs = [
        _ablation_spec(target, prior)
        for target in ("historical_utility", "confirmed_acceptance_reward")
        for prior in ("default", "low_noise")
    ]

    assert len({campaign_design_sha256(spec) for spec in specs}) == 1
    assert len({campaign_protocol_sha256(spec) for spec in specs}) == 4
    assert all(
        [initial_candidate(spec, 17, ordinal) for ordinal in range(3)]
        == [initial_candidate(specs[0], 17, ordinal) for ordinal in range(3)]
        for spec in specs
    )
    assert all(
        [random_candidate(spec, 17, ordinal) for ordinal in (3, 4, 9)]
        == [random_candidate(specs[0], 17, ordinal) for ordinal in (3, 4, 9)]
        for spec in specs
    )


def test_design_hash_still_tracks_nonfactor_scientific_inputs_and_logical_tasks():
    original = _ablation_spec()
    data = original.model_dump(mode="json")
    data["tasks"][0]["producer_id"] = "rebound-producer"
    data["tasks"][0]["observation_contract_id"] = "rebound-contract"
    rebound = parse_campaign_protocol(data)
    assert campaign_design_sha256(original) == campaign_design_sha256(rebound)
    assert campaign_protocol_sha256(original) != campaign_protocol_sha256(rebound)

    changed_task = deepcopy(data)
    changed_task["tasks"][0]["task_id"] = "different-logical-task"
    assert campaign_design_sha256(original) != campaign_design_sha256(
        parse_campaign_protocol(changed_task)
    )

    changed_domain = deepcopy(data)
    changed_domain["domain"]["strong_solvent_fraction"]["lower"] = 0.2
    assert campaign_design_sha256(original) != campaign_design_sha256(
        parse_campaign_protocol(changed_domain)
    )


def test_profile_dispatch_accepts_v1_legacy_forms_and_explicit_v2():
    base = CampaignProtocolSpec.model_validate(_base_spec())
    missing_profile = base.model_dump(mode="json")
    missing_profile.pop("profile")
    v2 = _ablation_spec()

    assert parse_campaign_protocol(base) is base
    assert isinstance(parse_campaign_protocol(missing_profile), CampaignProtocolSpec)
    assert parse_campaign_protocol(missing_profile).model_dump(mode="json") == base.model_dump(
        mode="json"
    )
    assert parse_campaign_protocol(v2) is v2
    assert parse_campaign_protocol(v2.model_dump(mode="json")).model_dump(
        mode="json"
    ) == v2.model_dump(mode="json")


def test_protocol_union_requires_explicit_ablation_profile_but_accepts_legacy_v1():
    adapter = TypeAdapter(ProtocolSpec)
    legacy = _base_spec()
    ablation_without_profile = _base_spec(
        optimizer_target="historical_utility",
        surrogate_noise_prior="default",
    )
    explicit_ablation = {
        **ablation_without_profile,
        "profile": ABLATION_CAMPAIGN_PROFILE,
    }

    assert isinstance(adapter.validate_python(legacy), CampaignProtocolSpec)
    with pytest.raises(ValidationError):
        adapter.validate_python(ablation_without_profile)
    assert isinstance(adapter.validate_python(explicit_ablation), AblationProtocolSpec)


@pytest.mark.parametrize(
    "change",
    [
        {"optimizer_target": "utility"},
        {"optimizer_target": True},
        {"surrogate_noise_prior": "matern"},
        {"surrogate_noise_prior": False},
        {"unexpected": "extra"},
        {"profile": "synthetic-isocratic-paired/2"},
    ],
)
def test_ablation_protocol_rejects_unsupported_or_non_strict_values(change):
    value = _base_spec(
        profile=ABLATION_CAMPAIGN_PROFILE,
        optimizer_target="historical_utility",
        surrogate_noise_prior="default",
    )
    value.update(change)
    with pytest.raises((ValidationError, ValueError)):
        parse_campaign_protocol(value)


@pytest.mark.parametrize("field", ["optimizer_target", "surrogate_noise_prior"])
def test_ablation_protocol_requires_both_predeclared_factors(field):
    value = _base_spec(
        profile=ABLATION_CAMPAIGN_PROFILE,
        optimizer_target="historical_utility",
        surrogate_noise_prior="default",
    )
    del value[field]
    with pytest.raises(ValidationError):
        parse_campaign_protocol(value)


def test_v2_factor_fields_without_new_profile_are_not_silently_accepted_as_v1():
    value = _base_spec(
        optimizer_target="historical_utility",
        surrogate_noise_prior="default",
    )
    with pytest.raises(ValidationError):
        parse_campaign_protocol(value)


def test_mcp_protocol_schema_exposes_both_profiles_and_v2_factors():
    from measure_service.server import mcp

    parameters = mcp._tool_manager.get_tool("measurement_register_campaign_protocol").parameters
    spec_schema = parameters["properties"]["spec"]
    assert len(spec_schema["anyOf"]) == 2
    definitions = parameters["$defs"]
    assert "CampaignProtocolSpec" in definitions
    assert "AblationProtocolSpec" in definitions
    v2 = definitions["AblationProtocolSpec"]
    assert set(v2["required"]) >= {"optimizer_target", "surrogate_noise_prior"}
    assert "profile" in v2["required"]


def test_registry_capabilities_preserve_v1_and_advertise_limited_v2(monkeypatch):
    from measure_service.server import measurement_registry_capabilities

    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "local")
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=None))
    capabilities = measurement_registry_capabilities(ctx)["synthetic_campaigns"]

    assert capabilities["profile"] == "synthetic-isocratic-paired/1"
    assert capabilities["supported_profiles"] == [
        "synthetic-isocratic-paired/1",
        ABLATION_CAMPAIGN_PROFILE,
    ]
    assert capabilities["supported_optimizer_targets"] == [
        "historical_utility",
        "confirmed_acceptance_reward",
    ]
    assert capabilities["supported_surrogate_noise_priors"] == ["default", "low_noise"]
    assert capabilities["surrogate_capabilities"]["compoundwise"] is False
    assert capabilities["surrogate_capabilities"]["matern_5_2"] is False


def test_mcp_tool_validation_requires_explicit_v2_profile(monkeypatch):
    import asyncio

    from mcp.server.fastmcp.exceptions import ToolError

    from measure_service import server

    tool = server.mcp._tool_manager.get_tool("measurement_register_campaign_protocol")
    missing_profile = _base_spec(
        optimizer_target="historical_utility",
        surrogate_noise_prior="default",
    )
    explicit_profile = {
        **missing_profile,
        "profile": ABLATION_CAMPAIGN_PROFILE,
    }
    captured = {}

    def register(owner, title, spec, key):
        captured.update(owner=owner, title=title, spec=spec, key=key)
        return {"protocol_id": "registered"}

    monkeypatch.setattr(server, "identity", lambda ctx: "chemist")
    monkeypatch.setattr(server.campaign_protocol_registry, "register_campaign_protocol", register)

    async def exercise():
        with pytest.raises(ToolError):
            await tool.run(
                {
                    "title": "Ablation",
                    "spec": missing_profile,
                    "idempotency_key": "missing-profile",
                }
            )
        response = await tool.run(
            {
                "title": "Ablation",
                "spec": explicit_profile,
                "idempotency_key": "explicit-profile",
            }
        )
        assert response == {"protocol_id": "registered"}

    asyncio.run(exercise())
    assert captured["owner"] == "chemist"
    assert isinstance(captured["spec"], AblationProtocolSpec)
