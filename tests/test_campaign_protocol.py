"""Strict protocol and reproducible candidate generation contracts."""

import copy
import json
import math

import pytest
from pydantic import ValidationError

from measure_service.campaign_protocol import (
    ANALYTICAL_SUCCESS_POLICY,
    BO_POLICY_ID,
    CAMPAIGN_PROFILE,
    FIDELITY_ROLE,
    FIDELITY_VALUE,
    OBSERVATION_ORIGIN,
    RANDOM_POLICY_ID,
    UTILITY_INPUT_DEFINITION,
    UTILITY_PROFILE,
    CampaignProtocolSpec,
    campaign_design_sha256,
    campaign_protocol_sha256,
    initial_candidate,
    method_declaration,
    parse_campaign_protocol,
    random_candidate,
)


def _spec(**changes):
    value = {
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
            },
            {
                "task_id": "task-b",
                "producer_id": "producer-b",
                "observation_contract_id": "contract-b",
            },
        ],
        "evaluation_cost": 1.0,
        "evaluation_cost_unit": "candidate-evaluation",
    }
    value.update(changes)
    return CampaignProtocolSpec.model_validate(value)


def _candidate(**changes):
    value = {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 5.0}
    value.update(changes)
    return value


def test_protocol_normalizes_supported_profile_defaults_and_links():
    spec = _spec()

    assert spec.profile == CAMPAIGN_PROFILE
    assert spec.evaluation_budget == 10
    assert spec.initial_design_size == 3
    assert spec.seeds == [1, 2, 3]
    assert spec.policies == [RANDOM_POLICY_ID, BO_POLICY_ID]
    assert spec.fidelity == FIDELITY_VALUE
    assert spec.fidelity_role == FIDELITY_ROLE == "high"
    assert spec.observation_origin == OBSERVATION_ORIGIN == "simulator_latent"
    assert spec.utility_profile == UTILITY_PROFILE
    assert spec.utility_input_definition == UTILITY_INPUT_DEFINITION
    assert spec.analytical_success_policy == ANALYTICAL_SUCCESS_POLICY
    assert spec.initial_evaluations_always_count is True
    assert len(spec.tasks) == 2
    json.dumps(spec.model_dump(mode="json"), allow_nan=False)


def test_v1_profile_dispatch_preserves_normalization_hashes_and_candidate_streams():
    spec = _spec(seeds=[17, 29, 43])
    legacy_mapping = spec.model_dump(mode="json")
    legacy_mapping.pop("profile")

    assert parse_campaign_protocol(spec) is spec
    parsed_legacy = parse_campaign_protocol(legacy_mapping)
    assert isinstance(parsed_legacy, CampaignProtocolSpec)
    assert parsed_legacy.model_dump(mode="json") == spec.model_dump(mode="json")
    assert (
        campaign_protocol_sha256(spec)
        == "3e12ee05793dfe3b10237dba335b113e38ce804a70b4bf15711d412334699f00"
    )
    assert (
        campaign_design_sha256(spec)
        == "c5b95044bfae6f863f9d204e2b04e4f5ecba32ef393d2dc906efc3076d024cca"
    )
    assert [initial_candidate(spec, 17, ordinal) for ordinal in range(3)] == [
        {
            "strong_solvent_fraction": 0.5698782055393766,
            "acquisition_duration_min": 9.44226980261875,
        },
        {
            "strong_solvent_fraction": 0.6599206037528836,
            "acquisition_duration_min": 3.1467130356304747,
        },
        {
            "strong_solvent_fraction": 0.2601158635447917,
            "acquisition_duration_min": 4.257998017073078,
        },
    ]
    assert [random_candidate(spec, 17, ordinal) for ordinal in (3, 4, 9)] == [
        {
            "strong_solvent_fraction": 0.5940468572987686,
            "acquisition_duration_min": 9.431606057854996,
        },
        {
            "strong_solvent_fraction": 0.4849343427907574,
            "acquisition_duration_min": 8.486545330731628,
        },
        {
            "strong_solvent_fraction": 0.6780335472387462,
            "acquisition_duration_min": 5.863893857883884,
        },
    ]


