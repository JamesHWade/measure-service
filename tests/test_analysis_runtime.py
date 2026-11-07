"""Shared preview/batch execution and process-wide resource boundaries."""

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from measure_service import analysis_runtime as runtime
from measure_service import server


def trace():
    return {
        "trace_id": "uv",
        "sample_id": "sample",
        "injection_id": "injection",
        "channel": "UV 254 nm",
        "separation": "LC",
        "detector": "UV",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "axis": [0.0, 1.0, 3.0],
        "signal": [0.0, 2.0, 0.0],
    }


def test_preview_uses_shared_processing_and_records_original_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "identity", lambda ctx: "owner")
    calls = []

    def worker(payload, **kwargs):
        calls.append(payload)
        return {"warnings": ["backend warning"], "peak_count": 0, "peaks": []}

    monkeypatch.setattr(runtime, "run_worker", worker)
    dataset = server.register_dataset(
        "owner", server.Dataset(title="declared input", traces=[server.Trace(**trace())])
    )
    method = server.ChromatogramMethod(smoothing="gaussian")
    response = server.measurement_analyze_chromatogram(dataset["dataset_id"], "uv", method, None)
    stored = server.analysis_for("owner", response["analysis_id"])
    assert calls == [
        {
            "operation": "chromatogram",
            "trace": server.Trace(**trace()).model_dump(mode="json"),
            "method": method.model_dump(mode="json"),
        }
    ]
    assert response["warnings"][0] == "backend warning"
    assert "spacing is irregular" in response["warnings"][1]
    assert stored["result"]["warnings"] == response["warnings"]
    assert stored["method"] == calls[0]["method"]
    assert stored["provenance"]["input_sha256"] == dataset["input_sha256"]


def test_two_permits_cover_preview_and_other_worker_operations(monkeypatch):
    entered = threading.Barrier(3)
    release = threading.Event()
    operations = []

    def execute(payload, **kwargs):
        operations.append(payload["operation"])
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        return {"ok": True}

    monkeypatch.setattr(runtime, "_execute_worker", execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(server.run_worker, {"operation": "chromatogram"})
        second = pool.submit(server.run_worker, {"operation": "sec"})
        try:
            entered.wait(timeout=5)
            with pytest.raises(ValueError, match="capacity is busy"):
                server.run_worker({"operation": "peak_quantitation"})
            assert set(operations) == {"chromatogram", "sec"}
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5) == {"ok": True}
    available = runtime.acquire_worker_slots(2)
    assert len(available) == 2
    for slot in available:
        slot.release()


def test_reserved_batch_permits_share_capacity_and_release_after_failure(monkeypatch):
    slots = runtime.acquire_worker_slots(2)
    assert len(slots) == 2
    try:
        with pytest.raises(ValueError, match="capacity is busy"):
            server.run_worker({"operation": "capabilities"})
        monkeypatch.setattr(
            runtime,
            "_execute_worker",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("controlled failure")),
        )
        with pytest.raises(ValueError, match="controlled failure"):
            slots[0].run({"operation": "chromatogram"})
    finally:
        for slot in slots:
            slot.release()
            slot.release()
    with pytest.raises(ValueError, match="released"):
        slots[0].run({"operation": "chromatogram"})
    available = runtime.acquire_worker_slots(2)
    assert len(available) == 2
    for slot in available:
        slot.release()


def test_failure_does_not_consume_a_worker_slot(tmp_path, monkeypatch):
    worker = tmp_path / "worker.R"
    worker.write_text("fixed source")

    def cannot_start(*args, **kwargs):
        raise subprocess.TimeoutExpired("R", 90)

    monkeypatch.setattr(runtime.subprocess, "run", cannot_start)
    for _ in range(3):
        with pytest.raises(ValueError, match="could not start or timed out"):
            runtime.run_worker({"operation": "chromatogram"}, worker_path=worker)


def test_worker_replacement_during_execution_rejects_result(tmp_path, monkeypatch):
    worker = tmp_path / "worker.R"
    worker.write_text("before")

    def changed(*args, **kwargs):
        worker.write_text("after")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "result": {"peak_count": 0}}),
        )

    monkeypatch.setattr(runtime.subprocess, "run", changed)
    with pytest.raises(ValueError, match="source changed"):
        runtime.run_worker({"operation": "chromatogram"}, worker_path=worker)


def test_failed_attempt_context_does_not_infer_an_R_build():
    context = runtime.execution_context()
    assert len(context["worker_sha256"]) == 64
    assert context["processing_profile"] == "measure-chromatogram/1"
    assert context["r_runtime_evidence"] == "unknown_until_success"
    assert "packages" not in context
    assert "r_version" not in context
