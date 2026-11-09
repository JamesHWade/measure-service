"""Exact pre-serialization JSON budget and hostile-tree contract tests."""

from __future__ import annotations

import json

import pytest

from measure_service import bounded_json, design_lineage_client, result_table_client


def _fail(message: str) -> ValueError:
    return ValueError(f"test failure: {message}")


def _encoded_size(value: object) -> int:
    return len(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


@pytest.mark.parametrize(
    "value",
    [
        {"text": "a\n" * 100},
        {"text": "é" * 100},
        [None, True, False, 1, -2, 1.5],
    ],
)
def test_preflight_matches_compact_utf8_json_at_exact_boundaries(value):
    expected = _encoded_size(value)

    bounded_json.preflight_json(value, "fixture", max_bytes=expected, fail=_fail)
    with pytest.raises(ValueError, match="canonical JSON limit"):
        bounded_json.preflight_json(value, "fixture", max_bytes=expected - 1, fail=_fail)


def test_preflight_counts_escaped_controls_without_serializer_allocation():
    value = ["\x01" * 11_000]

    with pytest.raises(ValueError, match="canonical JSON limit"):
        bounded_json.preflight_json(value, "control fixture", max_bytes=64_000, fail=_fail)


@pytest.mark.parametrize(
    "value",
    [
        ["x" * 40_000, "y" * 40_000],
        {"a": "é" * 20_000, "b": "ñ" * 20_000},
        [10**800] * 100,
    ],
)
def test_preflight_rejects_cumulative_document_bytes(value):
    with pytest.raises(ValueError, match="canonical JSON limit"):
        bounded_json.preflight_json(value, "aggregate fixture", max_bytes=64_000, fail=_fail)


def test_preflight_rejects_depth_nodes_nonfinite_and_json_subclasses():
    nested: object = 0
    for _ in range(bounded_json.MAX_JSON_DEPTH + 1):
        nested = [nested]
    with pytest.raises(ValueError, match="nesting"):
        bounded_json.preflight_json(nested, "deep fixture", max_bytes=64_000, fail=_fail)

    with pytest.raises(ValueError, match="node"):
        bounded_json.preflight_json(
            [None] * bounded_json.MAX_JSON_NODES,
            "node fixture",
            max_bytes=64_000,
            fail=_fail,
        )

    with pytest.raises(ValueError, match="non-finite"):
        bounded_json.preflight_json(float("inf"), "number fixture", max_bytes=64_000, fail=_fail)

    class DictSubclass(dict):
        pass

    class ListSubclass(list):
        pass

    for value in (DictSubclass(value=1), ListSubclass([1])):
        with pytest.raises(ValueError, match="canonical JSON"):
            bounded_json.preflight_json(value, "subclass fixture", max_bytes=64_000, fail=_fail)


def test_client_serializers_delegate_exact_aggregate_preflight(monkeypatch):
    value = ["\x01" * 11_000]

    monkeypatch.setattr(
        result_table_client.json,
        "dumps",
        lambda *_args, **_kwargs: pytest.fail("result-table serializer was called"),
    )
    with pytest.raises(ValueError, match="canonical JSON limit"):
        result_table_client._canonical(value, "result page")

    monkeypatch.setattr(
        design_lineage_client.json,
        "dumps",
        lambda *_args, **_kwargs: pytest.fail("lineage serializer was called"),
    )
    with pytest.raises(ValueError, match="canonical JSON limit"):
        design_lineage_client._canonical(value, "lineage page", max_bytes=64_000)


@pytest.mark.parametrize(
    ("value", "maximum"),
    [
        ({"v": "a\n" * 1_000}, 3_000),
        (["x" * 60_000] * 3, 160_000),
    ],
)
def test_lineage_entry_and_aggregate_budgets_precede_serializer(monkeypatch, value, maximum):
    monkeypatch.setattr(
        design_lineage_client.json,
        "dumps",
        lambda *_args, **_kwargs: pytest.fail("lineage serializer was called"),
    )

    with pytest.raises(ValueError, match="canonical JSON limit"):
        design_lineage_client._canonical(value, "lineage bounded document", max_bytes=maximum)


@pytest.mark.parametrize("maximum", [3_000, 64_000, 160_000])
def test_lineage_canonicalizer_accepts_exact_document_limits(maximum):
    if maximum <= 64_000:
        value = {"v": "x" * (maximum - 8)}
    else:
        value = {"a": "x" * 50_000, "b": "x" * 50_000, "c": "x" * 50_000}
        value["c"] += "x" * (maximum - _encoded_size(value))

    rendered, encoded_length = design_lineage_client._canonical(
        value, "lineage exact boundary", max_bytes=maximum
    )

    assert encoded_length == maximum
    assert len(rendered.encode("utf-8")) == maximum
