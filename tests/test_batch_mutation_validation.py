"""Corrupt declarations reject inside mutations; readers need no writer lock."""

import hashlib
import sqlite3

import pytest

from measure_service import batches, storage
from measure_service.analysis_models import Dataset, Trace
from measure_service.batch_models import BatchSpec
from measure_service.server import register_dataset


@pytest.fixture
def declaration(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    trace = Trace(
        trace_id="uv",
        sample_id="sample",
        injection_id="injection",
        channel="UV",
        separation="LC",
        detector="UV",
        axis_unit="min",
        signal_unit="mAU",
        axis=[0, 1, 2],
        signal=[0, 1, 0],
    )
    dataset = register_dataset("owner", Dataset(title="synthetic", traces=[trace]))
    spec = BatchSpec(
        title="batch",
        method={},
        inputs=[
            {
                "dataset_id": dataset["dataset_id"],
                "trace_id": "uv",
                "role": "unknown",
            }
        ],
    )
    return batches.create_batch("owner", spec, "create")


@pytest.mark.parametrize("operation", ["reserve", "cancel", "recover", "retry"])
@pytest.mark.parametrize("damage", ["manifest_digest", "item_binding"])
def test_invalid_declarations_reject_without_claim_state_event_or_receipt(
    declaration, operation, damage
):
    identifier = declaration["batch_id"]
    if operation == "retry":
        declaration = batches.cancel_batch(
            "owner", identifier, "prepare-retry", declaration["revision"]
        )
    with storage.connect() as con:
        if damage == "manifest_digest":
            con.execute(
                "UPDATE analysis_batches SET manifest_digest=? WHERE id=?", ("0" * 64, identifier)
            )
        else:
            con.execute(
                "UPDATE analysis_batch_items SET role='blank' WHERE batch_id=?", (identifier,)
            )
    before = hashlib.sha256(storage.db_path().read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="declaration|immutable manifest"):
        if operation == "reserve":
            batches.reserve_run("owner", identifier, "rejected", declaration["revision"])
        elif operation == "retry":
            batches.retry_items("owner", identifier, "rejected", declaration["revision"], [0])
        else:
            getattr(batches, operation + "_batch")(
                "owner", identifier, "rejected", declaration["revision"]
            )
    assert hashlib.sha256(storage.db_path().read_bytes()).hexdigest() == before
    with sqlite3.connect(storage.db_path()) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM registry_receipts WHERE request_key='rejected'"
        ).fetchone() == (0,)
        assert con.execute("SELECT COUNT(*) FROM analysis_batch_runs").fetchone() == (0,)
        assert con.execute("SELECT COUNT(*) FROM analysis_batch_attempts").fetchone() == (0,)
        assert con.execute("SELECT COUNT(*) FROM analysis_batch_control_events").fetchone() == (
            (1 if operation == "retry" else 0),
        )


def test_existing_schema_reads_committed_snapshot_while_another_writer_is_active(
    declaration, monkeypatch
):
    identifier = declaration["batch_id"]
    original_connect = sqlite3.connect

    def short_timeout(*args, **kwargs):
        kwargs["timeout"] = 0.1
        return original_connect(*args, **kwargs)

    with original_connect(storage.db_path()) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE analysis_batches SET revision=revision+100 WHERE id=?", (identifier,)
        )
        monkeypatch.setattr(storage.sqlite3, "connect", short_timeout)
        assert batches.get_batch("owner", identifier)["revision"] == declaration["revision"]
        assert batches.read_items("owner", identifier)["items"][0]["state"] == "pending"
        assert (
            batches.lookup_run_request("owner", identifier, "not-reserved", declaration["revision"])
            is None
        )
        writer.rollback()
