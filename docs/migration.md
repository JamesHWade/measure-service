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

The initial standalone implementation is proposed in
[measure-service #14](https://github.com/JamesHWade/measure-service/pull/14).
It replaces the two source service PRs. The SEC correction PR remains in
`measure.sec`.

Service issues were recreated here with links to the original planning
history. Their acceptance criteria remain open; relocation does not mean the
work is complete. Scientific models, technique packages and the detailed
literature audit remain in the R repositories.

| Original measure issue | Service issue |
| --- | --- |
| [#82](https://github.com/JamesHWade/measure/issues/82) | [#1: MCP service](https://github.com/JamesHWade/measure-service/issues/1) |
| [#86](https://github.com/JamesHWade/measure/issues/86) | [#2: Environments, CI and reference data](https://github.com/JamesHWade/measure-service/issues/2) |
| [#90](https://github.com/JamesHWade/measure/issues/90) | [#3: BO campaign roadmap](https://github.com/JamesHWade/measure-service/issues/3) |
| [#91](https://github.com/JamesHWade/measure/issues/91) | [#4: Experiment and result registry](https://github.com/JamesHWade/measure-service/issues/4) |
| [#94](https://github.com/JamesHWade/measure/issues/94) | [#5: Campaign benchmarks](https://github.com/JamesHWade/measure-service/issues/5) |
| [#95](https://github.com/JamesHWade/measure/issues/95) | [#6: BO backend adapter](https://github.com/JamesHWade/measure-service/issues/6) |
| [#96](https://github.com/JamesHWade/measure/issues/96) | [#7: Durable experiment loop](https://github.com/JamesHWade/measure-service/issues/7) |
| [#97](https://github.com/JamesHWade/measure/issues/97) | [#8: Optimization diagnostics](https://github.com/JamesHWade/measure-service/issues/8) |
| [#101](https://github.com/JamesHWade/measure/issues/101) | [#9: Instrument adapters](https://github.com/JamesHWade/measure-service/issues/9) |
| [#102](https://github.com/JamesHWade/measure/issues/102) | [#10: Evidence views and exports](https://github.com/JamesHWade/measure-service/issues/10) |
| [#103](https://github.com/JamesHWade/measure/issues/103) | [#11: Batch jobs and snapshots](https://github.com/JamesHWade/measure-service/issues/11) |
| [#104](https://github.com/JamesHWade/measure/issues/104) | [#12: Application demonstrations](https://github.com/JamesHWade/measure-service/issues/12) |
| [#106](https://github.com/JamesHWade/measure/issues/106) | [#13: Cost-aware multi-fidelity campaigns](https://github.com/JamesHWade/measure-service/issues/13) |

The [capability audit](https://github.com/JamesHWade/measure/issues/105)
retains the full source-to-requirement matrix and points service-owned work
to these issues. See the [roadmap](roadmap.md) for implementation order and
acceptance evidence.
