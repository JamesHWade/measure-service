"""Installed pilot client behavior, private receipts and actual local restart."""

import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest
from pilot_host_support import client_environment, free_port, synthetic_host

from measure_service.store_operations import backup_store, restore_store, verify_backup


def _run(tmp_path, port, arguments, *, overrides=None):
    env = client_environment(port)
    env.update(overrides or {})
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "measure_service.pilot_cli", *arguments],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=tmp_path,
    )


def _counts(store):
    with sqlite3.connect(store / "measure.sqlite3") as con:
        return {
            table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in (
                "datasets",
                "dataset_sources",
                "registry_receipts",
                "analyses",
                "experiments",
                "experiment_attempts",
                "experiment_results",
            )
        }


def _assert_public_report(completed, output, phase, *, numerical):
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert json.loads((output / "report.json").read_text()) == report
    assert report["phase"] == phase and report["status"] == "passed"
    assert report["transport_scope"] == "local_simulation"
    assert report["checks"]["independent_numerical_references"] == (
        "passed" if numerical else "not_run"
    )
    assert report["checks"]["anonymous_proxy_rejection"] == "observed_http_rejection"
    assert report["invitation_ready"] is False
    assert set(report["host_gates"].values()) == {"pending_operator_evidence"}
    assert not (output / "INCOMPLETE").exists()
    assert (output.stat().st_mode & 0o777) == 0o700
    for file in output.iterdir():
        assert (file.stat().st_mode & 0o777) == 0o600
    for private in (
        "synthetic-account",
        "127.0.0.1",
        str(output),
        "connect:",
        "dataset_id",
        "analysis_id",
        "Authorization",
        "synthetic-private-operator-metadata",
    ):
        assert private not in completed.stdout
    assert not completed.stderr
    return report


@pytest.mark.parametrize("numerical", [False, pytest.param(True, marks=pytest.mark.r_backend)])
def test_private_checkpoint_survives_real_proxy_process_restart_without_new_records(
    tmp_path, numerical
):
    before, after = tmp_path / "before", tmp_path / "after"
    port = free_port()
    arguments = [
        "prepare",
        "--allow-synthetic-writes",
        "--allow-local-http",
        "--output-dir",
        str(before),
    ]
    if numerical:
        arguments.append("--numerical")
    with synthetic_host(tmp_path, port, r_backend=numerical):
        prepared = _run(tmp_path, port, arguments)
        report = _assert_public_report(prepared, before, "prepare", numerical=numerical)
        counts_before = _counts(tmp_path / "store")
    assert counts_before["datasets"] == 2
    assert counts_before["analyses"] == (4 if numerical else 0)
    checkpoint_bytes = (before / "checkpoint.json").read_bytes()
    with synthetic_host(tmp_path, port, r_backend=numerical):
        verified = _run(
            tmp_path,
            port,
            [
                "verify",
                "--allow-local-http",
                "--checkpoint",
                str(before / "checkpoint.json"),
                "--output-dir",
                str(after),
            ],
        )
        verified_report = _assert_public_report(verified, after, "verify", numerical=numerical)
        assert _counts(tmp_path / "store") == counts_before
    # Both earlier processes are stopped before operator restore and cutover.
    source_database = tmp_path / "store" / "measure.sqlite3"
    source_digest = hashlib.sha256(source_database.read_bytes()).hexdigest()
    backup_store(str(tmp_path / "store"), str(tmp_path / "backup"))
    verify_backup(str(tmp_path / "backup"))
    restore_store(str(tmp_path / "backup"), str(tmp_path / "restored"))
    restored_output = tmp_path / "after-restored-cutover"
    with synthetic_host(tmp_path, port, r_backend=numerical, store=tmp_path / "restored"):
        restored = _run(
            tmp_path,
            port,
            [
                "verify",
                "--allow-local-http",
                "--checkpoint",
                str(before / "checkpoint.json"),
                "--output-dir",
                str(restored_output),
            ],
        )
        restored_report = _assert_public_report(
            restored, restored_output, "verify", numerical=numerical
        )
        assert _counts(tmp_path / "restored") == counts_before
        assert restored_report["checkpoint_sha256"] == report["checkpoint_sha256"]
    assert hashlib.sha256(source_database.read_bytes()).hexdigest() == source_digest
    assert verified_report["checkpoint_sha256"] == report["checkpoint_sha256"]
    assert (before / "checkpoint.json").read_bytes() == checkpoint_bytes
    assert verified_report["checks"]["historical_record_continuity"] == "passed"
    assert (before / "events.jsonl").stat().st_size > 0
    assert (after / "events.jsonl").stat().st_size > 0
    assert "synthetic-account" not in (before / "events.jsonl").read_text()
    if numerical:
        assert "synthetic-private-operator-metadata" in checkpoint_bytes.decode()
        assert prepared.stdout.count("gaussian_area_relative_error") == 2
        assert (
            verified_report["original_numerical_reference_summary"]
            == report["original_numerical_reference_summary"]
        )


def test_authentication_failure_keeps_private_incomplete_evidence_and_safe_error(tmp_path):
    port = free_port()
    output = tmp_path / "failure"
    with synthetic_host(tmp_path, port):
        completed = _run(
            tmp_path,
            port,
            [
                "prepare",
                "--allow-synthetic-writes",
                "--allow-local-http",
                "--output-dir",
                str(output),
            ],
            overrides={
                "MEASURE_PILOT_HEADERS_A": '{"Authorization":"Bearer PRIVATE_FAILURE_TOKEN"}'
            },
        )
    assert completed.returncode == 1 and completed.stdout == ""
    assert (output / "INCOMPLETE").is_file()
    assert not (output / "report.json").exists()
    assert not (output / "checkpoint.json").exists()
    for private in ("PRIVATE_FAILURE_TOKEN", "127.0.0.1", str(output), "Traceback"):
        assert private not in completed.stderr


def test_seed_requires_explicit_write_flag_and_help_is_r_free(tmp_path):
    completed = _run(tmp_path, 1, ["prepare", "--output-dir", str(tmp_path / "refused")])
    assert completed.returncode == 2
    assert not (tmp_path / "refused").exists()
    help_result = _run(tmp_path, 1, ["--help"])
    assert help_result.returncode == 0
    assert {"prepare", "verify"} <= set(
        help_result.stdout.replace("{", " ").replace("}", " ").replace(",", " ").split()
    )
    assert not list(tmp_path.iterdir())


def test_report_never_copies_remote_or_checkpoint_strings():
    from measure_service.pilot_cli import _report

    report = _report(
        "verify",
        numerical=True,
        local_simulation=False,
        proxy={
            "anonymous_proxy_rejection": "observed_http_rejection",
            "unverified_identity_proxy_rejection": "not_confirmed",
        },
        checkpoint_sha256="a" * 64,
    )
    assert report["invitation_ready"] is False
    assert report["checks"]["historical_record_continuity"] == "passed"
    assert report["host_gates"]["actual_host_restart"] == "pending_operator_evidence"
