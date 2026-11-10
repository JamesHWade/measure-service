"""Connect hosting adapters: Host/Origin guard and username-to-GUID identity."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx
import pytest

from measure_service import connect_hosting, http_authority

ALICE = uuid.UUID("11111111-1111-4111-8111-111111111111")
BOB = uuid.UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
def connect_env(monkeypatch):
    monkeypatch.setenv("CONNECT_SERVER", "https://connect.example/")
    monkeypatch.setenv("MEASURE_MCP_CONNECT_API_KEY", "service-key")
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "connect")
    monkeypatch.setenv("MEASURE_MCP_TRUST_CONNECT_HEADERS", "1")
    monkeypatch.setenv("MEASURE_MCP_CONNECT_USER_CLAIM", "username")


def users_api(users, *, status=200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        prefix = request.url.params["prefix"]
        results = [u for u in users if u["username"].startswith(prefix)]
        return httpx.Response(status, json={"results": results})

    return httpx.MockTransport(handler)


def run_adapter(headers, transport, extra_scope=None):
    captured = {}

    async def downstream(scope, receive, send):
        captured.update(scope)

    adapter = connect_hosting.ConnectUsernameIdentity(downstream, transport=transport)
    scope = {"type": "http", "headers": headers, **(extra_scope or {})}
    asyncio.run(adapter(scope, None, None))
    return captured


def credentials(user):
    return (b"rstudio-connect-credentials", json.dumps({"user": user}).encode())


@pytest.fixture
def bare_env(monkeypatch):
    for name in ("CONNECT_SERVER", "R_HOME", "MEASURE_MCP_R", "MEASURE_MCP_HTTP_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_inert_outside_connect(bare_env):
    assert connect_hosting.apply_connect_defaults() == {}
    assert "MEASURE_MCP_HTTP_ALLOWED_HOSTS" not in os.environ
    assert "MEASURE_MCP_R" not in os.environ


@pytest.mark.parametrize(
    ("server", "hosts"),
    [
        ("https://connect.example/", ["connect.example"]),
        ("https://connect.example:8443", ["connect.example:8443"]),
        # Loopback is already admitted; plain HTTP never names a public host.
        ("http://127.0.0.1:3939/", None),
    ],
)
def test_the_connect_host_becomes_the_default_host_allowlist(bare_env, monkeypatch, server, hosts):
    monkeypatch.setenv("CONNECT_SERVER", server)
    applied = connect_hosting.apply_connect_defaults()
    if hosts is None:
        assert applied == {}
        return
    assert json.loads(applied["MEASURE_MCP_HTTP_ALLOWED_HOSTS"]) == hosts
    settings = http_authority.http_transport_security()
    assert hosts[0] in settings.allowed_hosts
    assert settings.enable_dns_rebinding_protection is True


def test_the_worker_defaults_to_the_r_connect_restored_packages_for(
    bare_env, monkeypatch, tmp_path
):
    monkeypatch.setenv("R_HOME", str(tmp_path))
    assert connect_hosting.apply_connect_defaults() == {}

    executable = tmp_path / "bin" / "R"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")
    assert connect_hosting.apply_connect_defaults() == {"MEASURE_MCP_R": str(executable)}
    assert os.environ["MEASURE_MCP_R"] == str(executable)


def test_explicit_operator_settings_win(bare_env, monkeypatch, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "R").write_text("#!/bin/sh\n")
    monkeypatch.setenv("R_HOME", str(tmp_path))
    monkeypatch.setenv("CONNECT_SERVER", "https://connect.example/")
    monkeypatch.setenv("MEASURE_MCP_R", "/operator/R")
    monkeypatch.setenv("MEASURE_MCP_HTTP_ALLOWED_HOSTS", '["measure.example"]')

    assert connect_hosting.apply_connect_defaults() == {}
    assert os.environ["MEASURE_MCP_R"] == "/operator/R"
    assert os.environ["MEASURE_MCP_HTTP_ALLOWED_HOSTS"] == '["measure.example"]'


@pytest.mark.parametrize(
    "value",
    ["http://connect.example", "https://user:pw@connect.example", "https://", "ftp://x"],
)
def test_connect_server_must_be_a_plain_https_origin(bare_env, monkeypatch, value):
    monkeypatch.setenv("CONNECT_SERVER", value)
    with pytest.raises(ValueError):
        connect_hosting.apply_connect_defaults()
    assert "MEASURE_MCP_HTTP_ALLOWED_HOSTS" not in os.environ


def test_unknown_claim_is_rejected(monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_CONNECT_USER_CLAIM", "email")
    with pytest.raises(ValueError):
        connect_hosting.user_claim()


def test_username_resolves_to_exact_unlocked_guid(connect_env):
    seen = []
    users = [
        {"username": "alice", "guid": str(ALICE)},
        {"username": "alice2", "guid": str(BOB)},
    ]
    scope = run_adapter([credentials("alice")], users_api(users, seen=seen))
    assert scope[connect_hosting.RESOLVED_GUID_SCOPE_KEY] == str(ALICE)
    assert connect_hosting.IDENTITY_ERROR_SCOPE_KEY not in scope
    assert seen[0].headers["authorization"] == "Key service-key"
    assert str(seen[0].url).startswith("https://connect.example/__api__/v1/users?")


def test_each_request_is_looked_up_again(connect_env):
    seen = []
    transport = users_api([{"username": "alice", "guid": str(ALICE)}], seen=seen)
    run_adapter([credentials("alice")], transport)
    run_adapter([credentials("alice")], transport)
    assert len(seen) == 2


def test_guid_spelled_username_is_still_looked_up(connect_env):
    users = [{"username": str(BOB), "guid": str(ALICE)}]
    scope = run_adapter([credentials(str(BOB))], users_api(users))
    assert scope[connect_hosting.RESOLVED_GUID_SCOPE_KEY] == str(ALICE)


@pytest.mark.parametrize(
    "users",
    [
        [],
        [{"username": "alice", "guid": str(ALICE), "locked": True}],
        [{"username": "alice", "guid": str(ALICE)}, {"username": "alice", "guid": str(BOB)}],
        [{"username": "Alice", "guid": str(ALICE)}],
        [{"username": "alice", "guid": "not-a-guid"}],
    ],
)
def test_ambiguous_locked_or_malformed_accounts_fail_closed(connect_env, users):
    scope = run_adapter([credentials("alice")], users_api(users))
    assert connect_hosting.RESOLVED_GUID_SCOPE_KEY not in scope
    assert "could not be verified" in scope[connect_hosting.IDENTITY_ERROR_SCOPE_KEY]


def test_api_failure_fails_closed(connect_env):
    users = [{"username": "alice", "guid": str(ALICE)}]
    scope = run_adapter([credentials("alice")], users_api(users, status=500))
    assert connect_hosting.RESOLVED_GUID_SCOPE_KEY not in scope


def test_missing_key_fails_closed(connect_env, monkeypatch):
    monkeypatch.delenv("MEASURE_MCP_CONNECT_API_KEY")
    monkeypatch.delenv("CONNECT_API_KEY", raising=False)
    scope = run_adapter(
        [credentials("alice")], users_api([{"username": "alice", "guid": str(ALICE)}])
    )
    assert connect_hosting.RESOLVED_GUID_SCOPE_KEY not in scope


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [credentials("alice"), credentials("bob")],
        [(b"rstudio-connect-credentials", b"not json")],
        [(b"rstudio-connect-credentials", b'{"user": 7}')],
        [(b"rstudio-connect-credentials", b'{"user": ""}')],
    ],
)
def test_missing_or_ambiguous_credentials_never_reach_the_api(connect_env, headers):
    seen = []
    scope = run_adapter(headers, users_api([], seen=seen))
    assert seen == []
    assert connect_hosting.RESOLVED_GUID_SCOPE_KEY not in scope


def test_caller_cannot_preseed_a_resolved_identity(connect_env):
    scope = run_adapter(
        [], users_api([]), extra_scope={connect_hosting.RESOLVED_GUID_SCOPE_KEY: str(BOB)}
    )
    assert connect_hosting.RESOLVED_GUID_SCOPE_KEY not in scope


def test_identity_wrapper_requires_explicit_username_claim(monkeypatch, connect_env):
    async def app(scope, receive, send):
        return None

    assert isinstance(connect_hosting.wrap_identity(app), connect_hosting.ConnectUsernameIdentity)
    monkeypatch.setenv("MEASURE_MCP_CONNECT_USER_CLAIM", "guid")
    assert connect_hosting.wrap_identity(app) is app
    monkeypatch.setenv("MEASURE_MCP_CONNECT_USER_CLAIM", "username")
    monkeypatch.delenv("MEASURE_MCP_TRUST_CONNECT_HEADERS")
    assert connect_hosting.wrap_identity(app) is app
