# Numerical calls and concurrent requests

Capabilities, single-trace chromatogram analysis, relative SEC and peak
quantitation run their blocking R work in worker threads. The MCP dispatcher
can handle other requests, including stored-record reads, ping and health
checks, while an R subprocess runs. The request's principal and input records
are resolved before the thread starts; the returned analysis is saved for that
captured principal.

Single analyses and analytical batches share two R worker permits per service
process. A call that cannot acquire a permit returns a tool error; it does not
join an unbounded R work queue. This is a process-local bound, not a host-wide
resource quota. Other synchronous operations, such as storage and identity
lookup, can still delay request handling.

Cancelling a request while it awaits R does not terminate the Python thread or
R subprocess. That worker retains its permit until it returns or reaches the
90-second subprocess timeout. A request cancelled during that wait does not
save a later analysis. Cancellation after a result has already been saved
does not roll back the record. A lost response does not establish whether a
save happened, and single-analysis tools do not provide idempotency keys.
For durable execution history and explicit recovery, use
[analytical batches](chromatogram-batches.md).

The concurrency regression starts the real HTTP MCP transport with a
controlled worker. While one numerical call is held, a separate session reads
an owned result, sends a ping and reaches `/health/live`. It also checks the
completed result's ownership. Separate cancellation checks verify retained
capacity and absence of a late saved result. These are software concurrency
checks; numerical and actual-host qualification remain separate.