def test_method_declaration_maps_solvent_b_and_uses_declared_constraints():
    spec = _spec(
        constraints={
            "max_acquisition_duration_min": 10.0,
            "max_flow_ml_min": 0.5,
            "solvent_bounds": [
                {"solvent_id": "A", "min_fraction": 0.1, "max_fraction": 0.9},
                {"solvent_id": "B", "min_fraction": 0.1, "max_fraction": 0.9},
            ],
        }
    )
    declaration = method_declaration(spec, _candidate(strong_solvent_fraction=0.7))

    assert declaration.solvents == ["A", "B"]
    assert declaration.flow_unit == "mL/min"
    assert declaration.time_unit == "min"
    assert declaration.program.kind == "isocratic"
    assert declaration.program.acquisition_duration == 5.0
    assert declaration.program.state.flow == 0.5
    assert declaration.program.state.solvent_fractions["A"] == pytest.approx(0.3)
    assert declaration.program.state.solvent_fractions["B"] == 0.7
    assert declaration.pre_acquisition is None
    assert declaration.post_acquisition is None
    assert declaration.column is declaration.instrument is declaration.detector is None


def test_candidate_domain_is_closed_and_numeric():
    spec = _spec(
        domain={
            "strong_solvent_fraction": {"lower": 0.0, "upper": 1.0},
            "acquisition_duration_min": {"lower": 1.0, "upper": 10.0},
        }
    )
    lower = method_declaration(
        spec, _candidate(strong_solvent_fraction=0.0, acquisition_duration_min=1.0)
    )
    upper = method_declaration(
        spec, _candidate(strong_solvent_fraction=1.0, acquisition_duration_min=10.0)
    )

    assert lower.program.state.solvent_fractions == {"A": 1.0, "B": 0.0}
    assert upper.program.state.solvent_fractions == {"A": 0.0, "B": 1.0}
    assert lower.program.acquisition_duration == 1.0
    assert upper.program.acquisition_duration == 10.0

    for candidate in (
        _candidate(strong_solvent_fraction=-0.01),
        _candidate(strong_solvent_fraction=1.01),
        _candidate(acquisition_duration_min=0.99),
        _candidate(acquisition_duration_min=10.01),
    ):
        with pytest.raises((ValueError, ValidationError)):
            method_declaration(spec, candidate)


def test_initial_candidates_fill_each_latin_hypercube_stratum_and_replay():
    spec = _spec(initial_design_size=5, evaluation_budget=8)
    first = [initial_candidate(spec, seed=2, ordinal=index) for index in range(5)]
    replay = [initial_candidate(spec, seed=2, ordinal=index) for index in range(5)]

    assert first == replay
    for coordinate, (lower, upper) in {
        "strong_solvent_fraction": (0.1, 0.9),
        "acquisition_duration_min": (1.0, 10.0),
    }.items():
        strata = {min(4, int((item[coordinate] - lower) / (upper - lower) * 5)) for item in first}
        assert strata == {0, 1, 2, 3, 4}
    assert initial_candidate(spec, seed=3, ordinal=0) != first[0]


def test_random_candidates_are_seed_ordinal_deterministic_and_bounded():
    spec = _spec()
    one = random_candidate(spec, seed=1, ordinal=3)
    replay = random_candidate(spec, seed=1, ordinal=3)
    another = random_candidate(spec, seed=1, ordinal=4)

    assert one == replay
    assert one != another
    assert 0.1 <= one["strong_solvent_fraction"] <= 0.9
    assert 1.0 <= one["acquisition_duration_min"] <= 10.0


def test_initial_and_random_ordinals_require_predeclared_seed_and_budget_slot():
    spec = _spec()
    with pytest.raises(ValueError, match="predeclared"):
        initial_candidate(spec, seed=7, ordinal=0)
    with pytest.raises(TypeError, match="seed must be an integer"):
        initial_candidate(spec, seed=True, ordinal=0)
    with pytest.raises(ValueError, match="initial candidate ordinal"):
        initial_candidate(spec, seed=1, ordinal=3)
    with pytest.raises(TypeError, match="ordinal must be an integer"):
        initial_candidate(spec, seed=1, ordinal=False)
    with pytest.raises(ValueError, match="random candidate ordinal"):
        random_candidate(spec, seed=1, ordinal=2)
    with pytest.raises(ValueError, match="random candidate ordinal"):
        random_candidate(spec, seed=1, ordinal=10)


