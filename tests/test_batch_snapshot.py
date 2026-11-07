from __future__ import annotations

import copy
import json

import pytest
from snapshot_store_support import make_snapshot_store


@pytest.fixture
def captured(monkeypatch, tmp_path):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    from measure_service.batch_capture import capture_document

    state = make_snapshot_store()
    revision = state["batch"]["revision"]
    return {
        mode: capture_document("owner", state["batch_id"], revision, mode)
        for mode in ("references", "self_contained")
    }


def test_snapshot_roundtrip_and_manifest(captured):
    from measure_service.batch_snapshot import decode_snapshot, encode_snapshot, snapshot_manifest

    for mode, document in captured.items():
        original = copy.deepcopy(document)
        encoded = encode_snapshot(document)
        assert document == original
        assert decode_snapshot(encoded) == document
        manifest = snapshot_manifest(encoded)
        assert manifest == {
            "profile": "measure-chromatogram-batch-snapshot/1",
            "schema_version": "1.0",
            "mode": mode,
            "snapshot_sha256": __import__("hashlib").sha256(encoded).hexdigest(),
            "size_bytes": len(encoded),
            "batch_id": document["batch"]["batch_id"],
            "batch_revision": document["batch"]["revision"],
            "dataset_count": 1,
            "analysis_count": 2,
            "item_count": 3,
        }


def test_reference_snapshot_omits_assets(captured):
    document = captured["references"]
    assert document["datasets"][0]["payload"] is None
    assert document["datasets"][0]["source"]["content"] is None
    assert all(entry["result"] is None for entry in document["analyses"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["datasets"][0]["source"]["mapping"].update({"axis_column": "wrong"}),
        lambda d: d["items"][2].update({"state": "completed"}),
        lambda d: d["runs"][1].update({"status": "completed"}),
        lambda d: d["attempts"][1].update({"state": "completed"}),
    ],
)
def test_snapshot_rejects_cross_record_tampering(captured, mutate):
    from measure_service.batch_snapshot import encode_snapshot

    document = copy.deepcopy(captured["self_contained"])
    mutate(document)
    with pytest.raises(ValueError):
        encode_snapshot(document)


def test_snapshot_rejects_source_and_result_digest_tampering(captured):
    from measure_service.batch_snapshot import encode_snapshot

    document = copy.deepcopy(captured["self_contained"])
    document["datasets"][0]["source"]["mapping"]["signal_columns"][0]["column"] = "changed"
    with pytest.raises(ValueError):
        encode_snapshot(document)

    document = copy.deepcopy(captured["self_contained"])
    document["analyses"][0]["result"]["notes"].append("tampered")
    with pytest.raises(ValueError):
        encode_snapshot(document)


def test_snapshot_rejects_nonfinite_duplicate_and_execution_authority(captured):
    from measure_service.batch_snapshot import decode_snapshot, encode_snapshot

    document = copy.deepcopy(captured["self_contained"])
    document["datasets"][0]["payload"]["traces"][0]["axis"][0] = float("nan")
    with pytest.raises(ValueError):
        encode_snapshot(document)

    document = copy.deepcopy(captured["references"])
    document["batch"]["execution_token"] = "must-not-be-portable"
    with pytest.raises(ValueError):
        encode_snapshot(document)

    with pytest.raises(ValueError):
        decode_snapshot(b'{"profile":"x","profile":"y"}')


def test_snapshot_rejects_incomplete_item_history(captured):
    from measure_service.batch_snapshot import encode_snapshot

    document = copy.deepcopy(captured["references"])
    document["attempts"] = document["attempts"][1:]
    with pytest.raises(ValueError):
        encode_snapshot(document)


def test_snapshot_requires_canonical_bytes_and_bounded_depth(captured):
    from measure_service.batch_snapshot import decode_snapshot, encode_snapshot

    encoded = encode_snapshot(captured["references"])
    with pytest.raises(ValueError):
        decode_snapshot(encoded + b"\n")
    deep = b'{"x":' + b"[" * 100_000 + b"0" + b"]" * 100_000 + b"}"
    with pytest.raises(ValueError):
        decode_snapshot(deep)


def test_snapshot_rejects_missing_closure_and_cyclic_python_objects(captured):
    from measure_service.batch_snapshot import encode_snapshot

    document = copy.deepcopy(captured["self_contained"])
    document["datasets"] = []
    with pytest.raises(ValueError):
        encode_snapshot(document)

    document = copy.deepcopy(captured["references"])
    document["batch"]["title"] = "cycle"
    document["batch"]["manifest"]["title"] = "cycle"
    document["batch"]["manifest_sha256"] = __import__(
        "measure_service.storage", fromlist=["sha256"]
    ).sha256(document["batch"]["manifest"])
    document["batch"]["manifest"]["inputs"][0]["dataset_title"] = document["batch"]["title"]
    document["items"][0]["dataset_title"] = document["batch"]["title"]
    document["datasets"][0]["title"] = document["batch"]["title"]
    document["batch"]["manifest"]["cycle"] = document["batch"]["manifest"]
    with pytest.raises(ValueError):
        encode_snapshot(document)


def test_snapshot_rejects_duplicate_json_fields(captured):
    from measure_service.batch_snapshot import decode_snapshot, encode_snapshot

    encoded = encode_snapshot(captured["references"])
    duplicate = encoded.replace(
        b'"profile":"measure-chromatogram-batch-snapshot/1"',
        b'"profile":"measure-chromatogram-batch-snapshot/1","profile":"measure-chromatogram-batch-snapshot/1"',
        1,
    )
    with pytest.raises(ValueError):
        decode_snapshot(duplicate)
    assert json.loads(encoded)["profile"] == "measure-chromatogram-batch-snapshot/1"
