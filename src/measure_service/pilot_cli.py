"""Prepare and recheck synthetic invited-pilot evidence on a chosen endpoint."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import re
import sys
import uuid
from typing import Any

from .pilot_checks import prepare_pilot, validate_checkpoint, verify_pilot
from .pilot_evidence import EvidenceDirectory, read_checkpoint
from .pilot_transport import endpoint_from_environment, pilot_sessions, probe_proxy_rejection

CHECKPOINT_PROFILE = "measure-invited-pilot-checkpoint/1"
REPORT_PROFILE = "measure-invited-pilot-report/1"
RUN_TIMEOUT_SECONDS = 600
# These require evidence from the actual host/operator, independently of a
# successful synthetic MCP run. They are intentionally never auto-completed.
HOST_GATES = (
    "real_sign_in_and_client",
    "identity_header_stripping",
    "direct_upstream_blocked",
    "actual_host_restart",
    "restored_volume_cutover",
    "proxy_limits_and_host_quotas",
    "off_host_backup_and_schedule",
    "failure_alert_delivery",
    "access_retention_deletion_incident_policy",
    "authorized_representative_input",
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _validate_envelope(value: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"profile", "endpoint_sha256", "local_simulation", "workflow"}
        or value.get("profile") != CHECKPOINT_PROFILE
        or not isinstance(value.get("endpoint_sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", value["endpoint_sha256"]) is None
        or type(value.get("local_simulation")) is not bool
    ):
        raise ValueError("The pilot checkpoint envelope is invalid")
    validate_checkpoint(value["workflow"])
    return value


def _reference_summary(workflow: dict[str, Any]) -> dict[str, Any] | None:
    if not workflow["numerical"]:
        return None
    summary = {}
    for account in ("a", "b"):
        report = workflow["numeric"][account]["report"]
        values = {
            "gaussian_area_relative_error": report["chromatogram"]["relative_area_error"],
            "sec_mn_relative_error": report["sec"]["relative_mn_error"],
            "sec_mw_relative_error": report["sec"]["relative_mw_error"],
            "sec_dispersity_relative_error": report["sec"]["relative_dispersity_error"],
            "distribution_integral": report["distribution"]["integral"],
        }
        if any(
            type(value) not in (int, float) or not math.isfinite(value) for value in values.values()
        ):
            raise ValueError("The numerical reference summary is invalid")
        summary[account] = values
    return summary


def _report(
    phase: str,
    *,
    numerical: bool,
    local_simulation: bool,
    proxy: dict[str, str],
    checkpoint_sha256: str,
    original_numerical_reference_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Never copy fields or arbitrary strings from a remote response/checkpoint.
    return {
        "profile": REPORT_PROFILE,
        "phase": phase,
        "status": "passed",
        "transport_scope": "local_simulation" if local_simulation else "selected_https_endpoint",
        "checks": {
            "two_distinct_account_record_sets": "passed",
            "owned_reads_and_cross_account_denials": "passed",
            "cross_account_link_denials": "passed",
            "exact_registry_retry_receipts": "passed",
            "retained_synthetic_source": "passed",
            "historical_record_continuity": "passed" if phase == "verify" else "not_run",
            "independent_numerical_references": "passed" if numerical else "not_run",
            **proxy,
        },
        "checkpoint_sha256": checkpoint_sha256,
        "original_numerical_reference_summary": original_numerical_reference_summary,
        "host_gates": {gate: "pending_operator_evidence" for gate in HOST_GATES},
        "invitation_ready": False,
    }


async def _execute(args: argparse.Namespace, output: EvidenceDirectory) -> dict[str, Any]:
    endpoint = endpoint_from_environment(allow_local_http=args.allow_local_http)
    checkpoint = None
    if args.command == "verify":
        checkpoint = _validate_envelope(read_checkpoint(args.checkpoint))
        if checkpoint["endpoint_sha256"] != endpoint.endpoint_sha256:
            raise ValueError("Recheck the exact endpoint recorded by the pilot checkpoint")
        if checkpoint["local_simulation"] != args.allow_local_http:
            raise ValueError("Use the original pilot transport scope")
    # No synthetic writes occur before configuration and checkpoint validation.
    proxy = await probe_proxy_rejection(endpoint)
    async with pilot_sessions(endpoint, journal=output) as (client_a, client_b):
        if args.command == "prepare":
            workflow = await prepare_pilot(
                client_a, client_b, run_id=uuid.uuid4().hex, numerical=args.numerical
            )
            validate_checkpoint(workflow)
            checkpoint = {
                "profile": CHECKPOINT_PROFILE,
                "endpoint_sha256": endpoint.endpoint_sha256,
                "local_simulation": args.allow_local_http,
                "workflow": workflow,
            }
            output.write_json("checkpoint.json", checkpoint)
        else:
            await verify_pilot(client_a, client_b, checkpoint["workflow"])
    return _report(
        args.command,
        numerical=checkpoint["workflow"]["numerical"],
        local_simulation=checkpoint["local_simulation"],
        proxy=proxy,
        checkpoint_sha256=hashlib.sha256(_canonical(checkpoint)).hexdigest(),
        original_numerical_reference_summary=_reference_summary(checkpoint["workflow"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare or recheck private synthetic two-account pilot evidence. "
            "Set MEASURE_PILOT_URL and MEASURE_PILOT_HEADERS_A/B in the environment. "
            "Use real client credentials, never a trusted identity header."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="Create synthetic records for two accounts")
    prepare.add_argument("--allow-synthetic-writes", action="store_true", required=True)
    prepare.add_argument("--numerical", action="store_true", help="Require R/SEC reference checks")
    verify = subparsers.add_parser(
        "verify", help="Recheck retained records and original retry receipts"
    )
    verify.add_argument("--checkpoint", required=True, help="Private checkpoint.json from prepare")
    for command in (prepare, verify):
        command.add_argument(
            "--output-dir", required=True, help="New private directory; parent exists"
        )
        command.add_argument(
            "--allow-local-http",
            action="store_true",
            help="Explicitly label a loopback HTTP simulation; never qualifies a real proxy",
        )
    args = parser.parse_args()
    # SDK/HTTP exception logging may include private URLs or response bodies.
    # CLI output is deliberately restricted to our aggregate receipt/error.
    logging.disable(logging.CRITICAL)
    output = None
    try:
        output = EvidenceDirectory(args.output_dir)
        report = asyncio.run(asyncio.wait_for(_execute(args, output), RUN_TIMEOUT_SECONDS))
        output.write_json("report.json", report)
        output.complete()
    except KeyboardInterrupt:
        print(
            "Pilot interrupted; retain the incomplete private evidence directory.", file=sys.stderr
        )
        return 130
    except Exception:
        # The private event journal records requested/responded operations;
        # disconnected writes may have committed. Never auto-repeat a failed
        # prepare or emit arbitrary exception text, headers, URLs or records.
        print(
            "Pilot checks did not complete. Check configuration and retain any incomplete "
            "private evidence; do not blindly repeat non-idempotent operations.",
            file=sys.stderr,
        )
        return 1
    finally:
        if output is not None:
            output.close()
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
