"""Versioned tool-group and bounded schema-profile contracts."""

import ast
import asyncio
import copy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp.types import Tool
from pydantic import ValidationError

from measure_service.analysis_models import Trace
from measure_service.methods import MethodDeclaration
from measure_service.tool_catalog import (
    CANONICAL_SCHEMA_PROFILE,
    FUNCTION_HOST_SCHEMA_PROFILE,
    TOOL_CATALOG_VERSION,
    TOOL_GROUP_BY_NAME,
    TOOL_GROUP_META_KEY,
    TOOL_GROUPS,
    TOOL_GROUPS_ENV,
    TOOL_SCHEMA_PROFILE_ENV,
    SchemaStandardizationError,
    UnsupportedSchemaProfileError,
    configured_groups,
    decorate_tool,
    enabled,
    parse_selection,
    schema_for_profile,
    schema_profile,
    tool_group,
    validate_tool_registry,
)
from scripts.check_tool_schemas import _nonstandard_numeric_keywords


def registered_tool_names() -> list[str]:
    server_path = Path(__file__).parents[1] / "src" / "measure_service" / "server.py"
    module = ast.parse(server_path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        ):
            names.append(node.name)
    return names


def isocratic_payload() -> dict:
    return {
        "time_unit": "min",
        "flow_unit": "mL/min",
        "solvents": ["A", "B"],
        "program": {
            "kind": "isocratic",
            "acquisition_duration": 1,
            "state": {"flow": 1, "solvent_fractions": {"A": 0.5, "B": 0.5}},
        },
    }


def test_versioned_catalog_covers_each_registered_tool_exactly_once():
    names = registered_tool_names()

    assert TOOL_CATALOG_VERSION == "1.0"
    assert len(names) == 76
    assert len(TOOL_GROUP_BY_NAME) == 76
    validate_tool_registry(names)
    assert set(TOOL_GROUP_BY_NAME.values()) == set(TOOL_GROUPS)
    assert all(tool_group(name) in TOOL_GROUPS for name in names)

    with pytest.raises(ValueError, match="duplicates"):
        validate_tool_registry([*names, names[0]])
    with pytest.raises(ValueError, match="do not match"):
        validate_tool_registry([*names[:-1], "measurement_future_tool"])


def test_tool_groups_parse_bounded_known_selection_and_fail_without_values():
    assert parse_selection(None) == frozenset(TOOL_GROUPS)
    assert parse_selection("[]") == frozenset()
    assert parse_selection('["traces", "sec"]') == {"traces", "sec"}
    assert enabled("measurement_analyze_sec", {"sec"})
    assert not enabled("measurement_analyze_sec", {"traces"})

    for raw, expected in (
        ('["traces", "traces"]', "duplicate"),
        ('["private-value"]', "known group names"),
        ('{"traces": true}', "JSON array"),
        ('["traces"', "JSON array"),
        (" " * 1_025, "bounded JSON array"),
    ):
        with pytest.raises(ValueError, match=expected) as error:
            parse_selection(raw)
        assert "private-value" not in str(error.value)

    with pytest.raises(KeyError, match="not present"):
        enabled("measurement_not_registered", set())


def test_configured_selection_is_read_once_by_server_startup_caller(monkeypatch):
    monkeypatch.setenv(TOOL_GROUPS_ENV, '["campaigns"]')
    selected = configured_groups()
    assert selected == {"campaigns"}
    assert enabled("measurement_suggest_campaign", selected)
    assert not enabled("measurement_analyze_chromatogram", selected)

    monkeypatch.setenv(TOOL_GROUPS_ENV, '["unrecognized"]')
    with pytest.raises(ValueError) as error:
        configured_groups()
    assert "unrecognized" not in str(error.value)


def test_schema_profile_defaults_to_canonical_and_rejects_unknown_values(monkeypatch):
    monkeypatch.delenv(TOOL_SCHEMA_PROFILE_ENV, raising=False)
    assert schema_profile() == "canonical"

    monkeypatch.setenv(TOOL_SCHEMA_PROFILE_ENV, FUNCTION_HOST_SCHEMA_PROFILE)
    assert schema_profile() == FUNCTION_HOST_SCHEMA_PROFILE

    monkeypatch.setenv(TOOL_SCHEMA_PROFILE_ENV, "private-profile-value")
    with pytest.raises(ValueError) as error:
        schema_profile()
    assert "private-profile-value" not in str(error.value)


@pytest.mark.parametrize(
    ("alias", "standard"),
    [
        ("gt", "exclusiveMinimum"),
        ("ge", "minimum"),
        ("lt", "exclusiveMaximum"),
        ("le", "maximum"),
    ],
)
@pytest.mark.parametrize("profile", [CANONICAL_SCHEMA_PROFILE, FUNCTION_HOST_SCHEMA_PROFILE])
def test_numeric_schema_aliases_standardize_in_both_profiles(alias, standard, profile):
    source = {"type": "number", alias: 2.5}
    original = copy.deepcopy(source)

    projected = schema_for_profile(source, profile)

    assert projected == {"type": "number", standard: 2.5}
    assert source == original
    Draft202012Validator.check_schema(projected)


