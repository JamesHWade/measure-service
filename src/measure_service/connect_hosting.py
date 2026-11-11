"""Optional adapters for hosting the HTTP app as Posit Connect content.

Connect is the authenticating proxy. Nothing here authenticates a caller; these
adapters only interpret what a trusted Connect proxy delivers:

* ``apply_connect_defaults`` is for a bundle's entry module. It turns two facts
  Connect gives every content process into service settings an operator has
  not set: the public host Connect forwards, and the R installation whose
  packages Connect restored.
* ``ConnectSessionIdentity`` asks Connect who the visitor is. It exchanges the
  ``Posit-Connect-User-Session-Token`` Connect attaches to a request for a
  visitor API key, through a Connect API integration associated with the
  content, and reads the account GUID with that key.
* ``ConnectUsernameIdentity`` serves servers whose
  ``RStudio-Connect-Credentials.user`` is a username and that have no such
  integration. It resolves the username to the account's GUID through
  Connect's users API.

Owners are always keyed on the immutable account GUID, never on a mutable
name. All three are inert unless their environment variables are set.
"""

from __future__ import annotations

import asyncio
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
REQUEST_IDENTITY_SCOPE_KEY = "measure_service.connect_identity_resolver"
LOOKUP_TIMEOUT_SECONDS = 5.0
_MAX_CREDENTIAL_BYTES = 16_384
_MAX_USERNAME_LENGTH = 256
_MAX_TOKEN_BYTES = 16_384
_USER_CLAIMS = ("guid", "username", "session-token")
_UNVERIFIED = (
    "Your Connect account could not be verified; retry, and contact the "
    "service operator if this continues"
)
# RFC 8693 token exchange, as Connect's OAuth integrations define it.
_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
_SESSION_TOKEN_TYPE = "urn:posit:connect:user-session-token"
_API_KEY_TOKEN_TYPE = "urn:posit:connect:api-key"


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
    """Return which value Connect attaches to a request names the caller."""
    claim = os.environ.get("MEASURE_MCP_CONNECT_USER_CLAIM", "guid")
    if claim not in _USER_CLAIMS:
        raise ValueError(
            "MEASURE_MCP_CONNECT_USER_CLAIM must be 'guid', 'username' or 'session-token'"
        )
    return claim


def integration_audience() -> str | None:
    """Return the integration GUID to request a visitor key from, if one is named.

    Connect needs it only when the content has more than one integration.
    """
    raw = os.environ.get("MEASURE_MCP_CONNECT_INTEGRATION")
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise ValueError("MEASURE_MCP_CONNECT_INTEGRATION must be an integration GUID") from None


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


class ConnectLookupError(Exception):
    """Connect did not confirm which account a request belongs to."""


class UsernameLookupError(ConnectLookupError):
    """A username could not be resolved to exactly one unlocked account."""


def _lookup_target(purpose: str) -> tuple[str, str]:
    """Return ``(Connect origin, API key)`` for a lookup, or explain what is missing."""
    key = os.environ.get("MEASURE_MCP_CONNECT_API_KEY") or os.environ.get("CONNECT_API_KEY")
    if not key:
        raise ConnectLookupError(f"no Connect API key is configured for {purpose}")
    try:
        origin = connect_origin()
    except ValueError as exc:
        raise ConnectLookupError(str(exc)) from None
    if origin is None:
        raise ConnectLookupError("CONNECT_SERVER is not set")
    return origin[0], key


def _connect_error_code(response: httpx.Response) -> str:
    """Describe a refusal by status and Connect's numeric error code, never its text."""
    try:
        code = response.json()["code"]
    except (ValueError, KeyError, TypeError):
        code = None
    suffix = f", Connect error code {code}" if isinstance(code, int) else ""
    return f"HTTP {response.status_code}{suffix}"


