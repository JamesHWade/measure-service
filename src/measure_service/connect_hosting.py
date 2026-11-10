"""Optional adapters for hosting the HTTP app as Posit Connect content.

Connect is the authenticating proxy. Nothing here authenticates a caller; these
adapters only interpret what a trusted Connect proxy delivers:

* ``apply_connect_defaults`` is for a bundle's entry module. It turns two facts
  Connect gives every content process into service settings an operator has
  not set: the public host Connect forwards, and the R installation whose
  packages Connect restored.
* ``ConnectUsernameIdentity`` serves servers whose
  ``RStudio-Connect-Credentials.user`` is a username. It resolves the username
  to the account's immutable GUID through Connect's users API on every
  request. Owners are always keyed on the GUID, never on the mutable name.

Both are inert unless their environment variables are set.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

RESOLVED_GUID_SCOPE_KEY = "measure_service.connect_guid"
IDENTITY_ERROR_SCOPE_KEY = "measure_service.connect_identity_error"
LOOKUP_TIMEOUT_SECONDS = 5.0
_MAX_CREDENTIAL_BYTES = 16_384
_MAX_USERNAME_LENGTH = 256
_USER_CLAIMS = ("guid", "username")


def apply_connect_defaults() -> dict[str, str]:
    """Derive unset service settings from Connect's content environment.

    Call before importing ``measure_service.server``, which reads the Host
    allowlist at import. Explicit operator settings always win. Returns the
    settings this call made.

    * ``MEASURE_MCP_HTTP_ALLOWED_HOSTS``: Connect forwards its public host as
      ``Host`` and names it in ``CONNECT_SERVER``.
    * ``MEASURE_MCP_R``: Connect restores a manifest's R packages for the
      installation named by ``R_HOME``; ``R`` on ``PATH`` can be another, older
      installation that cannot load them.
    """
    applied = {}
    origin = connect_origin()
    if origin is not None and origin[0].startswith("https://"):
        applied["MEASURE_MCP_HTTP_ALLOWED_HOSTS"] = json.dumps([origin[1]])
    home = os.environ.get("R_HOME")
    if home and (Path(home) / "bin" / "R").is_file():
        applied["MEASURE_MCP_R"] = str(Path(home) / "bin" / "R")
    applied = {name: value for name, value in applied.items() if not os.environ.get(name)}
    os.environ.update(applied)
    return applied


def user_claim() -> str:
    """Return how this deployment's Connect writes the credential ``user`` field."""
    claim = os.environ.get("MEASURE_MCP_CONNECT_USER_CLAIM", "guid")
    if claim not in _USER_CLAIMS:
        raise ValueError("MEASURE_MCP_CONNECT_USER_CLAIM must be 'guid' or 'username'")
    return claim


def connect_origin() -> tuple[str, str] | None:
    """Return ``(scheme://host[:port], host[:port])`` from ``CONNECT_SERVER``, if set.

    Plain HTTP is accepted only for loopback, which keeps local simulations
    possible without allowing an API key to travel unencrypted.
    """
    raw = os.environ.get("CONNECT_SERVER")
    if not raw:
        return None
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("CONNECT_SERVER has an invalid port") from exc
    host = parsed.hostname
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("CONNECT_SERVER must be an origin URL naming a host")
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("CONNECT_SERVER must be an HTTPS URL (plain HTTP only on loopback)")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return f"{parsed.scheme}://{netloc}", netloc


class UsernameLookupError(Exception):
    """A username could not be resolved to exactly one unlocked account."""


async def resolve_username(
    username: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> uuid.UUID:
    """Return the GUID of the one unlocked Connect account named ``username``.

    Nothing is cached. Connect can rename an account and give the name to
    someone else; a remembered mapping would hand the new holder the previous
    holder's records.
    """
    key = os.environ.get("MEASURE_MCP_CONNECT_API_KEY") or os.environ.get("CONNECT_API_KEY")
    if not key:
        raise UsernameLookupError("no Connect API key is configured for username lookup")
    try:
        origin = connect_origin()
    except ValueError as exc:
        raise UsernameLookupError(str(exc)) from None
    if origin is None:
        raise UsernameLookupError("CONNECT_SERVER is not set")
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=LOOKUP_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            response = await client.get(
                origin[0] + "/__api__/v1/users",
                params={"prefix": username, "page_size": 500},
                headers={"Authorization": "Key " + key},
            )
    except httpx.HTTPError as exc:
        raise UsernameLookupError(
            f"Connect users API request failed ({type(exc).__name__})"
        ) from None
    if response.status_code != 200:
        raise UsernameLookupError(f"Connect users API returned HTTP {response.status_code}")
    try:
        matches = [
            user
            for user in response.json()["results"]
            if user["username"] == username and not user.get("locked", False)
        ]
        if len(matches) != 1:
            raise UsernameLookupError(
                f"{len(matches)} unlocked Connect users match the username exactly"
            )
        guid = uuid.UUID(matches[0]["guid"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise UsernameLookupError("Connect users API returned a malformed body") from None
    return guid


def _credential_username(scope: dict[str, Any]) -> str:
    values = [v for k, v in scope.get("headers", []) if k.lower() == b"rstudio-connect-credentials"]
    if len(values) != 1 or len(values[0]) > _MAX_CREDENTIAL_BYTES:
        raise ValueError("Connect credentials are missing or ambiguous")
    user = json.loads(values[0].decode("utf-8"))["user"]
    if not isinstance(user, str) or not user or len(user) > _MAX_USERNAME_LENGTH:
        raise ValueError("Connect credentials name no user")
    return user


class ConnectUsernameIdentity:
    """Resolve the proxy's username claim to a GUID before dispatch.

    The result, or a caller-safe failure reason, is attached to the ASGI scope.
    Requests are never rejected here: protocol discovery needs no principal,
    and a tool that does need one reports the failure as an MCP tool error
    that the caller can read. A value spelled like a GUID is still looked up
    as a username, so a name equal to another account's GUID cannot become it.
    """

    def __init__(
        self,
        app: Callable[..., Any],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.app, self.transport = app, transport

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        # Only this adapter may populate the keys the identity check trusts.
        scope.pop(RESOLVED_GUID_SCOPE_KEY, None)
        scope.pop(IDENTITY_ERROR_SCOPE_KEY, None)
        try:
            username = _credential_username(scope)
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            scope[IDENTITY_ERROR_SCOPE_KEY] = "Connect credentials are missing or ambiguous"
        else:
            try:
                guid = await resolve_username(username, transport=self.transport)
                scope[RESOLVED_GUID_SCOPE_KEY] = str(guid)
            except UsernameLookupError as exc:
                logger.warning("Connect username lookup failed: %s", exc)
                scope[IDENTITY_ERROR_SCOPE_KEY] = (
                    "Your Connect account could not be verified; retry, and contact the "
                    "service operator if this continues"
                )
        await self.app(scope, receive, send)


def wrap_identity(app: Callable[..., Any]) -> Callable[..., Any]:
    """Add username resolution only when this deployment declares that claim."""
    if (
        os.environ.get("MEASURE_MCP_IDENTITY", "local") == "connect"
        and os.environ.get("MEASURE_MCP_TRUST_CONNECT_HEADERS") == "1"
        and user_claim() == "username"
    ):
        return ConnectUsernameIdentity(app)
    return app
