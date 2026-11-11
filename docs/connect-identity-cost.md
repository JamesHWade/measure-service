# Connect identity lookup cost

Connect identity is resolved only when an enabled MCP tool is called over
HTTP. Health endpoints, MCP initialization, tool discovery, `ping`, protocol
notifications, protocol messages rejected before dispatch, and unknown or
disabled tool names do not contact
Connect. The proxy remains the authentication boundary; the service resolves
its trusted claim to an immutable Connect account GUID before the tool runs.

Each HTTP request gets a new resolver. The resolver serializes concurrent
identity checks in that request and remembers either the GUID or the safe
failure result until the request ends. It never reuses an identity between
requests, so a renamed, locked, revoked, or otherwise changed account is
checked again before its next tool call.

The expected Connect API cost for an HTTP request containing one or more
enabled owner-scoped tool calls is:

| Claim mode | Requests per HTTP request | Connect operations |
| --- | ---: | --- |
| `username` | 1 | Read the exact username through `/__api__/v1/users` and use its unlocked account GUID. |
| `session-token` | 2 | Exchange the request's user session token for a visitor API key, then read that visitor's account at `/__api__/v1/user`. |
| `guid` | 0 | Use the single trusted GUID in `RStudio-Connect-Credentials`; no Connect lookup is needed. |

For a session with initialization, discovery and N enabled owner-tool HTTP
requests, the service issues N visitor-key exchanges, rather than one for
every handshake or discovery request too. A registered enabled call can resolve
identity before its argument validation fails. The visitor key is used only
for that account read and is discarded locally; neither it nor the
resulting GUID is cached across requests. Missing or ambiguous claims do not
call Connect. Discarding a key locally does not revoke it at Connect; the
host controls issued-key lifetimes. Monitor its rate limits and resource
policy for long-lived processes. A lookup failure is returned as an MCP tool error (`isError`),
while the HTTP protocol response remains available to the client.

Stdio keeps its `local` principal and does not use Connect identity. This
behavior describes the service's lookup boundary and request cost; it does
not qualify a Connect deployment, proxy configuration, or account
authentication. See [Connect hosting](connect-hosting.md) and
[deployment limits](deployment.md) for setup requirements.
