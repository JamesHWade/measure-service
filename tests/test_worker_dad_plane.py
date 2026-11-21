"""Real-R contracts for the bounded native diode-array plane worker."""

from __future__ import annotations

import csv
import hashlib
import io
import os
from pathlib import Path

import pytest

from measure_service import analysis_runtime

CSV_MEMBER_NAMES = (
    "manifest.dcf",
    "samples.csv",
    "injections.csv",
    "channels.csv",
    "sources.csv",
    "curves.csv",
    "points.csv",
    "streams.csv",
    "scans.csv",
    "scan_points.csv",
    "arrays.csv",
    "array_axes.csv",
    "array_points.csv",
)

FIXTURE = Path(__file__).parent / "fixtures" / "native_dad_v1"
WORKER = Path(analysis_runtime.ROOT / "worker.R")


def _fixture_files() -> dict[str, str]:
    files = {}
    for name in CSV_MEMBER_NAMES:
        with (FIXTURE / name).open("r", encoding="utf-8", newline="") as stream:
            files[name] = stream.read()
    return files


def _run_worker(payload: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    r_binary = os.environ.get("MEASURE_MCP_TEST_R")
    if not r_binary:
        pytest.skip("Set MEASURE_MCP_TEST_R to the qualified R executable")
    monkeypatch.setenv("MEASURE_MCP_R", r_binary)
    return analysis_runtime.run_worker(payload, worker_path=WORKER)


def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


def _csv_text(rows: list[list[str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n", quoting=csv.QUOTE_ALL)
    writer.writerows(rows)
    return output.getvalue()


def _replace_manifest_hash(files: dict[str, str], table: str, member: str) -> None:
    digest = hashlib.sha256(files[member].encode("utf-8")).hexdigest()
    prefix = f"{table}-SHA256:"
    lines = []
    for line in files["manifest.dcf"].splitlines(keepends=True):
        if line.startswith(prefix):
            line = f"{prefix} {digest}\n"
        lines.append(line)
    files["manifest.dcf"] = "".join(lines)


def _reversed_axis_files() -> dict[str, str]:
    """Swap both native axes while keeping the observed physical plane fixed."""
    files = _fixture_files()
    files["array_axes.csv"] = _csv_text(
        [
            ["array_id", "axis_index", "storage_type", "axis_name", "axis_unit"],
            ["dad-1", "1", "double", "wavelength", "nm"],
            ["dad-1", "2", "double", "time", "min"],
        ]
    )
    rows = _csv_rows(files["array_points.csv"])
    for row in rows[1:]:
        row[2], row[3] = row[3], row[2]
    files["array_points.csv"] = _csv_text(rows)
    _replace_manifest_hash(files, "ArrayAxes", "array_axes.csv")
    _replace_manifest_hash(files, "ArrayPoints", "array_points.csv")
    return files


def _large_array_files(point_count: int = 4097) -> dict[str, str]:
    files = _fixture_files()
    rows = _csv_rows(files["array_points.csv"])
    for point_index in range(len(rows), point_count + 1):
        time = point_index - 5
        rows.append(
            [
                "dad-1",
                str(point_index),
                str(time),
                "250",
                str(point_index),
                str(point_index + 100),
                "0",
            ]
        )
    files["array_points.csv"] = _csv_text(rows)
    arrays = _csv_rows(files["arrays.csv"])
    arrays[1][-1] = str(point_count)
    files["arrays.csv"] = _csv_text(arrays)
    files["manifest.dcf"] = "".join(
        (f"ArrayPoints-Rows: {point_count}\n" if line.startswith("ArrayPoints-Rows:") else line)
        for line in files["manifest.dcf"].splitlines(keepends=True)
    )
    _replace_manifest_hash(files, "Arrays", "arrays.csv")
    _replace_manifest_hash(files, "ArrayPoints", "array_points.csv")
    return files


def _nan_value_files() -> dict[str, str]:
    files = _fixture_files()
    rows = _csv_rows(files["array_points.csv"])
    rows[1][4] = "NaN"
    files["array_points.csv"] = _csv_text(rows)
    _replace_manifest_hash(files, "ArrayPoints", "array_points.csv")
    return files


@pytest.mark.r_backend
def test_dad_plane_preserves_native_rows_and_registration_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _fixture_files()
    inspection = _run_worker(
        {"operation": "inspect_acquisition", "files": files},
        monkeypatch,
    )
    assert inspection["summary"]["arrays"] == [
        {"array_id": "dad-1", "stream_id": "array2d", "point_count": 8}
    ]
    assert inspection["summary"]["counts"]["arrays"] == 1

    result = _run_worker(
        {
            "operation": "read_dad_plane",
            "files": files,
            "array_id": "dad-1",
            "time_axis": 1,
        },
        monkeypatch,
    )

    assert set(result) == {
        "array_id",
        "stream_id",
        "injection_id",
        "channel_id",
        "source_id",
        "time_unit",
        "wavelength_unit",
        "value_unit",
        "points",
        "packages",
        "installed_r_code_md5",
        "r_version",
        "warnings",
    }
    assert result["array_id"] == "dad-1"
    assert result["stream_id"] == "array2d"
    assert result["injection_id"] == "injection-dad"
    assert result["channel_id"] == "dad"
    assert result["source_id"] == "source-dad"
    assert result["time_unit"] == "min"
    assert result["wavelength_unit"] == "nm"
    assert result["value_unit"] == "mAU"
    assert result["warnings"] == []
    assert result["points"] == [
        {
            "point_index": 1,
            "time": 0,
            "wavelength": 250,
            "value": 10,
            "source_row": 101,
            "saturated": None,
        },
        {
            "point_index": 2,
            "time": 0,
            "wavelength": 280,
            "value": 20,
            "source_row": None,
            "saturated": False,
        },
        {
            "point_index": 3,
            "time": 1,
            "wavelength": 250,
            "value": 30,
            "source_row": 103,
            "saturated": False,
        },
        {
            "point_index": 4,
            "time": 1,
            "wavelength": 260,
            "value": None,
            "source_row": 104,
            "saturated": False,
        },
        {
            "point_index": 5,
            "time": 1,
            "wavelength": 280,
            "value": 50,
            "source_row": 105,
            "saturated": True,
        },
        {
            "point_index": 6,
            "time": 2,
            "wavelength": 250,
            "value": 60,
            "source_row": 106,
            "saturated": False,
        },
        {
            "point_index": 7,
            "time": 2,
            "wavelength": 260,
            "value": 70,
            "source_row": 107,
            "saturated": None,
        },
        {
            "point_index": 8,
            "time": 2,
            "wavelength": 280,
            "value": 80,
            "source_row": 108,
            "saturated": False,
        },
    ]
    assert not any(point["time"] == 0 and point["wavelength"] == 260 for point in result["points"])


@pytest.mark.r_backend
def test_dad_plane_uses_the_caller_selected_reversed_axis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_worker(
        {
            "operation": "read_dad_plane",
            "files": _reversed_axis_files(),
            "array_id": "dad-1",
            "time_axis": 2,
        },
        monkeypatch,
    )
    assert [(row["time"], row["wavelength"]) for row in result["points"]] == [
        (0, 250),
        (0, 280),
        (1, 250),
        (1, 260),
        (1, 280),
        (2, 250),
        (2, 260),
        (2, 280),
    ]
    assert result["time_unit"] == "min"
    assert result["wavelength_unit"] == "nm"


@pytest.mark.r_backend
def test_dad_plane_rejects_nonfinite_values_but_keeps_na(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordinary = _run_worker(
        {
            "operation": "read_dad_plane",
            "files": _fixture_files(),
            "array_id": "dad-1",
            "time_axis": 1,
        },
        monkeypatch,
    )
    assert ordinary["points"][3]["value"] is None

    with pytest.raises(ValueError, match="finite or NA"):
        _run_worker(
            {
                "operation": "read_dad_plane",
                "files": _nan_value_files(),
                "array_id": "dad-1",
                "time_axis": 1,
            },
            monkeypatch,
        )


@pytest.mark.r_backend
def test_dad_plane_observed_point_bound_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="4096 observed-point"):
        _run_worker(
            {
                "operation": "read_dad_plane",
                "files": _large_array_files(),
                "array_id": "dad-1",
                "time_axis": 1,
            },
            monkeypatch,
        )
