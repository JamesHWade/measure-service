"""Versioned MCP tool groups and bounded schema-profile helpers.

The catalog is deliberately independent of MCP server internals. A server can
use :func:`enabled` before identity resolution, and can use
:func:`decorate_tool` when returning registered tools to add the stable group
hint to ``Tool._meta``.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any, TypeVar

TOOL_CATALOG_VERSION = "1.0"
TOOL_GROUPS = (
    "discovery",
    "traces",
    "sec",
    "batches",
    "evidence",
    "experiments",
    "methods",
    "observations",
    "retention",
    "campaigns",
)
TOOL_GROUP_META_KEY = "measure_service/tool_group"
TOOL_GROUPS_ENV = "MEASURE_MCP_TOOL_GROUPS"
TOOL_SCHEMA_PROFILE_ENV = "MEASURE_MCP_TOOL_SCHEMA_PROFILE"
CANONICAL_SCHEMA_PROFILE = "canonical"
FUNCTION_HOST_SCHEMA_PROFILE = "function-host/1"
_MAX_GROUP_SELECTION_BYTES = 1_024

_CATALOG_ROWS = (
    ("measurement_registry_capabilities", "discovery"),
    ("measurement_method_capabilities", "discovery"),
    ("measurement_capabilities", "discovery"),
    ("measurement_validate_method", "methods"),
    ("measurement_register_method", "methods"),
    ("measurement_get_method", "methods"),
    ("measurement_list_methods", "methods"),
    ("measurement_create_experiment", "experiments"),
    ("measurement_get_experiment", "experiments"),
    ("measurement_supersede_experiment", "experiments"),
    ("measurement_get_experiment_design_lineage", "experiments"),
    ("measurement_list_experiments", "experiments"),
    ("measurement_record_attempt", "experiments"),
    ("measurement_revise_attempt", "experiments"),
    ("measurement_get_attempt", "experiments"),
    ("measurement_list_attempts", "experiments"),
    ("measurement_record_results", "experiments"),
    ("measurement_get_result", "experiments"),
    ("measurement_list_results", "experiments"),
    ("measurement_preview_result_table", "experiments"),
    ("measurement_import_result_table", "experiments"),
    ("measurement_get_result_table_import", "experiments"),
    ("measurement_read_result_table_rows", "evidence"),
    ("measurement_read_result_table_source", "evidence"),
    ("measurement_register_observation_contract", "observations"),
    ("measurement_get_observation_contract", "observations"),
    ("measurement_evaluate_observation", "observations"),
    ("measurement_get_observation", "observations"),
    ("measurement_create_observation_snapshot", "observations"),
    ("measurement_get_observation_snapshot", "observations"),
    ("measurement_register_retention_model", "retention"),
    ("measurement_get_retention_model", "retention"),
    ("measurement_predict_retention", "retention"),
    ("measurement_get_retention_prediction", "retention"),
    ("measurement_register_gaussian_producer", "observations"),
    ("measurement_get_gaussian_producer", "observations"),
    ("measurement_record_gaussian_observation", "observations"),
    ("measurement_get_gaussian_observation", "observations"),
    ("measurement_register_campaign_protocol", "campaigns"),
    ("measurement_get_campaign_protocol", "campaigns"),
    ("measurement_create_campaign", "campaigns"),
    ("measurement_get_campaign", "campaigns"),
    ("measurement_get_campaign_suggestion_attempt", "campaigns"),
    ("measurement_list_campaign_suggestion_attempts", "campaigns"),
    ("measurement_abandon_campaign_suggestion_attempt", "campaigns"),
    ("measurement_suggest_campaign", "campaigns"),
    ("measurement_get_campaign_proposal", "campaigns"),
    ("measurement_consume_campaign_snapshot", "campaigns"),
    ("measurement_get_campaign_consumption", "campaigns"),
    ("measurement_register_dataset", "traces"),
    ("measurement_register_csv", "traces"),
    ("measurement_register_wide_csv", "traces"),
    ("measurement_register_long_csv", "traces"),
    ("measurement_inspect_dataset", "traces"),
    ("measurement_read_source", "evidence"),
    ("measurement_create_chromatogram_batch", "batches"),
    ("measurement_get_chromatogram_batch", "batches"),
    ("measurement_export_chromatogram_batch", "batches"),
    ("measurement_read_chromatogram_batch_export", "batches"),
    ("measurement_get_analytical_import", "batches"),
    ("measurement_read_chromatogram_batch_items", "batches"),
    ("measurement_read_chromatogram_batch_attempts", "batches"),
    ("measurement_read_chromatogram_batch_control_events", "batches"),
    ("measurement_run_chromatogram_batch", "batches"),
    ("measurement_get_chromatogram_batch_run", "batches"),
    ("measurement_cancel_chromatogram_batch", "batches"),
    ("measurement_recover_chromatogram_batch", "batches"),
    ("measurement_retry_chromatogram_batch_items", "batches"),
    ("measurement_analyze_chromatogram", "traces"),
    ("measurement_analyze_sec", "sec"),
    ("measurement_quantify_peak", "traces"),
    ("measurement_get_chromatogram_evidence", "evidence"),
    ("measurement_read_chromatogram_evidence", "evidence"),
    ("measurement_get_analysis", "traces"),
    ("measurement_compare_sec", "sec"),
    ("measurement_read_analysis_data", "evidence"),
)


def _build_registry(rows: Iterable[tuple[str, str]]) -> Mapping[str, str]:
    registry: dict[str, str] = {}
    for name, group in rows:
        if group not in TOOL_GROUPS:
            raise RuntimeError("Tool catalog contains an unknown group")
        if name in registry:
            raise RuntimeError("Tool catalog contains a duplicate tool name")
        registry[name] = group
    return MappingProxyType(registry)


TOOL_GROUP_BY_NAME = _build_registry(_CATALOG_ROWS)


def tool_group(name: str) -> str:
    """Return the registered group for a tool, rejecting uncataloged names."""
    try:
        return TOOL_GROUP_BY_NAME[name]
    except KeyError:
        raise KeyError("Tool is not present in the versioned tool catalog") from None


def validate_tool_registry(tool_names: Iterable[str]) -> None:
    """Require exact one-to-one coverage of the registered server tool names."""
    names = tuple(tool_names)
    if len(names) != len(set(names)):
        raise ValueError("Registered MCP tool names contain duplicates")
    current = set(names)
    known = set(TOOL_GROUP_BY_NAME)
    if current != known:
        # Keep this message independent of potentially untrusted registration
        # names while making the catalog drift easy to identify in logs.
        raise ValueError("Registered MCP tools do not match tool catalog version 1.0")


def parse_selection(raw: str | None) -> frozenset[str]:
    """Parse ``MEASURE_MCP_TOOL_GROUPS`` as a bounded array of known strings.

    Unset means all currently registered groups. An explicit empty array means
    no groups. Error messages never echo configuration values.
    """
    if raw is None:
        return frozenset(TOOL_GROUPS)
    if not isinstance(raw, str):
        raise ValueError(
            "MEASURE_MCP_TOOL_GROUPS must be a bounded JSON array of known group names"
        )
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(
            "MEASURE_MCP_TOOL_GROUPS must be a bounded JSON array of known group names"
        ) from None
    if size > _MAX_GROUP_SELECTION_BYTES:
        raise ValueError(
            "MEASURE_MCP_TOOL_GROUPS must be a bounded JSON array of known group names"
        )
    try:
        selected = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        raise ValueError(
            "MEASURE_MCP_TOOL_GROUPS must be a bounded JSON array of known group names"
        ) from None
    if not isinstance(selected, list) or len(selected) > len(TOOL_GROUPS):
        raise ValueError(
            "MEASURE_MCP_TOOL_GROUPS must be a bounded JSON array of known group names"
        )
    if any(not isinstance(group, str) for group in selected):
        raise ValueError("MEASURE_MCP_TOOL_GROUPS entries must be known group names")
    if len(selected) != len(set(selected)):
        raise ValueError("MEASURE_MCP_TOOL_GROUPS contains duplicate group names")
    if any(group not in TOOL_GROUPS for group in selected):
        raise ValueError("MEASURE_MCP_TOOL_GROUPS entries must be known group names")
    return frozenset(selected)


def configured_groups() -> frozenset[str]:
    """Read and validate the current group selection from the process environment."""
    return parse_selection(os.environ.get(TOOL_GROUPS_ENV))


def enabled(name: str, selected: Iterable[str] | None = None) -> bool:
    """Whether ``name`` belongs to the selected group set.

    Calling this helper also guarantees the tool name is cataloged, which is
    useful before any identity-dependent dispatch work.
    """
    group = tool_group(name)
    groups = configured_groups() if selected is None else frozenset(selected)
    if not groups.issubset(TOOL_GROUPS):
        raise ValueError("Selected tool groups must be known group names")
    return group in groups


_ToolT = TypeVar("_ToolT")


def decorate_tool(tool: _ToolT) -> _ToolT:
    """Return a copied MCP Tool with its stable group recorded in ``_meta``.

    Accepts either an MCP ``Tool``-like Pydantic model or a serialized mapping.
    This does not import or call MCP internals.
    """
    if isinstance(tool, Mapping):
        result = copy.deepcopy(dict(tool))
        name = result.get("name")
        group = tool_group(name) if isinstance(name, str) else None
        if group is None:
            raise ValueError("Tool mapping must have a registered string name")
        metadata_key = "_meta" if "_meta" in result or "meta" not in result else "meta"
        metadata = result.get(metadata_key)
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, Mapping):
            raise ValueError("Tool metadata must be a mapping")
        metadata = copy.deepcopy(dict(metadata))
        metadata[TOOL_GROUP_META_KEY] = group
        result[metadata_key] = metadata
        return result  # type: ignore[return-value]

    name = getattr(tool, "name", None)
    if not isinstance(name, str):
        raise ValueError("Tool object must have a registered string name")
    group = tool_group(name)
    metadata = getattr(tool, "meta", getattr(tool, "_meta", None))
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise ValueError("Tool metadata must be a mapping")
    new_metadata = copy.deepcopy(dict(metadata))
    new_metadata[TOOL_GROUP_META_KEY] = group
    model_copy = getattr(tool, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"meta": new_metadata}, deep=True)
    raise TypeError("Tool object must support Pydantic model_copy")


def schema_profile() -> str:
    """Read the optional schema projection profile from the environment."""
    value = os.environ.get(TOOL_SCHEMA_PROFILE_ENV, CANONICAL_SCHEMA_PROFILE)
    if value not in (CANONICAL_SCHEMA_PROFILE, FUNCTION_HOST_SCHEMA_PROFILE):
        raise ValueError("MEASURE_MCP_TOOL_SCHEMA_PROFILE must be 'canonical' or 'function-host/1'")
    return value


class UnsupportedSchemaProfileError(ValueError):
    """The opt-in function-host projection encountered an unreviewed construct."""


def _resolve_definition(schema: Mapping[str, Any], ref: Any) -> Mapping[str, Any] | None:
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return None
    path = ref[len("#/$defs/") :]
    if not path or "/" in path:
        return None
    definitions = schema.get("$defs")
    if not isinstance(definitions, Mapping):
        return None
    value = definitions.get(path.replace("~1", "/").replace("~0", "~"))
    return value if isinstance(value, Mapping) else None


def _is_disjoint_required_kind_union(root: Mapping[str, Any], node: Mapping[str, Any]) -> bool:
    variants = node.get("oneOf")
    discriminator = node.get("discriminator")
    if not isinstance(variants, list) or len(variants) < 2:
        return False
    if not isinstance(discriminator, Mapping) or discriminator.get("propertyName") != "kind":
        return False
    tags: set[str] = set()
    references: dict[str, str] = {}
    for variant in variants:
        if not isinstance(variant, Mapping) or set(variant) != {"$ref"}:
            return False
        ref = variant.get("$ref")
        definition = _resolve_definition(root, ref)
        if definition is None:
            return False
        required = definition.get("required")
        properties = definition.get("properties")
        if (
            not isinstance(required, list)
            or "kind" not in required
            or not isinstance(properties, Mapping)
        ):
            return False
        kind_schema = properties.get("kind")
        if not isinstance(kind_schema, Mapping) or kind_schema.get("type") != "string":
            return False
        tag = kind_schema.get("const")
        if tag is None:
            enum = kind_schema.get("enum")
            if not isinstance(enum, list) or len(enum) != 1:
                return False
            tag = enum[0]
        if not isinstance(tag, str) or tag in tags:
            return False
        tags.add(tag)
        references[tag] = ref
    mapping = discriminator.get("mapping")
    if mapping is not None:
        if not isinstance(mapping, Mapping) or dict(mapping) != references:
            return False
    return True


def _is_numeric_schema(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("type") == "number"


def _walk_and_project(node: Any, root: Mapping[str, Any], path: str = "$") -> None:
    if isinstance(node, dict):
        if "oneOf" in node:
            if "anyOf" in node or not _is_disjoint_required_kind_union(root, node):
                raise UnsupportedSchemaProfileError(
                    f"function-host/1 does not project an unproven oneOf at {path}"
                )
            node["anyOf"] = node.pop("oneOf")
            node.pop("discriminator", None)
        elif "discriminator" in node:
            raise UnsupportedSchemaProfileError(
                f"function-host/1 does not project a discriminator without a proven oneOf at {path}"
            )

        prefix_items = node.get("prefixItems")
        if prefix_items is not None:
            if (
                not isinstance(prefix_items, list)
                or len(prefix_items) != 2
                or not all(_is_numeric_schema(item) for item in prefix_items)
                or prefix_items[0] != prefix_items[1]
                or node.get("type") != "array"
                or node.get("minItems") != 2
                or node.get("maxItems") != 2
            ):
                raise UnsupportedSchemaProfileError(
                    f"function-host/1 does not project this prefixItems tuple at {path}"
                )
            existing_items = node.get("items")
            if existing_items is not None and existing_items != prefix_items[0]:
                raise UnsupportedSchemaProfileError(
                    f"function-host/1 cannot preserve items semantics for tuple at {path}"
                )
            node.pop("prefixItems")
            node["items"] = copy.deepcopy(prefix_items[0])
            node["minItems"] = 2
            node["maxItems"] = 2

        if node.get("type") == "array" and isinstance(node.get("maxItems"), int):
            maximum = node["maxItems"]
            if maximum > 16_000:
                if not _is_numeric_schema(node.get("items")):
                    raise UnsupportedSchemaProfileError(
                        f"function-host/1 does not omit this nonnumeric array limit at {path}"
                    )
                node.pop("maxItems")
                bound_note = f"Runtime still enforces the maximum of {maximum:,} items."
                description = node.get("description")
                if description:
                    node["description"] = f"{description.rstrip()} {bound_note}"
                else:
                    node["description"] = bound_note

        for key, value in tuple(node.items()):
            _walk_and_project(value, root, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk_and_project(value, root, f"{path}[{index}]")


def schema_for_profile(schema: Mapping[str, Any], profile: str | None = None) -> dict[str, Any]:
    """Return an independent schema copy in the canonical or opt-in host profile.

    The host projection only changes three reviewed constructs: a disjoint
    required string-``kind`` union, a homogeneous numeric pair, and numeric
    array maxima above 16,000. It fails when it sees unreviewed ``oneOf`` or
    tuple forms rather than implying broad vendor compatibility.
    """
    selected_profile = schema_profile() if profile is None else profile
    if selected_profile not in (CANONICAL_SCHEMA_PROFILE, FUNCTION_HOST_SCHEMA_PROFILE):
        raise ValueError("Unknown MCP tool schema profile")
    result = copy.deepcopy(dict(schema))
    if selected_profile == CANONICAL_SCHEMA_PROFILE:
        return result
    _walk_and_project(result, result)
    return result


__all__ = [
    "CANONICAL_SCHEMA_PROFILE",
    "FUNCTION_HOST_SCHEMA_PROFILE",
    "TOOL_CATALOG_VERSION",
    "TOOL_GROUPS",
    "TOOL_GROUPS_ENV",
    "TOOL_GROUP_BY_NAME",
    "TOOL_GROUP_META_KEY",
    "TOOL_SCHEMA_PROFILE_ENV",
    "UnsupportedSchemaProfileError",
    "configured_groups",
    "decorate_tool",
    "enabled",
    "parse_selection",
    "schema_for_profile",
    "schema_profile",
    "tool_group",
    "validate_tool_registry",
]
