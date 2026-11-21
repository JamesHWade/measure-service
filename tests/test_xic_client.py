"""Installed client round-trip from native member bytes to scan-linked files."""

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from measure_service.ms_trace_cli import _read_members

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "native_acquisition_xic_v1"


def command(tmp_path, *args):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "measure_service.client_cli", *map(str, args)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_client_reads_exact_native_bytes(tmp_path):
    files = _read_members(FIXTURE)
    assert len(files) == 13
    for name, text in files.items():
        assert text.encode("utf-8") == (FIXTURE / name).read_bytes()
        (tmp_path / name).write_bytes((FIXTURE / name).read_bytes())
    path = tmp_path / "manifest.dcf"
    path.unlink()
    path.symlink_to(FIXTURE / path.name)
    with pytest.raises(ValueError, match="regular file"):
        _read_members(tmp_path)


@pytest.mark.r_backend
def test_installed_xic_client_persists_and_reopens_source_rows(tmp_path):
    r = os.environ.get("MEASURE_MCP_TEST_R")
    if not r:
        pytest.skip("set MEASURE_MCP_TEST_R to exercise the installed R backend")
    # This is the R installation the service will start. Ask it where measure
    # was loaded, so the stdio child can find that same library.
    run_r = [r, "--vanilla", "--slave", "-e"]
    library = subprocess.check_output([*run_r, 'cat(dirname(find.package("measure")))'], text=True)
    store = tmp_path / "store"
    first = tmp_path / "initial"
    second = tmp_path / "reopened"
    receipt = command(
        tmp_path,
        "xic",
        FIXTURE,
        "--stream",
        "ms-xic",
        "--polarity",
        "positive",
        "--ms-level",
        "1",
        "--mz-min",
        "100",
        "--mz-max",
        "101",
        "--output",
        first,
        "--store",
        store,
        "--r",
        r,
        "--r-library",
        library,
    )
    reopened = command(
        tmp_path,
        "xic-report",
        receipt["trace_id"],
        "--store",
        store,
        "--output",
        second,
    )
    assert reopened["trace_id"] == receipt["trace_id"]
    names = {"report.html", "xic.svg", "evidence.json", "xic.csv", "source-rows.csv"}
    assert {item.name for item in first.iterdir()} == names
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes()

    source = """
    x <- measure::read_measure_acquisition(commandArgs(TRUE)[[1L]])
    y <- measure::measure_ms_trace(x, "ms-xic", "positive", 1L, c(100, 101))
    cat(jsonlite::toJSON(y[c("scan_id", "retention_time", "value", "status")], na = "null"))
    """
    direct = json.loads(
        subprocess.check_output([*run_r, source, "--args", str(FIXTURE)], text=True)
    )
    with (first / "xic.csv").open(newline="") as stream:
        points = list(csv.DictReader(stream))
    assert [
        (
            row["scan_id"],
            float(row["retention_time"]),
            float(row["value"]) if row["value"] else None,
            row["status"],
        )
        for row in points
    ] == [(row["scan_id"], row["retention_time"], row["value"], row["status"]) for row in direct]
    with (first / "source-rows.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["scan_id"], int(row["source_row"])) for row in rows] == [
        ("scan-1", 11),
        ("scan-1", 12),
        ("scan-saturated", 20),
        ("scan-saturated", 21),
        ("scan-5", 30),
    ]
