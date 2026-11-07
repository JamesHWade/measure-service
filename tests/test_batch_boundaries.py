"""Allowed metadata combinations remain readable without numerical recomputation."""

import pytest

from measure_service import analysis_runtime, batches, server
from measure_service.batch_models import BatchSpec
from measure_service.storage import canonical, connect


def _record(dataset, trace, method, notes=""):
    return analysis_runtime.make_analysis_record(
        dataset["dataset_id"],
        trace.trace_id,
        "chromatogram",
        method,
        {
            "kind": "chromatogram",
            "peak_count": 0,
            "peaks": [],
            "warnings": [],
            "notes": [notes],
            "processed_trace": {"axis": trace.axis, "signal": trace.signal},
        },
        dataset["input_sha256"],
        None,
    )


@pytest.fixture(autouse=True)
def own_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))


def _trace(identifier="uv", **extra):
    return server.Trace(
        trace_id=identifier,
        sample_id=identifier,
        injection_id=identifier,
        channel="UV",
        separation="LC",
        detector="UV",
        axis_unit="min",
        signal_unit="mAU",
        axis=[0, 1, 2],
        signal=[0, 1, 0],
        **extra,
    )


def test_unreadable_runtime_metadata_is_rejected_before_success_is_retained():
    trace = _trace()
    dataset = server.register_dataset("owner", server.Dataset(title="constructed", traces=[trace]))
    batch = batches.create_batch(
        "owner",
        BatchSpec(
            title="runtime boundary",
            method={},
            inputs=[
                {
                    "dataset_id": dataset["dataset_id"],
                    "trace_id": trace.trace_id,
                    "role": "unknown",
                }
            ],
        ),
        "create",
    )
    run, _ = batches.reserve_run("owner", batch["batch_id"], "run", batch["revision"])
    record = _record(dataset, trace, batch["method"])
    record["provenance"]["backend_build_description"] = "x" * 4_000
    assert (
        len(canonical(analysis_runtime.compact_analysis(record)).encode())
        < batches.MAX_COMPACT_RESULT_BYTES
    )
    with pytest.raises(ValueError, match="attempt runtime exceeds"):
        batches.complete_item(
            "owner", batch["batch_id"], run["run_id"], 0, run["items"][0]["execution_token"], record
        )
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 0
    assert batches.get_run("owner", run["run_id"])["status"] == "running"
    assert batches.read_attempts("owner", batch["batch_id"], 0)["count"] == 1


def test_default_item_page_shrinks_to_byte_limit_after_large_valid_results_complete():
    label = "🧪"
    traces = [
        server.Trace(
            trace_id=label * 98 + str(index),
            sample_id=label * 98 + str(index),
            injection_id=label * 98 + str(index),
            channel=label * 100,
            separation="LC",
            detector=label * 100,
            axis_unit=label * 40,
            signal_unit=label * 40,
            axis=[0, 1, 2],
            signal=[0, 1, 0],
        )
        for index in range(20)
    ]
    dataset = server.register_dataset("owner", server.Dataset(title=label * 200, traces=traces))
    batch = batches.create_batch(
        "owner",
        BatchSpec(
            title="metadata boundary",
            method={},
            inputs=[
                {
                    "dataset_id": dataset["dataset_id"],
                    "trace_id": trace.trace_id,
                    "role": "unknown",
                    "internal_standard_references": [label * 49 + f"{n:04}" for n in range(10)],
                }
                for trace in traces
            ],
        ),
        "create",
    )
    assert batches.read_items("owner", batch["batch_id"])["count"] == 20
    for start in range(0, 20, 2):
        run, _ = batches.reserve_run("owner", batch["batch_id"], f"run-{start}", batch["revision"])
        for claim in run["items"]:
            record = _record(dataset, traces[claim["ordinal"]], batch["method"], "n" * 5_800)
            assert (
                len(canonical(analysis_runtime.compact_analysis(record)).encode())
                <= batches.MAX_COMPACT_RESULT_BYTES
            )
            batches.complete_item(
                "owner",
                batch["batch_id"],
                run["run_id"],
                claim["ordinal"],
                claim["execution_token"],
                record,
            )
        batch = batches.get_batch("owner", batch["batch_id"])
    first = batches.read_items("owner", batch["batch_id"])
    assert 0 < first["count"] < 20
    assert first["next_offset"] == first["count"]
    assert len(canonical(first).encode()) <= 256 * 1024
    second = batches.read_items("owner", batch["batch_id"], first["next_offset"])
    assert second["next_offset"] is None
    items = first["items"] + second["items"]
    assert [item["ordinal"] for item in items] == list(range(20))
    assert len({item["analysis_id"] for item in items}) == 20
