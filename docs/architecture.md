# Architecture

The MCP process validates tool arguments, resolves the principal, enforces
record ownership and stores records in SQLite. Numerical calls pass JSON to
a fixed R subprocess. The worker returns JSON; the MCP process stores the
result and returns a compact record. Arrays and original text are read in pages.

No tool accepts executable code, a server file path or an arbitrary URL. The R
worker path comes from the installed Python package. A numerical call is
synchronous and has a 90-second subprocess timeout. There is no job queue or
worker pool.

## Repository boundaries

| Component | Responsibility |
| --- | --- |
| `measure` | R measurement structures, preprocessing, calibration and validation primitives |
| `measure.sec` | SEC calibration, moments, distributions and technique-specific calculations |
| Future technique packages | Domain models and methods that can be tested independently of an agent protocol |
| `measure-service` | MCP contracts, identities, evidence records, storage and backend adapters |
| Future BO adapter | Translate qualified evidence and objectives to an optimizer protocol |
| Optimizer engine | Surrogate models, acquisition functions and optimizer state |
| Client application | User interaction, authenticated uploads and policy for using outputs |

Scientific methods belong in reusable backend packages. This repository should
validate inputs and preserve scientific context without maintaining a second
implementation of the same calculation. A future LC or MS backend can be R or
Python; it must declare its supported data model, units, source revision and
qualification evidence.

The service owns experiment evidence: intended conditions, actual attempts,
measurement links, outcomes and corrections. An optimizer owns its model and
suggestion state. Campaign orchestration must record which immutable evidence
snapshot produced each suggestion and when an outcome was consumed. Those
contracts and synchronization logic are roadmap work; an external proposal
label alone does not implement them.

## Code layout

- `src/measure_service/server.py`: MCP tools, trace/method validation and R adapter.
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
