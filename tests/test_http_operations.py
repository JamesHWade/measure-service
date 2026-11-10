from __future__ import annotations

import asyncio
import json
import threading

import pytest

import measure_service.http_operations as http_operations
from measure_service.http_operations import wrap_http


def _scope(
    *,
    path: str = "/mcp",
    method: str = "POST",
    headers: list[tuple[bytes, bytes]] | None = None,
    scope_type: str = "http",
) -> dict:
    return {
        "type": scope_type,
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }


async def _call_async(
    app,
    messages: list[dict],
    *,
    path: str = "/mcp",
    method: str = "POST",
    headers: list[tuple[bytes, bytes]] | None = None,
    scope_type: str = "http",
) -> list[dict]:
    pending = list(messages)
    sent: list[dict] = []

    async def receive() -> dict:
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(
        _scope(path=path, method=method, headers=headers, scope_type=scope_type),
        receive,
        send,
    )
    return sent


def _call(app, messages: list[dict], **kwargs) -> list[dict]:
    return asyncio.run(_call_async(app, messages, **kwargs))


def _status(messages: list[dict]) -> int:
    return next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )


def _body(messages: list[dict]) -> bytes:
    return b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )


def _response_headers(messages: list[dict]) -> dict[bytes, bytes]:
    start = next(message for message in messages if message["type"] == "http.response.start")
    return dict(start["headers"])


def _downstream_response(body: bytes = b"ok"):
    async def app(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return app


def test_constructor_requires_strict_positive_finite_values() -> None:
    for bad in (True, 0, -1, 1.5, "3"):
        with pytest.raises((TypeError, ValueError)):
            wrap_http(_downstream_response(), max_body_bytes=bad)
    for bad in (True, 0, -1, 1.5, "3"):
        with pytest.raises((TypeError, ValueError)):
            wrap_http(_downstream_response(), max_in_flight=bad)
    for bad in (True, 0, -1, float("inf"), float("nan"), "3"):
        with pytest.raises((TypeError, ValueError)):
            wrap_http(_downstream_response(), body_timeout_seconds=bad)
    with pytest.raises(ValueError, match="finite"):
        wrap_http(_downstream_response(), body_timeout_seconds=10**1000)


def test_fragmented_body_is_measured_and_replayed_without_synthetic_messages() -> None:
    received: list[dict] = []

    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            received.append(message)
            if message["type"] == "http.disconnect" or not message.get("more_body", False):
                break
        received.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})

    wrapped = wrap_http(downstream, max_body_bytes=5)
    response = _call(
        wrapped,
        [
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": b"he", "more_body": True},
            {"type": "http.request", "body": b"llo", "more_body": False},
            {"type": "http.disconnect"},
        ],
        headers=[(b"content-length", b"5")],
    )

    assert _status(response) == 200
    assert received[0] == {
        "type": "http.request",
        "body": b"hello",
        "more_body": False,
    }
    assert received[1] == {"type": "http.disconnect"}


@pytest.mark.parametrize(
    ("headers", "messages"),
    [
        ([(b"content-length", b"1"), (b"content-length", b"1")], []),
        ([(b"content-length", b"nope")], []),
        ([(b"content-length", b"-1")], []),
        ([(b"content-length", b"2")], [{"type": "http.request", "body": b"x"}]),
    ],
)
def test_bad_or_disagreeing_content_length_rejects_before_dispatch(headers, messages) -> None:
    dispatched = False

    async def tripwire(scope, receive, send):
        nonlocal dispatched
        dispatched = True

    wrapped = wrap_http(tripwire, max_body_bytes=8)
    response = _call(wrapped, messages, headers=headers)

    assert _status(response) == 400
    assert not dispatched
    assert b"/mcp" not in _body(response)


def test_oversized_declared_content_length_rejects_before_reading_body() -> None:
    receive_called = False

    async def receive():
        nonlocal receive_called
        receive_called = True
        return {"type": "http.request", "body": b""}

    async def tripwire(scope, receive, send):
        raise AssertionError("oversized declarations must be rejected before dispatch")

    wrapped = wrap_http(tripwire, max_body_bytes=8)
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def run() -> None:
        await wrapped(
            _scope(headers=[(b"content-length", b"100000000000000000000000000000000000")]),
            receive,
            send,
        )

    asyncio.run(run())
    assert _status(sent) == 413
    assert not receive_called


def test_measured_limit_accepts_exact_bytes_and_rejects_first_extra_byte() -> None:
    accepted = wrap_http(_downstream_response(), max_body_bytes=4)
    assert _status(_call(accepted, [{"type": "http.request", "body": b"1234"}])) == 200

    for headers in ([], [(b"content-length", b"1")]):
        dispatched = False

        async def tripwire(scope, receive, send):
            nonlocal dispatched
            dispatched = True

        wrapped = wrap_http(tripwire, max_body_bytes=4)
        response = _call(
            wrapped,
            [
                {"type": "http.request", "body": b"12", "more_body": True},
                {"type": "http.request", "body": b"345", "more_body": False},
            ],
            headers=headers,
        )
        assert _status(response) == 413
        assert not dispatched
        assert b"12345" not in _body(response)


