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
| `measure_methods` in `measure-service` | Pure-Python experimental designs, resource integration, objective scores, analytical acceptance, ideal retention, latent Gaussian observations and campaign-success accounting |
| Future technique packages | Domain models and methods that can be tested independently of an agent protocol |
| `measure-service` | MCP contracts, identities, evidence records, storage and backend adapters |
| Optional BO-MCP adapter | Replay exact consumed synthetic evidence into the public optimizer backend |
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
snapshot produced each suggestion and when an outcome was consumed. The
[observation registry](observation-evidence.md) implements predeclared
analytical contracts, immutable evidence-bound assessments and explicit
snapshot membership. The [synthetic campaign registry](campaign-runtime.md)
binds those snapshots to sequential proposals and immutable consumption records.
An external proposal label alone does not implement that state.

## Code layout

- `src/measure_service/server.py`: MCP tools, trace/method validation and R adapter.
- `src/measure_service/methods.py`: declaration validation, normalization, constraints and numerical-backend adaptation.
- `src/measure_methods/`: pure-Python resource, objective and campaign-trajectory calculations, packaged with the service.
- `src/measure_service/method_backend.py`: containing-distribution and PEP 610 source metadata.
- `src/measure_service/method_registry.py`: immutable owned method records and identities.
- `src/measure_service/experiments.py`: registry models, evidence checks and writes.
- `src/measure_service/observations.py`: analytical contracts, owned evidence and immutable snapshot membership.
- `src/measure_service/retention.py`: explicit ideal-model inputs, canonical-method adaptation and retained predictions.
- `src/measure_service/gaussian_producer.py`: predeclared synthetic widths, immutable producer runs and exact generated-result evidence.
- `src/measure_methods/design.py`: explicit seeded Latin-hypercube and uniform designs, independent of service schemas and identity.
- `src/measure_service/campaign_protocol.py`: protocol normalization, identity-derived numerical seeds and candidate feasibility.
- `src/measure_service/campaign_protocol_registry.py`: owned immutable task/model/producer/contract bindings.
- `src/measure_service/campaigns.py`: sequential proposals, revision checks, immutable consumption and evidence replay.
- `src/measure_service/optimizer_backend.py`: bounded JSON adapter to an optional installed BO-MCP interpreter.
- `src/measure_service/storage.py`: canonical serialization and SQLite tables.
- `src/measure_service/worker.R`: adapter to the installed scientific packages.
- `tests/`: validation, persistence, concurrency, MCP and HTTP contracts.
- `examples/`: complete client workflows using the installed package.

Ideal retention calculations live in `measure_methods.retention`, independent
of service schemas and storage. The service requires explicit model units and
column-inlet timing, resolves owned immutable methods/training lineage, and
retains numerical/backend evidence. A training reference does not perform or
qualify a fit. The [retention contract](retention-predictions.md) excludes
variable flow, inferred delays, peak widths and physical validation.

## Evidence before automation

An agent can retrieve method settings, warnings, provenance and reported
outcome states. This supports an informed decision; it does not establish
fitness for a particular analytical use. Future objective extraction must
version its definitions, preserve failed and censored observations, and avoid
treating simulated data as physical measurements. The service has no blanket
human-signoff gate and currently sends no instrument commands.


The [Gaussian latent producer](gaussian-observation-producer.md) binds synthetic
width assumptions before attempts, consumes an owned immutable retention
prediction for the exact actual method, and records generated result identities
in one transaction. Its resolution calculation reuses the pure chromatography
helper. The observation registry verifies those exact generated results for a
producer-bound experiment. It does not accept caller replacements as generated
evidence. Existing experiments without a producer binding retain their original
caller-reported evidence contract and retry identities.

Campaign suggestions use the complete ordered consumed history. BO-MCP owns
surrogate fitting and acquisition optimization; this service implements no
second Bayesian optimizer. The optional child receives explicit finite inputs
and records its actual installed source/version/model/acquisition evidence.
The service keeps evidence and retry receipts, reconstructing training state
after restart. There is one pending proposal per campaign, full initialization
expenditure counts against its budget, and no fallback policy is substituted
after a backend failure. This path is limited to the predeclared two-coordinate
isocratic latent profile and does not execute an instrument.
