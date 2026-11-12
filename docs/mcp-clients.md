# Local MCP client configuration and checks

`measure-service-client` prints a local MCP configuration fragment or checks
the installed stdio service. Configuration output goes to stdout; the command
does not edit Claude Code, Codex or other client settings files. It uses an
absolute Python executable and argument arrays, so paths are not assembled
into a shell command.

Run the command with the Python environment that has `measure-service`
installed. If `--python` is omitted, the configuration keeps the absolute
`sys.executable` path used to run the CLI. Supply an absolute data directory:

```sh
measure-service-client config claude \
  --data-dir /absolute/path/to/private-measure-data

measure-service-client config codex \
  --data-dir /absolute/path/to/private-measure-data
```

For Claude Code, merge the JSON entry into the project's `.mcp.json` and
approve that project's MCP server when the client requests trust. For Codex,
merge the TOML table into `~/.codex/config.toml` or a trusted project's
`.codex/config.toml`. Preserve other server entries. Restart the client, inspect
its MCP server list, then run the synthetic workflow below before using your
own measurements. The generated fragments do not register a marketplace plugin.
See the official [Claude Code](https://code.claude.com/docs/en/mcp) and
[Codex](https://developers.openai.com/codex/mcp/) configuration guides.

The default catalog selects `discovery`, `traces` and `evidence`. Add
`--include-sec` to include the relative SEC tools. The default schema profile
is the bounded `function-host/1` projection; pass
`--schema-profile canonical` to retain the canonical schemas. The Codex
fragment sets `tool_timeout_sec = 120`; the R worker has its own 90-second
limit.

When R and its packages are installed in a separate library, pass both paths
to the config command. `--r` is the R executable, and `--r-library` becomes
both `R_LIBS` and `R_LIBS_USER` in the generated client environment so the
selected library takes precedence over an inherited user library:

```sh
measure-service-client config codex \
  --data-dir /absolute/path/to/private-measure-data \
  --r /absolute/path/to/R \
  --r-library /absolute/path/to/R/library
```

To check the installed local stdio path, run:

```sh
measure-service-client check
```

The check starts the installed module through the MCP Python SDK, initializes
and pings two separate stdio sessions, and verifies the selected catalog and
schema profile. It registers one synthetic CSV trace in a fresh temporary
store, checks its retained source digest and metadata, confirms that an
unselected tool returns the expected `isError`, then restarts the service and
compares the exact inspection and source responses. The temporary store uses
private directory and file permissions and is removed when the command exits.
The JSON report contains check outcomes and selected groups, not temporary
paths, record identifiers or retained source text.

Add `--include-sec --numerical` to run the installed synthetic numerical
qualification. It reuses `pilot_numerics.qualify_numerics` and therefore
requires the corrected `measure` and `measure.sec` R backends. Numerical
checks require matching absolute `--r` and `--r-library` paths:

```sh
measure-service-client check \
  --include-sec \
  --numerical \
  --r /absolute/path/to/R \
  --r-library /absolute/path/to/R/library
```

The numerical check also reads back the exact retained analysis responses
after the service process restarts. The command has a 120-second total deadline
by default. The check exercises
the installed MCP SDK stdio transport and local store lifecycle. Its report
marks actual Claude Code, Codex and model workflow acceptance as pending;
those require testing in each client with its model and are not inferred from
this check. It does not call a model API or qualify an HTTP host, authentication
proxy, instrument or analytical method.

## Check the actual client

In each intended client, ask the model to call registry and numerical
capabilities, import an explicitly mapped synthetic trace, inspect its units,
analyze it, retrieve the analysis and follow its evidence pages. Require the
model to report warnings and a controlled missing-record `isError` visibly.
Close and reopen the client and retrieve the same retained records. Record the
client version, selected catalog/profile, installed backend identities and
actual outcomes. A connected status alone does not establish this workflow.

## Connect a Streamable HTTP host

A host that starts only HTTP MCP connections needs a deployed endpoint rather
than the local stdio configuration above. Configure the service using
[Connect hosting](connect-hosting.md), [deployment and identity](deployment.md)
and [public Host/Origin admission](http-authorities.md), then add its exact
HTTPS `/mcp` URL to the host's server registry. Use that host's supported
authentication route. The service does not implement provider-independent
OAuth; a trusted identity header is never a substitute for authentication.

Select `discovery`, `traces`, `evidence` and optionally `sec` on the service
process. Verify the actual host's schema conversion, structured/text results,
warnings, `isError` and paging. This is a tool service; it publishes no MCP Apps
UI resources. A host's app cards or visual analysis surface require a separate
integration. Complete [actual-host acceptance](pilot-host-record.md) before
inviting researchers to a shared deployment.
