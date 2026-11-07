"""Durable analytical batch state-machine contract tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from measure_service import analysis_runtime, batches
from measure_service.analysis_models import ChromatogramMethod, Dataset, Trace
from measure_service.batch_models import BatchInput, BatchSpec
from measure_service.server import register_dataset
from measure_service.storage import canonical, connect


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))


def _trace(trace_id: str, *, injection: str | None = None, sample_type: str = "unknown") -> Trace:
    return Trace(
        trace_id=trace_id,
        sample_id=f"sample-{trace_id}",
        injection_id=injection or f"injection-{trace_id}",
        channel="uv-214",
        separation="LC",
        detector="DAD",
        axis_unit="min",
        signal_unit="mAU",
        sample_type=sample_type,
        axis=[0.0, 1.0, 2.0, 3.0, 4.0],
        signal=[0.0, 1.0, 3.0, 1.0, 0.0],
    )


def _dataset(owner: str = "owner", *, with_source: bool = False, trace_id: str = "trace-1") -> dict:
    dataset = Dataset(title="Synthetic chromatogram", traces=[_trace(trace_id)])
    source = None
    if with_source:
        text = "time,signal\n0,0\n1,1\n2,3\n3,1\n4,0\n"
        source = {
            "kind": "delimited_text",
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "content": text,
            "mapping": {
                "axis_column": "time",
                "signal_columns": [{"column": "signal", "trace_id": trace_id}],
            },
        }
    return register_dataset(owner, dataset, source)


def _spec(dataset_id: str, trace_id: str = "trace-1", *, role: str = "unknown") -> BatchSpec:
    return BatchSpec(
        title="Synthetic batch",
        method=ChromatogramMethod(),
        inputs=[
            BatchInput(
                dataset_id=dataset_id,
                trace_id=trace_id,
                role=role,
                internal_standard_references=["caller-label"] if role == "standard" else [],
            )
        ],
    )


def _claimed(owner: str = "owner", *, source: bool = False):
    dataset = _dataset(owner, with_source=source)
    batch = batches.create_batch(owner, _spec(dataset["dataset_id"]), "create")
    run, created = batches.reserve_run(
        owner,
        batch["batch_id"],
        "run",
        batch["revision"],
        execution_context={"service_sha256": "a" * 64, "worker_sha256": "b" * 64},
    )
    assert created
    return dataset, batch, run


def _record(dataset: dict, *, method: dict | None = None, analysis_id: str = "analysis-1") -> dict:
    with connect() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT payload, digest FROM datasets WHERE id=?", (dataset["dataset_id"],)
        ).fetchone()
        source_row = con.execute(
            "SELECT digest FROM dataset_sources WHERE dataset_id=?", (dataset["dataset_id"],)
        ).fetchone()
    data = json.loads(row["payload"])
    trace = data["traces"][0]
    method = method or ChromatogramMethod().model_dump(mode="json")
    result = {
        "kind": "chromatogram",
        "peak_count": 1,
        "peaks": [
            {
                "peak_id": 1,
                "location": 2.0,
                "height": 3.0,
                "left_base": 1.0,
                "right_base": 3.0,
                "prominence": 3.0,
                "area": 2.0,
            }
        ],
        "processed_trace": {"axis": trace["axis"], "signal": [0.0, 1.0, 3.0, 1.0, 0.0]},
        "warnings": [],
        "notes": [],
        "packages": {"measure": "synthetic"},
    }
    return analysis_runtime.make_analysis_record(
        dataset["dataset_id"],
        trace["trace_id"],
        "chromatogram",
        method,
        result,
        row["digest"],
        None if source_row is None else source_row["digest"],
    ) | {"analysis_id": analysis_id}


def test_create_freezes_roles_metadata_sources_and_owner_scope():
    dataset = _dataset(with_source=True)
    spec = _spec(dataset["dataset_id"], role="standard")
    batch = batches.create_batch("owner", spec, "create")
    assert batch["profile"] == "measure-chromatogram-batch/1"
    assert batch["manifest"]["inputs"][0]["role"] == "standard"
    assert batch["manifest"]["inputs"][0]["trace"]["sample_type"] == "unknown"
    assert batch["manifest"]["inputs"][0]["source"]["mapping"]["axis_column"] == "time"
    assert batches.create_batch("owner", spec, "create") == batch
    with pytest.raises(ValueError, match="not found or is not accessible"):
        batches.get_batch("other-owner", batch["batch_id"])
    item = batches.read_items("owner", batch["batch_id"])["items"][0]
    assert "axis" not in item and "signal" not in item["trace"]


def test_manifest_and_item_binding_tampering_is_detected():
    dataset = _dataset()
    batch = batches.create_batch("owner", _spec(dataset["dataset_id"]), "create")
    with connect() as con:
        row = con.execute(
            "SELECT manifest FROM analysis_batches WHERE id=?", (batch["batch_id"],)
        ).fetchone()
        manifest = json.loads(row[0])
        manifest["title"] = "tampered"
        con.execute(
            "UPDATE analysis_batches SET manifest=? WHERE id=?",
            (canonical(manifest), batch["batch_id"]),
        )
    with pytest.raises(ValueError, match="unsupported profile|digest"):
        batches.get_batch("owner", batch["batch_id"])

    batch = batches.create_batch("owner", _spec(dataset["dataset_id"]), "create-2")
    with connect() as con:
        row = con.execute(
            "SELECT payload FROM analysis_batch_items WHERE batch_id=?", (batch["batch_id"],)
        ).fetchone()
        payload = json.loads(row[0])
        payload["role"] = "qc"
        con.execute(
            "UPDATE analysis_batch_items SET payload=? WHERE batch_id=?",
            (canonical(payload), batch["batch_id"]),
        )
    with pytest.raises(ValueError, match="immutable manifest"):
        batches.read_items("owner", batch["batch_id"])


def test_duplicate_grain_and_declared_bounds_are_rejected():
    first = _dataset()
    with pytest.raises(ValueError, match="role"):
        BatchInput(dataset_id=first["dataset_id"], trace_id="trace-1")
    with pytest.raises(ValueError, match="only once"):
        BatchSpec(
            title="duplicate",
            method=ChromatogramMethod(),
            inputs=[
                BatchInput(dataset_id=first["dataset_id"], trace_id="trace-1", role="unknown"),
                BatchInput(dataset_id=first["dataset_id"], trace_id="trace-1", role="qc"),
            ],
        )
    with pytest.raises(ValueError, match="at most 32"):
        BatchSpec(
            title="too many",
            method=ChromatogramMethod(),
            inputs=[
                BatchInput(dataset_id=first["dataset_id"], trace_id="trace-1", role="unknown")
                for _ in range(33)
            ],
        )


def test_reserve_is_idempotent_cas_bounded_and_public_reads_hide_tokens():
    dataset = _dataset()
    batch = batches.create_batch("owner", _spec(dataset["dataset_id"]), "create")
    first, created = batches.reserve_run("owner", batch["batch_id"], "run", batch["revision"])
    assert created and first["items"][0]["execution_token"]
    replay, created = batches.reserve_run("owner", batch["batch_id"], "run", batch["revision"])
    assert not created and replay["run_id"] == first["run_id"]
    public = batches.get_run("owner", first["run_id"])
    assert "execution_token" not in public["items"][0]
    assert (
        batches.lookup_run_request("owner", batch["batch_id"], "run", batch["revision"])["run_id"]
        == first["run_id"]
    )
    with pytest.raises(ValueError, match="revision conflict"):
        batches.reserve_run("owner", batch["batch_id"], "other", batch["revision"])
    with pytest.raises(ValueError, match="different registry request"):
        batches.reserve_run("owner", batch["batch_id"], "run", batch["revision"], max_items=1)


def test_failure_recovery_retry_and_stale_claim_fence():
    dataset, batch, run = _claimed()
    token = run["items"][0]["execution_token"]
    with pytest.raises(ValueError, match="expired"):
        with connect() as con:
            con.execute(
                "UPDATE analysis_batch_attempts SET lease_expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", run["items"][0]["attempt_id"]),
            )
        batches.fail_item("owner", batch["batch_id"], run["run_id"], 0, token, "late")
    recovered = batches.recover_batch("owner", batch["batch_id"], "recover", batch["revision"] + 1)
    assert recovered["counts"]["interrupted"] == 1
    with pytest.raises(ValueError, match="stale"):
        batches.complete_item("owner", batch["batch_id"], run["run_id"], 0, token, _record(dataset))
    retried = batches.retry_items("owner", batch["batch_id"], "retry", recovered["revision"], [0])
    assert retried["counts"]["pending"] == 1
    failed_run, _ = batches.reserve_run("owner", batch["batch_id"], "run-2", retried["revision"])
    failed_item = batches.fail_item(
        "owner",
        batch["batch_id"],
        failed_run["run_id"],
        0,
        failed_run["items"][0]["execution_token"],
        {"kind": "synthetic"},
    )
    assert failed_item["state"] == "failed"
    assert batches.read_attempts("owner", batch["batch_id"], 0)["total"] == 2


def test_completion_is_atomic_source_bound_and_success_is_not_duplicated():
    dataset, batch, run = _claimed(source=True)
    claim = run["items"][0]
    record = _record(dataset)
    with pytest.raises(ValueError, match="source"):
        with connect() as con:
            con.execute(
                "UPDATE dataset_sources SET content=? WHERE dataset_id=?",
                ("corrupt", dataset["dataset_id"]),
            )
        batches.complete_item(
            "owner", batch["batch_id"], run["run_id"], 0, claim["execution_token"], record
        )
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 0
        assert con.execute("SELECT state FROM analysis_batch_items").fetchone()[0] == "running"
        con.execute(
            "UPDATE dataset_sources SET content=? WHERE dataset_id=?",
            ("time,signal\n0,0\n1,1\n2,3\n3,1\n4,0\n", dataset["dataset_id"]),
        )
    # Restore the exact source digest after the corruption check.
    completed = batches.complete_item(
        "owner", batch["batch_id"], run["run_id"], 0, claim["execution_token"], record
    )
    assert completed["state"] == "completed"
    again = batches.complete_item(
        "owner", batch["batch_id"], run["run_id"], 0, claim["execution_token"], record
    )
    assert again["analysis_id"] == completed["analysis_id"]
    item_page = batches.read_items("owner", batch["batch_id"])
    assert item_page["items"][0]["result"]["analysis_id"] == completed["analysis_id"]
    attempt_page = batches.read_attempts("owner", batch["batch_id"], 0)
    assert attempt_page["attempts"][0]["result"] is None
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 1
