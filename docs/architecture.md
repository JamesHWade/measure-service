# Architecture

The MCP process validates tool arguments, resolves the principal, enforces
record ownership and stores records in SQLite. Trace-analysis calls pass JSON to
a fixed R subprocess. Protocol resource accounting calls the R-free Python
`measure_methods` package shipped in the service distribution. The R worker
returns JSON; the MCP process stores the result and returns a compact record.
Arrays and original text are read in pages.

No tool accepts executable code, a server file path or an arbitrary URL. The R
worker path comes from the installed Python package. An R-backed call is
synchronous and has a 90-second subprocess timeout. There is no job queue or
worker pool.

## Repository boundaries

| Component | Responsibility |
| --- | --- |
| `measure` | R measurement structures, preprocessing, calibration and validation primitives |
| `measure.sec` | SEC calibration, moments, distributions and technique-specific calculations |
| `measure_methods` in `measure-service` | Pure-Python resource integration, objective scores and campaign-success accounting |
| Future technique packages | Domain models and methods that can be tested independently of an agent protocol |
| `measure-service` | MCP contracts, identities, evidence records, storage and backend adapters |
| Future BO adapter | Translate qualified evidence and objectives to an optimizer protocol |
| Optimizer engine | Surrogate models, acquisition functions and optimizer state |
| Client application | User interaction, authenticated uploads and policy for using outputs |

Scientific methods belong in reusable backend packages. This repository should
validate inputs and preserve scientific context without maintaining a second
implementation of the same calculation. `measure_methods` is a pure Python
package boundary inside this repository. Its numerical API stays independent of
MCP, storage, and R, while the `measure-service` wheel and source archive ship
both import packages together.

The current project consumer inventory identifies `measure-service` as the
consumer of `measure_methods`; the import exists in this service checkout. At
decision time, the indexed GitHub search found the standalone backend source
while this service implementation was still unmerged. That search does not
establish a complete list of external consumers. The package was co-located
from merged backend source `088b81551f7798a048299b506b632620346c1c39`; its
public import remains:

`from measure_methods import constant_solvent_use, program_solvent_use`.
If another project consumer needs a separate distribution, that boundary can
be reviewed with evidence of that use.

New method evaluations record the containing `measure-service` distribution,
its installed version, and PEP 610 source revision/ref when pip provides them.
Unavailable source metadata is stored as `null`. Historical evaluations that
name `measure-methods`, omit the package field under its former default, or
have unknown backend evidence, remain readable as stored; the service does not
rewrite them. Parsing an omitted package retains the historical
`measure-methods` meaning. An idempotent retry returns the original stored
evaluation and provenance. CI reports the checkout source separately; it does
not fill in missing runtime installation metadata.

An environment upgraded from the former standalone Python distribution must
remove `measure-methods` before force-reinstalling `measure-service`; the
[upgrade instructions](quickstart.md#upgrade-from-a-separately-installed-method-package)
give the required order. CI exercises it with local wheels and no package index.

The service owns experiment evidence: intended conditions, actual attempts,
measurement links, outcomes and corrections. An optimizer owns its model and
suggestion state. Campaign orchestration must record which immutable evidence
snapshot produced each suggestion and when an outcome was consumed. Those
contracts and synchronization logic are roadmap work; an external proposal
label alone does not implement them.

## Code layout

- `src/measure_service/server.py`: MCP tools, trace/method validation and R adapter.
- `src/measure_service/methods.py`: declaration validation, normalization, constraints and numerical-backend adaptation.
- `src/measure_methods/`: pure-Python resource, objective and campaign-trajectory calculations, packaged with the service.
- `src/measure_service/method_backend.py`: containing-distribution and PEP 610 source metadata.
- `src/measure_service/method_registry.py`: immutable owned method records and identities.
- `src/measure_service/experiments.py`: registry models, evidence checks and writes.
- `src/measure_service/storage.py`: canonical serialization and SQLite tables.
- `src/measure_service/worker.R`: adapter to the installed scientific packages.
- `tests/`: validation, persistence, concurrency, MCP and HTTP contracts.
- `examples/`: complete client workflows using the installed package.

## Evidence before automation

An agent can retrieve method settings, warnings, provenance and reported
outcome states. This supports an informed decision; it does not establish
fitness for a particular analytical use. Future objective extraction must
version its definitions, preserve failed and censored observations, and avoid
treating simulated data as physical measurements. The service has no blanket
human-signoff gate and currently sends no instrument commands.
