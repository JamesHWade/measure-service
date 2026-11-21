"""Local stdio workflow for a single centroid extracted-ion chromatogram."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import stat
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from . import ms_trace_report
from .evidence_client import _call


def add_commands(subparsers) -> None:
    extract = subparsers.add_parser("xic", help="register a native acquisition and export its XIC")
    extract.add_argument("bundle", type=Path, help="local native measure acquisition directory")
    extract.add_argument("--stream", required=True, help="declared MS stream ID")
    extract.add_argument("--polarity", required=True, choices=("positive", "negative"))
    extract.add_argument("--ms-level", type=int, required=True)
    extract.add_argument("--mz-min", type=float, required=True, help="inclusive lower m/z bound")
    extract.add_argument("--mz-max", type=float, required=True, help="inclusive upper m/z bound")
    extract.add_argument("--r", type=Path, help="absolute R executable")
    extract.add_argument("--r-library", type=Path, help="absolute R library directory")
    reopen = subparsers.add_parser("xic-report", help="export a retained XIC from its trace ID")
    reopen.add_argument("trace_id", help="trace ID from the original extraction")
    for command in (extract, reopen):
        command.add_argument(
            "--store", required=True, type=Path, help="local service data directory"
        )
        command.add_argument("--output", required=True, type=Path, help="new report directory")


def _read_members(directory: Path) -> dict[str, str]:
    from .acquisition_models import (
        CSV_MEMBER_NAMES,
        MAX_ACQUISITION_BYTES,
        MAX_MEMBER_BYTES,
        TSV_MEMBER_NAMES,
    )

    files = {}
    members = [
        group
        for group in (CSV_MEMBER_NAMES, TSV_MEMBER_NAMES)
        if all((directory / name).exists() for name in group)
    ]
    if len(members) != 1:
        raise ValueError("expected manifest.dcf and one complete set of native CSV or TSV tables")
    remaining = MAX_ACQUISITION_BYTES
    for name in sorted(members[0]):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{name} must be a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as member:
            if not stat.S_ISREG(os.fstat(member.fileno()).st_mode):
                raise ValueError(f"{name} must be a regular file")
            allowance = min(remaining, MAX_MEMBER_BYTES)
            content = member.read(allowance + 1)
            if len(content) > allowance:
                raise ValueError("native acquisition exceeds the input byte limit")
        remaining -= len(content)
        files[name] = content.decode("utf-8")
    return files


async def run_workflow(args, parameters, stderr_file, files):
    failure = None
    result = None
    async with stdio_client(parameters, errlog=stderr_file) as (read, write):
        async with ClientSession(read, write) as client:
            try:
                await client.initialize()
                if args.command == "xic":
                    acquisition = await _call(
                        client, "measurement_register_acquisition", {"files": files}
                    )
                    retained = await _call(
                        client,
                        "measurement_extract_ms_trace",
                        {
                            "acquisition_id": acquisition["acquisition_id"],
                            "stream_id": args.stream,
                            "polarity": args.polarity,
                            "ms_level": args.ms_level,
                            "mz_min": args.mz_min,
                            "mz_max": args.mz_max,
                        },
                    )
                    trace_id = retained["trace_id"]
                else:
                    trace_id = args.trace_id
                await ms_trace_report.export_xic_report(client, trace_id, args.output)
                result = {"trace_id": trace_id, "output": str(args.output)}
            except (ValueError, OSError) as exc:
                failure = exc
    if failure:
        raise failure
    return result


def run(args, parser: argparse.ArgumentParser) -> int:
    from .client_cli import _private_umask, _start_session

    r = getattr(args, "r", None)
    library = getattr(args, "r_library", None)
    if bool(r) != bool(library) or (r and not (r.is_absolute() and library.is_absolute())):
        parser.error("supply --r and --r-library together as absolute paths")
    if args.command == "xic":
        if not (math.isfinite(args.mz_min) and math.isfinite(args.mz_max)):
            parser.error("both m/z bounds must be finite")
        if args.ms_level < 1 or not 0 < args.mz_min <= args.mz_max:
            parser.error("use a positive MS level and ordered m/z bounds")
    try:
        if os.path.lexists(args.output):
            raise ValueError("the output directory already exists; choose a new directory")
        files = _read_members(args.bundle) if args.command == "xic" else None
        with _private_umask(), tempfile.TemporaryDirectory(prefix="measure-xic-") as temporary:
            parameters, stderr_file = _start_session(
                python=sys.executable,
                store_dir=args.store.absolute(),
                groups=("ms",),
                schema_profile="function-host/1",
                temporary_root=Path(temporary),
                process_name="server",
                r=str(r) if r else None,
                r_library=str(library) if library else None,
            )
            with stderr_file:
                result = asyncio.run(run_workflow(args, parameters, stderr_file, files))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0
