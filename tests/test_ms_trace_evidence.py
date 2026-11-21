"""Black-box contracts for retained worker-derived MS XIC evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from measure_service import acquisition_store, analysis_runtime, ms_trace_evidence, storage
from measure_service.acquisition_models import CSV_MEMBER_NAMES


def _files() -> dict[str, str]:
    files = {name: f"{name}\n" for name in CSV_MEMBER_NAMES}
    files["manifest.dcf"] = "Format: measure-acquisition\r\nVersion: 1\r\n"
    return files


def _inspection_worker(payload: dict) -> dict:
    assert payload["operation"] == "inspect_acquisition"
    return {
        "profile": "measure-acquisition-inspection/1",
        "summary": {"sources": [{"source_id": "source-a", "source_sha256": "a" * 64}]},
    }


class _WorkerResult(dict):
    worker_sha256 = "b" * 64


def _trace_worker_result(*, mixed_status: bool = False) -> _WorkerResult:
    statuses = ["calculated", "empty_window"]
    values = [12.5, 0.0]
    selected = [2, 0]
    indices = [[1, 3], []]
    if mixed_status:
        statuses.extend(["missing_scan", "missing_intensity", "saturated"])
        values.extend([None, None, None])
        selected.extend([0, 2, 1])
        indices.extend([[], [2, 4], [1]])
    points = [
        {
            "scan_id": f"scan-{ordinal + 1}",
            "scan_index": ordinal + 1,
            "source_scan_id": f"raw-scan-{ordinal + 1}",
            "retention_time": float(ordinal) + 0.25,
            "value": value,
            "status": status,
            "selected_points": count,
            "saturation_unknown": status in {"missing_intensity", "saturated"},
        }
        for ordinal, (status, value, count) in enumerate(
            zip(statuses, values, selected, strict=True)
        )
    ]
    source_rows = []
    for point, contributors in zip(points, indices, strict=True):
        for point_index in contributors:
            source_rows.append(
                {
                    "scan_id": point["scan_id"],
                    "mz": 150.0,
                    "intensity": None if point["status"] == "missing_intensity" else 6.25,
                    "saturated": point["status"] == "saturated",
                    "point_index": point_index,
                    "source_row": len(source_rows) + 10,
                }
            )
    return _WorkerResult(
        points=points,
        source_rows=source_rows,
        processing={
            "operation": "xic",
            "aggregation": "centroid_sum",
            "missing_intensity": "propagate",
            "saturation": "propagate",
            "source_sha256": "d" * 64,
        },
        warnings=[],
        packages={"measure": "0.0.0"},
        installed_r_code_md5={"measure": "c" * 32},
        r_version="4.4.0",
    )


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    return tmp_path


def _registered_acquisition() -> dict:
    return acquisition_store.register_acquisition("owner-a", _files(), worker=_inspection_worker)


def test_owner_scope_replay_and_restart_never_reinvoke_worker(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquisition = _registered_acquisition()
    calls: list[dict] = []

    def worker(payload: dict) -> _WorkerResult:
        calls.append(payload)
        return _trace_worker_result()

    monkeypatch.setattr(analysis_runtime, "run_worker", worker)
    manifest = ms_trace_evidence.extract_ms_trace(
        "owner-a", acquisition["acquisition_id"], "ms-centroid", "positive", 1, 100.0, 200.0
    )
    assert manifest["profile"] == "measure-ms-trace/1"
    assert manifest["acquisition_id"] == acquisition["acquisition_id"]
    assert manifest["point_count"] == 2
    assert manifest["source_row_count"] == 2
    assert manifest["evidence_sha256"]
    assert manifest["processing"]["source_sha256"] == "d" * 64
    assert len(calls) == 1
    assert calls[0]["operation"] == "extract_ms_trace"
    assert calls[0]["files"] == _files()
    assert calls[0]["stream_id"] == "ms-centroid"
    assert calls[0]["polarity"] == "positive"
    assert calls[0]["ms_level"] == 1
    assert calls[0]["mz_min"] == 100.0
    assert calls[0]["mz_max"] == 200.0

    # A fresh storage connection represents a process restart. The durable
    # manifest and both row views remain readable without an R dispatch.
    storage.connect().close()
    assert ms_trace_evidence.get_ms_trace("owner-a", manifest["trace_id"]) == manifest
    points = ms_trace_evidence.read_ms_trace(
        "owner-a", manifest["trace_id"], "points", manifest["evidence_sha256"], limit=1
    )
    sources = ms_trace_evidence.read_ms_trace(
        "owner-a", manifest["trace_id"], "source_rows", manifest["evidence_sha256"], limit=1
    )
    assert points["count"] == 1
    assert points["next_offset"] == 1
    assert sources["count"] == 1
    assert sources["next_offset"] == 1

    def unavailable(_payload: dict) -> dict:
        raise AssertionError("same-owner replay must not invoke the worker")

    monkeypatch.setattr(analysis_runtime, "run_worker", unavailable)
    assert (
        ms_trace_evidence.extract_ms_trace(
            "owner-a",
            acquisition["acquisition_id"],
            "ms-centroid",
            "positive",
            1,
            100.0,
            200.0,
        )
        == manifest
    )
    with pytest.raises(ValueError, match="not found or is not accessible"):
        ms_trace_evidence.get_ms_trace("owner-b", manifest["trace_id"])
    with pytest.raises(ValueError, match="not found or is not accessible"):
        ms_trace_evidence.read_ms_trace(
            "owner-b", manifest["trace_id"], "points", manifest["evidence_sha256"]
        )


def test_mixed_statuses_and_independent_pages_preserve_worker_ordinals(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquisition = _registered_acquisition()
    result = _trace_worker_result(mixed_status=True)
    monkeypatch.setattr(analysis_runtime, "run_worker", lambda _payload: result)
    manifest = ms_trace_evidence.extract_ms_trace(
        "owner-a", acquisition["acquisition_id"], "ms-centroid", "positive", 1, 100.0, 200.0
    )

    point_pages = []
    offset = 0
    while offset is not None:
        page = ms_trace_evidence.read_ms_trace(
            "owner-a", manifest["trace_id"], "points", manifest["evidence_sha256"], offset, 2
        )
        assert page["offset"] == offset
        assert page["count"] == len(page["data"])
        point_pages.extend(page["data"])
        offset = page["next_offset"]
    assert [row["scan_index"] for row in point_pages] == list(range(1, manifest["point_count"] + 1))
    assert [row["status"] for row in point_pages] == [
        "calculated",
        "empty_window",
        "missing_scan",
        "missing_intensity",
        "saturated",
    ]
    assert [row["value"] for row in point_pages] == [12.5, 0.0, None, None, None]
    assert point_pages[3]["saturation_unknown"] is True
    assert point_pages[4]["saturation_unknown"] is True

    # The source-row cursor is independent from the points cursor. Its first
    # page starts at zero even after all point pages were consumed.
    source_pages = []
    offset = 0
    while offset is not None:
        page = ms_trace_evidence.read_ms_trace(
            "owner-a",
            manifest["trace_id"],
            "source_rows",
            manifest["evidence_sha256"],
            offset,
            1,
        )
        assert page["offset"] == offset
        assert page["count"] == len(page["data"])
        source_pages.extend(page["data"])
        offset = page["next_offset"]
    assert [row["point_index"] for row in source_pages] == [1, 3, 2, 4, 1]
    assert [row["source_row"] for row in source_pages] == list(
        range(10, 10 + manifest["source_row_count"])
    )


def test_bounds_digest_and_link_validation_fail_before_retention(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquisition = _registered_acquisition()
    called = False

    def worker(_payload: dict) -> _WorkerResult:
        nonlocal called
        called = True
        return _trace_worker_result()

    monkeypatch.setattr(analysis_runtime, "run_worker", worker)
    for arguments in (
        ("positive", 1, 0.0, 200.0),
        ("positive", 1, 200.0, 100.0),
        ("positive", 1, float("nan"), 200.0),
        ("positive", 0, 100.0, 200.0),
        ("unknown", 1, 100.0, 200.0),
    ):
        with pytest.raises(ValueError):
            ms_trace_evidence.extract_ms_trace(
                "owner-a", acquisition["acquisition_id"], "ms-centroid", *arguments
            )
    assert not called

    manifest = ms_trace_evidence.extract_ms_trace(
        "owner-a", acquisition["acquisition_id"], "ms-centroid", "positive", 1, 100.0, 200.0
    )
    with pytest.raises(ValueError, match="digest"):
        ms_trace_evidence.read_ms_trace("owner-a", manifest["trace_id"], "points", "0" * 64)
    with pytest.raises(ValueError, match="past the end"):
        ms_trace_evidence.read_ms_trace(
            "owner-a",
            manifest["trace_id"],
            "points",
            manifest["evidence_sha256"],
            manifest["point_count"] + 1,
        )


def test_worker_link_mismatch_is_not_retained(isolated_store: Path, monkeypatch) -> None:
    acquisition = _registered_acquisition()
    bad = _trace_worker_result()
    bad["source_rows"] = [{"scan_id": "scan-1", "point_index": 1}]
    monkeypatch.setattr(analysis_runtime, "run_worker", lambda _payload: bad)
    with pytest.raises(ValueError, match="inconsistent"):
        ms_trace_evidence.extract_ms_trace(
            "owner-a", acquisition["acquisition_id"], "ms-centroid", "positive", 1, 100.0, 200.0
        )
