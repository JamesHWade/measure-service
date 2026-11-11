"""Standard JSON Schema and runtime contracts for method numeric bounds."""

import copy

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from measure_service import methods
from measure_service.tool_catalog import CANONICAL_SCHEMA_PROFILE, schema_for_profile


def nested_method_input() -> dict:
    return {
        "declaration": {
            "time_unit": "min",
            "flow_unit": "mL/min",
            "solvents": ["A", "B"],
            "program": {
                "kind": "gradient_absolute",
                "points": [
                    {
                        "time": 0,
                        "state": {"flow": 1, "solvent_fractions": {"A": 0.5, "B": 0.5}},
                    },
                    {
                        "time": 1,
                        "state": {"flow": 1, "solvent_fractions": {"A": 0.5, "B": 0.5}},
                    },
                ],
            },
        },
        "constraints": {
            "max_flow_ml_min": 2,
            "max_abs_fraction_slope_per_min": 0,
            "solvent_bounds": [
                {
                    "solvent_id": "A",
                    "min_fraction": 0,
                    "max_fraction": 0.7,
                    "min_total_use_ml": 0,
                }
            ],
        },
    }


def all_keyword_paths(schema, path="$", keywords=("gt", "ge", "lt", "le")):
    found = []
    if isinstance(schema, dict):
        found.extend(f"{path}.{keyword}" for keyword in keywords if keyword in schema)
        for key, value in schema.items():
            found.extend(all_keyword_paths(value, f"{path}.{key}", keywords))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            found.extend(all_keyword_paths(value, f"{path}[{index}]", keywords))
    return found


@pytest.mark.parametrize(
    ("alias", "valid", "invalid", "expected_keywords", "pydantic_keyword"),
    [
        (methods.PositiveNumber, [1, 0.25], [0, -0.25], {"exclusiveMinimum": 0}, "gt"),
        (methods.NonnegativeNumber, [0, 0.25], [-0.25], {"minimum": 0}, "ge"),
        (
            methods.Fraction,
            [0, 0.25, 1],
            [-0.25, 1.25],
            {"minimum": 0, "maximum": 1},
            "ge",
        ),
    ],
)
def test_numeric_aliases_use_standard_schema_keywords_and_match_runtime(
    alias, valid, invalid, expected_keywords, pydantic_keyword
):
    adapter = TypeAdapter(alias)
    raw_schema = adapter.json_schema()
    assert pydantic_keyword in raw_schema
    schema = schema_for_profile(raw_schema, CANONICAL_SCHEMA_PROFILE)
    Draft202012Validator.check_schema(schema)
    assert {key: schema[key] for key in expected_keywords} == expected_keywords
    validator = Draft202012Validator(schema)

    for value in valid:
        assert validator.is_valid(value)
        adapter.validate_python(value)
    for value in invalid:
        assert not validator.is_valid(value)
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


@pytest.mark.parametrize(
    "value", [True, False, "1", "0.25", float("nan"), float("inf"), -float("inf")]
)
def test_numeric_aliases_reject_non_json_numbers_at_runtime(value):
    for alias in (methods.PositiveNumber, methods.NonnegativeNumber, methods.Fraction):
        with pytest.raises(ValidationError):
            TypeAdapter(alias).validate_python(value)


def test_real_nested_method_schemas_are_draft_2020_12_and_match_pydantic_field_bounds():
    raw_schema = methods._MethodEvaluationInput.model_json_schema()
    assert all_keyword_paths(raw_schema)
    schema = schema_for_profile(raw_schema, CANONICAL_SCHEMA_PROFILE)
    Draft202012Validator.check_schema(schema)
    assert all_keyword_paths(schema) == []
    validator = Draft202012Validator(schema)

    valid = nested_method_input()
    candidates = [
        valid,
        {
            **copy.deepcopy(valid),
            "declaration": {**valid["declaration"], "program": {"kind": "unknown"}},
        },
    ]
    zero_flow = copy.deepcopy(valid)
    zero_flow["declaration"]["program"]["points"][0]["state"]["flow"] = 0
    candidates.append(zero_flow)
    bool_flow = copy.deepcopy(valid)
    bool_flow["declaration"]["program"]["points"][0]["state"]["flow"] = True
    candidates.append(bool_flow)
    fraction_below_range = copy.deepcopy(valid)
    fraction_below_range["declaration"]["program"]["points"][0]["state"]["solvent_fractions"] = {
        "A": -0.1,
        "B": 1.1,
    }
    candidates.append(fraction_below_range)
    fraction_above_range = copy.deepcopy(valid)
    fraction_above_range["declaration"]["program"]["points"][0]["state"]["solvent_fractions"] = {
        "A": 1.1,
        "B": -0.1,
    }
    candidates.append(fraction_above_range)
    negative_constraint = copy.deepcopy(valid)
    negative_constraint["constraints"]["max_abs_fraction_slope_per_min"] = -0.1
    candidates.append(negative_constraint)
    string_flow = copy.deepcopy(valid)
    string_flow["declaration"]["program"]["points"][0]["state"]["flow"] = "1"
    candidates.append(string_flow)

    for candidate in candidates:
        try:
            methods._MethodEvaluationInput.model_validate(candidate)
            pydantic_accepts = True
        except ValidationError:
            pydantic_accepts = False
        assert validator.is_valid(candidate) == pydantic_accepts


def test_campaign_coordinate_bounds_also_emit_standard_numeric_keywords():
    from measure_service.campaign_protocol import CandidateCoordinates

    raw_schema = CandidateCoordinates.model_json_schema()
    assert all_keyword_paths(raw_schema)
    schema = schema_for_profile(raw_schema, CANONICAL_SCHEMA_PROFILE)
    Draft202012Validator.check_schema(schema)
    assert all_keyword_paths(schema) == []
    validator = Draft202012Validator(schema)
    candidates = [
        {"strong_solvent_fraction": 0, "acquisition_duration_min": 1},
        {"strong_solvent_fraction": 1, "acquisition_duration_min": 2},
        {"strong_solvent_fraction": -0.1, "acquisition_duration_min": 1},
        {"strong_solvent_fraction": 1.1, "acquisition_duration_min": 1},
        {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 0.9},
        {"strong_solvent_fraction": True, "acquisition_duration_min": 1},
        {"strong_solvent_fraction": "0.5", "acquisition_duration_min": 1},
    ]
    for candidate in candidates:
        try:
            CandidateCoordinates.model_validate(candidate)
            pydantic_accepts = True
        except ValidationError:
            pydantic_accepts = False
        assert validator.is_valid(candidate) == pydantic_accepts


def test_direct_method_and_campaign_models_project_standard_numeric_bounds():
    from measure_service.campaign_protocol import CampaignProtocolSpec, CandidateCoordinates

    models = (
        methods.MethodDeclaration,
        methods.MethodConstraints,
        methods._MethodEvaluationInput,
        methods.MethodEvaluation,
        CampaignProtocolSpec,
        CandidateCoordinates,
    )
    for model in models:
        projected = schema_for_profile(model.model_json_schema(), CANONICAL_SCHEMA_PROFILE)
        Draft202012Validator.check_schema(projected)
        assert all_keyword_paths(projected) == []