def test_candidates_reject_extra_or_missing_coordinates_and_bad_numbers():
    spec = _spec()
    bad_candidates = (
        {"strong_solvent_fraction": 0.5},
        _candidate(unused=1),
        _candidate(strong_solvent_fraction=True),
        _candidate(strong_solvent_fraction="0.5"),
        _candidate(strong_solvent_fraction=math.nan),
        _candidate(acquisition_duration_min=math.inf),
    )
    for candidate in bad_candidates:
        with pytest.raises((ValueError, ValidationError)):
            method_declaration(spec, candidate)


def test_method_constraints_that_make_any_corner_infeasible_reject_protocol():
    impossible_constraints = (
        {"max_flow_ml_min": 0.4},
        {"max_acquisition_duration_min": 0.9},
        {"min_segment_duration_min": 11.0},
        {"solvent_bounds": [{"solvent_id": "B", "min_fraction": 0.95}]},
        {"equipment_constraints": [{"component": "instrument", "required": True}]},
    )
    for constraints in impossible_constraints:
        with pytest.raises(ValidationError, match="corners"):
            _spec(constraints=constraints)


@pytest.mark.parametrize(
    "change",
    [
        {"profile": "other"},
        {"unexpected": True},
        {"fidelity": True},
        {"fidelity": 0.5},
        {"fidelity_role": "low"},
        {"observation_origin": "physical"},
        {"utility_profile": "other"},
        {"utility_input_definition": "other"},
        {"analytical_success_policy": "combined"},
        {"initial_evaluations_always_count": 1},
        {"evaluation_cost": 0},
        {"evaluation_cost_unit": " "},
        {"flow_ml_min": True},
        {"flow_ml_min": math.inf},
        {"evaluation_budget": True},
        {"evaluation_budget": 51},
        {"initial_design_size": 2},
        {"initial_design_size": 10},
        {"policies": ["random", "other"]},
        {"stopping_policy": "early_success"},
    ],
)
def test_protocol_rejects_unsupported_or_non_strict_values(change):
    base = {
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
        "evaluation_cost": 1.0,
        "evaluation_cost_unit": "candidate-evaluation",
    }
    base.update(change)

    with pytest.raises((ValidationError, ValueError)):
        CampaignProtocolSpec.model_validate(base)


def test_invalid_domains_tasks_seeds_and_budget_combinations_are_rejected():
    base = _spec().model_dump(mode="python")
    for domain in (
        {
            "strong_solvent_fraction": {"lower": -0.1, "upper": 0.9},
            "acquisition_duration_min": {"lower": 1.0, "upper": 10.0},
        },
        {
            "strong_solvent_fraction": {"lower": 0.9, "upper": 0.9},
            "acquisition_duration_min": {"lower": 1.0, "upper": 10.0},
        },
        {
            "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
            "acquisition_duration_min": {"lower": 0.5, "upper": 10.0},
        },
        {
            "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
            "acquisition_duration_min": {"lower": 1.0, "upper": 1.0},
        },
    ):
        invalid = copy.deepcopy(base)
        invalid["domain"] = domain
        with pytest.raises(ValidationError):
            CampaignProtocolSpec.model_validate(invalid)

    invalid = copy.deepcopy(base)
    invalid["tasks"].append(copy.deepcopy(invalid["tasks"][0]))
    with pytest.raises(ValidationError, match="task_id values must be unique"):
        CampaignProtocolSpec.model_validate(invalid)
    invalid = copy.deepcopy(base)
    invalid["tasks"] = []
    with pytest.raises(ValidationError):
        CampaignProtocolSpec.model_validate(invalid)
    invalid = copy.deepcopy(base)
    invalid["seeds"] = [1, 1]
    with pytest.raises(ValidationError, match="seeds must be unique"):
        CampaignProtocolSpec.model_validate(invalid)
    invalid = copy.deepcopy(base)
    invalid["seeds"] = [True]
    with pytest.raises(ValidationError):
        CampaignProtocolSpec.model_validate(invalid)
    invalid = copy.deepcopy(base)
    invalid["seeds"] = [-1]
    with pytest.raises(ValidationError):
        CampaignProtocolSpec.model_validate(invalid)
    invalid = copy.deepcopy(base)
    invalid["seeds"] = [2**31]
    with pytest.raises(ValidationError):
        CampaignProtocolSpec.model_validate(invalid)
    invalid = copy.deepcopy(base)
    invalid["seeds"] = list(range(21))
    with pytest.raises(ValidationError):
        CampaignProtocolSpec.model_validate(invalid)
    invalid = copy.deepcopy(base)
    invalid["evaluation_budget"] = 3
    with pytest.raises(ValidationError):
        CampaignProtocolSpec.model_validate(invalid)
    invalid = copy.deepcopy(base)
    invalid["initial_design_size"] = invalid["evaluation_budget"]
    with pytest.raises(ValidationError):
        CampaignProtocolSpec.model_validate(invalid)


