"""Print local MCP client configuration and check an installed stdio client."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from . import pilot_numerics, tool_catalog

DEFAULT_GROUPS = ("discovery", "traces", "evidence")
CHECK_TIMEOUT_SECONDS = 120
CHECK_PROFILE = "measure-service-client-check/1"
CLIENT_SETTINGS_KEY = "measure-service"


class _CheckFailure(Exception):
    """A failure identified by a fixed public check name."""

    def __init__(self, check: str):
        super().__init__(check)
        self.check = check


def _absolute_path(value: str, name: str) -> str:
    """Normalize an absolute path while preserving executable symlink spelling."""
    path = Path(value)
    if not path.is_absolute() or "\x00" in value:
        raise ValueError(f"{name} must be an absolute path")
    return os.path.normpath(value)


def _runtime_paths(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict[str, str]:
    try:
        python = _absolute_path(args.python or sys.executable, "--python")
        data_dir = _absolute_path(args.data_dir, "--data-dir")
        r_path = _absolute_path(args.r, "--r") if args.r else None
        r_library = _absolute_path(args.r_library, "--r-library") if args.r_library else None
    except ValueError as exc:
        parser.error(str(exc))

    if (r_path is None) != (r_library is None):
        parser.error("--r and --r-library must be provided together")

    return {
        "python": python,
        "data_dir": data_dir,
        "r": r_path,
        "r_library": r_library,
    }


def _groups(include_sec: bool) -> tuple[str, ...]:
    selected = set(DEFAULT_GROUPS)
    if include_sec:
        selected.add("sec")
    return tuple(group for group in tool_catalog.TOOL_GROUPS if group in selected)


def _environment(
    *,
    data_dir: str,
    groups: tuple[str, ...],
    schema_profile: str,
    r: str | None = None,
    r_library: str | None = None,
) -> dict[str, str]:
    values = {
        "MEASURE_MCP_DATA_DIR": data_dir,
        "MEASURE_MCP_IDENTITY": "local",
        "MEASURE_MCP_TOOL_GROUPS": json.dumps(list(groups), separators=(",", ":")),
        "MEASURE_MCP_TOOL_SCHEMA_PROFILE": schema_profile,
    }
    if r is not None and r_library is not None:
        values["MEASURE_MCP_R"] = r
        values["R_LIBS"] = r_library
        values["R_LIBS_USER"] = r_library
    return values


def _json_string(value: str) -> str:
    """Encode a string as a JSON basic string, also valid for TOML strings."""
    return json.dumps(value, ensure_ascii=False)


def _client_configuration(
    client: str,
    *,
    python: str,
    data_dir: str,
    groups: tuple[str, ...],
    schema_profile: str,
    r: str | None,
    r_library: str | None,
) -> str:
    env = _environment(
        data_dir=data_dir,
        groups=groups,
        schema_profile=schema_profile,
        r=r,
        r_library=r_library,
    )
    args = ["-m", "measure_service"]
    if client == "claude":
        value = {
            "mcpServers": {
                CLIENT_SETTINGS_KEY: {
                    "command": python,
                    "args": args,
                    "env": env,
                }
            }
        }
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n"

    quoted_args = ", ".join(_json_string(item) for item in args)
    quoted_env = ", ".join(f"{name} = {_json_string(value)}" for name, value in sorted(env.items()))
    return (
        f"[mcp_servers.{CLIENT_SETTINGS_KEY}]\n"
        f"command = {_json_string(python)}\n"
        f"args = [{quoted_args}]\n"
        f"env = {{ {quoted_env} }}\n"
        "tool_timeout_sec = 120\n"
    )


def _base_report(groups: tuple[str, ...], schema_profile: str, numerical: bool) -> dict[str, Any]:
    return {
        "profile": CHECK_PROFILE,
        "status": "pending",
        "scope": "local_sdk_stdio",
        "sdk_stdio_tested": False,
        "selected_groups": list(groups),
        "schema_profile": schema_profile,
        "checks": {
            "initialize": "pending",
            "ping": "pending",
            "tool_listing": "pending",
            "catalog_selection": "pending",
            "synthetic_trace_import": "pending",
            "source_digest": "pending",
            "expected_disabled_tool_error": "pending",
            "restart_persistence": "pending",
            "numerical_restart_persistence": "pending" if numerical else "not_run",
            "independent_numerical_references": "pending" if numerical else "not_run",
        },
        "client_acceptance": {
            "claude_code": "pending_external_acceptance",
            "codex": "pending_external_acceptance",
            "model_workflow": "pending_external_acceptance",
        },
    }


def _require(condition: bool, check: str) -> None:
    if not condition:
        raise _CheckFailure(check)


async def _call(client: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await client.call_tool(name, arguments)
    except Exception:
        raise _CheckFailure("tool_call") from None
    if getattr(result, "isError", None) is not False:
        raise _CheckFailure("tool_call")
    structured = getattr(result, "structuredContent", None)
    if not isinstance(structured, dict):
        raise _CheckFailure("tool_call")
    return structured


def _synthetic_source() -> tuple[str, dict[str, str]]:
    csv_text = "time,response\n0,0.1\n1,1.2\n2,0.2\n"
    metadata = {
        "trace_id": "client-check-trace",
        "sample_id": "synthetic-sample",
        "injection_id": "synthetic-injection",
        "channel": "uv",
        "separation": "LC",
        "detector": "UV",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "sample_type": "reference",
    }
    return csv_text, metadata


def _start_session(
    *,
    python: str,
    store_dir: Path,
    groups: tuple[str, ...],
    schema_profile: str,
    temporary_root: Path,
    process_name: str,
    r: str | None,
    r_library: str | None,
) -> tuple[StdioServerParameters, TextIO]:
    stderr_path = temporary_root / f"{process_name}.stderr"
    stderr_file = stderr_path.open("w", encoding="utf-8")
    parameters = StdioServerParameters(
        command=python,
        args=["-m", "measure_service"],
        env=_environment(
            data_dir=str(store_dir),
            groups=groups,
            schema_profile=schema_profile,
            r=r,
            r_library=r_library,
        ),
        cwd=temporary_root,
    )
    return parameters, stderr_file


async def _first_process(
    *,
    python: str,
    store_dir: Path,
    groups: tuple[str, ...],
    schema_profile: str,
    temporary_root: Path,
    report: dict[str, Any],
    numerical: bool,
    r: str | None,
    r_library: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    parameters, stderr_file = _start_session(
        python=python,
        store_dir=store_dir,
        groups=groups,
        schema_profile=schema_profile,
        temporary_root=temporary_root,
        process_name="server-first",
        r=r,
        r_library=r_library,
    )
    try:
        async with stdio_client(parameters, errlog=stderr_file) as (read, write):
            async with ClientSession(read, write) as client:
                try:
                    await client.initialize()
                except Exception:
                    raise _CheckFailure("initialize") from None
                report["checks"]["initialize"] = "passed"

                try:
                    await client.send_ping()
                except Exception:
                    raise _CheckFailure("ping") from None
                report["checks"]["ping"] = "passed"

                try:
                    tools = (await client.list_tools()).tools
                except Exception:
                    raise _CheckFailure("tool_listing") from None
                names = {tool.name for tool in tools}
                expected_names = {
                    name
                    for name, group in tool_catalog.TOOL_GROUP_BY_NAME.items()
                    if group in groups
                }
                _require(names == expected_names, "tool_listing")
                for tool in tools:
                    _require(
                        tool.meta is not None
                        and tool.meta.get(tool_catalog.TOOL_GROUP_META_KEY)
                        == tool_catalog.tool_group(tool.name),
                        "tool_listing",
                    )
                report["checks"]["tool_listing"] = "passed"

                try:
                    registry = await _call(client, "measurement_registry_capabilities", {})
                except _CheckFailure as exc:
                    raise _CheckFailure("catalog_selection") from exc
                catalog = registry.get("tool_catalog")
                expected_order = [group for group in tool_catalog.TOOL_GROUPS if group in groups]
                _require(
                    isinstance(catalog, dict)
                    and catalog.get("groups") == expected_order
                    and catalog.get("schema_profile") == schema_profile
                    and catalog.get("enabled_tools") == len(expected_names),
                    "catalog_selection",
                )
                report["checks"]["catalog_selection"] = "passed"

                disabled_tool = "measurement_create_experiment"
                _require(
                    tool_catalog.TOOL_GROUP_BY_NAME.get(disabled_tool) == "experiments"
                    and "experiments" not in groups,
                    "expected_disabled_tool_error",
                )
                try:
                    rejected = await client.call_tool(disabled_tool, {})
                except Exception:
                    raise _CheckFailure("expected_disabled_tool_error") from None
                messages = [item.text for item in rejected.content if isinstance(item, TextContent)]
                _require(
                    getattr(rejected, "isError", None) is True
                    and len(messages) == 1
                    and messages[0] == "Tool is not available in this deployment",
                    "expected_disabled_tool_error",
                )
                report["checks"]["expected_disabled_tool_error"] = "passed"

                csv_text, metadata = _synthetic_source()
                try:
                    registration = await _call(
                        client,
                        "measurement_register_csv",
                        {
                            "title": "Synthetic client check trace",
                            "csv_text": csv_text,
                            "axis_column": "time",
                            "signal_column": "response",
                            "metadata": metadata,
                            "delimiter": ",",
                        },
                    )
                except _CheckFailure as exc:
                    raise _CheckFailure("synthetic_trace_import") from exc
                dataset_id = registration.get("dataset_id")
                source_digest = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
                _require(
                    isinstance(dataset_id, str)
                    and bool(dataset_id)
                    and registration.get("source_sha256") == source_digest
                    and registration.get("point_count") == 3,
                    "synthetic_trace_import",
                )
                report["checks"]["synthetic_trace_import"] = "passed"

                try:
                    inspection = await _call(
                        client, "measurement_inspect_dataset", {"dataset_id": dataset_id}
                    )
                    source = await _call(
                        client,
                        "measurement_read_source",
                        {"dataset_id": dataset_id, "offset": 0, "limit": 1000},
                    )
                except _CheckFailure as exc:
                    raise _CheckFailure("source_digest") from exc
                _require(
                    inspection.get("dataset_id") == dataset_id
                    and inspection.get("source", {}).get("source_sha256") == source_digest
                    and len(inspection.get("traces", [])) == 1
                    and inspection["traces"][0].get("point_count") == 3
                    and source.get("dataset_id") == dataset_id
                    and source.get("source_sha256") == source_digest
                    and source.get("text") == csv_text
                    and source.get("count") == len(csv_text)
                    and source.get("next_offset") is None,
                    "source_digest",
                )
                report["checks"]["source_digest"] = "passed"

                numerical_evidence = None
                if numerical:
                    try:
                        numerical_evidence = await pilot_numerics.qualify_numerics(client)
                    except Exception:
                        raise _CheckFailure("independent_numerical_references") from None
                    report["checks"]["independent_numerical_references"] = "passed"

                return dataset_id, inspection, source, numerical_evidence
    finally:
        stderr_file.close()


async def _second_process(
    *,
    python: str,
    store_dir: Path,
    groups: tuple[str, ...],
    schema_profile: str,
    temporary_root: Path,
    dataset_id: str,
    expected_inspection: dict[str, Any],
    expected_source: dict[str, Any],
    numerical_records: list[dict[str, Any]] | None,
    r: str | None,
    r_library: str | None,
) -> None:
    parameters, stderr_file = _start_session(
        python=python,
        store_dir=store_dir,
        groups=groups,
        schema_profile=schema_profile,
        temporary_root=temporary_root,
        process_name="server-restarted",
        r=r,
        r_library=r_library,
    )
    try:
        async with stdio_client(parameters, errlog=stderr_file) as (read, write):
            async with ClientSession(read, write) as client:
                try:
                    await client.initialize()
                    await client.send_ping()
                    tools = (await client.list_tools()).tools
                except Exception:
                    raise _CheckFailure("restart_persistence") from None
                expected_names = {
                    name
                    for name, group in tool_catalog.TOOL_GROUP_BY_NAME.items()
                    if group in groups
                }
                _require({tool.name for tool in tools} == expected_names, "restart_persistence")
                try:
                    registry = await _call(client, "measurement_registry_capabilities", {})
                    inspection = await _call(
                        client, "measurement_inspect_dataset", {"dataset_id": dataset_id}
                    )
                    source = await _call(
                        client,
                        "measurement_read_source",
                        {"dataset_id": dataset_id, "offset": 0, "limit": 1000},
                    )
                except _CheckFailure as exc:
                    raise _CheckFailure("restart_persistence") from exc
                catalog = registry.get("tool_catalog")
                expected_order = [group for group in tool_catalog.TOOL_GROUPS if group in groups]
                _require(
                    isinstance(catalog, dict)
                    and catalog.get("groups") == expected_order
                    and catalog.get("schema_profile") == schema_profile
                    and catalog.get("enabled_tools") == len(expected_names)
                    and inspection == expected_inspection
                    and source == expected_source,
                    "restart_persistence",
                )
                if numerical_records is not None:
                    for record in numerical_records:
                        tool = record.get("tool")
                        arguments = record.get("arguments")
                        expected = record.get("response")
                        _require(
                            isinstance(tool, str)
                            and isinstance(arguments, dict)
                            and isinstance(expected, dict),
                            "numerical_restart_persistence",
                        )
                        try:
                            actual = await _call(client, tool, arguments)
                        except _CheckFailure as exc:
                            raise _CheckFailure("numerical_restart_persistence") from exc
                        _require(
                            actual == expected,
                            "numerical_restart_persistence",
                        )
    finally:
        stderr_file.close()


@contextmanager
def _private_umask():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


async def _run_check(
    *,
    python: str,
    groups: tuple[str, ...],
    schema_profile: str,
    numerical: bool,
    r: str | None,
    r_library: str | None,
    report: dict[str, Any],
) -> None:
    with tempfile.TemporaryDirectory(prefix="measure-service-client-") as raw_root:
        temporary_root = Path(raw_root)
        temporary_root.chmod(0o700)
        store_dir = temporary_root / "private-store"
        _require(not store_dir.exists(), "private_store")
        with _private_umask():
            dataset_id, inspection, source, numerical_evidence = await _first_process(
                python=python,
                store_dir=store_dir,
                groups=groups,
                schema_profile=schema_profile,
                temporary_root=temporary_root,
                report=report,
                numerical=numerical,
                r=r,
                r_library=r_library,
            )
            await _second_process(
                python=python,
                store_dir=store_dir,
                groups=groups,
                schema_profile=schema_profile,
                temporary_root=temporary_root,
                dataset_id=dataset_id,
                expected_inspection=inspection,
                expected_source=source,
                numerical_records=(
                    numerical_evidence["records"] if numerical_evidence is not None else None
                ),
                r=r,
                r_library=r_library,
            )
        report["checks"]["restart_persistence"] = "passed"
        if numerical_evidence is not None:
            report["checks"]["numerical_restart_persistence"] = "passed"


def _add_connection_options(parser: argparse.ArgumentParser, *, config: bool) -> None:
    parser.add_argument("--python", help="absolute Python executable; defaults to this interpreter")
    parser.add_argument(
        "--data-dir",
        required=True,
        help="absolute service data directory (configuration output does not create it)",
    )
    parser.add_argument("--include-sec", action="store_true", help="include the SEC tool group")
    parser.add_argument(
        "--schema-profile",
        choices=(tool_catalog.CANONICAL_SCHEMA_PROFILE, tool_catalog.FUNCTION_HOST_SCHEMA_PROFILE),
        default=tool_catalog.FUNCTION_HOST_SCHEMA_PROFILE,
        help="advertised tool schema profile",
    )
    parser.add_argument("--r", help="absolute R executable for numerical configuration/checks")
    parser.add_argument("--r-library", help="absolute R library directory paired with --r")
    if not config:
        parser.add_argument(
            "--numerical",
            action="store_true",
            help="also run the installed synthetic numerical qualification (requires --include-sec)",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="measure-service-client")
    subparsers = parser.add_subparsers(dest="command", required=True)

    config = subparsers.add_parser(
        "config", help="print a client configuration fragment without editing settings"
    )
    config.add_argument("client", choices=("claude", "codex"))
    _add_connection_options(config, config=True)

    check = subparsers.add_parser(
        "check", help="check installed stdio startup, tool selection and restart persistence"
    )
    check.add_argument("--python", help="absolute Python executable; defaults to this interpreter")
    check.add_argument("--include-sec", action="store_true", help="include the SEC tool group")
    check.add_argument(
        "--schema-profile",
        choices=(tool_catalog.CANONICAL_SCHEMA_PROFILE, tool_catalog.FUNCTION_HOST_SCHEMA_PROFILE),
        default=tool_catalog.FUNCTION_HOST_SCHEMA_PROFILE,
        help="expected advertised tool schema profile",
    )
    check.add_argument("--r", help="absolute R executable for numerical qualification")
    check.add_argument("--r-library", help="absolute R library directory paired with --r")
    check.add_argument(
        "--numerical",
        action="store_true",
        help="also run the installed synthetic numerical qualification (requires --include-sec)",
    )
    check.add_argument(
        "--timeout",
        type=int,
        default=CHECK_TIMEOUT_SECONDS,
        help=f"total check timeout in seconds (default: {CHECK_TIMEOUT_SECONDS})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "config":
        paths = _runtime_paths(args, parser)
        groups = _groups(args.include_sec)
        sys.stdout.write(
            _client_configuration(
                args.client,
                python=paths["python"],
                data_dir=paths["data_dir"],
                groups=groups,
                schema_profile=args.schema_profile,
                r=paths["r"],
                r_library=paths["r_library"],
            )
        )
        return 0

    try:
        python = _absolute_path(args.python or sys.executable, "--python")
        r = _absolute_path(args.r, "--r") if args.r else None
        r_library = _absolute_path(args.r_library, "--r-library") if args.r_library else None
    except ValueError as exc:
        parser.error(str(exc))
    if (r is None) != (r_library is None):
        parser.error("--r and --r-library must be provided together")
    if args.numerical and "sec" not in _groups(args.include_sec):
        parser.error("--numerical requires --include-sec")
    if args.numerical and (r is None or r_library is None):
        parser.error("--numerical requires both --r and --r-library absolute paths")
    if (r is not None or r_library is not None) and not args.numerical:
        parser.error("--r and --r-library are only used with --numerical for check")
    if args.timeout < 1 or args.timeout > CHECK_TIMEOUT_SECONDS:
        parser.error(f"--timeout must be between 1 and {CHECK_TIMEOUT_SECONDS} seconds")

    groups = _groups(args.include_sec)
    report = _base_report(groups, args.schema_profile, args.numerical)
    try:
        asyncio.run(
            asyncio.wait_for(
                _run_check(
                    python=python,
                    groups=groups,
                    schema_profile=args.schema_profile,
                    numerical=args.numerical,
                    r=r,
                    r_library=r_library,
                    report=report,
                ),
                timeout=args.timeout,
            )
        )
    except _CheckFailure as exc:
        report["checks"][exc.check] = "failed"
        report["failure_code"] = "client_check_failed"
    except asyncio.TimeoutError:
        report["checks"]["deadline"] = "failed"
        report["failure_code"] = "timeout"
    except Exception:
        report["failure_code"] = "client_check_failed"
    else:
        report["status"] = "passed"
        report["sdk_stdio_tested"] = True

    if report["status"] != "passed":
        report["status"] = "failed"
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
