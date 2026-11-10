# Admit the selected public Host and Origin

Configure the authority preserved by your trusted proxy before running the
pilot client. The MCP SDK's loopback defaults reject an unlisted public Host
with HTTP 421 and an unlisted Origin with HTTP 403. Authentication and the
trusted identity adapter remain separate; admission of a hostname does not
verify a caller or permit direct upstream access.

## Set explicit startup allowlists

Use separate environment variables for the server authority and browser
origins that the actual client sends:

```sh
MEASURE_MCP_HTTP_ALLOWED_HOSTS='["measure.example.org"]' \
MEASURE_MCP_HTTP_ALLOWED_ORIGINS='["https://workbench.example.org"]' \
MEASURE_MCP_IDENTITY=connect MEASURE_MCP_TRUST_CONNECT_HEADERS=1 \
  .venv/bin/python -m uvicorn measure_service.server:app \
  --host 127.0.0.1 --port 8000
```

The host list accepts explicit authorities such as `measure.example.org` or
`measure.example.org:8443`, without a scheme or path. The origin list accepts
explicit HTTPS origins such as `https://workbench.example.org`. A configured
browser origin does not add its hostname to the server Host list. Add only the
names required by the intended proxy and clients. MCP clients that omit Origin
do not need an additional origin entry.

Both variables are bounded JSON arrays. Unset variables and empty arrays add
no public entries. Invalid configuration fails during service import/startup,
including stdio startup; it does not silently disable protection. Wildcards,
empty values, duplicates after normalization, whitespace, userinfo, paths,
queries, fragments, invalid ports, terminal DNS root dots and non-HTTPS public
origins are rejected.
DNS names use ASCII or punycode; IP literals include bracketed IPv6. Names are
normalized for ASCII case and IPv6 spelling. HTTPS default port 443 is treated
as equivalent to an omitted port; a configured nondefault port authorizes only
that port.

Loopback operation remains available. DNS-rebinding protection stays enabled,
with only the predefined loopback port allowances. Forwarded headers do not
add trusted authorities. Keep the upstream process on loopback and configure
the real proxy to authenticate, strip caller-supplied trusted identity headers
and inject verified identity as described in [deployment](deployment.md).

## Preserve routing and header boundaries

A public URL such as `https://measure.example.org/service/mcp` has authority
`measure.example.org`; `/service/mcp` is routing. Configure that route in your
proxy or hosting platform. Do not put a URL prefix in either allowlist. The
service's MCP path remains `/mcp` at its upstream ASGI boundary.

Before SDK dispatch, the service requires exactly one valid Host and at most
one nonempty, valid Origin. Duplicate headers, comma-joined values, opaque
`Origin: null`, invalid ports and ambiguous authorities receive a fixed HTTP
400 response. Even identical duplicates are rejected. Valid but unlisted
Host/Origin values remain subject to the SDK's 421/403 checks. Rejected
requests do not dispatch a tool. Existing health routes retain their minimal
operational scope; a health response does not establish MCP admission.

Origin admission does not implement browser CORS/preflight support, OAuth,
TLS termination or user authentication. Verify the actual selected MCP client
and proxy. A local simulation of preserved headers establishes the service's
request contract, not the real host's sign-in or network isolation.

## Troubleshoot the first hosted connection

| Observation | Check |
| --- | --- |
| HTTP 421 | Inspect the actual upstream Host; configure its exact approved authority or deliberately rewrite it at the trusted proxy |
| HTTP 403 from MCP transport | Inspect the actual Origin and add only the required HTTPS origin; a Host rewrite alone does not change Origin |
| HTTP 400 for transport headers | Remove duplicate/blank/opaque/joined values and correct the port syntax at the client/proxy boundary |
| Startup configuration error | Check JSON array syntax and explicit values; error text omits the supplied configuration |
| HTTP success with MCP `isError` | Inspect the MCP result; Host/Origin admission does not establish identity or backend success |

After configuring the boundary, run [the two-account pilot checks](pilot-client.md)
and record [actual host acceptance](pilot-host-record.md). Keep service issue
#83 open until that evidence is complete.
