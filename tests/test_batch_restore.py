"""Adversarial, R-free tests for portable chromatogram-batch restoration."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from measure_service import analysis_evidence, batch_capture, batch_restore, import_origins
from measure_service.storage import canonical


def _make_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "self_contained",
    with_source: bool = True,
):
    bundle = pytest.importorskip("measure_service.batch_bundle")
    from snapshot_store_support import make_snapshot_store

    source_store = tmp_path / "source-store"
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(source_store))
    fixture = make_snapshot_store("owner", with_source=with_source)
    snapshot = batch_capture.capture_snapshot(
        "owner", fixture["batch_id"], fixture["batch"]["revision"], mode
    )
    archive = bundle.pack_bundle(snapshot)
    source = tmp_path / f"{mode}.zip"
    source.write_bytes(archive)
    return source, archive, snapshot, fixture, source_store


def _rows(database: Path, query: str, parameters: tuple = ()) -> list[sqlite3.Row]:
    con = sqlite3.connect(database)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(query, parameters).fetchall()
    finally:
        con.close()


def test_restore_is_self_contained_atomic_and_keeps_history_in_archive(tmp_path, monkeypatch):
    source, archive, snapshot, fixture, source_store = _make_archive(tmp_path, monkeypatch)
    destination = tmp_path / "restored"
    original_environment = os.environ["MEASURE_MCP_DATA_DIR"]

    original_db = source_store / "measure.sqlite3"
    original_datasets = _rows(
        original_db,
        "SELECT id, owner, digest, payload, created_at FROM datasets ORDER BY id",
    )
    original_sources = _rows(
        original_db,
        "SELECT dataset_id, kind, digest, content, mapping FROM dataset_sources ORDER BY dataset_id",
    )
    original_analyses = _rows(
        original_db,
        "SELECT id, dataset_id, trace_id, kind, method, result, provenance, created_at "
        "FROM analyses ORDER BY id",
    )

    receipt = batch_restore.restore_bundle(source, destination)

    assert os.environ["MEASURE_MCP_DATA_DIR"] == original_environment
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert not (destination / batch_restore.INCOMPLETE_NAME).exists()
    assert (destination / batch_restore.ARCHIVE_NAME).read_bytes() == archive
    receipt_path = destination / batch_restore.RECEIPT_NAME
    assert json.loads(receipt_path.read_text()) == receipt
    assert receipt["profile"] == import_origins.RESTORE_PROFILE
    assert receipt["source_batch_id"] == fixture["batch_id"]
    assert receipt["new_batch_id"] != fixture["batch_id"]
    assert receipt["no_execution_performed"] is True
    assert "record_integrity" not in receipt["limits"]
    assert set(receipt["dataset_map"]) == {row["id"] for row in original_datasets}
    assert set(receipt["analysis_map"]) == {row["id"] for row in original_analyses}

    restored_db = destination / "measure.sqlite3"
    restored_datasets = _rows(
        restored_db,
        "SELECT id, owner, digest, payload, created_at FROM datasets ORDER BY id",
    )
    assert len(restored_datasets) == len(original_datasets) == 1
    for row in restored_datasets:
        source_id = next(key for key, value in receipt["dataset_map"].items() if value == row["id"])
        original = next(item for item in original_datasets if item["id"] == source_id)
        assert row["owner"] == "local"
        assert row["digest"] == original["digest"]
        assert row["payload"] == original["payload"]
        assert row["created_at"] == original["created_at"]

    restored_sources = _rows(
        restored_db,
        "SELECT dataset_id, kind, digest, content, mapping FROM dataset_sources ORDER BY dataset_id",
    )
    assert len(restored_sources) == len(original_sources) == 1
    assert restored_sources[0]["kind"] == original_sources[0]["kind"]
    assert restored_sources[0]["digest"] == original_sources[0]["digest"]
    assert restored_sources[0]["content"] == original_sources[0]["content"]
    assert json.loads(restored_sources[0]["mapping"]) == json.loads(original_sources[0]["mapping"])

    restored_analyses = _rows(
        restored_db,
        "SELECT id, owner, dataset_id, trace_id, kind, method, result, provenance, created_at "
        "FROM analyses ORDER BY id",
    )
    assert len(restored_analyses) == len(original_analyses) == 2
    for row in restored_analyses:
        source_id = next(
            key for key, value in receipt["analysis_map"].items() if value == row["id"]
        )
        original = next(item for item in original_analyses if item["id"] == source_id)
        assert row["owner"] == "local"
        assert row["trace_id"] == original["trace_id"]
        assert row["kind"] == original["kind"] == "chromatogram"
        assert row["method"] == original["method"]
        assert row["result"] == original["result"]
        assert row["provenance"] == original["provenance"]
        assert row["created_at"] == original["created_at"]
        assert row["dataset_id"] == receipt["dataset_map"][original["dataset_id"]]

    new_batch = _rows(
        restored_db,
        "SELECT id, owner, status, revision FROM analysis_batches WHERE id=?",
        (receipt["new_batch_id"],),
    )
    assert len(new_batch) == 1
    assert new_batch[0]["owner"] == "local"
    assert new_batch[0]["status"] == "active"
    restored_items = _rows(
        restored_db,
        "SELECT state, analysis_id FROM analysis_batch_items WHERE batch_id=? ORDER BY ordinal",
        (receipt["new_batch_id"],),
    )
    assert [tuple(row) for row in restored_items] == [
        ("pending", None),
        ("pending", None),
        ("pending", None),
    ]

    # Operational history and execution authority never become active records.
    for table in (
        "analysis_batch_attempts",
        "analysis_batch_runs",
        "analysis_batch_control_events",
        "registry_receipts",
    ):
        assert _rows(restored_db, f"SELECT COUNT(*) AS count FROM {table}")[0]["count"] == 0

    con = sqlite3.connect(restored_db)
    con.row_factory = sqlite3.Row
    try:
        dataset_origin = import_origins.get_import_origin(
            con, "local", "dataset", receipt["dataset_map"][fixture["dataset_id"]]
        )
        assert dataset_origin is not None
        assert dataset_origin["source_record_id"] == fixture["dataset_id"]
        assert dataset_origin["preserved_content_sha256"] == original_datasets[0]["digest"]
        restored_source = {
            "kind": restored_sources[0]["kind"],
            "source_sha256": restored_sources[0]["digest"],
            "mapping": json.loads(restored_sources[0]["mapping"]),
            "content": restored_sources[0]["content"],
        }
        assert dataset_origin["original_record_sha256"] == import_origins.dataset_core_sha256(
            fixture["dataset_id"],
            restored_datasets[0]["digest"],
            restored_datasets[0]["created_at"],
            json.loads(restored_datasets[0]["payload"]),
            restored_source,
        )
        analysis_origin = import_origins.get_import_origin(
            con, "local", "analysis", receipt["analysis_map"][fixture["analysis_ids"][0]]
        )
        assert analysis_origin is not None
        restored_analysis = next(
            row
            for row in restored_analyses
            if row["id"] == receipt["analysis_map"][fixture["analysis_ids"][0]]
        )
        assert analysis_origin["original_record_sha256"] == import_origins.analysis_core_sha256(
            fixture["analysis_ids"][0],
            fixture["dataset_id"],
            restored_analysis["trace_id"],
            restored_analysis["kind"],
            json.loads(restored_analysis["method"]),
            json.loads(restored_analysis["result"]),
            json.loads(restored_analysis["provenance"]),
            restored_analysis["created_at"],
        )
        assert import_origins.get_import_receipt(con, "local", receipt["import_id"]) == receipt
        assert import_origins.get_import_receipt(con, "other", receipt["import_id"]) is None
    finally:
        con.close()

    # The ordinary analysis record remains readable without an R invocation,
    # and the retained result/evidence still binds to the restored dataset.
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(destination))
    from measure_service import server

    restored_record = server.analysis_for(
        "local", receipt["analysis_map"][fixture["analysis_ids"][0]]
    )
    assert restored_record["analysis_id"] == receipt["analysis_map"][fixture["analysis_ids"][0]]
    dataset = json.loads(restored_datasets[0]["payload"])
    ordinary_record = {
        key: restored_record[key]
        for key in (
            "analysis_id",
            "dataset_id",
            "trace_id",
            "kind",
            "method",
            "result",
            "provenance",
            "created_at",
        )
    }
    evidence = analysis_evidence.build_evidence(
        ordinary_record,
        dataset,
        restored_datasets[0]["digest"],
    )
    assert evidence["analysis"]["analysis_id"] == restored_record["analysis_id"]
    assert len(evidence["evidence_sha256"]) == 64


def test_restore_rejects_reference_and_malformed_archives_before_destination(tmp_path, monkeypatch):
    source, _, _, _, _ = _make_archive(tmp_path, monkeypatch, mode="references")
    destination = tmp_path / "references-destination"
    with pytest.raises(ValueError, match="self-contained"):
        batch_restore.restore_bundle(source, destination)
    assert not destination.exists()

    malformed = tmp_path / "malformed.zip"
    malformed.write_bytes(b"not a bundle")
    malformed_destination = tmp_path / "malformed-destination"
    with pytest.raises(ValueError):
        batch_restore.restore_bundle(malformed, malformed_destination)
    assert not malformed_destination.exists()

    source_link = tmp_path / "source-link.zip"
    source_link.symlink_to(source)
    link_destination = tmp_path / "link-destination"
    with pytest.raises(ValueError, match="regular"):
        batch_restore.restore_bundle(source_link, link_destination)
    assert not link_destination.exists()


@pytest.mark.parametrize("kind", ["empty-directory", "file", "symlink"])
def test_restore_never_clobbers_existing_destination(tmp_path, monkeypatch, kind):
    source, _, _, _, _ = _make_archive(tmp_path, monkeypatch)
    destination = tmp_path / "already-there"
    if kind == "empty-directory":
        destination.mkdir()
    elif kind == "file":
        before = b"caller-owned"
        destination.write_bytes(before)
    else:
        target = tmp_path / "symlink-target"
        target.mkdir()
        destination.symlink_to(target, target_is_directory=True)
        before = target

    with pytest.raises(ValueError, match="destination already exists"):
        batch_restore.restore_bundle(source, destination)
    if kind == "empty-directory":
        assert list(destination.iterdir()) == []
    elif kind == "file":
        assert destination.read_bytes() == before
    else:
        assert destination.is_symlink()
        assert destination.resolve() == before.resolve()


def test_restore_preserves_structured_only_input_without_fabricating_source(tmp_path, monkeypatch):
    source, _, _, _, _ = _make_archive(tmp_path, monkeypatch, with_source=False)
    destination = tmp_path / "structured-only"
    receipt = batch_restore.restore_bundle(source, destination)
    database = destination / "measure.sqlite3"
    assert _rows(database, "SELECT COUNT(*) AS count FROM dataset_sources")[0]["count"] == 0
    assert all(
        json.loads(row[0])["source_sha256"] is None
        for row in _rows(database, "SELECT provenance FROM analyses")
    )
    assert receipt["no_execution_performed"] is True


def test_restore_rebuilds_manifest_inputs_by_ordinal_when_items_are_reordered(
    tmp_path, monkeypatch
):
    source, _, snapshot_bytes, _, _ = _make_archive(tmp_path, monkeypatch)
    bundle = pytest.importorskip("measure_service.batch_bundle")
    from measure_service import batch_snapshot

    document = batch_snapshot.decode_snapshot(snapshot_bytes)
    original_inputs = document["batch"]["manifest"]["inputs"]
    expected = [
        (
            item["ordinal"],
            item["dataset_id"],
            item["trace_id"],
            item["role"],
            item["internal_standard_references"],
        )
        for item in original_inputs
    ]
    items_by_ordinal = {item["ordinal"]: item for item in document["items"]}
    document["items"] = [items_by_ordinal[2], items_by_ordinal[0], items_by_ordinal[1]]
    reordered_snapshot = batch_snapshot.encode_snapshot(document)
    reordered_archive = bundle.pack_bundle(reordered_snapshot)
    source.write_bytes(reordered_archive)

    destination = tmp_path / "reordered-restore"
    receipt = batch_restore.restore_bundle(source, destination)
    assert (destination / batch_restore.ARCHIVE_NAME).read_bytes() == reordered_archive

    manifest_row = _rows(
        destination / "measure.sqlite3",
        "SELECT manifest FROM analysis_batches WHERE id=?",
        (receipt["new_batch_id"],),
    )[0]
    manifest = json.loads(manifest_row["manifest"])
    actual = [
        (
            item["ordinal"],
            next(
                source_id
                for source_id, local_id in receipt["dataset_map"].items()
                if local_id == item["dataset_id"]
            ),
            item["trace_id"],
            item["role"],
            item["internal_standard_references"],
        )
        for item in manifest["inputs"]
    ]
    assert actual == expected

    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(destination))
    from measure_service import batches

    restored_batch = batches.get_batch("local", receipt["new_batch_id"])
    run, _ = batches.reserve_run(
        "local", receipt["new_batch_id"], "ordinal-order-check", restored_batch["revision"], 1
    )
    assert run["items"][0]["ordinal"] == 0


@pytest.mark.parametrize(
    "tamper",
    ["source-content", "source-mapping", "analysis-method", "analysis-trace", "analysis-created"],
)
def test_restored_origin_reads_reject_full_record_tampering(tmp_path, monkeypatch, tamper):
    source, _, _, fixture, _ = _make_archive(tmp_path, monkeypatch)
    destination = tmp_path / "tampered-origin"
    receipt = batch_restore.restore_bundle(source, destination)
    database = destination / "measure.sqlite3"
    dataset_id = receipt["dataset_map"][fixture["dataset_id"]]
    analysis_id = receipt["analysis_map"][fixture["analysis_ids"][0]]
    con = sqlite3.connect(database)
    if tamper == "source-content":
        con.execute(
            "UPDATE dataset_sources SET content=? WHERE dataset_id=?", ("tampered", dataset_id)
        )
        kind, record_id = "dataset", dataset_id
    elif tamper == "source-mapping":
        con.execute(
            "UPDATE dataset_sources SET mapping=? WHERE dataset_id=?",
            (canonical({"changed": True}), dataset_id),
        )
        kind, record_id = "dataset", dataset_id
    elif tamper == "analysis-method":
        con.execute(
            "UPDATE analyses SET method=? WHERE id=?",
            (canonical({"changed": True}), analysis_id),
        )
        kind, record_id = "analysis", analysis_id
    elif tamper == "analysis-trace":
        con.execute("UPDATE analyses SET trace_id=? WHERE id=?", ("tampered", analysis_id))
        kind, record_id = "analysis", analysis_id
    else:
        con.execute(
            "UPDATE analyses SET created_at=? WHERE id=?",
            ("2026-12-01T00:00:00+00:00", analysis_id),
        )
        kind, record_id = "analysis", analysis_id
    con.commit()
    with pytest.raises(ValueError, match="origin"):
        import_origins.get_import_origin(con, "local", kind, record_id)
    con.close()


def test_restore_rolls_back_database_but_retains_honest_incomplete_marker(tmp_path, monkeypatch):
    source, _, _, _, _ = _make_archive(tmp_path, monkeypatch)
    destination = tmp_path / "failed-restore"

    from measure_service import batches

    def fail_batch_creation(*args, **kwargs):
        raise RuntimeError("controlled batch persist failure")

    monkeypatch.setattr(batches, "_create_batch_record", fail_batch_creation)
    with pytest.raises(RuntimeError, match="controlled batch persist failure"):
        batch_restore.restore_bundle(source, destination)

    assert (destination / batch_restore.INCOMPLETE_NAME).exists()
    assert (destination / batch_restore.ARCHIVE_NAME).exists()
    assert not (destination / batch_restore.RECEIPT_NAME).exists()
    database = destination / "measure.sqlite3"
    assert _rows(database, "SELECT COUNT(*) AS count FROM datasets")[0]["count"] == 0
    assert _rows(database, "SELECT COUNT(*) AS count FROM analyses")[0]["count"] == 0
    assert _rows(database, "SELECT COUNT(*) AS count FROM analysis_batches")[0]["count"] == 0
    assert (
        _rows(
            database,
            "SELECT 1 AS present FROM sqlite_master WHERE name='analytical_imports'",
        )
        == []
    )
    before_connect = database.read_bytes()
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(destination))
    from measure_service import storage

    with pytest.raises(ValueError, match="incomplete"):
        storage.connect()
    assert database.read_bytes() == before_connect


def test_two_restore_writers_reserve_one_destination(tmp_path, monkeypatch):
    source, _, _, _, _ = _make_archive(tmp_path, monkeypatch)
    destination = tmp_path / "racing-destination"

    def run_restore():
        return batch_restore.restore_bundle(source, destination)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_restore), pool.submit(run_restore)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(("ok", future.result()))
            except Exception as exc:  # one loser is the expected outcome
                outcomes.append(("error", exc))

    assert [kind for kind, _ in outcomes].count("ok") == 1
    assert [kind for kind, _ in outcomes].count("error") == 1
    assert (destination / batch_restore.RECEIPT_NAME).exists()
    assert not (destination / batch_restore.INCOMPLETE_NAME).exists()


def test_restore_cli_reports_final_receipt(tmp_path, monkeypatch):
    source, _, _, _, _ = _make_archive(tmp_path, monkeypatch)
    destination = tmp_path / "cli-destination"
    environment = os.environ.copy()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "measure_service.batch_restore",
            str(source),
            "--destination",
            str(destination),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    receipt = json.loads(result.stdout)
    assert receipt["profile"] == import_origins.RESTORE_PROFILE
    assert receipt["no_execution_performed"] is True
    assert json.loads((destination / batch_restore.RECEIPT_NAME).read_text()) == receipt
