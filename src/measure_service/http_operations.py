"""Bound HTTP requests and expose small process-local health endpoints.

The request envelope buffers every ordinary HTTP request before dispatch. Its
limits are process-local and do not provide rate limiting, a storage quota, or
authentication. Health responses report only service liveness or the result of
an injected readiness check; neither proves proxy authentication, writable
storage, or numerical-backend readiness.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

DEFAULT_MAX_BODY_BYTES = 8_000_000
DEFAULT_MAX_IN_FLIGHT = 16
DEFAULT_BODY_TIMEOUT_SECONDS = 30.0
_READINESS_TIMEOUT_SECONDS = 1.0
_RETRY_AFTER_SECONDS = b"1"


class _MalformedRequest(Exception):
    """The request framing is invalid."""


class _BodyTooLarge(Exception):
    """The measured request body exceeded its configured limit."""


class _ClientDisconnected(Exception):
    """The client disconnected before the request body was complete."""


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_finite_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("body_timeout_seconds must be a positive finite number")
    try:
        seconds = float(value)
    except OverflowError as exc:
        raise ValueError("body_timeout_seconds must be a positive finite number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("body_timeout_seconds must be a positive finite number")
    return seconds


def _content_length(headers: list[tuple[bytes, bytes]], max_body_bytes: int) -> int | None:
    values = [value for name, value in headers if name.lower() == b"content-length"]
    if not values:
        return None
    if len(values) != 1:
        raise _MalformedRequest

    value = values[0].strip(b" \t")
    if not value or any(byte < 48 or byte > 57 for byte in value):
        raise _MalformedRequest

    normalized = value.lstrip(b"0") or b"0"
    declared_length = 0
    for byte in normalized:
        declared_length = declared_length * 10 + byte - 48
        if declared_length > max_body_bytes:
            raise _BodyTooLarge
    return declared_length


async def _read_request_body(
    receive: Callable[[], Any],
    max_body_bytes: int,
    declared_length: int | None,
    timeout_seconds: float,
) -> bytes:
    body_buffer = bytearray()
    measured_bytes = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise _BodyTimedOut
        try:
            message = await asyncio.wait_for(receive(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise _BodyTimedOut from exc
        if loop.time() >= deadline:
            raise _BodyTimedOut
        message_type = message.get("type")
        if message_type == "http.disconnect":
            raise _ClientDisconnected
        if message_type != "http.request":
            raise _MalformedRequest

        body = message.get("body", b"")
        if not isinstance(body, bytes):
            raise _MalformedRequest
        measured_bytes += len(body)
        if measured_bytes > max_body_bytes:
            raise _BodyTooLarge
        if body:
            body_buffer.extend(body)

        if not message.get("more_body", False):
            if declared_length is not None and measured_bytes != declared_length:
                raise _MalformedRequest
            return bytes(body_buffer)


class _BodyTimedOut(Exception):
    """The total request body receive deadline expired."""


class _Capacity:
    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self._maximum:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1


class _ReadinessProbe:
    """Coalesce readiness callers around at most one blocking checker thread."""

    def __init__(self, checker: Callable[[], bool]) -> None:
        self._checker = checker
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="measure-ready")
        self._lock = threading.Lock()
        self._future: Future[bool] | None = None

    def _clear_when_done(self, completed: Future[bool]) -> None:
        with self._lock:
            if self._future is completed:
                self._future = None

    async def is_ready(self) -> bool:
        add_callback = False
        with self._lock:
            future = self._future
            if future is None:
                future = self._executor.submit(self._checker)
                self._future = future
                add_callback = True
        if add_callback:
            future.add_done_callback(self._clear_when_done)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _READINESS_TIMEOUT_SECONDS
        while not future.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(remaining, 0.01))

        try:
            result = future.result()
        except Exception:
            return False
        return result is True


def _json_response(
    status: int, payload: dict[str, str], *, extra_headers: tuple[tuple[bytes, bytes], ...] = ()
) -> tuple[list[tuple[bytes, bytes]], bytes]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
        *extra_headers,
    ]
    return headers, body


async def _send_json(
    scope: dict[str, Any],
    send: Callable[[dict[str, Any]], Any],
    status: int,
    payload: dict[str, str],
    *,
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> None:
    headers, body = _json_response(status, payload, extra_headers=extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send(
        {
            "type": "http.response.body",
            "body": b"" if scope.get("method") == "HEAD" else body,
            "more_body": False,
        }
    )


async def _handle_health(
    scope: dict[str, Any],
    send: Callable[[dict[str, Any]], Any],
    readiness: _ReadinessProbe | None,
) -> None:
    method = scope.get("method")
    if method not in {"GET", "HEAD"}:
        await _send_json(
            scope,
            send,
            405,
            {"error": "method_not_allowed"},
            extra_headers=((b"allow", b"GET, HEAD"),),
        )
        return

    if scope.get("path") == "/health/live":
        await _send_json(scope, send, 200, {"status": "live"})
        return

    ready = await readiness.is_ready() if readiness is not None else False
    await _send_json(
        scope,
        send,
        200 if ready else 503,
        {"status": "ready" if ready else "not_ready"},
    )


async def _send_request_error(
    scope: dict[str, Any],
    send: Callable[[dict[str, Any]], Any],
    status: int,
    error: str,
    *,
    retry_after: bool = False,
) -> None:
    extra_headers = ((b"retry-after", _RETRY_AFTER_SECONDS),) if retry_after else ()
    await _send_json(scope, send, status, {"error": error}, extra_headers=extra_headers)


def wrap_http(
    app: Callable[..., Any],
    *,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    body_timeout_seconds: int | float = DEFAULT_BODY_TIMEOUT_SECONDS,
    readiness_check: Callable[[], bool] | None = None,
) -> Callable[..., Any]:
    """Wrap an ASGI app with a bounded HTTP request envelope and health routes.

    All HTTP routes except the two health endpoints reserve one process-local
    slot before reading the request body. Body bytes are counted from ASGI
    request messages and buffered for replay before application dispatch. The
    absolute receive deadline starts when body reading begins. The slot remains
    held until the downstream ASGI call returns, including streamed responses
    and any downstream cleanup after the final response message.

    Non-HTTP scopes are passed through unchanged. The fixed health responses
    bypass request capacity; an optional readiness checker is coalesced to one
    blocking worker and has a one-second response deadline. The checker result
    is an operator-supplied signal, not an authentication or full-service
    qualification claim.
    """
    body_limit = _positive_int("max_body_bytes", max_body_bytes)
    capacity_limit = _positive_int("max_in_flight", max_in_flight)
    body_timeout = _positive_finite_seconds(body_timeout_seconds)
    readiness = _ReadinessProbe(readiness_check) if readiness_check is not None else None
    capacity = _Capacity(capacity_limit)

    async def bounded_app(
        scope: dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return

        path = scope.get("path")
        if path in {"/health/live", "/health/ready"}:
            await _handle_health(scope, send, readiness)
            return

        if not capacity.try_acquire():
            await _send_request_error(
                scope,
                send,
                503,
                "capacity_exhausted",
                retry_after=True,
            )
            return

        release_guard = threading.Lock()
        released = False

        def release_once() -> None:
            nonlocal released
            with release_guard:
                if released:
                    return
                released = True
            capacity.release()

        try:
            try:
                declared_length = _content_length(scope.get("headers", []), body_limit)
                request_body = await _read_request_body(
                    receive,
                    body_limit,
                    declared_length,
                    body_timeout,
                )
            except _MalformedRequest:
                await _send_request_error(scope, send, 400, "invalid_request")
                return
            except _BodyTooLarge:
                await _send_request_error(scope, send, 413, "request_too_large")
                return
            except _BodyTimedOut:
                await _send_request_error(scope, send, 408, "request_timeout")
                return
            except _ClientDisconnected:
                return

            replayed_body = False

            async def replay_receive() -> dict[str, Any]:
                nonlocal replayed_body
                if not replayed_body:
                    replayed_body = True
                    return {
                        "type": "http.request",
                        "body": request_body,
                        "more_body": False,
                    }
                return await receive()

            await app(scope, replay_receive, send)
        finally:
            release_once()

    return bounded_app
