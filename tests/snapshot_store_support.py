"""Construct durable state fixtures; no R calculation or scientific oracle."""

from measure_service import analysis_runtime, batches, server
from measure_service.batch_models import BatchSpec


def make_snapshot_store(owner="owner", *, with_source=True):
    names = ["reference", "blank", "qc", "unselected"]
    metadata = [
        server.TraceMetadata(
            trace_id=name,
            sample_id=f"sample-{name}",
            injection_id=f"injection-{name}",
            channel=f"UV-{index}",
            separation="LC",
            detector="UV",
            axis_unit="min",
            signal_unit="mAU",
            sample_type=name if name != "unselected" else "unknown",
        )
        for index, name in enumerate(names)
    ]
    text = "time,reference,blank,qc,unselected\r\n0,0,0,0,0\r\n1,1,0,2,3\r\n2,2,0,3,4\r\n3,1,0,2,3\r\n4,0,0,0,0\r\n"
    traces, source = server.parse_csv(
        text,
        "time",
        [server.CSVChannel(column=name, metadata=meta) for name, meta in zip(names, metadata)],
        ",",
    )
    dataset = server.register_dataset(
        owner,
        server.Dataset(title="Snapshot state fixture", traces=traces),
        source if with_source else None,
    )
    spec = BatchSpec(
        title="Immutable history fixture",
        method={},
        inputs=[
            {
                "dataset_id": dataset["dataset_id"],
                "trace_id": name,
                "role": name,
                "internal_standard_references": ["caller-label-only"] if name == "qc" else [],
            }
            for name in names[:3]
        ],
    )
    batch = batches.create_batch(owner, spec, "fixture-create")
    identifier = batch["batch_id"]
    analyses = []

    def reserve(key):
        current = batches.get_batch(owner, identifier)
        return batches.reserve_run(owner, identifier, key, current["revision"], 1)[0]

    def complete(run, ordinal):
        trace = traces[ordinal]
        record = analysis_runtime.make_analysis_record(
            dataset["dataset_id"],
            trace.trace_id,
            "chromatogram",
            spec.method.model_dump(mode="json"),
            {
                "kind": "chromatogram",
                "peak_count": 0,
                "peaks": [],
                "processed_trace": {"axis": trace.axis, "signal": trace.signal},
                "warnings": [],
                "notes": ["Synthetic state fixture; no R calculation."],
                "packages": {"fixture": "state-test-only"},
                # Exercise the worker's nested shape without claiming observed R evidence.
                "installed_r_code_md5": {"fixture_not_executed": "0" * 32, "unavailable": None},
            },
            dataset["input_sha256"],
            dataset["source_sha256"],
        )
        batches.complete_item(
            owner, identifier, run["run_id"], ordinal, run["items"][0]["execution_token"], record
        )
        analyses.append(record["analysis_id"])

    complete(reserve("fixture-run-first"), 0)
    failed = reserve("fixture-run-fail")
    batches.fail_item(
        owner,
        identifier,
        failed["run_id"],
        1,
        failed["items"][0]["execution_token"],
        {"kind": "fixture_failure", "message": "Controlled state fixture; R was not invoked."},
    )
    current = batches.get_batch(owner, identifier)
    cancelled = batches.cancel_batch(owner, identifier, "fixture-cancel", current["revision"])
    batches.retry_items(owner, identifier, "fixture-retry", cancelled["revision"], [1, 2])
    complete(reserve("fixture-run-retry"), 1)
    return {
        "batch_id": identifier,
        "dataset_id": dataset["dataset_id"],
        "analysis_ids": analyses,
        "failed_run_id": failed["run_id"],
        "batch": batches.get_batch(owner, identifier),
    }
