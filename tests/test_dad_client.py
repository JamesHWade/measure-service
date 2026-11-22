"""Installed-client workflows for native DAD plane evidence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from measure_service import client_cli, dad_cli
from measure_service.ms_trace_cli import _read_members

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "native_dad_v1"


def _command(tmp_path: Path, *arguments: object, check: bool = True, overrides=None):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    for name in (
        "MEASURE_MCP_DATA_DIR",
        "MEASURE_MCP_R",
        "MEASURE_MCP_IDENTITY",
        "MEASURE_MCP_TOOL_GROUPS",
        "MEASURE_MCP_TOOL_SCHEMA_PROFILE",
        "R_LIBS",
        "R_LIBS_USER",
    ):
        environment.pop(name, None)
    environment.update(overrides or {})
    result = subprocess.run(
        [sys.executable, "-m", "measure_service.client_cli", *map(str, arguments)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if check:
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    return result


def test_dad_reader_preserves_exact_native_member_bytes(tmp_path: Path) -> None:
    files = _read_members(FIXTURE)
    assert len(files) == 13
    for name, text in files.items():
        assert text.encode("utf-8") == (FIXTURE / name).read_bytes()

    copied = tmp_path / "copied"
    copied.mkdir()
    for name in files:
        (copied / name).write_bytes((FIXTURE / name).read_bytes())
    broken = copied / "manifest.dcf"
    broken.unlink()
    broken.symlink_to(FIXTURE / "manifest.dcf")
    with pytest.raises(ValueError, match="regular file"):
        _read_members(copied)


def test_dad_report_replays_without_stdio(tmp_path: Path, monkeypatch) -> None:
    evidence = {
        "acquisition_id": "acq-1",
        "array_id": "dad-1",
        "evidence_sha256": "a" * 64,
    }
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    output = tmp_path / "report"
    calls = []

    def read_dad_evidence(path):
        assert path == evidence_path
        return evidence

    def write_dad_report(value, destination):
        calls.append((value, destination))
        destination.mkdir()

    monkeypatch.setattr(dad_cli.dad_report, "read_dad_evidence", read_dad_evidence)
    monkeypatch.setattr(dad_cli.dad_report, "write_dad_report", write_dad_report)
    parser = client_cli._parser()
    args = parser.parse_args(["dad-report", str(evidence_path), "--output", str(output)])
    assert dad_cli.run(args, parser) == 0
    assert calls == [(evidence, output)]


def test_region_response_requires_matching_selection_and_preserves_legacy_profile() -> None:
    common = {
        "acquisition_id": "acq-1",
        "array_id": "dad-1",
        "time_axis": 1,
        "profile": "measure-dad-plane/1",
        "evidence_sha256": "a" * 64,
    }
    dad_cli._validate_plane_response(
        common,
        acquisition_id="acq-1",
        array_id="dad-1",
        time_axis=1,
        time_range=None,
        wavelength_range=None,
    )

    region = {
        **common,
        "profile": "measure-dad-region/1",
        "selection": {"time_range": [1.0, 2.5], "wavelength_range": None},
        "native_point_count": 8,
        "points": [{"point_index": 3}],
    }
    dad_cli._validate_plane_response(
        region,
        acquisition_id="acq-1",
        array_id="dad-1",
        time_axis=1,
        time_range=[1.0, 2.5],
        wavelength_range=None,
    )
    with pytest.raises(ValueError, match="selection"):
        dad_cli._validate_plane_response(
            region,
            acquisition_id="acq-1",
            array_id="dad-1",
            time_axis=1,
            time_range=[2.0, 2.5],
            wavelength_range=None,
        )


def test_dad_report_rejects_malformed_json(tmp_path: Path) -> None:
    evidence_path = tmp_path / "malformed.json"
    evidence_path.write_text("not-json", encoding="utf-8")
    result = _command(
        tmp_path,
        "dad-report",
        evidence_path,
        "--output",
        tmp_path / "output",
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert not (tmp_path / "output").exists()


@pytest.mark.r_backend
@pytest.mark.parametrize(
    ("ranges", "indices"),
    [
        ([], list(range(1, 9))),
        (["--wavelength-range", "250", "250"], [1, 3, 6]),
        (["--time-range", "1", "1", "--wavelength-range", "260", "280"], [4, 5]),
    ],
)
def test_installed_dad_client_roundtrip_and_r_free_replay(tmp_path: Path, ranges, indices) -> None:
    r = os.environ.get("MEASURE_MCP_TEST_R", "/opt/homebrew/opt/r/bin/R")
    if not Path(r).is_file():
        pytest.skip("the configured R executable is unavailable")
    run_r = [r, "--vanilla", "--slave", "-e"]
    library = os.environ.get("MEASURE_MCP_TEST_R_LIBRARY") or os.environ.get("R_LIBS")
    if library:
        library = library.split(os.pathsep)[0]
    else:
        try:
            library = subprocess.check_output(
                [*run_r, 'cat(dirname(find.package("measure")))'], text=True
            ).strip()
        except subprocess.CalledProcessError:
            pytest.skip("the configured R library does not contain measure")
    store = tmp_path / "store"
    first = tmp_path / "first"
    second = tmp_path / "second"
    receipt = _command(
        tmp_path,
        "dad",
        FIXTURE,
        "--array",
        "dad-1",
        "--time-axis",
        "1",
        *ranges,
        "--store",
        store,
        "--output",
        first,
        "--r",
        r,
        "--r-library",
        library,
    )
    assert receipt["acquisition_id"]
    assert receipt["array_id"] == "dad-1"
    assert len(receipt["evidence_sha256"]) == 64
    if not ranges:
        missing = _command(
            tmp_path,
            "dad",
            FIXTURE,
            "--array",
            "absent",
            "--time-axis",
            "1",
            "--store",
            store,
            "--output",
            tmp_path / "missing",
            "--r",
            r,
            "--r-library",
            library,
            check=False,
        )
        assert missing.returncode != 0 and "array_id was not found" in missing.stderr
    original = {path.name: path.read_bytes() for path in first.iterdir()}

    evidence = json.loads(original["evidence.json"])
    assert [p["point_index"] for p in evidence["points"]] == indices
    if ranges:
        assert evidence["native_point_count"] == 8
        assert evidence["profile"] == "measure-dad-region/1"

    # A missing R executable and missing service data are intentional here:
    # replay must only read evidence.json and invoke the renderer.
    replay = _command(
        tmp_path,
        "dad-report",
        first / "evidence.json",
        "--output",
        second,
        overrides={
            "MEASURE_MCP_R": str(tmp_path / "no-r"),
            "R_LIBS": str(tmp_path / "no-r-library"),
            "R_LIBS_USER": str(tmp_path / "no-r-library"),
        },
    )
    assert replay == {
        "acquisition_id": receipt["acquisition_id"],
        "array_id": receipt["array_id"],
        "evidence_sha256": receipt["evidence_sha256"],
        "output": str(second),
    }
    assert original == {path.name: path.read_bytes() for path in second.iterdir()}


@pytest.mark.parametrize("bad_member", ["missing", "invalid-utf8"])
def test_dad_command_rejects_malformed_bundle(tmp_path: Path, bad_member: str) -> None:
    bundle = tmp_path / bad_member
    shutil.copytree(FIXTURE, bundle)
    if bad_member == "missing":
        (bundle / "arrays.csv").unlink()
    else:
        (bundle / "arrays.csv").write_bytes(b"\xff\xfe")
    result = _command(
        tmp_path,
        "dad",
        bundle,
        "--array",
        "dad-1",
        "--time-axis",
        "1",
        "--store",
        tmp_path / "store",
        "--output",
        tmp_path / "output",
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert not (tmp_path / "output").exists()