def test_nonfinite_total_evaluation_cost_is_rejected_at_declaration():
    with pytest.raises(ValidationError, match="evaluation_budget times evaluation_cost"):
        _spec(evaluation_cost=1e308)


@pytest.mark.parametrize("initial_design_size", [3, 5])
def test_binary64_narrow_domains_that_collapse_lhs_points_are_rejected(initial_design_size):
    with pytest.raises(ValidationError, match="unique initial candidates in every LHS stratum"):
        _spec(
            domain={
                "strong_solvent_fraction": {
                    "lower": 0.5,
                    "upper": math.nextafter(0.5, math.inf),
                },
                "acquisition_duration_min": {
                    "lower": 2.0,
                    "upper": math.nextafter(2.0, math.inf),
                },
            },
            seeds=[1, 2, 3],
            initial_design_size=initial_design_size,
            evaluation_budget=8,
        )


def test_protocol_hash_uses_normalized_canonical_json():
    spec = _spec()
    data = spec.model_dump(mode="json")
    reordered = {key: data[key] for key in reversed(data)}

    assert campaign_protocol_sha256(spec) == campaign_protocol_sha256(reordered)


def test_design_hash_and_candidates_ignore_opaque_task_record_ids():
    spec = _spec()
    rebound_data = spec.model_dump(mode="json")
    rebound_data["tasks"][0]["producer_id"] = "new-producer-a"
    rebound_data["tasks"][0]["observation_contract_id"] = "new-contract-a"
    rebound_data["tasks"][1]["producer_id"] = "new-producer-b"
    rebound_data["tasks"][1]["observation_contract_id"] = "new-contract-b"
    rebound = CampaignProtocolSpec.model_validate(rebound_data)

    assert campaign_protocol_sha256(spec) != campaign_protocol_sha256(rebound)
    assert campaign_design_sha256(spec) == campaign_design_sha256(rebound)
    assert [initial_candidate(spec, 2, i) for i in range(3)] == [
        initial_candidate(rebound, 2, i) for i in range(3)
    ]
    assert random_candidate(spec, 2, 3) == random_candidate(rebound, 2, 3)


def test_design_hash_canonicalizes_logical_task_order_but_tracks_task_identity():
    spec = _spec()
    data = spec.model_dump(mode="json")
    reordered_data = copy.deepcopy(data)
    reordered_data["tasks"].reverse()
    renamed_data = copy.deepcopy(data)
    renamed_data["tasks"][0]["task_id"] = "different-task"
    reordered = CampaignProtocolSpec.model_validate(reordered_data)
    renamed = CampaignProtocolSpec.model_validate(renamed_data)

    assert campaign_design_sha256(spec) == campaign_design_sha256(reordered)
    assert campaign_design_sha256(spec) != campaign_design_sha256(renamed)
    assert initial_candidate(spec, 2, 0) == initial_candidate(reordered, 2, 0)
    assert initial_candidate(spec, 2, 0) != initial_candidate(renamed, 2, 0)
