"""Versioned duplicate policy on matched synthetic campaign protocols."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import TypeAdapter, ValidationError

from measure_methods.gaussian_observations import RESOLUTION_DEFINITION
from measure_service import campaign_protocol_registry, gaussian_producer, observations, retention
from measure_service.campaign_protocol import (
    ABLATION_CAMPAIGN_PROFILE,
    CONFIRMED_ACCEPTANCE_PROFILE,
    REPLICATION_CAMPAIGN_PROFILE,
    UTILITY_PROFILE,
    AblationProtocolSpec,
    CampaignProtocolSpec,
    ProtocolSpec,
    ReplicationProtocolSpec,
    campaign_design_sha256,
    campaign_protocol_sha256,
    initial_candidate,
    optimizer_configuration,
    parse_campaign_protocol,
    random_candidate,
)
from measure_service.storage import canonical, registry_connection


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
    return parse_campaign_protocol(
        _base_spec(
            profile=ABLATION_CAMPAIGN_PROFILE,
            optimizer_target=target,
            surrogate_noise_prior=prior,
            **changes,
        )
    )


def _replication_spec(
    target="historical_utility", prior="default", duplicate_policy="reject", **changes
):
    return parse_campaign_protocol(
        _base_spec(
            profile=REPLICATION_CAMPAIGN_PROFILE,
            optimizer_target=target,
            surrogate_noise_prior=prior,
            duplicate_policy=duplicate_policy,
            **changes,
        )
    )


def test_legacy_v1_and_ablation_v1_hashes_and_coordinates_are_unchanged():
    v1 = CampaignProtocolSpec.model_validate(_base_spec())
    v2 = _ablation_spec()

    assert campaign_protocol_sha256(v1) == (
        "a48e1e252a870379e3a4208a59b346cc91ae85bdb36eeec9926f1e24c3575d6c"
    )
    assert campaign_design_sha256(v1) == (
        "a82e4220712f5a0fafd7fcc66d8a912cef915513b5597136d271e7160e507315"
    )
    assert campaign_protocol_sha256(v2) == (
        "1802e07f2f8a2e1962a3d44b72761ff28ee88f80beaec6174a976518667cd967"
    )
    assert campaign_design_sha256(v2) == (
        "eb5cbdbea8fbec9bbf5cf8209eed006382d1c34f8646f6ff5fe591651132ce1e"
    )
    assert "duplicate_policy" not in v1.model_dump(mode="json")
    assert "duplicate_policy" not in v2.model_dump(mode="json")
    assert [initial_candidate(v1, 17, ordinal) for ordinal in range(3)] == [
        {
            "strong_solvent_fraction": 0.47658120430593764,
            "acquisition_duration_min": 7.346052239241124,
        },
        {
            "strong_solvent_fraction": 0.2708049441077297,
            "acquisition_duration_min": 7.315089911676673,
        },
        {
            "strong_solvent_fraction": 0.8587912088362032,
            "acquisition_duration_min": 3.4731699255648305,
        },
    ]
    assert [random_candidate(v1, 17, ordinal) for ordinal in (3, 4, 9)] == [
        {
            "strong_solvent_fraction": 0.5922093662357399,
            "acquisition_duration_min": 3.8786772317746934,
        },
        {
            "strong_solvent_fraction": 0.7476330865738373,
            "acquisition_duration_min": 7.589289998618251,
        },
        {
            "strong_solvent_fraction": 0.39495302339296756,
            "acquisition_duration_min": 2.0756581875956757,
        },
    ]
    assert [initial_candidate(v2, 17, ordinal) for ordinal in range(3)] == [
        {
            "strong_solvent_fraction": 0.5506442913717458,
            "acquisition_duration_min": 6.605585321648071,
        },
        {
            "strong_solvent_fraction": 0.8670713311995548,
            "acquisition_duration_min": 8.400194321765493,
        },
        {
            "strong_solvent_fraction": 0.18674626019471285,
            "acquisition_duration_min": 2.3921178455184218,
        },
    ]
    assert [random_candidate(v2, 17, ordinal) for ordinal in (3, 4, 9)] == [
        {
            "strong_solvent_fraction": 0.20539782339311438,
            "acquisition_duration_min": 5.996034355273784,
        },
        {
            "strong_solvent_fraction": 0.4383145457342036,
            "acquisition_duration_min": 8.943875403257675,
        },
        {
            "strong_solvent_fraction": 0.6807694292197582,
            "acquisition_duration_min": 4.65609113207752,
        },
    ]


def test_ablation_v2_optimizer_configuration_remains_unchanged():
    assert optimizer_configuration(_ablation_spec()) == {
        "target_name": "utility",
        "target_profile": UTILITY_PROFILE,
        "noise_prior_params": [1.1, 0.05],
        "adapter_profile": "bo-mcp-gp-logei-ablation/1",
    }
    assert optimizer_configuration(_ablation_spec("confirmed_acceptance_reward", "low_noise")) == {
        "target_name": "confirmed_acceptance_reward",
        "target_profile": CONFIRMED_ACCEPTANCE_PROFILE,
        "noise_prior_params": [1.1, 100.0],
        "adapter_profile": "bo-mcp-gp-logei-ablation/1",
    }


def test_new_profile_is_explicit_and_dispatches_subclass_first():
    value = _base_spec(
        profile=REPLICATION_CAMPAIGN_PROFILE,
        optimizer_target="historical_utility",
        surrogate_noise_prior="default",
        duplicate_policy="replicate",
    )
    parsed = parse_campaign_protocol(value)
    union_value = TypeAdapter(ProtocolSpec).validate_python(value)

    assert isinstance(parsed, ReplicationProtocolSpec)
    assert isinstance(parsed, AblationProtocolSpec)
    assert union_value == parsed
    assert parsed.profile == REPLICATION_CAMPAIGN_PROFILE
    assert parsed.duplicate_policy == "replicate"
    assert parse_campaign_protocol(parsed) is parsed

    v1 = TypeAdapter(ProtocolSpec).validate_python(_base_spec())
    v2 = TypeAdapter(ProtocolSpec).validate_python(
        _base_spec(
            profile=ABLATION_CAMPAIGN_PROFILE,
            optimizer_target="historical_utility",
            surrogate_noise_prior="default",
        )
    )
    assert type(v1) is CampaignProtocolSpec
    assert type(v2) is AblationProtocolSpec


@pytest.mark.parametrize(
    "change",
    [
        {"duplicate_policy": None},
        {"duplicate_policy": True},
        {"duplicate_policy": "allow"},
        {"unexpected": "extra"},
    ],
)
def test_new_profile_rejects_invalid_duplicate_policy_and_extra_fields(change):
    value = _base_spec(
        profile=REPLICATION_CAMPAIGN_PROFILE,
        optimizer_target="historical_utility",
        surrogate_noise_prior="default",
        duplicate_policy="reject",
    )
    value.update(change)
    with pytest.raises((ValidationError, ValueError)):
        parse_campaign_protocol(value)
    with pytest.raises(ValidationError):
        TypeAdapter(ProtocolSpec).validate_python(value)


def test_new_profile_requires_explicit_profile_and_duplicate_policy():
    value = _base_spec(
        profile=REPLICATION_CAMPAIGN_PROFILE,
        optimizer_target="historical_utility",
        surrogate_noise_prior="default",
        duplicate_policy="reject",
    )
    missing_profile = deepcopy(value)
    missing_profile.pop("profile")
    missing_duplicate_policy = deepcopy(value)
    missing_duplicate_policy.pop("duplicate_policy")

    assert type(TypeAdapter(ProtocolSpec).validate_python(_base_spec())) is CampaignProtocolSpec
    with pytest.raises((ValidationError, ValueError)):
        parse_campaign_protocol(missing_profile)
    with pytest.raises(ValidationError):
        TypeAdapter(ProtocolSpec).validate_python(missing_profile)
    with pytest.raises(ValidationError):
        parse_campaign_protocol(missing_duplicate_policy)


def test_all_eight_new_profile_arms_share_design_but_keep_distinct_full_identities():
    specs = [
        _replication_spec(target, prior, duplicate_policy)
        for target in ("historical_utility", "confirmed_acceptance_reward")
        for prior in ("default", "low_noise")
        for duplicate_policy in ("reject", "replicate")
    ]

    assert len({campaign_design_sha256(spec) for spec in specs}) == 1
    assert len({campaign_protocol_sha256(spec) for spec in specs}) == 8
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


def test_new_profile_design_hash_still_tracks_non_treatment_inputs():
    original = _replication_spec()
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

    changed_budget = deepcopy(data)
    changed_budget["evaluation_budget"] = 11
    assert campaign_design_sha256(original) != campaign_design_sha256(
        parse_campaign_protocol(changed_budget)
    )


def test_new_profile_optimizer_configuration_matches_existing_target_prior_route():
    for target in ("historical_utility", "confirmed_acceptance_reward"):
        for prior in ("default", "low_noise"):
            expected = optimizer_configuration(_ablation_spec(target, prior))
            for duplicate_policy in ("reject", "replicate"):
                actual = optimizer_configuration(_replication_spec(target, prior, duplicate_policy))
                assert actual == expected


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))


def _registered_task_inputs(owner: str) -> dict:
    model = retention.register_retention_model(
        owner,
        "Synthetic protocol test model",
        {
            "gradient_coordinate": "column_inlet_at_analyte_entry",
            "analytes": [
                {"analyte_id": "A", "model": "linear", "ln_k0": 3.0, "S1": 2.0},
                {"analyte_id": "B", "model": "linear", "ln_k0": 4.0, "S1": 3.0},
            ],
            "column_length_m": 0.1,
            "effective_area_m2": 2.5e-6,
            "strong_solvent": "B",
            "parameter_origin": "synthetic",
            "parameter_reference": "Constructed protocol test inputs",
        },
        "model",
    )
    producer = gaussian_producer.register_gaussian_producer(
        owner,
        "Synthetic protocol test producer",
        model["model_id"],
        {
            "widths": [{"analyte_id": analyte_id, "fwhm_min": 0.2} for analyte_id in ("A", "B")],
            "width_origin": "synthetic_assumption",
            "width_reference": "Fixed synthetic widths for protocol test",
        },
        "producer",
    )
    contract = observations.register_observation_contract(
        owner,
        "Synthetic protocol test contract",
        {
            "required_analytes": ["A", "B"],
            "resolution_definition": RESOLUTION_DEFINITION,
            "max_acquisition_duration_min": 10.0,
        },
        "contract",
    )
    return {
        "domain": {
            "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
            "acquisition_duration_min": {"lower": 2.0, "upper": 10.0},
        },
        "flow_ml_min": 0.5,
        "constraints": {},
        "tasks": [
            {
                "task_id": "constructed-task",
                "producer_id": producer["producer_id"],
                "observation_contract_id": contract["contract_id"],
            }
        ],
        "seeds": [17, 29, 43],
        "evaluation_budget": 10,
        "initial_design_size": 3,
        "evaluation_cost": 1.0,
        "evaluation_cost_unit": "synthetic_evaluation",
    }


def test_registry_roundtrip_and_retry_retain_new_profile_and_optimizer_configuration():
    owner = "chemist"
    protocol_spec = _registered_task_inputs(owner)
    protocol_spec.update(
        {
            "profile": REPLICATION_CAMPAIGN_PROFILE,
            "optimizer_target": "confirmed_acceptance_reward",
            "surrogate_noise_prior": "low_noise",
            "duplicate_policy": "replicate",
        }
    )
    first = campaign_protocol_registry.register_campaign_protocol(
        owner, "Replicate policy", protocol_spec, "protocol"
    )
    reject_spec = {**protocol_spec, "duplicate_policy": "reject"}
    reject_response = campaign_protocol_registry.register_campaign_protocol(
        owner, "Replicate policy", reject_spec, "protocol-reject"
    )
    retry = campaign_protocol_registry.register_campaign_protocol(
        owner, "Replicate policy", protocol_spec, "protocol"
    )

    assert first == retry
    assert campaign_protocol_registry.get_campaign_protocol(owner, first["protocol_id"]) == first
    assert first["spec"]["profile"] == REPLICATION_CAMPAIGN_PROFILE
    assert first["spec"]["duplicate_policy"] == "replicate"
    assert first["optimizer_configuration"] == optimizer_configuration(first["spec"])
    assert campaign_protocol_sha256(first["spec"]) == campaign_protocol_sha256(protocol_spec)
    assert campaign_protocol_sha256(first["spec"]) != campaign_protocol_sha256(
        reject_response["spec"]
    )
    assert first["input_sha256"] != reject_response["input_sha256"]


def test_registry_rejects_tampering_with_declared_duplicate_policy():
    owner = "chemist"
    protocol_spec = _registered_task_inputs(owner)
    protocol_spec.update(
        {
            "profile": REPLICATION_CAMPAIGN_PROFILE,
            "optimizer_target": "historical_utility",
            "surrogate_noise_prior": "default",
            "duplicate_policy": "replicate",
        }
    )
    response = campaign_protocol_registry.register_campaign_protocol(
        owner, "Replicate policy", protocol_spec, "protocol"
    )
    with registry_connection(write=True) as con:
        row = con.execute(
            "SELECT payload FROM campaign_protocols WHERE id=?", (response["protocol_id"],)
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["spec"]["duplicate_policy"] = "reject"
        con.execute(
            "UPDATE campaign_protocols SET payload=? WHERE id=?",
            (canonical(payload), response["protocol_id"]),
        )

    with pytest.raises(ValueError, match="input digest"):
        campaign_protocol_registry.get_campaign_protocol(owner, response["protocol_id"])
