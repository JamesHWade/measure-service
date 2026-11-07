"""Dataset-scoped grain identity matches the durable batch writer."""

import pytest

from measure_service import batch_capture, batches, server
from measure_service.batch_models import BatchSpec
from measure_service.batch_snapshot import decode_snapshot


def _dataset(title: str) -> dict:
    trace = server.Trace(
        trace_id="same-trace",
        sample_id="same-sample",
        injection_id="same-injection",
        channel="same-channel",
        separation="LC",
        detector="UV",
        axis_unit="min",
        signal_unit="mAU",
        axis=[0.0, 1.0, 2.0],
        signal=[0.0, 1.0, 0.0],
    )
    return server.register_dataset("owner", server.Dataset(title=title, traces=[trace]), None)


def test_distinct_datasets_may_share_sample_injection_channel_grain(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    first = _dataset("first")
    second = _dataset("second")
    spec = BatchSpec(
        title="dataset-scoped grain",
        method={},
        inputs=[
            {"dataset_id": first["dataset_id"], "trace_id": "same-trace", "role": "unknown"},
            {"dataset_id": second["dataset_id"], "trace_id": "same-trace", "role": "unknown"},
        ],
    )
    batch = batches.create_batch("owner", spec, "create")

    for mode in ("references", "self_contained"):
        document = decode_snapshot(
            batch_capture.capture_snapshot("owner", batch["batch_id"], batch["revision"], mode)
        )
        assert len(document["items"]) == 2
        assert {item["dataset_id"] for item in document["items"]} == {
            first["dataset_id"],
            second["dataset_id"],
        }


def test_same_dataset_duplicate_selection_remains_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    dataset = _dataset("one dataset")
    with pytest.raises(ValueError, match="only once"):
        BatchSpec(
            title="duplicate selection",
            method={},
            inputs=[
                {"dataset_id": dataset["dataset_id"], "trace_id": "same-trace", "role": "unknown"},
                {"dataset_id": dataset["dataset_id"], "trace_id": "same-trace", "role": "qc"},
            ],
        )
