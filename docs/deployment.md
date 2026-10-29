# Deployment and storage

Local stdio is the supported development path. HTTP contracts are tested using
a simulated identity proxy. No real hosted proxy/client deployment has been
verified by this repository's tests.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MEASURE_MCP_DATA_DIR` | `~/.local/share/measure-mcp` | Directory containing `measure.sqlite3` |
| `MEASURE_MCP_R` | `R` | R executable for numerical calls; not `Rscript` |
| `MEASURE_MCP_IDENTITY` | `local` | `local` for stdio or `connect` for the proxy header adapter |
| `MEASURE_MCP_TRUST_CONNECT_HEADERS` | unset | Must be `1` to enable the Connect header adapter |
| `MEASURE_MCP_MEASURE_REVISION` | unset | Operator-declared source revision in analysis provenance |
| `MEASURE_MCP_SEC_REVISION` | unset | Operator-declared SEC source revision in analysis provenance |

Standard R library variables such as `R_LIBS_USER` are inherited by the worker.
The repository rename preserves these variable names, tool names and default
storage path for compatibility. There is no automatic data relocation.

## Identity and HTTP

Stdio uses a single `local` principal. Clients sharing its data directory share
that principal's records. This is not per-user isolation.

The ASGI app is `measure_service.server:app` with Streamable HTTP at `/mcp`.
HTTP calls fail in local identity mode. The optional `connect` adapter requires
one `RStudio-Connect-Credentials` header containing JSON with a UUID `user`.
The principal is `connect:<UUID>`. Every data lookup and link is scoped to that
principal.

**The header is not authentication.** A trusted proxy must authenticate callers,
strip caller-supplied identity headers, inject verified identity and prevent
access to the upstream process. The service does not verify a bearer token,
OAuth grant or header signature. Setting the two environment variables without
that boundary allows identity spoofing.

An operator evaluating Posit Connect must verify GUID delivery
(`Authorization.ContentCredentialsUseGUID`) and the actual request shape.
See the official documentation for
[user metadata](https://docs.posit.co/connect/user/fastapi/#user-meta-data) and
[GUID configuration](https://docs.posit.co/connect/admin/appendix/advanced-user-group/#uniqueness-and-leverage-the-connect-server-api-using-posit-connect-guids).
The automated test injects two UUIDs and checks missing/malformed identities
and record ownership. It does not test Connect configuration, token exchange,
TLS or proxy bypass.

For an already configured, verified proxy on the same host, the upstream
process can be started on loopback:

```sh
MEASURE_MCP_IDENTITY=connect MEASURE_MCP_TRUST_CONNECT_HEADERS=1 \
  .venv/bin/python -m uvicorn measure_service.server:app \
  --host 127.0.0.1 --port 8000
```

This command alone does not create a secure deployment. The service has no
public health endpoint or browser UI. MCP tool errors use the protocol's
`isError` field; an HTTP success status does not imply a successful tool call.

## Persistence and recovery

SQLite is a single-node store. Registry records and idempotency receipts commit
together; reads and writes enforce ownership. Dataset/analysis records are
immutable through the tools. New registry tables are added alongside existing
analysis tables. There is no general schema-migration framework or downgrade
support.

Use a private persistent local directory accessible to the service account.
The code creates a new directory with mode `0700`; it does not repair existing
directory/file permissions or encrypt the database. Raw source text, numeric
arrays, sample IDs and declared conditions may all be stored there. Do not use
a shared temporary directory for real measurements.

For a simple consistent backup, stop clients and the server, copy the data
directory, then restore a copy into a separate directory and verify records
before resuming. No automated backup, restore drill or retention/deletion API
is implemented. Never run two independent copies and expect their IDs,
revisions or retry receipts to synchronize. Shared network filesystems and
multi-node service replicas are not supported by the current design.

Application payload limits bound accepted traces and registry records. They do
not replace an upstream HTTP body limit, rate limit or storage quota. Quotas,
resource scheduling, audit-log export, authenticated asset ingestion and
multi-node persistence remain deployment work. Numerical calls run inline;
there is no cancellation API, durable job state or physical execution control.
