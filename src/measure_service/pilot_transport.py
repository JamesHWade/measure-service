"""Bounded HTTP adapter for an operator's two independently authenticated clients."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MAX_RESPONSE_BYTES = 8_000_000
CALL_TIMEOUT_SECONDS = 120
TRUSTED_IDENTITY_HEADER = "rstudio-connect-credentials"
_BLOCKED_HEADERS = {
    TRUSTED_IDENTITY_HEADER,
    "host",
    "content-length",
    "content-type",
    "transfer-encoding",
    "connection",
    "accept",
    "accept-encoding",
    "mcp-session-id",
    "mcp-protocol-version",
    "last-event-id",
    "forwarded",
}


class PilotTransportError(ValueError):
    """A fixed safe message; never includes credentials or remote exceptions."""


@dataclass(frozen=True)
class PilotEndpoint:
    # repr=False prevents accidental credential or endpoint disclosure in errors.
    url: str
    headers_a: dict[str, str]
    headers_b: dict[str, str]

    def __repr__(self) -> str:
        return "PilotEndpoint(<private configuration>)"

    @property
    def endpoint_sha256(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise PilotTransportError("Authentication headers must be unique")
        value[name] = item
    return value


def _headers(encoded: str | None) -> dict[str, str]:
    if not encoded or len(encoded) > 32_768:
        raise PilotTransportError("Provide both authentication header maps in the environment")
    try:
        value = json.loads(encoded, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise PilotTransportError("Authentication headers must be bounded JSON objects") from exc
    if not isinstance(value, dict) or not 1 <= len(value) <= 16:
        raise PilotTransportError("Authentication headers must be bounded JSON objects")
    result: dict[str, str] = {}
    for name, header in value.items():
        if (
            not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}", name)
            or name.lower() in result
            or name.lower() in _BLOCKED_HEADERS
            or name.lower().startswith("x-forwarded-")
            or not isinstance(header, str)
            or not 1 <= len(header) <= 8192
            or any(ord(char) < 32 or ord(char) > 126 for char in header)
        ):
            raise PilotTransportError("Authentication header configuration is not permitted")
        result[name.lower()] = header
    return result


def endpoint_from_environment(*, allow_local_http: bool = False) -> PilotEndpoint:
    """Load one exact MCP endpoint and two credential maps without logging them."""
    url = os.environ.get("MEASURE_PILOT_URL", "")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise PilotTransportError("Provide a valid MCP endpoint in the environment") from exc
    if (
        not 1 <= len(url) <= 2048
        or any(ord(char) < 33 or ord(char) > 126 for char in url)
        or parts.username is not None
        or parts.password is not None
        or not parts.hostname
        or parts.query
        or parts.fragment
        or not parts.path.startswith("/")
        or parts.scheme not in ("http", "https")
        or port == 0
    ):
        raise PilotTransportError(
            "Provide a valid MCP endpoint without userinfo, query or fragment"
        )
    if parts.scheme != "https":
        try:
            is_loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            is_loopback = parts.hostname == "localhost"
        if not allow_local_http or not is_loopback:
            raise PilotTransportError("HTTPS is required except for explicit local simulations")
    headers_a = _headers(os.environ.get("MEASURE_PILOT_HEADERS_A"))
    headers_b = _headers(os.environ.get("MEASURE_PILOT_HEADERS_B"))
    if headers_a == headers_b:
        raise PilotTransportError(
            "Provide independent authentication credentials for both accounts"
        )
    return PilotEndpoint(url, headers_a, headers_b)


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream):
        self.stream = stream

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise PilotTransportError("The remote response exceeded the pilot byte limit")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class BoundedTransport(httpx.AsyncBaseTransport):
    """Bound raw response bytes and reject compression before SDK decoding."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport or httpx.AsyncHTTPTransport(
            retries=0,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.transport.handle_async_request(request)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise PilotTransportError("Compressed pilot responses are not supported")
        if response.is_stream_consumed and len(response.content) > MAX_RESPONSE_BYTES:
            await response.aclose()
            raise PilotTransportError("The remote response exceeded the pilot byte limit")
        response.stream = _BoundedStream(response.stream)
        return response

    async def aclose(self):
        await self.transport.aclose()


def _http_client(headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"Accept-Encoding": "identity", **(headers or {})},
        transport=BoundedTransport(),
        timeout=httpx.Timeout(CALL_TIMEOUT_SECONDS, connect=10),
        follow_redirects=False,
        max_redirects=0,
        trust_env=False,
    )


async def probe_proxy_rejection(endpoint: PilotEndpoint) -> dict[str, str]:
    """Observe 401/403 only; generic MCP errors/redirects remain inconclusive."""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "measurement_registry_capabilities", "arguments": {}},
    }
    observations: dict[str, str] = {}
    async with _http_client() as client:
        for label, headers in (
            ("anonymous_proxy_rejection", {}),
            (
                "unverified_identity_proxy_rejection",
                {TRUSTED_IDENTITY_HEADER: '{"user":"d9b77890-bce8-4682-8815-e05307590f0f"}'},
            ),
        ):
            response = await client.post(
                endpoint.url,
                headers={"Accept": "application/json, text/event-stream", **headers},
                json=request,
            )
            if response.status_code in (401, 403):
                observations[label] = "observed_http_rejection"
                continue
            if response.status_code == 200 and response.headers.get(
                "content-type", ""
            ).lower().startswith("text/event-stream"):
                raise PilotTransportError(
                    "An anonymous event stream requires manual proxy verification before writes"
                )
            # Fail before creating records when an unauthenticated request
            # demonstrably reaches an application tool. Other results do not
            # establish an authentication boundary.
            try:
                body = response.json()
            except ValueError:
                body = None
            if (
                response.status_code == 200
                and isinstance(body, dict)
                and isinstance(body.get("result"), dict)
                and body["result"].get("isError", False) is False
            ):
                raise PilotTransportError("An unauthenticated request reached an MCP tool")
            observations[label] = "not_confirmed"
    return observations


class _RecordedClient:
    def __init__(self, client: ClientSession, account: str, journal: Any):
        self.client, self.account, self.journal = client, account, journal

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        # Journal the intent first, so an ambiguous disconnect retains exactly
        # which synthetic operation may have committed. No automatic retries.
        if self.journal is not None:
            self.journal.append_event(
                {
                    "account": self.account,
                    "event": "requested",
                    "tool": name,
                    "arguments": arguments,
                }
            )
        response = await asyncio.wait_for(
            self.client.call_tool(name, arguments), timeout=CALL_TIMEOUT_SECONDS
        )
        if self.journal is not None:
            self.journal.append_event(
                {
                    "account": self.account,
                    "event": "responded",
                    "tool": name,
                    "response": response.model_dump(mode="json"),
                }
            )
        return response


@asynccontextmanager
async def pilot_sessions(endpoint: PilotEndpoint, *, journal: Any = None):
    """Open two independent sessions; credentials never enter the event journal."""
    async with AsyncExitStack() as stack:
        clients = []
        for account, headers in (("a", endpoint.headers_a), ("b", endpoint.headers_b)):
            http = await stack.enter_async_context(_http_client(headers))
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(endpoint.url, http_client=http)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=CALL_TIMEOUT_SECONDS)
            clients.append(_RecordedClient(session, account, journal))
        yield tuple(clients)
