# Run the invited-pilot checks

Use `measure-service-pilot` from the installed service distribution to create
synthetic evidence through two independently authenticated accounts, then check
its continuity after an operator restart. The command uses the ordinary MCP
endpoint. It does not configure authentication, start the service, or restart
it. Keep the actual host acceptance record in [the host worksheet](pilot-host-record.md).

## Configure two actual client accounts

Install the service distribution into your client environment. The service and
numerical backends are installed on the host as described in [deployment](deployment.md).
Select an endpoint whose proxy you administer and two invited test accounts
with separate credentials. Set these environment variables privately:

| Variable | Value |
| --- | --- |
| `MEASURE_PILOT_URL` | Exact HTTPS MCP endpoint, including its `/mcp` path or deployed prefix |
| `MEASURE_PILOT_HEADERS_A` | JSON object of authentication headers for account A |
| `MEASURE_PILOT_HEADERS_B` | JSON object of authentication headers for account B |

For a proxy accepting bearer tokens, each object has the shape
`{"Authorization":"Bearer <account-specific-token>"}`. Use the authentication
scheme supported by your actual proxy/client. Do not put credentials in URLs,
command arguments, repository files, reports or issue comments. The command
rejects `RStudio-Connect-Credentials`, protocol/framing headers and forwarded
identity headers. It never supplies a verified identity on behalf of a caller.

TLS verification remains enabled. Redirects and environment HTTP proxy settings
are disabled; use the final endpoint directly. URL userinfo, queries and
fragments are rejected. Response bodies are bounded to 8,000,000 bytes without
compression. Calls have a 120-second deadline and the complete run a 600-second
deadline. There are no automatic tool retries. Browser-interactive OAuth and
login-cookie acquisition remain client/provider setup; this command does not
implement them.

## Prepare synthetic evidence

Choose a new private directory under an existing trusted parent:

```sh
measure-service-pilot prepare --allow-synthetic-writes --numerical \
  --output-dir ./pilot-before
```

This command writes new synthetic datasets, experiment designs, attempts,
results and numerical analyses to both accounts. Use test data and accounts;
the service has no general deletion API. A second `prepare` creates a new run
and more records. To check the existing run, use `verify` instead.

Preparation checks that identical experiment requests and idempotency keys
produce distinct records across accounts, that each account can read its own
records, and that cross-account reads and links are denied. It saves exact
responses and proves registry retry receipts replay without new attempts or
results. A generic MCP error, an unavailable tool or a failed backend does not
count as an ownership denial.

With `--numerical`, the command imports a synthetic Gaussian CSV trace and
checks its exact retained text, metadata and source digest. It checks a single
peak's location and integrated area against an analytic Gaussian reference,
then relative SEC moments against independent trapezoidal arithmetic and a
normalized molecular-weight distribution. The input signal is explicitly
caller-declared baseline-corrected and proportional to weight concentration.
These fixtures qualify the installed numerical path for this example; they do
not qualify physical measurements, standards, compound identities, instruments
or full MoSeS reproduction. Omit `--numerical` for a registry-only check; that
leaves numerical qualification visibly unrun.

Before mutations, the CLI sends two harmless unauthenticated registry-discovery
requests, one without an identity header and one with an unverified synthetic
identity header. Only HTTP 401/403 is recorded as observed proxy rejection.
Redirects, login HTML, MCP errors and other failures remain unconfirmed. An
anonymous HTTP 200 event stream stops preparation for manual proxy verification. A
successful unauthenticated MCP tool response stops preparation. These probes
do not prove verified sign-in, header stripping, TLS configuration or blocked
upstream access.

## Restart and verify the original records

Record the actual process/host restart in your private host worksheet. Restart
through your normal supervisor and confirm the same private persistent volume
is attached. Then run:

```sh
measure-service-pilot verify --checkpoint ./pilot-before/checkpoint.json \
  --output-dir ./pilot-after-restart
```

Keep the endpoint URL exactly the same; the checkpoint binds its digest.
Credentials can be refreshed, but account A and B must still represent the
original principals in the original order. Verification first checks owned
historical records and foreign-read denials. It then replays only the original
idempotent experiment/attempt/result requests and checks complete responses and
attempt/result listings. It does not re-import datasets or rerun numerical
analyses. Numerical mode also checks that the current backend capability
snapshot matches the original; a backend change requires separate
requalification rather than silently accepting runtime drift. Use it again after a controlled [backup restore and cutover](store-recovery.md),
with a new output directory. Stop the old writer before cutover.

Continuity alone does not prove a restart or restore happened. The command
never marks those operator gates complete. It also cannot establish that a
particular browser or desktop client performs the same authentication flow.

## Keep private evidence private

Each new output directory is mode `0700`, with exclusive mode `0600` files on
POSIX. Parent locations are trusted operator choices and resolved before use.
Existing outputs are never overwritten. Windows is not currently supported by
this evidence publisher.

- `checkpoint.json` contains private record identities and exact synthetic
  responses. Use it only with the supported verifier. It is not a signature or
  an authorization grant; protect it from modification.
- `events.jsonl` records each requested operation before dispatch and its
  response, without transport credentials. It can contain owner identities,
  backend paths and arbitrary server warnings. Treat it as private data.
- `report.json` and successful stdout contain only fixed aggregate statuses and
  a checkpoint digest and, in numerical mode, the original reference error
  magnitudes and distribution integrals for accounts A/B. They omit credentials,
  URLs, owners and record IDs. Verification reports the original numerical
  comparison; it does not rerun R analyses.
- `INCOMPLETE` normally remains after failure or interruption. A failure during
  the final directory sync triggers best-effort marker recreation; filesystem
  errors or abrupt termination can prevent it. Any nonzero command exit means
  the run is incomplete even if the marker is absent or a report already exists.
  A requested write without
  a recorded response may have committed. Retain this evidence and inspect it
  before deciding how to proceed; do not blindly rerun preparation.

The command exits nonzero on failure and emits a fixed safe error instead of
remote exception text. A successful report still has `invitation_ready: false`
and pending operator gates. Complete [the pilot matrix](pilot-readiness.md) and
[host worksheet](pilot-host-record.md) before inviting researchers.

`--allow-local-http` permits only explicit loopback HTTP simulations and labels
the evidence accordingly. It does not allow trusted identity-header credentials
or qualify an actual authentication proxy. Repository tests use a simulated
proxy solely to exercise these client checks and actual process restart.
