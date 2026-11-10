# HTTP request limits and health

The ASGI entrypoint `measure_service.server:app` wraps every ordinary HTTP route
in a measured request envelope before MCP dispatch. Stdio remains unchanged.
Authentication still belongs to the separately verified
[trusted proxy boundary](deployment.md#identity-and-http).

## Request envelope

The installed entrypoint uses these fixed process-local defaults:

| Limit | Behavior |
| --- | --- |
| Body | At most 8,000,000 bytes, measured across all received body fragments |
| Receive deadline | 30 seconds total to receive a complete body, starting after admission |
| In-flight requests | 16, including incomplete uploads and streamed responses, through downstream ASGI completion |

The envelope reserves capacity before receiving a body. It buffers the bounded
body and passes it to MCP only when complete and valid. Missing Content-Length
does not disable measurement. Duplicate, malformed or inconsistent lengths fail
closed. A disconnected, oversized or timed-out upload never invokes a tool.
The receive deadline does not limit a tool's execution time; numerical calls
retain their separate R worker timeout and capacity limits.

Rejections return compact JSON with `Cache-Control: no-store`:

| HTTP status | Meaning |
| --- | --- |
| 400 | Invalid body framing or inconsistent declared length |
| 408 | The complete body did not arrive before the receive deadline |
| 413 | Declared or measured body length exceeds the limit |
| 503 | Process-local request capacity is exhausted; `Retry-After: 1` is included |

Capacity is released on normal completion, rejection, disconnect, cancellation
or a downstream exception. There is no waiting queue. Long-lived HTTP streams
occupy a slot. Embedding applications can construct `wrap_http` with explicit
positive finite limits; the documented server has no environment override that
disables them. Body buffering and protocol parsing can require more memory than
the input bytes alone. Set host memory limits and upstream connection, header
and rate limits for the actual deployment.

## Observe liveness and basic storage readiness

GET and HEAD are supported at two exact paths:

| Path | Success | Failure |
| --- | --- | --- |
| `/health/live` | 200 with `status` equal to `live` | A failure to connect or respond indicates the process path is unavailable |
| `/health/ready` | 200 with `status` equal to `ready` | 503 with `status` equal to `not_ready` |

Other methods return 405. HEAD has the same headers as GET and an empty body.
The responses contain no paths, owners, counts, versions or exception details.
Health endpoints bypass ordinary request capacity so liveness can remain
observable during stalled uploads. They do not authenticate callers.

Basic readiness requires the Connect identity configuration and an existing
readable store without an `INCOMPLETE` marker. The probe opens SQLite read-only,
checks four physical core tables and their required fields without reading
records, and closes the connection. It does not initialize a missing directory,
create or migrate tables, invoke R, or inspect scientific results.

The probe runs outside the event loop, with at most one blocking checker per
wrapper. Concurrent requests share that work, with a one-second response
deadline; timing out a response does not start another checker while the old
one is still running. The SQLite probe also uses a short busy timeout and a
cooperative query deadline. Filesystem calls are subject to the host's I/O
behavior.

`ready` does not establish complete schema integrity, write capacity, free disk
space, R/SEC availability, a functioning authentication proxy or a working
client deployment. Use [operator inspection](store-recovery.md), numerical
capability discovery and the [pilot qualification matrix](pilot-readiness.md)
for those separate questions. HTTP 200 also does not establish a successful
MCP tool call: inspect its `isError` field.
