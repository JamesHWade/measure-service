"""Portable records retain inputs already accepted by the actual writers."""

import pytest

from measure_service import analysis_runtime, batch_capture, batches, server
from measure_service.batch_models import BatchSpec
from measure_service.batch_snapshot import decode_snapshot


@pytest.mark.parametrize("layout", ["wide-unicode", "wide-whitespace", "long-blank", "structured"])
def test_existing_importer_labels_and_worker_metadata_remain_portable(
    tmp_path, monkeypatch, layout
):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    unicode_labels = layout == "wide-unicode"
    metadata = server.TraceMetadata(
        trace_id="🧪" * 100 if unicode_labels else "trace",
        sample_id="🧪" * 100 if unicode_labels else "sample",
        injection_id="injection",
        channel="UV",
        separation="LC",
        detector="UV",
        axis_unit="min",
        signal_unit="mAU",
    )
    title = "🧪" * 200 if unicode_labels else " "
    if layout in {"wide-unicode", "wide-whitespace"}:
        axis_column = "axis-" + "x" * 201 if unicode_labels else ""
        column = "🧪" * 200 if unicode_labels else " "
        text = f"{axis_column},{column}\r\n0,0\r\n1,1\r\n2,0\r\n"
        traces, source = server.parse_csv(
            text, axis_column, [server.CSVChannel(column=column, metadata=metadata)], ","
        )
    elif layout == "long-blank":
        group_column, signal_column = "g" * 201, "s" * 201
        text = f",{signal_column},{group_column}\r\n0,0,\r\n1,1,\r\n2,0,\r\n"
        traces, source = server.parse_long_csv(
            text,
            "",
            signal_column,
            [group_column],
            [server.CSVGroup(values=[""], metadata=metadata)],
            ",",
        )
    else:
        traces = [server.Trace(**metadata.model_dump(), axis=[0, 1, 2], signal=[0, 1, 0])]
        source = None
    dataset = server.register_dataset("owner", server.Dataset(title=title, traces=traces), source)
    spec = BatchSpec(
        title="🧪" * 200,
        method={},
        inputs=[{"dataset_id": dataset["dataset_id"], "trace_id": metadata.trace_id, "role": "qc"}],
    )
    batch = batches.create_batch("owner", spec, "create")
    run, _ = batches.reserve_run("owner", batch["batch_id"], "run", batch["revision"], 1)
    record = analysis_runtime.make_analysis_record(
        dataset["dataset_id"],
        metadata.trace_id,
        "chromatogram",
        spec.method.model_dump(mode="json"),
        {
            "kind": "chromatogram",
            "peak_count": 0,
            "peaks": [],
            "processed_trace": {"axis": traces[0].axis, "signal": traces[0].signal},
            "warnings": [],
            "notes": "Synthetic schema-only fixture. " + "x" * 3_000,
            "packages": {"fixture": "not-executed"},
            "installed_r_code_md5": {"fixture_not_executed": "0" * 32, "unavailable": None},
        },
        dataset["input_sha256"],
        dataset["source_sha256"],
    )
    batches.complete_item(
        "owner", batch["batch_id"], run["run_id"], 0, run["items"][0]["execution_token"], record
    )
    current = batches.get_batch("owner", batch["batch_id"])
    for mode in ("references", "self_contained"):
        document = decode_snapshot(
            batch_capture.capture_snapshot("owner", batch["batch_id"], current["revision"], mode)
        )
        assert document["batch"]["title"] == spec.title
        assert document["datasets"][0]["title"] == title
        assert document["items"][0]["trace_id"] == metadata.trace_id
        assert document["analyses"][0]["provenance"] == record["provenance"]
        assert document["items"][0]["result"]["notes"] == record["result"]["notes"]
        if mode == "self_contained":
            assert document["datasets"][0]["source"] == source
