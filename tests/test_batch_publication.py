"""Independent consistency checks for the published chromatogram batch."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from pathlib import Path

ARCHIVE = Path(__file__).parents[1] / "docs" / "reference-data" / "chromatogram-batch-example"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _receipt() -> dict:
    return json.loads((ARCHIVE / "workflow_receipt.json").read_text())


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _regular(relative: str) -> Path:
    """Resolve a receipt path while rejecting traversal, links and directories."""

    path = ARCHIVE / relative
    assert not Path(relative).is_absolute()
    assert ".." not in Path(relative).parts
    assert path.resolve().is_relative_to(ARCHIVE.resolve())
    assert not path.is_symlink()
    assert path.is_file()
    assert path.stat().st_mode & 0o170000 == 0o100000
    return path


def _csv_rows(name: str) -> list[dict[str, str]]:
    with (ARCHIVE / name).open(newline="") as stream:
        return list(csv.DictReader(stream))


def test_receipt_hashes_and_export_joins_are_complete() -> None:
    receipt = _receipt()
    hashes = receipt["canonical_file_hashes"]
    assert hashes
    for relative, expected in hashes.items():
        path = _regular(relative)
        assert SHA256.fullmatch(expected)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected

    table_names = {"items.csv", "attempts.csv", "control-events.csv", "control-items.csv"}
    assert set(receipt["tables"]) == table_names
    tables = {name: _csv_rows(name) for name in table_names}
    expected_counts = {
        "items.csv": 4,
        "attempts.csv": 5,
        "control-events.csv": 2,
        "control-items.csv": 3,
    }
    for name, rows in tables.items():
        table = receipt["tables"][name]
        assert len(rows) == table["row_count"] == expected_counts[name]
        assert hashlib.sha256((ARCHIVE / name).read_bytes()).hexdigest() == table["sha256"]

    items = sorted(tables["items.csv"], key=lambda row: int(row["ordinal"]))
    assert len({(row["batch_id"], row["ordinal"]) for row in items}) == 4
    assert [row["role"] for row in items] == ["reference", "blank", "qc", "unknown"]
    assert [row["original_sample_type"] for row in items] == [
        "reference",
        "blank",
        "qc",
        "unknown",
    ]
    assert all(row["trace_id"] == "uv-254" for row in items)
    assert all(row["channel"] == "UV 254 nm" for row in items)
    assert all(row["axis_unit"] == "min" and row["signal_unit"] == "mAU" for row in items)
    assert all(row["point_count"] == "501" for row in items)

    attempts = tables["attempts.csv"]
    assert len({(row["attempt_id"], row["ordinal"], row["attempt_no"]) for row in attempts}) == 5
    events = tables["control-events.csv"]
    affected = tables["control-items.csv"]
    assert len({(row["event_id"], row["event_no"]) for row in events}) == 2
    assert len({(row["event_id"], row["ordinal"]) for row in affected}) == 3
    assert {row["event_id"] for row in affected} == {row["event_id"] for row in events}

    publication = json.loads((ARCHIVE / "publication.json").read_text())
    publication_files = publication["files"]
    assert set(publication_files) == set(hashes) | {
        "workflow_receipt.json",
        "independent-review.json",
    }
    assert "publication.json" not in publication_files
    assert {
        path.relative_to(ARCHIVE).as_posix() for path in ARCHIVE.rglob("*") if path.is_file()
    } == set(publication_files) | {"publication.json"}
    receipt_sha256 = hashlib.sha256((ARCHIVE / "workflow_receipt.json").read_bytes()).hexdigest()
    assert publication["workflow_receipt_sha256"] == receipt_sha256
    for relative, metadata in publication_files.items():
        path = _regular(relative)
        assert metadata["size_bytes"] == path.stat().st_size
        assert metadata["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_reports_preserve_numeric_traces_peaks_and_stored_result_hashes() -> None:
    receipt = _receipt()
    item_rows = {int(row["ordinal"]): row for row in _csv_rows("items.csv")}
    reports = {int(report["ordinal"]): report for report in receipt["reports"]}
    assert set(item_rows) == set(reports) == {0, 1, 2, 3}
    total_peaks = 0

    for ordinal, item in item_rows.items():
        report = reports[ordinal]
        evidence_path = _regular(report["files"]["evidence.json"]["path"])
        evidence = json.loads(evidence_path.read_text())
        assert evidence["analysis"]["analysis_id"] == item["analysis_id"] == report["analysis_id"]
        assert evidence["trace"]["sample_type"] == item["original_sample_type"]
        assert evidence["trace"]["axis_unit"] == item["axis_unit"]
        assert evidence["trace"]["signal_unit"] == item["signal_unit"]
        assert len(evidence["points"]) == int(item["point_count"]) == 501
        assert len(evidence["peaks"]) == int(item["peak_count"]) == report["peak_count"]
        assert evidence["result"] == report["result_metadata"]
        assert _canonical_sha256(report["result_metadata"]) == report["result_metadata_sha256"]

        trace_rows = list(
            csv.DictReader(
                io.StringIO(_regular(report["files"]["trace.csv"]["path"]).read_text(), newline="")
            )
        )
        assert len(trace_rows) == 501
        for field, evidence_field in (
            ("axis", "axis"),
            ("processed_axis", "processed_axis"),
            ("raw_signal", "raw_signal"),
            ("processed_signal", "processed_signal"),
        ):
            values = [float(row[field]) for row in trace_rows]
            expected = [float(point[evidence_field]) for point in evidence["points"]]
            assert all(math.isfinite(value) for value in values)
            assert values == expected
        assert [int(row["sample_index"]) for row in trace_rows] == list(range(501))
        assert all(row["analysis_id"] == report["analysis_id"] for row in trace_rows)
        assert all(row["evidence_sha256"] == report["evidence_sha256"] for row in trace_rows)

        peak_rows = list(
            csv.DictReader(
                io.StringIO(_regular(report["files"]["peaks.csv"]["path"]).read_text(), newline="")
            )
        )
        assert len(peak_rows) == len(evidence["peaks"])
        for csv_peak, evidence_peak in zip(peak_rows, evidence["peaks"], strict=True):
            assert int(csv_peak["peak_id"]) == evidence_peak["peak_id"]
            for field in ("location", "height", "left_base", "right_base", "area"):
                assert float(csv_peak[field]) == float(evidence_peak[field])
            assert csv_peak["area_basis"] == evidence_peak.get("area_basis", "")
        total_peaks += len(peak_rows)

        reconstructed = dict(evidence["result"])
        reconstructed["peaks"] = evidence["peaks"]
        reconstructed["processed_trace"] = {
            "axis": [point["processed_axis"] for point in evidence["points"]],
            "signal": [point["processed_signal"] for point in evidence["points"]],
        }
        stored_digest = evidence["analysis"]["stored_result_sha256"]
        assert _canonical_sha256(reconstructed) == stored_digest == report["stored_result_sha256"]

    assert total_peaks == 6
    assert int(item_rows[1]["peak_count"]) == 0


def test_history_retains_failed_attempt_cancel_checkpoint_and_replay_identity() -> None:
    receipt = _receipt()
    items = {int(row["ordinal"]): row for row in _csv_rows("items.csv")}
    attempts = _csv_rows("attempts.csv")
    events = _csv_rows("control-events.csv")
    affected = _csv_rows("control-items.csv")

    failed = [row for row in attempts if row["state"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["ordinal"] == "2" and failed[0]["r_version"] == ""
    assert failed[0]["error_message"]
    unknown = receipt["original_failure_unknowns"]
    assert unknown["runtime"] == {"runtime_status": "unknown"}
    assert unknown["r_runtime_evidence"] == "unknown_until_success"

    first_ids = [item["analysis_id"] for item in receipt["run_records"]["first_success"]["items"]]
    replay_ids = [
        item["analysis_id"] for item in receipt["run_records"]["replay_first_success_no_r"]["items"]
    ]
    assert first_ids == replay_ids == receipt["first_two_analysis_ids"]
    old_failed = receipt["run_records"]["replay_original_failure_no_r"]
    assert old_failed["status"] == "failed"
    assert old_failed["items"][0]["state"] == "failed"
    assert old_failed["items"][0]["item_state"] == items[2]["state"] == "completed"

    history = {event["event_id"]: event for event in receipt["control_history"]["events"]}
    assert [event["kind"] for event in events] == ["cancel_requested", "retry_selected"]
    assert [event["event_no"] for event in events] == ["1", "2"]
    for event in events:
        retained = history[event["event_id"]]
        assert retained["kind"] == event["kind"]
        joined = [
            {
                "ordinal": int(row["ordinal"]),
                "from_state": row["from_state"],
                "to_state": row["to_state"],
            }
            for row in affected
            if row["event_id"] == event["event_id"]
        ]
        assert joined == retained["items"]
    assert [(row["ordinal"], row["from_state"], row["to_state"]) for row in affected] == [
        ("3", "pending", "cancelled"),
        ("2", "failed", "pending"),
        ("3", "cancelled", "pending"),
    ]
