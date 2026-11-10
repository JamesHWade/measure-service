"""Canonicalize and validate HTTP authorities before MCP transport checks.

The MCP SDK enforces the configured host and origin allowlists. This module
limits configuration to explicit authorities and rejects ambiguous ASGI
headers before the SDK sees them; it does not establish proxy identity.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

_HOSTS_ENV = "MEASURE_MCP_HTTP_ALLOWED_HOSTS"
_ORIGINS_ENV = "MEASURE_MCP_HTTP_ALLOWED_ORIGINS"
_MAX_CONFIG_CHARS = 8192
_MAX_CONFIG_ITEMS = 64
_MAX_AUTHORITY_CHARS = 260
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_NUMERIC_HOST = re.compile(r"[0-9.]+\Z")
_LEGACY_NUMERIC_HOST = re.compile(r"(?i)(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*\Z")

_LOOPBACK_HOSTS = (
    "127.0.0.1",
    "127.0.0.1:*",
    "localhost",
    "localhost:*",
    "[::1]",
    "[::1]:*",
)
_LOOPBACK_ORIGINS = (
    "http://127.0.0.1",
    "http://127.0.0.1:*",
    "http://localhost",
    "http://localhost:*",
    "http://[::1]",
    "http://[::1]:*",
)
_BAD_HEADER_BODY = b'{"error":"invalid Host or Origin header"}'
_BAD_HEADER_HEADERS = [
    (b"content-type", b"application/json"),
    (b"content-length", str(len(_BAD_HEADER_BODY)).encode("ascii")),
    (b"cache-control", b"no-store"),
]


def _invalid_authority() -> ValueError:
    return ValueError("invalid HTTP authority")


def _bad_chars(value: str) -> bool:
    return any(char.isspace() or ord(char) < 0x21 or ord(char) == 0x7F for char in value)


def _canonical_host(host: str) -> str:
    if not host or not host.isascii() or _bad_chars(host) or "%" in host:
        raise _invalid_authority()

    # Reject noncanonical numeric forms that URI clients may interpret as IPv4.
    if _NUMERIC_HOST.fullmatch(host):
        if host.count(".") != 3:
            raise _invalid_authority()
        try:
            return str(ipaddress.IPv4Address(host))
        except ipaddress.AddressValueError as exc:
            raise _invalid_authority() from exc
    if _LEGACY_NUMERIC_HOST.fullmatch(host) and any(c in host.lower() for c in "x"):
        raise _invalid_authority()

    if not host or host.endswith(".") or len(host) > 253:
        raise _invalid_authority()
    labels = host.split(".")
    if any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise _invalid_authority()
    return host.lower()


def _parse_authority(value: str) -> tuple[str, int | None]:
    """Return a normalized bracketed-or-DNS host and an optional numeric port."""
    if (
        not value
        or len(value) > _MAX_AUTHORITY_CHARS
        or not value.isascii()
        or _bad_chars(value)
        or any(char in value for char in ",@/?#\\%*")
    ):
        raise _invalid_authority()

    if value.startswith("["):
        closing = value.find("]")
        if closing < 0 or "]" in value[closing + 1 :]:
            raise _invalid_authority()
        literal = value[1:closing]
        if not literal or "%" in literal:
            raise _invalid_authority()
        try:
            host = f"[{ipaddress.IPv6Address(literal).compressed}]"
        except ipaddress.AddressValueError as exc:
            raise _invalid_authority() from exc
        suffix = value[closing + 1 :]
        if suffix and not suffix.startswith(":"):
            raise _invalid_authority()
        port_text = suffix[1:] if suffix else None
    else:
        if "[" in value or "]" in value or value.count(":") > 1:
            raise _invalid_authority()
        host_text, separator, port_text = value.partition(":")
        host = _canonical_host(host_text)
        if not separator:
            port_text = None

    if port_text is None:
        return host, None
    if not port_text or len(port_text) > 5 or any(char < "0" or char > "9" for char in port_text):
        raise _invalid_authority()
    port = int(port_text)
    if port < 1 or port > 65535:
        raise _invalid_authority()
    return host, port


def _canonical_authority(value: str) -> str:
    host, port = _parse_authority(value)
    # The service's HTTP frontend is HTTPS, so its explicit default port is
    # canonicalized the same way as an omitted port.
    return host if port is None or port == 443 else f"{host}:{port}"


def _canonical_origin(value: str, *, configured: bool = False) -> str:
    if not value or len(value) > 512 or not value.isascii() or _bad_chars(value):
        raise _invalid_authority()
    if any(char in value for char in ",\\%") or "?" in value or "#" in value:
        raise _invalid_authority()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise _invalid_authority() from exc
    scheme = parsed.scheme.lower()
    if scheme not in ({"https"} if configured else {"http", "https"}):
        raise _invalid_authority()
    if not parsed.netloc or parsed.path or parsed.query or parsed.fragment or "@" in parsed.netloc:
        raise _invalid_authority()
    host, port = _parse_authority(parsed.netloc)
    default_port = 443 if scheme == "https" else 80
    if port is None or port == default_port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _configured_values(
    environ: Mapping[str, str], name: str, parser: Callable[[str], str]
) -> list[str]:
    if name not in environ:
        return []
    raw = environ[name]
    failure = ValueError(f"{name} must be a JSON array of at most 64 valid strings")
    if not isinstance(raw, str) or len(raw) > _MAX_CONFIG_CHARS:
        raise failure
    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        raise failure from None
    if not isinstance(entries, list) or len(entries) > _MAX_CONFIG_ITEMS:
        raise failure

    normalized: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str):
            raise failure
        try:
            canonical = parser(entry)
        except ValueError:
            raise failure from None
        if canonical in seen:
            raise failure
        seen.add(canonical)
        normalized.append(canonical)
    return normalized


def http_transport_security(
    environ: Mapping[str, str] | None = None,
) -> TransportSecuritySettings:
    """Build SDK transport allowlists from bounded, explicit operator settings."""
    selected_environment = os.environ if environ is None else environ
    hosts = _configured_values(selected_environment, _HOSTS_ENV, _canonical_authority)
    origins = _configured_values(
        selected_environment,
        _ORIGINS_ENV,
        lambda value: _canonical_origin(value, configured=True),
    )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*_LOOPBACK_HOSTS, *hosts],
        allowed_origins=[*_LOOPBACK_ORIGINS, *origins],
    )


def _header_value(value: object) -> str:
    if not isinstance(value, bytes):
        raise _invalid_authority()
    try:
        return value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise _invalid_authority() from exc


async def _reject(scope: Scope, send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 400,
            "headers": _BAD_HEADER_HEADERS,
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b"" if scope.get("method") == "HEAD" else _BAD_HEADER_BODY,
            "more_body": False,
        }
    )


def wrap_authority_headers(app: ASGIApp) -> ASGIApp:
    """Reject ambiguous HTTP authority headers before dispatching to the SDK."""

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return

        try:
            headers = scope.get("headers", [])
            hosts: list[tuple[int, bytes]] = []
            origins: list[tuple[int, bytes]] = []
            normalized_headers: list[tuple[bytes, bytes]] = []
            for index, pair in enumerate(headers):
                if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                    raise _invalid_authority()
                name, value = pair
                if not isinstance(name, bytes) or not isinstance(value, bytes):
                    raise _invalid_authority()
                lowered = name.lower()
                normalized_headers.append((name, value))
                if lowered == b"host":
                    hosts.append((index, value))
                elif lowered == b"origin":
                    origins.append((index, value))

            if len(hosts) != 1 or len(origins) > 1:
                raise _invalid_authority()
            host_value = _canonical_authority(_header_value(hosts[0][1]))
            origin_value = _canonical_origin(_header_value(origins[0][1])) if origins else None
            normalized_headers[hosts[0][0]] = (b"host", host_value.encode("ascii"))
            if origins:
                normalized_headers[origins[0][0]] = (
                    b"origin",
                    origin_value.encode("ascii"),
                )
        except (TypeError, ValueError):
            await _reject(scope, send)
            return

        normalized_scope = {**scope, "headers": normalized_headers}
        await app(normalized_scope, receive, send)

    return guarded
