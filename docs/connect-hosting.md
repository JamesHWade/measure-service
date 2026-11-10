# Host the service on Posit Connect

Posit Connect can run the HTTP app as `python-fastapi` content. Connect
authenticates each caller and proxies the request; the service reads the
identity Connect attaches. Read [deployment and storage](deployment.md) first:
its trust boundary and persistence limits apply unchanged.

This route was run on one Connect 2026.10.0 server using native execution, with
R 4.6 and Python 3.12. On that server the bundle built, the health checks
answered, one account listed tools, and a synthetic chromatogram was analyzed by
the R worker, with the caller identified first by the `username` claim and then
by the `session-token` claim described below. Separate owners were checked only
against a simulated Connect API in `tests/test_connect_hosting_socket.py`, not
with two real accounts. Repository CI does not contact a Connect server.
Complete the
[pilot matrix](pilot-readiness.md) on your own server before inviting users.

## What the bundle contains

`scripts/build_connect_bundle.py` stages a directory and a `.tar.gz` beside it.
It contacts no server and reads no deployment settings.

| File | Purpose |
| --- | --- |
| `wheels/<digest prefix>/measure_service-*.whl` | The service, built from the checkout |
| `requirements.txt` | Your exact third-party pins, then the wheel's relative path |
| `app.py` | The ASGI entry point, `app:app` |
| `manifest.json` | Content type, Python version, and optionally the R runtime and packages |

Connect reuses a cached Python environment when `requirements.txt` is unchanged.
The wheel's directory is named from its SHA-256 digest, so new service code
yields new requirements text and a new environment.

## Build it

Work in an environment with the `dev` extra installed.

1. Resolve exact pins for the server's Python version and platform from the
   package index your server uses. For example, using `uv`:

   ```sh
   uv pip compile pyproject.toml --python-version 3.12 \
     --python-platform x86_64-manylinux_2_28 --no-header --no-annotate -o pins.txt
   ```

2. Record the R packages the worker needs. In an R library holding the
   `measure` and `measure.sec` builds you intend to run, put these lines in an
   otherwise empty directory as `plumber.R`:

   ```r
   library(jsonlite)
   library(recipes)
   library(tibble)
   library(measure)
   library(measure.sec)
   ```

   Then run `rsconnect::writeManifest(appDir = "<that directory>", appFiles = "plumber.R")`.
   Only the `platform` and `packages` fields of the result are used, so the
   file name is just a way to make rsconnect scan the `library()` calls. Every
   package must come from a repository your Connect server can reach. Omit
   `measure.sec` if you do not need SEC processing.

3. Stage the bundle:

   ```sh
   python scripts/build_connect_bundle.py \
     --pins pins.txt \
     --python-version 3.12.10 \
     --r-manifest <that directory>/manifest.json \
     --r-requires '>=4.5.0' \
     --output build/bundle
   ```

Upload `build/bundle.tar.gz` to a content item using your usual publishing
tool. Connect restores the R packages into the content's own library, then
installs the Python requirements.

`--python-version` is the server's Python as `X.Y.Z`; the manifest accepts any
patch release of that minor version. `--r-requires` is the range of R versions
Connect may select. Set it: without a constraint Connect matches the recorded
`platform` version to the nearest installed R, which may be older than the
numerical backends support. The backends are developed against R 4.5 and newer.
Without `--r-manifest` the bundle carries no R packages and numerical tools
fail until an operator supplies R another way.

`--app` replaces the entry module. Use it to set non-secret defaults using
`os.environ.setdefault` before importing `measure_service.server`. Keep the
call to `connect_hosting.apply_connect_defaults()` shown below, and keep it
ahead of that import.

## What the entry module derives from Connect

The default entry module runs this before it imports the server:

```python
from measure_service import connect_hosting

connect_hosting.apply_connect_defaults()
```

It fills two settings from the environment Connect gives a content process.
A value you set yourself is never replaced.

| Setting | Derived from | Why |
| --- | --- | --- |
| `MEASURE_MCP_HTTP_ALLOWED_HOSTS` | The host of `CONNECT_SERVER`, when that is an HTTPS URL | Requests arrive with the Connect server's `Host`, which the service otherwise refuses with HTTP 421 |
| `MEASURE_MCP_R` | `$R_HOME/bin/R`, when that file exists | Connect restores packages for the R named by `R_HOME`; `R` on the content's `PATH` can be an older installation that cannot load them |

Host and Origin admission is otherwise unchanged; see
[admitting a public Host and Origin](http-authorities.md). Set
`MEASURE_MCP_HTTP_ALLOWED_HOSTS` yourself when callers reach the content under
another name, such as a load balancer or a custom domain, and set
`MEASURE_MCP_HTTP_ALLOWED_ORIGINS` for browser clients. `CONNECT_SERVER` that
is not a plain origin stops the import with an error.

