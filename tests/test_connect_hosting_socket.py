"""Exercise username identity through the installed ASGI app over real sockets.

A loopback stand-in answers the users API. This checks the service's handling
of what a proxy delivers; it does not test a Connect server, its authentication
or its proxy configuration.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

ANALYST_A = "11111111-1111-4111-8111-111111111111"
ANALYST_B = "22222222-2222-4222-8222-222222222222"
REPLACEMENT = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def users_api():
    accounts = {"analyst-a": ANALYST_A, "analyst-b": ANALYST_B}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            requests.append((url.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != "Key synthetic-lookup-key":
                self.send_response(401)
                self.end_headers()
                return
            prefix = parse_qs(url.query)["prefix"][0]
            body = json.dumps(
                {
                    "results": [
                        {"username": name, "guid": guid, "locked": False}
                        for name, guid in accounts.items()
                        if name.startswith(prefix)
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], accounts, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def running_service(tmp_path, users_api):
    api_port, _, _ = users_api
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = dict(
        os.environ,
        MEASURE_MCP_DATA_DIR=str(tmp_path / "store"),
        MEASURE_MCP_R=str(tmp_path / "unavailable-R"),
        MEASURE_MCP_IDENTITY="connect",
        MEASURE_MCP_TRUST_CONNECT_HEADERS="1",
        MEASURE_MCP_CONNECT_USER_CLAIM="username",
        MEASURE_MCP_CONNECT_API_KEY="synthetic-lookup-key",
        CONNECT_SERVER=f"http://127.0.0.1:{api_port}/",
    )
    environment.pop("CONNECT_API_KEY", None)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "measure_service.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=tmp_path,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            assert process.poll() is None, "Service exited before accepting connections"
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                assert time.monotonic() < deadline, "Service did not start"
                time.sleep(0.02)
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def call(port, user, name, arguments, *, host=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if user is not None:
        headers["RStudio-Connect-Credentials"] = json.dumps({"user": user})
    if host is not None:
        headers["Host"] = host
    return httpx.post(
        f"http://127.0.0.1:{port}/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        timeout=10,
        trust_env=False,
    )


def create(port, user, key):
    response = call(
        port,
        user,
        "measurement_create_experiment",
        {"spec": {"title": "Synthetic identity check", "source": "manual"}, "idempotency_key": key},
    )
    assert response.status_code == 200
    return response.json()["result"]


def read(port, user, experiment_id):
    response = call(port, user, "measurement_get_experiment", {"experiment_id": experiment_id})
    assert response.status_code == 200
    return response.json()["result"]


def error_text(result):
    return " ".join(item.get("text", "") for item in result["content"])


def test_usernames_resolve_to_separate_owners(running_service, users_api):
    port = running_service
    _, _, requests = users_api
    created = create(port, "analyst-a", "a-1")
    assert created["isError"] is False
    experiment_id = created["structuredContent"]["experiment_id"]

    assert read(port, "analyst-a", experiment_id)["isError"] is False
    assert read(port, "analyst-b", experiment_id)["isError"] is True
    assert all(path == "/__api__/v1/users" for path, _ in requests)
    assert all(key == "Key synthetic-lookup-key" for _, key in requests)
    assert len(requests) == 3, "Each request must look the account up again"


def test_a_reassigned_username_does_not_inherit_records(running_service, users_api):
    port = running_service
    _, accounts, _ = users_api
    created = create(port, "analyst-a", "a-1")
    experiment_id = created["structuredContent"]["experiment_id"]

    # The name now belongs to a different account; the original account was renamed.
    accounts["analyst-a"] = REPLACEMENT
    accounts["analyst-a-renamed"] = ANALYST_A
    assert read(port, "analyst-a", experiment_id)["isError"] is True
    assert read(port, "analyst-a-renamed", experiment_id)["isError"] is False


def test_unresolved_accounts_get_a_readable_tool_error(running_service, users_api):
    port = running_service
    _, accounts, _ = users_api
    for user in ("no-such-account", ANALYST_A):
        result = create(port, user, "never-stored")
        assert result["isError"] is True
        assert "could not be verified" in error_text(result)
    # A GUID in the header is a username in this mode, never a principal.
    accounts[ANALYST_B] = ANALYST_A
    created = create(port, "analyst-a", "a-1")
    experiment_id = created["structuredContent"]["experiment_id"]
    assert read(port, ANALYST_B, experiment_id)["isError"] is False
    assert read(port, "analyst-b", experiment_id)["isError"] is True

    missing = create(port, None, "never-stored")
    assert missing["isError"] is True
    assert "missing or ambiguous" in error_text(missing)


def test_discovery_needs_no_account_and_foreign_hosts_are_refused(running_service):
    port = running_service
    listing = httpx.post(
        f"http://127.0.0.1:{port}/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        timeout=10,
        trust_env=False,
    )
    assert listing.status_code == 200
    assert listing.json()["result"]["tools"]

    refused = call(port, "analyst-a", "measurement_registry_capabilities", {}, host="other.example")
    assert refused.status_code == 421
