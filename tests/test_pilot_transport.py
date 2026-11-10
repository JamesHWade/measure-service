"""Credential handling and bounded response behavior for the real-host adapter."""

import asyncio
import json

import httpx
import pytest

from measure_service import pilot_transport as transport


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("MEASURE_PILOT_URL", "https://example.invalid/service/mcp")
    monkeypatch.setenv("MEASURE_PILOT_HEADERS_A", '{"Authorization":"Bearer SECRET_A"}')
    monkeypatch.setenv("MEASURE_PILOT_HEADERS_B", '{"Authorization":"Bearer SECRET_B"}')


def test_private_configuration_repr_and_endpoint_binding(configured):
    endpoint = transport.endpoint_from_environment()
    assert "SECRET" not in repr(endpoint) and "example" not in repr(endpoint)
    assert len(endpoint.endpoint_sha256) == 64
    assert endpoint.headers_a != endpoint.headers_b


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid/mcp",
        "https://user:secret@example.invalid/mcp",
        "https://example.invalid/mcp?token=secret",
        "https://example.invalid/mcp#secret",
        "https://example.invalid",
        "https://example.invalid:0/mcp",
        "https://example.invalid:bad/mcp",
        "https://example.invalid/mcp\n",
        "file:///tmp/mcp",
    ],
)
def test_endpoint_rejects_unsafe_or_ambiguous_configuration(configured, monkeypatch, url):
    monkeypatch.setenv("MEASURE_PILOT_URL", url)
    with pytest.raises(transport.PilotTransportError):
        transport.endpoint_from_environment(allow_local_http=True)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_explicit_loopback_scope(configured, monkeypatch, host):
    monkeypatch.setenv("MEASURE_PILOT_URL", f"http://{host}:8000/mcp")
    with pytest.raises(transport.PilotTransportError):
        transport.endpoint_from_environment()
    assert transport.endpoint_from_environment(allow_local_http=True).url.endswith("/mcp")


@pytest.mark.parametrize(
    "value",
    [
        '{"RSTUDIO-CONNECT-CREDENTIALS":"spoof"}',
        '{"Host":"other"}',
        '{"Mcp-Session-Id":"other"}',
        '{"X-Forwarded-For":"other"}',
        '{"Authorization":"a","authorization":"b"}',
        '{"Authorization":"a","Authorization":"b"}',
        '{"Authorization":"bad\\r\\nheader"}',
        '{"Authorization":null}',
        "{}",
        "[]",
        '"bad"',
    ],
)
def test_headers_reject_identity_forgery_framing_and_duplicates(configured, monkeypatch, value):
    monkeypatch.setenv("MEASURE_PILOT_HEADERS_A", value)
    with pytest.raises(transport.PilotTransportError) as error:
        transport.endpoint_from_environment()
    assert "spoof" not in str(error.value)


def test_same_header_maps_fail_before_traffic(configured, monkeypatch):
    monkeypatch.setenv("MEASURE_PILOT_HEADERS_B", '{"authorization":"Bearer SECRET_A"}')
    with pytest.raises(transport.PilotTransportError):
        transport.endpoint_from_environment()


def test_response_byte_limit_and_compression_are_enforced(monkeypatch):
    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", 10)

    async def exercise():
        async with httpx.AsyncClient(
            transport=transport.BoundedTransport(
                httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 11))
            )
        ) as client:
            with pytest.raises(transport.PilotTransportError, match="byte limit"):
                await client.get("https://example.invalid/")
        async with httpx.AsyncClient(
            transport=transport.BoundedTransport(
                httpx.MockTransport(
                    lambda _: httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"")
                )
            )
        ) as client:
            with pytest.raises(transport.PilotTransportError, match="Compressed"):
                await client.get("https://example.invalid/")

    asyncio.run(exercise())