def test_all_ordinary_http_paths_are_bounded() -> None:
    dispatched = False

    async def tripwire(scope, receive, send):
        nonlocal dispatched
        dispatched = True

    wrapped = wrap_http(tripwire, max_body_bytes=1)
    response = _call(
        wrapped,
        [{"type": "http.request", "body": b"too long"}],
        path="/alternate-route",
    )

    assert _status(response) == 413
    assert not dispatched


def test_receive_timeout_is_total_and_does_not_dispatch(monkeypatch) -> None:
    dispatched = False

    async def slow_receive():
        await asyncio.sleep(0.1)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def tripwire(scope, receive, send):
        nonlocal dispatched
        dispatched = True

    wrapped = wrap_http(tripwire, body_timeout_seconds=0.02)
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def run() -> None:
        await wrapped(_scope(), slow_receive, send)

    asyncio.run(run())
    assert _status(sent) == 408
    assert not dispatched
    assert _response_headers(sent)[b"cache-control"] == b"no-store"


def test_disconnect_before_body_completion_never_dispatches_or_sends_fake_error() -> None:
    dispatched = False

    async def tripwire(scope, receive, send):
        nonlocal dispatched
        dispatched = True

    wrapped = wrap_http(tripwire)
    response = _call(
        wrapped,
        [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ],
    )

    assert response == []
    assert not dispatched


def test_capacity_is_non_queueing_and_holds_during_streaming_response() -> None:
    started = asyncio.Event()
    finish_stream = asyncio.Event()

    async def downstream(scope, receive, send):
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"part", "more_body": True})
        started.set()
        await finish_stream.wait()
        await send({"type": "http.response.body", "body": b"end", "more_body": False})

    wrapped = wrap_http(downstream, max_in_flight=1)

    async def run() -> None:
        first = asyncio.create_task(_call_async(wrapped, [{"type": "http.request", "body": b"x"}]))
        await started.wait()
        rejected = await _call_async(
            wrapped,
            [{"type": "http.request", "body": b"unread"}],
            path="/secret/path",
        )
        assert _status(rejected) == 503
        assert _response_headers(rejected)[b"retry-after"] == b"1"
        assert b"/secret/path" not in _body(rejected)
        finish_stream.set()
        first_response = await first
        assert _status(first_response) == 200
        assert [
            message for message in first_response if message["type"] == "http.response.body"
        ] == [
            {"type": "http.response.body", "body": b"part", "more_body": True},
            {"type": "http.response.body", "body": b"end", "more_body": False},
        ]

    asyncio.run(run())


def test_capacity_remains_held_after_final_response_until_downstream_returns() -> None:
    sent_final = asyncio.Event()
    return_from_app = asyncio.Event()
    calls = 0

    async def downstream(scope, receive, send):
        nonlocal calls
        calls += 1
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done", "more_body": False})
        if calls == 1:
            sent_final.set()
            await return_from_app.wait()

    wrapped = wrap_http(downstream, max_in_flight=1)

    async def run() -> None:
        first = asyncio.create_task(_call_async(wrapped, [{"type": "http.request", "body": b""}]))
        await sent_final.wait()
        rejected = await _call_async(wrapped, [{"type": "http.request", "body": b""}])
        assert _status(rejected) == 503
        return_from_app.set()
        assert _status(await first) == 200

    asyncio.run(run())


def test_cancellation_releases_capacity() -> None:
    started = asyncio.Event()
    block = asyncio.Event()
    calls = 0

    async def downstream(scope, receive, send):
        nonlocal calls
        calls += 1
        await receive()
        if calls == 1:
            started.set()
            await block.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = wrap_http(downstream, max_in_flight=1)

    async def run() -> None:
        first = asyncio.create_task(_call_async(wrapped, [{"type": "http.request", "body": b""}]))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = await _call_async(wrapped, [{"type": "http.request", "body": b""}])
        assert _status(second) == 200

    asyncio.run(run())


def test_downstream_exception_propagates_and_releases_capacity() -> None:
    calls = 0

    async def downstream(scope, receive, send):
        nonlocal calls
        calls += 1
        await receive()
        if calls == 1:
            raise RuntimeError("upstream failure")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = wrap_http(downstream, max_in_flight=1)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="upstream failure"):
            await _call_async(wrapped, [{"type": "http.request", "body": b""}])
        response = await _call_async(wrapped, [{"type": "http.request", "body": b""}])
        assert _status(response) == 200

    asyncio.run(run())