@pytest.mark.parametrize(
    ("alias", "standard"),
    [
        ("gt", "exclusiveMinimum"),
        ("ge", "minimum"),
        ("lt", "exclusiveMaximum"),
        ("le", "maximum"),
    ],
)
def test_numeric_schema_alias_collisions_fail_closed(alias, standard):
    schema = {"type": "number", alias: 2, standard: 1}

    with pytest.raises(SchemaStandardizationError, match="Conflicting standard"):
        schema_for_profile(schema, CANONICAL_SCHEMA_PROFILE)


@pytest.mark.parametrize("value", [True, "1", float("nan"), float("inf"), -float("inf")])
def test_numeric_schema_alias_requires_a_finite_json_number(value):
    with pytest.raises(SchemaStandardizationError, match="Non-finite or nonnumeric"):
        schema_for_profile({"type": "number", "gt": value}, CANONICAL_SCHEMA_PROFILE)


def test_numeric_schema_alias_requires_a_number_schema_node():
    with pytest.raises(SchemaStandardizationError, match="outside a number schema"):
        schema_for_profile({"type": "string", "ge": 1}, CANONICAL_SCHEMA_PROFILE)


def test_schema_standardization_does_not_treat_property_names_or_examples_as_schemas():
    schema = {
        "type": "object",
        "properties": {
            "gt": {
                "type": "string",
                "default": {"gt": 1, "ge": 2},
                "enum": [{"lt": 3}],
                "examples": [{"le": 4}],
            }
        },
        "default": {"gt": 5, "ge": 6},
        "enum": [{"lt": 7}],
        "examples": [{"le": 8}],
        "discriminator": {"propertyName": "kind", "mapping": {"gt": "#/$defs/Kind"}},
    }
    original = copy.deepcopy(schema)

    assert schema_for_profile(schema, CANONICAL_SCHEMA_PROFILE) == original
    assert schema == original


def test_actual_registered_catalog_is_standard_under_both_profiles(monkeypatch):
    from measure_service.server import mcp

    monkeypatch.setattr(mcp, "_selected_groups", frozenset(TOOL_GROUPS))
    tools = asyncio.run(mcp.registered_tools())
    assert len(tools) == 76
    validate_tool_registry(tool.name for tool in tools)
    for profile in (CANONICAL_SCHEMA_PROFILE, FUNCTION_HOST_SCHEMA_PROFILE):
        for tool in tools:
            for schema in (tool.inputSchema, tool.outputSchema):
                if schema is not None:
                    projected = schema_for_profile(schema, profile)
                    Draft202012Validator.check_schema(projected)
                    assert _nonstandard_numeric_keywords(projected) == []

        monkeypatch.setattr(mcp, "_schema_profile", profile)
        advertised = asyncio.run(mcp.list_tools())
        assert len(advertised) == 76
        for tool in advertised:
            assert tool.meta[TOOL_GROUP_META_KEY] in TOOL_GROUPS
            for schema in (tool.inputSchema, tool.outputSchema):
                if schema is not None:
                    Draft202012Validator.check_schema(schema)
                    assert _nonstandard_numeric_keywords(schema) == []


def test_decorate_tool_copies_mapping_and_mcp_model_metadata():
    source = {
        "name": "measurement_register_dataset",
        "_meta": {"upstream": "kept"},
    }
    decorated = decorate_tool(source)
    assert decorated is not source
    assert decorated["_meta"] == {
        "upstream": "kept",
        TOOL_GROUP_META_KEY: "traces",
    }
    assert source == {"name": "measurement_register_dataset", "_meta": {"upstream": "kept"}}

    mcp_tool = Tool(
        name="measurement_analyze_sec",
        inputSchema={"type": "object"},
        _meta={"source": "test"},
    )
    decorated_model = decorate_tool(mcp_tool)
    assert decorated_model is not mcp_tool
    assert decorated_model.meta == {
        "source": "test",
        TOOL_GROUP_META_KEY: "sec",
    }
    assert mcp_tool.meta == {"source": "test"}


