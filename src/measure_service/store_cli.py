"""Command-line interface for local store recovery operations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from .store_operations import (
    StoreOperationError,
    backup_store,
    initialize_store,
    inspect_store,
    restore_store,
    verify_backup,
)


class _CliFailure(Exception):
    pass


class _HelpRequested(Exception):
    pass


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _CliFailure

    def exit(self, status: int = 0, message: str | None = None) -> None:
        del message
        if status == 0:
            raise _HelpRequested
        raise _CliFailure


def _parser() -> argparse.ArgumentParser:
    parser = _SafeParser(
        prog="measure-service-store",
        description="Inspect, initialize, back up, verify, or restore a local Measure store.",
    )
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_SafeParser)

    inspect_parser = commands.add_parser("inspect", help="inspect an existing local store")
    inspect_parser.add_argument("--data-dir", required=True)

    initialize_parser = commands.add_parser("initialize", help="create a new local store")
    initialize_parser.add_argument("--data-dir", required=True)

    backup_parser = commands.add_parser("backup", help="create an online consistent backup")
    backup_parser.add_argument("--data-dir", required=True)
    backup_parser.add_argument("--output-dir", required=True)

    verify_parser = commands.add_parser("verify", help="verify a sealed store backup")
    verify_parser.add_argument("--backup-dir", required=True)

    restore_parser = commands.add_parser("restore", help="restore into a new local store")
    restore_parser.add_argument("--backup-dir", required=True)
    restore_parser.add_argument("--data-dir", required=True)
    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "inspect":
        return inspect_store(args.data_dir)
    if args.command == "initialize":
        return initialize_store(args.data_dir)
    if args.command == "backup":
        return backup_store(args.data_dir, args.output_dir)
    if args.command == "verify":
        return verify_backup(args.backup_dir)
    if args.command == "restore":
        return restore_store(args.backup_dir, args.data_dir)
    raise StoreOperationError("Unsupported store operation")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local operator CLI, writing compact JSON or a safe error."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        result = _dispatch(args)
        sys.stdout.write(
            json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        )
        return 0
    except _HelpRequested:
        return 0
    except _CliFailure:
        sys.stderr.write("error: invalid command line\n")
        return 2
    except StoreOperationError as error:
        sys.stderr.write(f"error: {error}\n")
        return 2
    except Exception:
        # Keep filesystem paths and SQLite details out of the operator output.
        sys.stderr.write("error: store operation failed\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