Connect also sets `R_LIBS` to the restored library, which the worker inherits.
The service does not pass `R_HOME` on to the worker, because R's launcher
ignores an inherited value and reports that on standard output. Call
`measurement_capabilities` after each deployment; it reports the R version and
backend package versions the worker actually loaded.

## Settings on the content item

Set these environment variables on the content, or as defaults in a custom
entry module:

| Variable | Value |
| --- | --- |
| `MEASURE_MCP_IDENTITY` | `connect` |
| `MEASURE_MCP_TRUST_CONNECT_HEADERS` | `1` |
| `MEASURE_MCP_DATA_DIR` | A persistent directory outside the content directory |
| `MEASURE_MCP_CONNECT_USER_CLAIM` | `guid` (default), `username` or `session-token`; see below |
| `MEASURE_MCP_CONNECT_INTEGRATION` | Only for `session-token`, when the content has more than one integration |
| `MEASURE_MCP_CONNECT_API_KEY` | Optional, for `username` and `session-token`; see below |

Connect replaces the content directory on every deployment, so a store placed
there is lost. Create the store once with
[`measure-service-store`](store-recovery.md) or from your entry module. The
store is a single-node SQLite database: limit the content to one process, and
do not treat a network filesystem as durable storage for real measurements.

New content is visible only to its publisher. Each account you add to the
access list gets its own records. The service has no sharing, project or delete
operation: one account cannot read or change another account's records, and the
access list is the only control over who may call the service.

## How callers are identified

Records are always owned by `connect:<account GUID>`.
`MEASURE_MCP_CONNECT_USER_CLAIM` selects how the service learns that GUID.

| Claim | The service reads | Needs |
| --- | --- | --- |
| `guid` | The `user` field of `RStudio-Connect-Credentials`, as a GUID | A server configured to write GUIDs there |
| `session-token` | `Posit-Connect-User-Session-Token`, exchanged with Connect | A Connect API integration associated with the content |
| `username` | The `user` field of `RStudio-Connect-Credentials`, as a name | An API key that can read `/__api__/v1/users` |

Check which form your server writes in the `user` field before choosing. Keep
`guid` when it is the account GUID; nothing else is needed. When it is the
username, prefer `session-token`: Connect validates the token it issued and
names the account itself, so the service matches no names and needs no key that
can list users. Use `username` on a server without Connect API integrations.

A username treated as a GUID is rejected. A GUID-shaped value under the
`username` claim is looked up as a name.

### The `session-token` claim

An administrator creates one integration of the Connect API type. The content
owner then adds it under the content's integration settings. An integration
limited to the viewer role is enough, because the service only reads the
caller's own account.

On every request the service:

1. Posts the session token to
   `/__api__/v1/oauth/integrations/credentials` on `CONNECT_SERVER`, asking for
   an API key that acts as the caller.
2. Reads `/__api__/v1/user` with that key and uses the `guid` it returns. A
   locked account is refused.

The key is then discarded. `RStudio-Connect-Credentials` is not read in this
mode. Nothing is cached, so each request costs two calls to Connect.

The exchange itself is authenticated with `MEASURE_MCP_CONNECT_API_KEY`, then
`CONNECT_API_KEY`. Connect requires that key to belong to an owner of the
content. Servers that inject `CONNECT_API_KEY` for the content owner need no
further setting.

When the content has more than one integration, set
`MEASURE_MCP_CONNECT_INTEGRATION` to the GUID of the Connect API integration.
Connect refuses an exchange that does not name one. A value that is not a GUID
stops the import with an error.

Connect deletes the keys it issues when the content process exits. A process
that never stops accumulates one per request until it is restarted, and Connect
can answer an exchange with HTTP 429. On the server named above, 30 consecutive
tool calls from one account were all answered; no higher rate was tried.

### The `username` claim

On every request the service asks Connect's users API for the one unlocked
account holding that exact name and uses its GUID. Nothing is cached, because
Connect can rename an account and give the old name to another person.

The lookup uses `MEASURE_MCP_CONNECT_API_KEY`, then `CONNECT_API_KEY`. Some
servers inject `CONNECT_API_KEY` for the content owner; check whether you want
lookups to run under that account, and prefer a dedicated key.

### When an account cannot be verified

A failed exchange or lookup never selects an owner. Tool discovery still
answers; a tool that needs an owner returns an error stating that the account
could not be verified. The content log has one line per failure, starting
`Connect identity lookup failed:`, with the HTTP status and Connect's numeric
error code. It never contains the token or a key. Connect documents code 212
for content with no integration associated.

## Connect a client

The MCP endpoint is the content URL followed by `mcp`. Clients authenticate to
Connect, for example by sending a Connect API key:

```sh
claude mcp add --transport http measure "https://<connect host>/content/<content GUID>/mcp" \
  --header "Authorization: Key <the caller's Connect API key>"
```

Use the [pilot client](pilot-client.md) with two accounts to check that owners
stay separate on your server.