def test_real_client_keeps_tls_redirect_and_proxy_defaults_safe():
    async def exercise():
        async with transport._http_client({"Authorization": "Bearer SECRET"}) as client:
            assert client.follow_redirects is False
            assert client.trust_env is False
            assert client.headers["accept-encoding"] == "identity"
            assert client.timeout.connect == 10

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "observed_http_rejection"),
        (403, "observed_http_rejection"),
        (302, "not_confirmed"),
        (500, "not_confirmed"),
        (200, "not_confirmed"),
    ],
)
def test_anonymous_probe_requires_real_http_rejection(configured, monkeypatch, status, expected):
    requests = []

    def respond(request):
        requests.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(status, json={"result": {"isError": True}})

    monkeypatch.setattr(
        transport, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    report = asyncio.run(transport.probe_proxy_rejection(transport.endpoint_from_environment()))
    assert set(report.values()) == {expected}
    assert len(requests) == 2
    assert transport.TRUSTED_IDENTITY_HEADER not in requests[0].headers
    assert json.loads(requests[1].headers[transport.TRUSTED_IDENTITY_HEADER])["user"]


def test_anonymous_tool_success_blocks_before_seed(configured, monkeypatch):
    monkeypatch.setattr(
        transport,
        "_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"result": {"isError": False, "structuredContent": {}}}
                )
            )
        ),
    )
    with pytest.raises(transport.PilotTransportError, match="unauthenticated"):
        asyncio.run(transport.probe_proxy_rejection(transport.endpoint_from_environment()))


@pytest.mark.parametrize("location", ["/other-path", "https://elsewhere.invalid/mcp"])
def test_sdk_does_not_forward_credentials_or_tool_request_on_redirect(location):
    from mcp.shared._httpx_utils import stream_within_origin

    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": location})

    async def exercise():
        # Match the actual CLI factory settings. The SDK explicitly uses
        # max_redirects instead of consulting follow_redirects.
        async with transport._http_client({"Authorization": "Bearer PRIVATE"}) as client:
            assert client.max_redirects == 0
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer PRIVATE"},
            transport=httpx.MockTransport(redirect),
            follow_redirects=False,
            max_redirects=0,
        ) as client:
            async with stream_within_origin(
                client, "POST", "https://example.invalid/mcp", json={"synthetic": "tool-request"}
            ) as response:
                assert response.status_code == 307
        assert len(requests) == 1
        assert requests[0].url.path == "/mcp"

    asyncio.run(exercise())


def test_anonymous_sse_response_cannot_hide_tool_success(configured, monkeypatch):
    body = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"isError":false}}\n\n'
    monkeypatch.setattr(
        transport,
        "_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=body
                )
            )
        ),
    )
    with pytest.raises(transport.PilotTransportError, match="before writes"):
        asyncio.run(transport.probe_proxy_rejection(transport.endpoint_from_environment()))


def test_streamed_limit_closes_underlying_response(monkeypatch):
    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", 10)
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * 6
            yield b"b" * 5

        async def aclose(self):
            closed.append(True)

    async def exercise():
        async with httpx.AsyncClient(
            transport=transport.BoundedTransport(
                httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream()))
            )
        ) as client:
            with pytest.raises(transport.PilotTransportError, match="byte limit"):
                await client.get("https://example.invalid/mcp")
        assert closed

    asyncio.run(exercise())


def test_timeout_preserves_requested_operation_without_fabricated_response(monkeypatch):
    monkeypatch.setattr(transport, "CALL_TIMEOUT_SECONDS", 0.01)
    events = []

    class Journal:
        def append_event(self, value):
            events.append(value)

    class Interrupted:
        async def call_tool(self, name, arguments):
            await asyncio.Event().wait()

    async def exercise():
        recorded = transport._RecordedClient(Interrupted(), "a", Journal())
        with pytest.raises(TimeoutError):
            await recorded.call_tool("measurement_create_experiment", {"synthetic": True})

    asyncio.run(exercise())
    assert events == [
        {
            "account": "a",
            "event": "requested",
            "tool": "measurement_create_experiment",
            "arguments": {"synthetic": True},
        }
    ]
