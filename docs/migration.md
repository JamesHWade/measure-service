# Extraction from measure

The service began in the `mcp/` directory of `measure`. This repository starts
from the source snapshots below; it does not import the R package's
full history.

| Source | Revision |
| --- | --- |
| Service foundation, [measure #83](https://github.com/JamesHWade/measure/pull/83) | `668e3e99f1ab51493551581f607e11167f586b20` |
| Experiment registry, [measure #109](https://github.com/JamesHWade/measure/pull/109) | `850c21bf411244589b7aea444bddb83b65c01948` |

The extraction moves Python modules under `src/measure_service`, uses relative
imports, packages the R worker, adds an installed CLI/module entrypoint and
runs tests against the installed package. The scientific R calculations are
retained. The MIT copyright notice is preserved in [LICENSE](../LICENSE).

## Compatibility

MCP tool names, request models, identity principal strings, environment
variables, SQLite filename and default data directory are retained. The
wire-level service name remains `measure-separations`. Registry schema version
remains `1.0`. This move does not add an optimizer, production authentication,
a job queue or a new analytical method.

Replace a client command that runs `measure/mcp/server.py` with the new
installed interpreter and `-m measure_service`, or the `measure-service` CLI.
Replace the HTTP ASGI target `server:app` with
`measure_service.server:app`. Internal Python imports from the prototype's
standalone modules were not a public library API.

Before using existing data, stop the old process and take a backup. Point the
new process at the same explicit `MEASURE_MCP_DATA_DIR`; do not run old and new
servers concurrently against it during the switch. The suite checks that the
new tables preserve an earlier analysis database. It does not implement a
general migration/downgrade framework.

## Work tracking

Existing `measure` issues retain the scientific literature audit and planning
history. The [roadmap](roadmap.md) separates service work from backend work.
Service issues and the two draft PRs should be linked to their replacement
here once the new remote and review branch are available. Until that move is
verified, their source links remain the record of the proposed implementation.
The SEC correction PR stays in `measure.sec`.