async def resolve_session_token(
    token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> uuid.UUID:
    """Return the GUID of the account Connect issued ``token`` to.

    Connect validates the token and returns an API key that acts as that
    visitor; the key is used once, to read the visitor's own account, and is
    then discarded. Nothing is cached.
    """
    origin, key = _lookup_target("the visitor key exchange")
    form = {
        "grant_type": _GRANT_TYPE,
        "subject_token_type": _SESSION_TOKEN_TYPE,
        "subject_token": token,
        "requested_token_type": _API_KEY_TOKEN_TYPE,
    }
    try:
        audience = integration_audience()
    except ValueError as exc:
        raise ConnectLookupError(str(exc)) from None
    if audience is not None:
        form["audience"] = audience
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=LOOKUP_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            exchange = await client.post(
                origin + "/__api__/v1/oauth/integrations/credentials",
                data=form,
                headers={"Authorization": "Key " + key},
            )
            if exchange.status_code != 200:
                raise ConnectLookupError(
                    "Connect refused the visitor key exchange ("
                    + _connect_error_code(exchange)
                    + ")"
                )
            try:
                visitor_key = exchange.json()["access_token"]
            except (ValueError, KeyError, TypeError):
                visitor_key = None
            if not isinstance(visitor_key, str) or not visitor_key or len(visitor_key) > 4096:
                raise ConnectLookupError("Connect returned no visitor key")
            account = await client.get(
                origin + "/__api__/v1/user", headers={"Authorization": "Key " + visitor_key}
            )
    except httpx.HTTPError as exc:
        raise ConnectLookupError(f"Connect request failed ({type(exc).__name__})") from None
    if account.status_code != 200:
        raise ConnectLookupError(
            "Connect refused the visitor key (" + _connect_error_code(account) + ")"
        )
    try:
        body = account.json()
        if body.get("locked", False) is not False:
            raise ConnectLookupError("the visitor's Connect account is locked")
        return uuid.UUID(body["guid"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ConnectLookupError("Connect returned a malformed account") from None


async def resolve_username(
    username: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> uuid.UUID:
    """Return the GUID of the one unlocked Connect account named ``username``.

    Nothing is cached. Connect can rename an account and give the name to
    someone else; a remembered mapping would hand the new holder the previous
    holder's records.
    """
    origin, key = _lookup_target("username lookup")
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=LOOKUP_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            response = await client.get(
                origin + "/__api__/v1/users",
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


def _session_token(scope: dict[str, Any]) -> str:
    values = [
        v for k, v in scope.get("headers", []) if k.lower() == b"posit-connect-user-session-token"
    ]
    if len(values) != 1 or not values[0] or len(values[0]) > _MAX_TOKEN_BYTES:
        raise ValueError("Connect session token is missing or ambiguous")
    return values[0].decode("ascii")


class _ResolvedIdentity:
    """Install a request-local resolver for a proxy claim.

    Resolution is triggered by an enabled tool call. The result, or a
    caller-safe failure reason, is attached to the ASGI scope. Requests are
    never rejected here: protocol discovery needs no principal, and a tool
    that does need one reports the failure as an MCP tool error.
    """

    missing: str

    def __init__(
        self,
        app: Callable[..., Any],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.app, self.transport = app, transport

    def _claim(self, scope: dict[str, Any]) -> str:
        raise NotImplementedError

    async def _resolve(self, claim: str) -> uuid.UUID:
        raise NotImplementedError

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        # Only this adapter may populate the keys the identity check trusts.
        scope.pop(RESOLVED_GUID_SCOPE_KEY, None)
        scope.pop(IDENTITY_ERROR_SCOPE_KEY, None)
        scope.pop(REQUEST_IDENTITY_SCOPE_KEY, None)
        scope[REQUEST_IDENTITY_SCOPE_KEY] = _RequestIdentityResolver(self, scope)
        await self.app(scope, receive, send)


class _RequestIdentityResolver:
    """Cache one identity result, including failure, inside one HTTP request."""

    def __init__(self, adapter: _ResolvedIdentity, scope: dict[str, Any]) -> None:
        self.adapter, self.scope = adapter, scope
        self.lock = asyncio.Lock()
        self.complete = False

    async def resolve(self) -> None:
        async with self.lock:
            if self.complete:
                return
            try:
                claim = self.adapter._claim(self.scope)
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                self.scope[IDENTITY_ERROR_SCOPE_KEY] = self.adapter.missing
            else:
                try:
                    guid = await self.adapter._resolve(claim)
                except ConnectLookupError as exc:
                    logger.warning("Connect identity lookup failed: %s", exc)
                    self.scope[IDENTITY_ERROR_SCOPE_KEY] = _UNVERIFIED
                except Exception as exc:
                    # An unexpected adapter/transport error must fail closed
                    # without echoing request credentials or response bodies.
                    logger.warning("Connect identity lookup failed (%s)", type(exc).__name__)
                    self.scope[IDENTITY_ERROR_SCOPE_KEY] = _UNVERIFIED
                else:
                    self.scope[RESOLVED_GUID_SCOPE_KEY] = str(guid)
            self.complete = True


async def resolve_request_identity(request: Any) -> None:
    """Resolve the current HTTP identity if a Connect adapter installed state."""
    resolver = request.scope.get(REQUEST_IDENTITY_SCOPE_KEY)
    if isinstance(resolver, _RequestIdentityResolver):
        await resolver.resolve()


class ConnectUsernameIdentity(_ResolvedIdentity):
    """Resolve the proxy's username claim through Connect's users API.

    A value spelled like a GUID is still looked up as a username, so a name
    equal to another account's GUID cannot become it.
    """

    missing = "Connect credentials are missing or ambiguous"

    def _claim(self, scope: dict[str, Any]) -> str:
        return _credential_username(scope)

    async def _resolve(self, claim: str) -> uuid.UUID:
        return await resolve_username(claim, transport=self.transport)


class ConnectSessionIdentity(_ResolvedIdentity):
    """Resolve the proxy's user session token through a Connect API integration.

    ``RStudio-Connect-Credentials`` is not read in this mode.
    """

    missing = "Connect session token is missing or ambiguous"

    def _claim(self, scope: dict[str, Any]) -> str:
        return _session_token(scope)

    async def _resolve(self, claim: str) -> uuid.UUID:
        return await resolve_session_token(claim, transport=self.transport)


def wrap_identity(app: Callable[..., Any]) -> Callable[..., Any]:
    """Add identity resolution only when this deployment declares a claim that needs it."""
    if (
        os.environ.get("MEASURE_MCP_IDENTITY", "local") != "connect"
        or os.environ.get("MEASURE_MCP_TRUST_CONNECT_HEADERS") != "1"
    ):
        return app
    claim = user_claim()
    if claim == "username":
        return ConnectUsernameIdentity(app)
    if claim == "session-token":
        integration_audience()
        return ConnectSessionIdentity(app)
    return app
