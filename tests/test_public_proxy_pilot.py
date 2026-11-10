"""Public Host/Origin admission over a synthetic authenticated proxy."""

import asyncio
import json
import sqlite3
import subprocess
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pilot_host_support import client_environment, free_port, synthetic_host

PUBLIC_HOST = "measure.example.invalid"
PUBLIC_ORIGIN = "https://workbench.example.invalid"
MOUNT_PREFIX = "/service"


def _initialize_request():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "public-proxy-contract", "version": "1"},
        },
    }


def _proxy_request(
    port,
    *,
    prefix="",
    origin=None,
    authorization="Bearer synthetic-account-a",
    extra_headers=None,
):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if authorization is not None:
        headers["Authorization"] = authorization
    if origin is not None:
        headers["Origin"] = origin
    headers.update(extra_headers or {})

    async def send():
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            return await client.post(
                f"http://127.0.0.1:{port}{prefix}/mcp",
                headers=headers,
                json=_initialize_request(),
            )

    return asyncio.run(send())


def _discover_and_create(port, *, prefix="", origin=PUBLIC_ORIGIN):
    async def exercise():
        endpoint = f"http://127.0.0.1:{port}{prefix}/mcp"
        headers = {
            "Authorization": "Bearer synthetic-account-a",
            "Origin": origin,
        }
        async with httpx.AsyncClient(headers=headers, timeout=30, trust_env=False) as http:
            async with streamable_http_client(endpoint, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    assert "measurement_create_experiment" in tools
                    created = await session.call_tool(
                        "measurement_create_experiment",
                        {
                            "spec": {
                                "title": "Synthetic public-proxy acceptance",
                                "source": "manual",
                            },
                            "idempotency_key": "public-proxy-create",
                        },
                    )
                    assert not created.isError, created.content
                    assert created.structuredContent["experiment_id"]
                    return created.structuredContent

    return asyncio.run(exercise())


def _assert_no_store(store):
    assert not store.exists(), "Authority or proxy rejection reached storage"


def test_default_allowlist_rejects_preserved_public_host_before_dispatch(tmp_path):
    port = free_port()
    store = tmp_path / "default-store"
    with synthetic_host(tmp_path, port, store=store, public_host=PUBLIC_HOST):
        response = _proxy_request(port)
    assert response.status_code == 421
    _assert_no_store(store)


def test_allowed_public_host_does_not_allow_unapproved_origin(tmp_path):
    port = free_port()
    store = tmp_path / "origin-store"
    with synthetic_host(
        tmp_path,
        port,
        store=store,
        public_host=PUBLIC_HOST,
        allowed_hosts=[PUBLIC_HOST],
    ):
        response = _proxy_request(port, origin="https://unlisted.example.invalid")
    assert response.status_code == 403
    _assert_no_store(store)


def test_allowed_origin_does_not_grant_public_host(tmp_path):
    port = free_port()
    store = tmp_path / "host-store"
    with synthetic_host(
        tmp_path,
        port,
        store=store,
        public_host=PUBLIC_HOST,
        allowed_origins=[PUBLIC_ORIGIN],
    ):
        response = _proxy_request(port, origin=PUBLIC_ORIGIN)
    assert response.status_code == 421
    _assert_no_store(store)


def test_normalized_public_authorities_allow_mcp_discovery_and_create(tmp_path):
    port = free_port()
    store = tmp_path / "normalized-store"
    with synthetic_host(
        tmp_path,
        port,
        store=store,
        public_host=PUBLIC_HOST.upper(),
        allowed_hosts=[PUBLIC_HOST],
        allowed_origins=[PUBLIC_ORIGIN],
        mount_prefix=MOUNT_PREFIX,
    ):
        result = _discover_and_create(
            port,
            prefix=MOUNT_PREFIX,
            origin="HTTPS://WORKBENCH.EXAMPLE.INVALID:443",
        )
    assert result["spec"]["title"] == "Synthetic public-proxy acceptance"
    with sqlite3.connect(store / "measure.sqlite3") as con:
        assert con.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 1


def test_nondefault_host_port_requires_exact_allowlist_entry(tmp_path):
    public_authority = f"{PUBLIC_HOST}:8443"
    port = free_port()
    store = tmp_path / "port-store"
    with synthetic_host(
        tmp_path,
        port,
        store=store,
        public_host=public_authority,
        allowed_hosts=[PUBLIC_HOST],
        allowed_origins=[PUBLIC_ORIGIN],
    ):
        rejected = _proxy_request(port, origin=PUBLIC_ORIGIN)
    assert rejected.status_code == 421
    _assert_no_store(store)

    with synthetic_host(
        tmp_path,
        port,
        store=store,
        public_host=public_authority,
        allowed_hosts=[public_authority],
        allowed_origins=[PUBLIC_ORIGIN],
    ):
        result = _discover_and_create(port)
    assert result["spec"]["title"] == "Synthetic public-proxy acceptance"
    with sqlite3.connect(store / "measure.sqlite3") as con:
        assert con.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 1


def test_forwarded_host_cannot_override_actual_unlisted_host(tmp_path):
    port = free_port()
    store = tmp_path / "forwarded-store"
    with synthetic_host(
        tmp_path,
        port,
        store=store,
        public_host="unlisted.example.invalid",
        allowed_hosts=[PUBLIC_HOST],
        allowed_origins=[PUBLIC_ORIGIN],
    ):
        response = _proxy_request(
            port,
            origin=PUBLIC_ORIGIN,
            extra_headers={"X-Forwarded-Host": PUBLIC_HOST},
        )
    assert response.status_code == 421
    _assert_no_store(store)


def test_missing_or_forged_connect_identity_is_rejected_at_proxy(tmp_path):
    port = free_port()
    store = tmp_path / "identity-store"
    with synthetic_host(
        tmp_path,
        port,
        store=store,
        public_host=PUBLIC_HOST,
        allowed_hosts=[PUBLIC_HOST],
        allowed_origins=[PUBLIC_ORIGIN],
    ):
        for headers in (
            {},
            {"RStudio-Connect-Credentials": '{"user":"00000000-0000-0000-0000-000000000001"}'},
        ):
            response = _proxy_request(
                port,
                origin=PUBLIC_ORIGIN,
                authorization=None,
                extra_headers=headers,
            )
            assert response.status_code == 401
    _assert_no_store(store)


def test_installed_cli_restart_preserves_checkpoint_and_two_account_store(tmp_path):
    port = free_port()
    store = tmp_path / "cli-store"
    before = tmp_path / "prepare"
    after = tmp_path / "verify"
    checkpoint = before / "checkpoint.json"
    environment = client_environment(port, mount_prefix=MOUNT_PREFIX, origin=PUBLIC_ORIGIN)
    environment.pop("PYTHONPATH", None)

    def run_cli(arguments):
        return subprocess.run(
            [sys.executable, "-m", "measure_service.pilot_cli", *arguments],
            capture_output=True,
            text=True,
            timeout=180,
            env=environment,
            cwd=tmp_path,
        )

    def report_for(completed, output, phase):
        assert completed.returncode == 0, completed.stderr
        report = json.loads(completed.stdout)
        assert json.loads((output / "report.json").read_text()) == report
        assert report["phase"] == phase and report["status"] == "passed"
        assert report["transport_scope"] == "local_simulation"
        assert report["invitation_ready"] is False
        assert report["checks"]["anonymous_proxy_rejection"] == "observed_http_rejection"
        assert set(report["host_gates"].values()) == {"pending_operator_evidence"}
        return report

    with synthetic_host(
        tmp_path,
        port,
        store=store,
        public_host=PUBLIC_HOST,
        allowed_hosts=[PUBLIC_HOST],
        allowed_origins=[PUBLIC_ORIGIN],
        mount_prefix=MOUNT_PREFIX,
    ):
        prepared = run_cli(
            [
                "prepare",
                "--allow-synthetic-writes",
                "--allow-local-http",
                "--output-dir",
                str(before),
            ]
        )
        prepare_report = report_for(prepared, before, "prepare")
        checkpoint_bytes = checkpoint.read_bytes()
        with sqlite3.connect(store / "measure.sqlite3") as con:
            counts_before = {
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
        assert counts_before["datasets"] == 2

    with synthetic_host(
        tmp_path,
        port,
        store=store,
        public_host=PUBLIC_HOST,
        allowed_hosts=[PUBLIC_HOST],
        allowed_origins=[PUBLIC_ORIGIN],
        mount_prefix=MOUNT_PREFIX,
    ):
        verified = run_cli(
            [
                "verify",
                "--allow-local-http",
                "--checkpoint",
                str(checkpoint),
                "--output-dir",
                str(after),
            ]
        )
        verify_report = report_for(verified, after, "verify")
        with sqlite3.connect(store / "measure.sqlite3") as con:
            counts_after = {
                table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in counts_before
            }

    assert counts_after == counts_before
    assert checkpoint.read_bytes() == checkpoint_bytes
    assert verify_report["checkpoint_sha256"] == prepare_report["checkpoint_sha256"]
    assert verify_report["checks"]["historical_record_continuity"] == "passed"
