"""Public collection checks for immutable experiment-design lineage."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from measure_service import design_lineage_client as client_module
from measure_service.design_lineage_client import collect_experiment_design_lineage

LINEAGE_PROFILE = "measure-experiment-design-lineage/1"
ENTRY_PROFILE = "measure-experiment-design-version/1"
SCHEMA_VERSION = "1.0"
EXPERIMENT_ID = "experiment-root"


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _entry(
    revision: int,
    experiment_id: str,
    previous: dict | None,
    *,
    reason: str | None = None,
) -> dict:
    payload = {
        "profile": ENTRY_PROFILE,
        "schema_version": SCHEMA_VERSION,
        "root_experiment_id": EXPERIMENT_ID,
        "revision": revision,
        "experiment_id": experiment_id,
        "experiment_input_sha256": f"{revision:064x}",
        "supersedes_experiment_id": None if previous is None else previous["experiment_id"],
        "supersedes_entry_sha256": None if previous is None else previous["entry_sha256"],
        "reason": "Initial immutable experiment"
        if previous is None
        else reason or f"Revision {revision}",
        "created_at": f"2026-11-09T00:{revision - 1:02d}:00+00:00",
    }
    payload["entry_sha256"] = _sha(payload)
    return payload


def _entries(count: int = 1, *, reason: str | None = None) -> list[dict]:
    entries: list[dict] = []
    for revision in range(1, count + 1):
        entries.append(
            _entry(
                revision,
                EXPERIMENT_ID if revision == 1 else f"experiment-{revision}",
                entries[-1] if entries else None,
                reason=reason,
            )
        )
    return entries


def _page(entries: list[dict], offset: int, *, selected: dict | None = None) -> dict:
    selected = selected or entries[0]
    current = entries[-1]
    page_entries = entries[offset : offset + 20]
    end = offset + len(page_entries)
    return {
        "profile": LINEAGE_PROFILE,
        "schema_version": SCHEMA_VERSION,
        "root_experiment_id": entries[0]["root_experiment_id"],
        "selected_experiment_id": selected["experiment_id"],
        "selected_revision": selected["revision"],
        "current_experiment_id": current["experiment_id"],
        "current_revision": current["revision"],
        "current_entry_sha256": current["entry_sha256"],
        "count": len(entries),
        "offset": offset,
        "limit": 20,
        "next_offset": end if end < len(entries) else None,
        "entries": copy.deepcopy(page_entries),
    }


class FakeClient:
    def __init__(self, entries: list[dict], mutate=None):
        self.entries = entries
        self.mutate = mutate
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, copy.deepcopy(arguments)))
        assert name == "measurement_get_experiment_design_lineage"
        page = _page(self.entries, arguments["offset"])
        if self.mutate is not None:
            page = self.mutate(len(self.calls), arguments, page)
        return SimpleNamespace(isError=False, structuredContent=page, content=[])


def test_collects_and_normalizes_a_singleton_lineage():
    entries = _entries()
    client = FakeClient(entries)

    actual = asyncio.run(collect_experiment_design_lineage(client, EXPERIMENT_ID))

    assert actual == {
        **_page(entries, 0),
        "offset": 0,
        "limit": 1,
        "next_offset": None,
    }
    assert client.calls == [
        (
            "measurement_get_experiment_design_lineage",
            {"experiment_id": EXPERIMENT_ID, "offset": 0, "limit": 20},
        )
    ]


def test_collects_fifty_entries_with_maximum_utf8_reasons():
    entries = _entries(50, reason="é" * 1_000)
    client = FakeClient(entries)

    actual = asyncio.run(collect_experiment_design_lineage(client, EXPERIMENT_ID))

    assert actual["count"] == 50
    assert actual["offset"] == 0
    assert actual["limit"] == 50
    assert actual["next_offset"] is None
    assert actual["entries"] == entries
    assert [arguments["offset"] for _name, arguments in client.calls] == [0, 20, 40]
    assert all(arguments["limit"] == 20 for _name, arguments in client.calls)


@pytest.mark.parametrize("mutation", ["entry_hash", "predecessor", "extra_field"])
def test_rejects_entry_integrity_or_shape_tampering(mutation):
    entries = _entries(3)

    def mutate(_call_number, _arguments, page):
        if mutation == "entry_hash":
            page["entries"][0]["entry_sha256"] = "0" * 64
        elif mutation == "predecessor":
            page["entries"][1]["supersedes_experiment_id"] = "wrong-parent"
            page["entries"][1]["entry_sha256"] = _sha(
                {key: value for key, value in page["entries"][1].items() if key != "entry_sha256"}
            )
        else:
            page["entries"][0]["unexpected"] = "not allowed"
        return page

    with pytest.raises(ValueError):
        asyncio.run(collect_experiment_design_lineage(FakeClient(entries, mutate), EXPERIMENT_ID))


def test_rejects_entry_canonical_payload_over_three_kibibytes():
    entries = _entries(2)

    def mutate(_call_number, _arguments, page):
        page["entries"][1]["reason"] = "a\n" * 1_000
        payload = {key: value for key, value in page["entries"][1].items() if key != "entry_sha256"}
        page["entries"][1]["entry_sha256"] = _sha(payload)
        return page

    with pytest.raises(ValueError, match="3,000|canonical JSON"):
        asyncio.run(collect_experiment_design_lineage(FakeClient(entries, mutate), EXPERIMENT_ID))


def test_rejects_changed_head_between_pages():
    entries = _entries(21)

    def mutate(call_number, _arguments, page):
        if call_number == 2:
            page["current_experiment_id"] = "later-head"
        return page

    with pytest.raises(ValueError, match="envelope|head"):
        asyncio.run(collect_experiment_design_lineage(FakeClient(entries, mutate), EXPERIMENT_ID))


@pytest.mark.parametrize("field", ["root_experiment_id", "selected_revision", "count"])
def test_rejects_envelope_changes_between_pages(field):
    entries = _entries(21)

    def mutate(call_number, _arguments, page):
        if call_number == 2:
            if field == "selected_revision":
                page[field] = 2
            elif field == "count":
                page[field] = 20
            else:
                page[field] = "different-root"
        return page

    with pytest.raises(ValueError, match="envelope|count|selected|root|entries"):
        asyncio.run(collect_experiment_design_lineage(FakeClient(entries, mutate), EXPERIMENT_ID))


@pytest.mark.parametrize("mutation", ["offset", "limit", "next_offset", "bool_count"])
def test_rejects_bad_pagination_or_strict_numeric_types(mutation):
    entries = _entries(2)

    def mutate(_call_number, _arguments, page):
        if mutation == "offset":
            page["offset"] = 1
        elif mutation == "limit":
            page["limit"] = 19
        elif mutation == "next_offset":
            page["next_offset"] = 1
        else:
            page["count"] = True
        return page

    with pytest.raises(ValueError):
        asyncio.run(collect_experiment_design_lineage(FakeClient(entries, mutate), EXPERIMENT_ID))


@pytest.mark.parametrize("revision", [1.0, True])
def test_rejects_non_strict_entry_revision_types(revision):
    entries = _entries()

    def mutate(_call_number, _arguments, page):
        page["entries"][0]["revision"] = revision
        return page

    with pytest.raises(ValueError, match="revision|canonical"):
        asyncio.run(collect_experiment_design_lineage(FakeClient(entries, mutate), EXPERIMENT_ID))


def test_rejects_oversized_reason_before_serialization(monkeypatch):
    entries = _entries()
    entries[0]["reason"] = "x" * 1_000_000
    client = FakeClient(entries)
    monkeypatch.setattr(
        client_module.json,
        "dumps",
        lambda *_args, **_kwargs: pytest.fail("oversized entry was serialized"),
    )

    with pytest.raises(ValueError, match="reason|JSON string"):
        asyncio.run(collect_experiment_design_lineage(client, EXPERIMENT_ID))


@pytest.mark.parametrize("reason", ["é" * 1_001, "   ", "contains\x00nul"])
def test_rejects_invalid_reason_bounds(reason):
    entries = _entries()
    entries[0]["reason"] = reason

    with pytest.raises(ValueError, match="reason"):
        asyncio.run(collect_experiment_design_lineage(FakeClient(entries), EXPERIMENT_ID))


@pytest.mark.parametrize("mutation", ["nonfinite", "deep", "huge_page_field"])
def test_rejects_hostile_json_before_canonicalization(mutation):
    entries = _entries()

    def mutate(_call_number, _arguments, page):
        if mutation == "nonfinite":
            page["entries"][0]["reason"] = float("nan")
        elif mutation == "deep":
            nested: dict[str, object] = {"leaf": 1}
            for _ in range(40):
                nested = {"nested": nested}
            page["unexpected"] = nested
        else:
            page["unexpected"] = "x" * 1_000_000
        return page

    with pytest.raises(ValueError, match="JSON|canonical|bound|finite"):
        asyncio.run(collect_experiment_design_lineage(FakeClient(entries, mutate), EXPERIMENT_ID))


def test_rejects_page_canonical_bytes_over_sixty_four_kibibytes():
    entries = _entries()

    def mutate(_call_number, _arguments, page):
        page["unexpected"] = "x" * 64_000
        return page

    with pytest.raises(ValueError, match="64,000|canonical JSON"):
        asyncio.run(collect_experiment_design_lineage(FakeClient(entries, mutate), EXPERIMENT_ID))


@pytest.mark.parametrize("experiment_id", ["", "   ", True, "x" * 101])
def test_rejects_invalid_requested_id_without_calling_public_tool(experiment_id):
    client = FakeClient(_entries())

    with pytest.raises(ValueError, match="experiment_id"):
        asyncio.run(collect_experiment_design_lineage(client, experiment_id))
    assert client.calls == []
