"""Installed local workflows for native DAD plane evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from . import dad_plane, dad_report, ms_trace_cli
from .evidence_client import _call


def add_commands(subparsers: Any) -> None:
    """Register the DAD acquisition and report-replay subcommands."""
    dad = subparsers.add_parser(
        "dad", help="register a native acquisition and export its DAD plane"
    )
    dad.add_argument(
        "bundle", type=Path, metavar="BUNDLE", help="local native measure acquisition directory"
    )
    dad.add_argument("--array", required=True, help="declared native DAD array ID")
    dad.add_argument(
        "--time-axis",
        required=True,
        type=int,
        choices=(1, 2),
        help="native axis containing time coordinates (1 or 2)",
    )
    dad.add_argument(
        "--time-range",
        nargs=2,
        type=float,
        metavar=("LOW", "HIGH"),
        help="inclusive time window in the native time unit",
    )
    dad.add_argument(
        "--wavelength-range",
        nargs=2,
        type=float,
        metavar=("LOW", "HIGH"),
        help="inclusive wavelength window in the native wavelength unit",
    )
    dad.add_argument("--store", required=True, type=Path, help="local service data directory")
    dad.add_argument("--output", required=True, type=Path, help="new report directory")
    dad.add_argument("--r", type=Path, help="absolute R executable")
    dad.add_argument("--r-library", type=Path, help="absolute R library directory")

    replay = subparsers.add_parser(
        "dad-report", help="render a retained DAD evidence JSON file without starting a service"
    )
    replay.add_argument("evidence_json", metavar="EVIDENCE_JSON", type=Path)
    replay.add_argument("--output", required=True, type=Path, help="new report directory")


async def _read_plane(
    *,
    parameters: Any,
    stderr_file: Any,
    files: dict[str, str],
    array_id: str,
    time_axis: int,
    time_range: list[float] | None = None,
    wavelength_range: list[float] | None = None,
) -> dict[str, Any]:
    """Register an exact bundle and read one DAD plane over installed stdio."""
    async with stdio_client(parameters, errlog=stderr_file) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            acquisition = await _call(client, "measurement_register_acquisition", {"files": files})
            acquisition_id = acquisition.get("acquisition_id")
            if not isinstance(acquisition_id, str) or not acquisition_id:
                raise ValueError("Acquisition registration returned no acquisition ID")
            request = {
                "acquisition_id": acquisition_id,
                "array_id": array_id,
                "time_axis": time_axis,
            }
            if time_range is not None:
                request["time_range"] = time_range
            if wavelength_range is not None:
                request["wavelength_range"] = wavelength_range
            plane = await _call(
                client,
                "measurement_read_dad_plane",
                request,
            )
    _validate_plane_response(
        plane,
        acquisition_id=acquisition_id,
        array_id=array_id,
        time_axis=time_axis,
        time_range=time_range,
        wavelength_range=wavelength_range,
    )
    return plane


def _validate_plane_response(
    plane: Any,
    *,
    acquisition_id: str,
    array_id: str,
    time_axis: int,
    time_range: list[float] | None,
    wavelength_range: list[float] | None,
) -> None:
    """Check that a worker returned the requested DAD view and evidence identity."""
    if not isinstance(plane, dict):
        raise ValueError("DAD plane response was not an object")
    if (
        plane.get("acquisition_id") != acquisition_id
        or plane.get("array_id") != array_id
        or plane.get("time_axis") != time_axis
    ):
        raise ValueError("DAD plane response identity or axis does not match the request")
    expected_region = time_range is not None or wavelength_range is not None
    expected_profile = dad_plane.REGION_PROFILE if expected_region else dad_plane.PROFILE
    if plane.get("profile") != expected_profile:
        raise ValueError("DAD plane response profile does not match the requested view")
    if expected_region:
        expected_selection = {
            "time_range": time_range,
            "wavelength_range": wavelength_range,
        }
        if plane.get("selection") != expected_selection:
            raise ValueError("DAD plane response selection does not match the request")
        native_point_count = plane.get("native_point_count")
        if type(native_point_count) is not int or native_point_count < len(plane.get("points", [])):
            raise ValueError("DAD plane response has an invalid native point count")
    evidence_sha256 = plane.get("evidence_sha256")
    if not isinstance(evidence_sha256, str) or len(evidence_sha256) != 64:
        raise ValueError("DAD plane response is missing its SHA-256 digest")


def _validate_runtime_paths(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[str | None, str | None]:
    r = getattr(args, "r", None)
    library = getattr(args, "r_library", None)
    if bool(r) != bool(library) or (r and not (r.is_absolute() and library.is_absolute())):
        parser.error("supply --r and --r-library together as absolute paths")
    return (str(r), str(library)) if r else (None, None)


def _existing_output(path: Path) -> None:
    if os.path.lexists(path):
        raise ValueError("the output directory already exists; choose a new directory")


def _dad_workflow(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict[str, str]:
    r, r_library = _validate_runtime_paths(args, parser)
    if not args.array or "\x00" in args.array:
        parser.error("--array must be a non-empty array ID")
    time_range = dad_plane.validate_range(args.time_range, wavelength=False)
    wavelength_range = dad_plane.validate_range(args.wavelength_range, wavelength=True)
    try:
        _existing_output(args.output)
        files = ms_trace_cli._read_members(args.bundle)
        from .client_cli import _private_umask, _start_session

        with _private_umask(), tempfile.TemporaryDirectory(prefix="measure-dad-") as temporary:
            temporary_root = Path(temporary)
            parameters, stderr_file = _start_session(
                python=sys.executable,
                store_dir=args.store.absolute(),
                groups=("ms", "dad"),
                schema_profile="function-host/1",
                temporary_root=temporary_root,
                process_name="server",
                r=r,
                r_library=r_library,
            )
            try:
                plane = asyncio.run(
                    _read_plane(
                        parameters=parameters,
                        stderr_file=stderr_file,
                        files=files,
                        array_id=args.array,
                        time_axis=args.time_axis,
                        time_range=time_range,
                        wavelength_range=wavelength_range,
                    )
                )
            finally:
                stderr_file.close()
        dad_report.write_dad_report(plane, args.output)
    except Exception as exc:
        raise ValueError(str(exc) or "DAD workflow failed") from exc
    return {
        "acquisition_id": plane["acquisition_id"],
        "array_id": plane["array_id"],
        "evidence_sha256": plane["evidence_sha256"],
        "output": str(args.output),
    }


def _dad_report_workflow(args: argparse.Namespace) -> dict[str, str]:
    _existing_output(args.output)
    evidence = dad_report.read_dad_evidence(args.evidence_json)
    dad_report.write_dad_report(evidence, args.output)
    return {
        "acquisition_id": evidence["acquisition_id"],
        "array_id": evidence["array_id"],
        "evidence_sha256": evidence["evidence_sha256"],
        "output": str(args.output),
    }


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Run a DAD command and emit a compact JSON receipt."""
    try:
        result = (
            _dad_workflow(args, parser) if args.command == "dad" else _dad_report_workflow(args)
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc) or "DAD workflow failed", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0
