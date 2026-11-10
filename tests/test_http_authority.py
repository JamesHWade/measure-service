"""HTTP authority configuration and pre-SDK header validation."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from measure_service.http_authority import http_transport_security, wrap_authority_headers

_HOSTS_ENV = "MEASURE_MCP_HTTP_ALLOWED_HOSTS"
_ORIGINS_ENV = "MEASURE_MCP_HTTP_ALLOWED_ORIGINS"


def _scope(
    headers: list[tuple[bytes, bytes]], *, scope_type: str = "http", method: str = "POST"
) -> dict:
    return {
        "type": scope_type,
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }


def _exercise(app, scope: dict) -> tuple[list[dict], bool]:
    sent: list[dict] = []
    receive_called = False

    async def receive() -> dict:
        nonlocal receive_called
        receive_called = True
        return {"type": "http.request", "body": b"request body"}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent, receive_called


def _status(messages: list[dict]) -> int:
    return next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )


def _body(messages: list[dict]) -> bytes:
    return b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )


def test_defaults_allow_only_the_documented_loopback_authorities() -> None:
    settings = http_transport_security({})

    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == [
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
        "[::1]",
        "[::1]:*",
    ]
    assert settings.allowed_origins == [
        "http://127.0.0.1",
        "http://127.0.0.1:*",
        "http://localhost",
        "http://localhost:*",
        "http://[::1]",
        "http://[::1]:*",
    ]
    assert http_transport_security({_HOSTS_ENV: "[]", _ORIGINS_ENV: "[]"}) == settings


def test_configuration_canonicalizes_hosts_and_https_origins_separately() -> None:
    settings = http_transport_security(
        {
            _HOSTS_ENV: '["MEASURE.Example.org:443", "[2001:0db8::1]:8443", "192.0.2.8:8000", "example.net:65535"]',
            _ORIGINS_ENV: '["HTTPS://Measure.Example.org:443", "https://measure.example.org:8443", "https://[2001:0db8::1]:443"]',
        }
    )

    assert settings.allowed_hosts[-4:] == [
        "measure.example.org",
        "[2001:db8::1]:8443",
        "192.0.2.8:8000",
        "example.net:65535",
    ]
    assert settings.allowed_origins[-3:] == [
        "https://measure.example.org",
        "https://measure.example.org:8443",
        "https://[2001:db8::1]",
    ]
    assert "measure.example.org" not in settings.allowed_hosts[:-4]
    assert "https://measure.example.org" not in settings.allowed_hosts


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (_HOSTS_ENV, ""),
        (_HOSTS_ENV, "null"),
        (_HOSTS_ENV, '["*.example.org"]'),
        (_HOSTS_ENV, '["example.org:*"]'),
        (_HOSTS_ENV, '["example.org", "EXAMPLE.ORG."]'),
        (_HOSTS_ENV, '["example.org", "EXAMPLE.ORG:443"]'),
        (_HOSTS_ENV, '["example.org."]'),
        (_HOSTS_ENV, '["example.org:bad"]'),
        (_HOSTS_ENV, '["example.org:65536"]'),
        (_HOSTS_ENV, '["[fe80::1%25eth0]"]'),
        (_HOSTS_ENV, '["bücher.example"]'),
        (_HOSTS_ENV, '["user@example.org"]'),
        (_HOSTS_ENV, '["example.org/path"]'),
        (_HOSTS_ENV, '["127.1"]'),
        (_HOSTS_ENV, '["[::1]", 3]'),
        (_HOSTS_ENV, "[" * 1200 + "0" + "]" * 1200),
        (_HOSTS_ENV, "[]" + " " * 8191),
        (_HOSTS_ENV, "[" + ",".join('"x%d.example"' % i for i in range(65)) + "]"),
        (_ORIGINS_ENV, ""),
        (_ORIGINS_ENV, '["http://measure.example.org"]'),
        (_ORIGINS_ENV, '["null"]'),
        (_ORIGINS_ENV, '["https://measure.example.org/path"]'),
        (_ORIGINS_ENV, '["https://measure.example.org?"]'),
        (_ORIGINS_ENV, '["https://measure.example.org#fragment"]'),
        (_ORIGINS_ENV, '["https://user@measure.example.org"]'),
        (_ORIGINS_ENV, '["https://measure.example.org:bad"]'),
        (_ORIGINS_ENV, '["https://measure.example.org", "https://MEASURE.example.org:443"]'),
        (_ORIGINS_ENV, '["https://measure.example.org:*"]'),
        (_ORIGINS_ENV, '["https://bücher.example"]'),
    ],
)
def test_invalid_configuration_fails_with_a_value_free_message(name: str, value: str) -> None:
    with pytest.raises(ValueError) as error:
        http_transport_security({name: value})

    assert name in str(error.value)
    if value:
        assert value not in str(error.value)


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"host", b"measure.example.org"), (b"host", b"measure.example.org")],
        [(b"Host", b"measure.example.org"), (b"host", b"measure.example.org")],
        [
            (b"host", b"measure.example.org"),
            (b"origin", b"https://measure.example.org"),
            (b"Origin", b"https://measure.example.org"),
        ],
        [(b"host", b"measure.example.org"), (b"origin", b"")],
        [(b"host", b"measure.example.org"), (b"origin", b"null")],
        [(b"host", b"measure.example.org, other.example.org")],
        [(b"host", b"measure.example.org.")],
        [(b"host", b"measure.example.org:bad")],
        [(b"host", b"measure.example.org:0")],
        [(b"host", b"measure.example.org:65536")],
        [(b"host", b"user@measure.example.org")],
        [(b"host", b"measure.example.org/path")],
        [(b"host", b"[fe80::1%25eth0]")],
        [(b"host", b"measure.example.org"), (b"origin", b"https://measure.example.org/mcp")],
        [
            (b"host", b"measure.example.org"),
            (b"origin", b"https://measure.example.org,https://evil.example"),
        ],
    ],
)
def test_malformed_or_ambiguous_headers_reject_before_read_or_dispatch(
    headers: list[tuple[bytes, bytes]],
) -> None:
    dispatched = False

    async def downstream(scope, receive, send) -> None:
        nonlocal dispatched
        dispatched = True

    sent, receive_called = _exercise(wrap_authority_headers(downstream), _scope(headers))

    assert _status(sent) == 400
    assert not dispatched
    assert not receive_called
    assert json.loads(_body(sent)) == {"error": "invalid Host or Origin header"}
    response_headers = dict(sent[0]["headers"])
    assert response_headers[b"cache-control"] == b"no-store"
    assert response_headers[b"content-type"] == b"application/json"


def test_valid_headers_are_canonicalized_and_forwarded_headers_are_ignored() -> None:
    seen: dict[str, object] = {}

    async def downstream(scope, receive, send) -> None:
        seen["headers"] = scope["headers"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    headers = [
        (b"HOST", b"MEASURE.Example.org:443"),
        (b"ORIGIN", b"HTTPS://Measure.Example.org:443"),
        (b"x-forwarded-host", b"other.example.org"),
        (b"x-forwarded-proto", b"http"),
    ]
    sent, receive_called = _exercise(wrap_authority_headers(downstream), _scope(headers))

    assert _status(sent) == 200
    assert not receive_called
    assert seen["headers"] == [
        (b"host", b"measure.example.org"),
        (b"origin", b"https://measure.example.org"),
        (b"x-forwarded-host", b"other.example.org"),
        (b"x-forwarded-proto", b"http"),
    ]


def test_non_http_scopes_pass_through_without_authority_headers() -> None:
    observed: list[str] = []

    async def downstream(scope, receive, send) -> None:
        observed.append(scope["type"])

    asyncio.run(wrap_authority_headers(downstream)(_scope([], scope_type="lifespan"), None, None))

    assert observed == ["lifespan"]


def test_sdk_enforces_allowlists_after_guard_canonicalization() -> None:
    async def exercise() -> tuple[list[int], list[dict]]:
        settings = http_transport_security(
            {
                _HOSTS_ENV: '["selected.example.test"]',
                _ORIGINS_ENV: '["https://selected.example.test"]',
            }
        )
        mcp = FastMCP(
            "synthetic-authority-test",
            stateless_http=True,
            json_response=True,
            transport_security=settings,
        )
        mcp_app = mcp.streamable_http_app()
        app = wrap_authority_headers(mcp_app)
        responses: list[int] = []
        async with mcp_app.router.lifespan_context(mcp_app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
            ) as client:
                cases = [
                    (
                        "SELECTED.EXAMPLE.TEST:443",
                        "HTTPS://SELECTED.EXAMPLE.TEST:443",
                        "selected.example.test",
                        200,
                    ),
                    (
                        "other.example.test",
                        "https://selected.example.test",
                        "selected.example.test",
                        421,
                    ),
                    (
                        "selected.example.test",
                        "https://other.example.test",
                        "selected.example.test",
                        403,
                    ),
                    (
                        "other.example.test",
                        "https://selected.example.test",
                        "other.example.test",
                        421,
                    ),
                ]
                for host, origin, forwarded_host, expected in cases:
                    response = await client.post(
                        "/mcp",
                        headers=[
                            ("host", host),
                            ("origin", origin),
                            ("x-forwarded-host", forwarded_host),
                            ("accept", "application/json, text/event-stream"),
                            ("content-type", "application/json"),
                        ],
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/list",
                            "params": {},
                        },
                    )
                    responses.append(response.status_code)
        return responses, settings.allowed_hosts

    statuses, allowed_hosts = asyncio.run(exercise())

    assert "selected.example.test" in allowed_hosts
    assert statuses == [200, 421, 403, 421]