def test_function_host_profile_preserves_disjoint_method_union_semantics():
    canonical = MethodDeclaration.model_json_schema()
    original = copy.deepcopy(canonical)
    projected = schema_for_profile(canonical, FUNCTION_HOST_SCHEMA_PROFILE)
    Draft202012Validator.check_schema(canonical)
    Draft202012Validator.check_schema(projected)
    assert canonical == original

    canonical_validator = Draft202012Validator(canonical)
    host_validator = Draft202012Validator(projected)
    samples = [
        isocratic_payload(),
        {
            "time_unit": "min",
            "flow_unit": "mL/min",
            "solvents": ["A", "B"],
            "program": {
                "kind": "gradient_absolute",
                "points": [
                    {"time": 0, "state": {"flow": 1, "solvent_fractions": {"A": 0.5, "B": 0.5}}},
                    {"time": 1, "state": {"flow": 1, "solvent_fractions": {"A": 0.5, "B": 0.5}}},
                ],
            },
        },
        {
            "time_unit": "min",
            "flow_unit": "mL/min",
            "solvents": ["A", "B"],
            "program": {
                "kind": "gradient_duration",
                "initial_state": {"flow": 1, "solvent_fractions": {"A": 0.5, "B": 0.5}},
                "segments": [
                    {
                        "duration": 1,
                        "end_state": {"flow": 1, "solvent_fractions": {"A": 0.5, "B": 0.5}},
                    }
                ],
            },
        },
        {**isocratic_payload(), "program": {"kind": "future", "acquisition_duration": 1}},
        {**isocratic_payload(), "program": {"kind": "isocratic", "acquisition_duration": 1}},
    ]
    for sample in samples:
        assert canonical_validator.is_valid(sample) == host_validator.is_valid(sample)

    program = projected["properties"]["program"]
    assert "oneOf" not in program
    assert "discriminator" not in program
    assert len(program["anyOf"]) == 3


def test_function_host_profile_preserves_homogeneous_tuple_and_large_array_runtime_limits():
    from measure_service.server import SECMethod

    sec_schema = SECMethod.model_json_schema()
    original = copy.deepcopy(sec_schema)
    projected_sec = schema_for_profile(sec_schema, FUNCTION_HOST_SCHEMA_PROFILE)
    assert sec_schema == original
    array_schema = next(
        branch
        for branch in projected_sec["properties"]["integration_range"]["anyOf"]
        if branch.get("type") == "array"
    )
    assert array_schema["minItems"] == array_schema["maxItems"] == 2
    assert array_schema["items"] == {"type": "number"}
    assert "prefixItems" not in array_schema
    Draft202012Validator.check_schema(projected_sec)

    common = {
        "standards": [
            {"location": 0, "molecular_weight_g_mol": 1},
            {"location": 1, "molecular_weight_g_mol": 2},
        ],
        "standard_description": "Synthetic polymer standard",
        "baseline_corrected": True,
        "signal_proportional_to_weight_concentration": True,
    }
    canonical_validator = Draft202012Validator(sec_schema)
    host_validator = Draft202012Validator(projected_sec)
    for integration_range in ([0, 1], [0], [0, 1, 2], ["0", 1], None):
        sample = {**common, "integration_range": integration_range}
        assert canonical_validator.is_valid(sample) == host_validator.is_valid(sample)

    trace_schema = Trace.model_json_schema()
    projected_trace = schema_for_profile(trace_schema, FUNCTION_HOST_SCHEMA_PROFILE)
    for field in ("axis", "signal"):
        field_schema = projected_trace["properties"][field]
        assert "maxItems" not in field_schema
        assert "50,000" in field_schema["description"]
    Draft202012Validator.check_schema(projected_trace)

    trace = {
        "trace_id": "t-1",
        "sample_id": "s-1",
        "injection_id": "i-1",
        "channel": "UV",
        "separation": "LC",
        "detector": "DAD",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "axis": list(range(50_001)),
        "signal": [1.0] * 50_001,
    }
    assert Draft202012Validator(projected_trace).is_valid(trace)
    with pytest.raises(ValidationError, match="at most 50000"):
        Trace.model_validate(trace)


def test_function_host_profile_fails_on_unproven_union_or_tuple():
    heterogeneous_union = {
        "oneOf": [
            {"type": "string"},
            {"type": "number"},
        ]
    }
    with pytest.raises(UnsupportedSchemaProfileError, match="unproven oneOf"):
        schema_for_profile(heterogeneous_union, FUNCTION_HOST_SCHEMA_PROFILE)

    heterogeneous_tuple = {
        "type": "array",
        "prefixItems": [{"type": "string"}, {"type": "number"}],
        "minItems": 2,
        "maxItems": 2,
    }
    with pytest.raises(UnsupportedSchemaProfileError, match="prefixItems tuple"):
        schema_for_profile(heterogeneous_tuple, FUNCTION_HOST_SCHEMA_PROFILE)


def test_mapping_decorator_does_not_alias_nested_metadata():
    source = {"name": "measurement_analyze_chromatogram", "_meta": {"nested": {"value": 1}}}
    decorated = decorate_tool(source)
    decorated["_meta"]["nested"]["value"] = 2
    assert source["_meta"]["nested"]["value"] == 1


def test_selection_errors_do_not_echo_raw_configuration():
    secret = '"sensitive-unlisted-group"'
    with pytest.raises(ValueError) as error:
        parse_selection(f"[{secret}]")
    assert secret not in str(error.value)
