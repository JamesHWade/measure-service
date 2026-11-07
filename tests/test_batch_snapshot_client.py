"""Reject malformed manifests/pages before accepting any portable evidence."""

import asyncio
import copy
import sqlite3
from collections import OrderedDict
from types import SimpleNamespace

import pytest
from snapshot_store_support import make_snapshot_store

from measure_service import batch_exports, storage
from measure_service.batch_snapshot import decode_snapshot
from measure_service.batch_snapshot_client import collect_batch_snapshot


@pytest.fixture
def declared(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(batch_exports, "_CACHE", OrderedDict())
    monkeypatch.setattr(batch_exports, "_CACHE_BYTES", 0)
    return make_snapshot_store()


class Client:
    def __init__(self, mutate=None):
        self.calls = 0
        self.mutate = mutate

    async def call_tool(self, name, args):
        self.calls += 1
        if name == "measurement_export_chromatogram_batch":
            value = batch_exports.export_batch(
                "owner", args["batch_id"], args["expected_revision"], args["mode"]
            )
        else:
            value = batch_exports.read_export(
                "owner", args["snapshot_sha256"], args["offset"], args["limit"]
            )
        value = copy.deepcopy(value)
        if self.mutate:
            self.mutate(name, value)
        return SimpleNamespace(isError=False, structuredContent=value, content=[])


def test_collects_complete_self_contained_and_references_documents(declared):
    for mode in ["self_contained", "references"]:
        client = Client()
        data = asyncio.run(
            collect_batch_snapshot(
                client, declared["batch_id"], declared["batch"]["revision"], mode, page_size=2000
            )
        )
        assert decode_snapshot(data)["mode"] == mode and client.calls > 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("batch_id", ""),
        ("expected_revision", True),
        ("expected_revision", 0),
        ("mode", "automatic"),
        ("page_size", False),
    ],
)
def test_invalid_capture_requests_make_no_remote_call(field, value):
    arguments = {"batch_id": "batch", "expected_revision": 1, "mode": "self_contained"}
    arguments[field] = value
    client = Client()
    with pytest.raises(ValueError):
        asyncio.run(collect_batch_snapshot(client, **arguments))
    assert client.calls == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("dataset_count", True),
        ("analysis_count", 2.0),
        ("item_count", 3.0),
        ("dataset_count", 33),
        ("analysis_count", -1),
    ],
)
def test_bad_counts_fail_before_any_page(declared, field, value):
    def mutate(name, response):
        if name == "measurement_export_chromatogram_batch":
            response[field] = value

    client = Client(mutate)
    with pytest.raises(ValueError, match="record count"):
        asyncio.run(
            collect_batch_snapshot(
                client, declared["batch_id"], declared["batch"]["revision"], "self_contained"
            )
        )
    assert client.calls == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("offset", True),
        ("count", False),
        ("next_offset", 1),
        ("data_base64", "!notbase64"),
        ("data_base64", ""),
    ],
)
def test_bad_pages_never_return_a_partial_snapshot(declared, field, value):
    def mutate(name, response):
        if name == "measurement_read_chromatogram_batch_export":
            response[field] = value

    with pytest.raises(ValueError):
        asyncio.run(
            collect_batch_snapshot(
                Client(mutate),
                declared["batch_id"],
                declared["batch"]["revision"],
                "self_contained",
                page_size=2000,
            )
        )


def test_owner_store_identity_and_eviction_fence_cached_pages(declared, tmp_path, monkeypatch):
    monkeypatch.setattr(batch_exports, "MAX_EXPORTS", 1)
    first = batch_exports.export_batch(
        "owner", declared["batch_id"], declared["batch"]["revision"], "self_contained"
    )
    assert batch_exports.read_export("owner", first["snapshot_sha256"], 0, 10)["next_offset"] == 10
    with pytest.raises(ValueError, match="not found"):
        batch_exports.read_export("other", first["snapshot_sha256"])
    second = batch_exports.export_batch(
        "owner", declared["batch_id"], declared["batch"]["revision"], "references"
    )
    with pytest.raises(ValueError, match="not found"):
        batch_exports.read_export("owner", first["snapshot_sha256"])
    replacement = tmp_path / "replacement.sqlite3"
    with sqlite3.connect(replacement):
        pass
    replacement.replace(storage.db_path())
    with pytest.raises(ValueError, match="not found"):
        batch_exports.read_export("owner", second["snapshot_sha256"])