def test_health_routes_are_minimal_and_head_preserves_get_length() -> None:
    async def unexpected(scope, receive, send):
        raise AssertionError("health requests must bypass the downstream app")

    wrapped = wrap_http(unexpected)
    live_get = _call(wrapped, [], path="/health/live", method="GET")
    live_head = _call(wrapped, [], path="/health/live", method="HEAD")
    ready_get = _call(wrapped, [], path="/health/ready", method="GET")
    ready_head = _call(wrapped, [], path="/health/ready", method="HEAD")

    assert _status(live_get) == 200
    assert json.loads(_body(live_get)) == {"status": "live"}
    assert _status(live_head) == 200
    assert _body(live_head) == b""
    assert (
        _response_headers(live_head)[b"content-length"]
        == _response_headers(live_get)[b"content-length"]
    )
    assert _status(ready_get) == 503
    assert json.loads(_body(ready_get)) == {"status": "not_ready"}
    assert _status(ready_head) == 503
    assert _body(ready_head) == b""
    assert (
        _response_headers(ready_head)[b"content-length"]
        == _response_headers(ready_get)[b"content-length"]
    )
    for response in (live_get, live_head, ready_get, ready_head):
        assert _response_headers(response)[b"cache-control"] == b"no-store"
        assert b"/health" not in _body(response)


def test_health_method_must_be_get_or_head() -> None:
    called = False

    def checker() -> bool:
        nonlocal called
        called = True
        return True

    wrapped = wrap_http(_downstream_response(), readiness_check=checker)
    response = _call(wrapped, [], path="/health/ready", method="POST")

    assert _status(response) == 405
    assert json.loads(_body(response)) == {"error": "method_not_allowed"}
    assert _response_headers(response)[b"allow"] == b"GET, HEAD"
    assert not called


def test_readiness_checker_can_report_ready_without_leaking_details() -> None:
    wrapped = wrap_http(_downstream_response(), readiness_check=lambda: True)
    response = _call(wrapped, [], path="/health/ready", method="GET")

    assert _status(response) == 200
    assert json.loads(_body(response)) == {"status": "ready"}
    assert b"/health" not in _body(response)


def test_readiness_timeout_coalesces_and_keeps_probe_busy_until_checker_returns(
    monkeypatch,
) -> None:
    monkeypatch.setattr(http_operations, "_READINESS_TIMEOUT_SECONDS", 0.1)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0

    def checker() -> bool:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        finished.set()
        return True

    wrapped = wrap_http(_downstream_response(), readiness_check=checker)
    try:

        async def run() -> tuple[list[dict], list[dict]]:
            first = await _call_async(wrapped, [], path="/health/ready", method="GET")
            second = await _call_async(wrapped, [], path="/health/ready", method="GET")
            return first, second

        first, second = asyncio.run(run())
        assert _status(first) == 503
        assert _status(second) == 503
        assert started.is_set()
        assert calls == 1
    finally:
        release.set()
        assert finished.wait(timeout=1)


def test_empty_body_fragments_cannot_extend_total_receive_deadline() -> None:
    received = 0
    dispatched = False

    async def empty_receive():
        nonlocal received
        received += 1
        return {"type": "http.request", "body": b"", "more_body": True}

    async def tripwire(scope, receive, send):
        nonlocal dispatched
        dispatched = True

    wrapped = wrap_http(tripwire, body_timeout_seconds=0.01)
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def run() -> None:
        await wrapped(_scope(), empty_receive, send)

    asyncio.run(run())
    assert _status(sent) == 408
    assert received > 0
    assert not dispatched


def test_periodic_body_fragments_share_one_total_deadline() -> None:
    received = 0
    dispatched = False

    async def slow_fragments():
        nonlocal received
        received += 1
        await asyncio.sleep(0.02)
        return {"type": "http.request", "body": b"x", "more_body": True}

    async def tripwire(scope, receive, send):
        nonlocal dispatched
        dispatched = True

    wrapped = wrap_http(tripwire, max_body_bytes=10, body_timeout_seconds=0.05)
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def run() -> None:
        await wrapped(_scope(), slow_fragments, send)

    asyncio.run(run())
    assert _status(sent) == 408
    assert received < 4
    assert not dispatched


def test_liveness_bypasses_a_slot_held_by_a_slow_upload() -> None:
    body_started = asyncio.Event()
    finish_body = asyncio.Event()

    async def receive():
        body_started.set()
        await finish_body.wait()
        return {"type": "http.request", "body": b"", "more_body": False}

    wrapped = wrap_http(_downstream_response(), max_in_flight=1, body_timeout_seconds=1)

    async def run() -> None:
        slow = asyncio.create_task(_call_with_receive_async(wrapped, receive))
        await body_started.wait()
        live = await _call_async(wrapped, [], path="/health/live", method="GET")
        assert _status(live) == 200
        assert json.loads(_body(live)) == {"status": "live"}
        finish_body.set()
        assert _status(await slow) == 200

    asyncio.run(run())


async def _call_with_receive_async(app, receive) -> list[dict]:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await app(_scope(), receive, send)
    return sent


def test_non_http_scopes_are_forwarded_unchanged() -> None:
    observed: list[object] = []

    async def downstream(scope, receive, send):
        observed.extend([scope, receive, send])

    wrapped = wrap_http(downstream)
    scope = _scope(scope_type="lifespan")

    def receive():
        return None

    def send(message):
        return None

    asyncio.run(wrapped(scope, receive, send))
    assert observed == [scope, receive, send]
