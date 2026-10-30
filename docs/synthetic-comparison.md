# Run the predeclared paired synthetic comparison

The `synthetic-isocratic-paired-comparison/1` benchmark runs both supported
policies on the same two constructed three-analyte tasks and seeds 17, 29 and
43. Each arm has ten evaluations, including three common Latin-hypercube
initial points. The complete plan contains twelve campaigns and 120 evaluation
slots. Every task and seed stays in the report, whether it succeeds, fails or
leaves an outcome unresolved.

Install the [optional BO-MCP runtime](campaign-runtime.md#optional-real-bo-mcp-runtime)
and configure `MEASURE_BO_PYTHON`. From an installed service environment:

```sh
python -m measure_service.benchmarks run-synthetic-campaigns \
  --data-dir /tmp/measure-paired-synthetic-store \
  --output-dir /tmp/measure-paired-synthetic-report
```

No R installation or network request is needed to run the benchmark after the
service and optional backend have been installed. The command prints the
generated `report.md` path. It may take several minutes because each GP
suggestion reconstructs the complete consumed training history in its separate
runtime. The command does not execute an instrument.

## Fixed scientific inputs

The packaged
[declaration](../src/measure_service/benchmarks/reference_data/synthetic-isocratic-paired-v1.json)
was fixed before fresh optimization. Its SHA256 is
`0728c6b2e03132d2889fef030c57a5331cae24067ca3fc5e6c40256f1238b38e`.
The command verifies those exact bytes and exposes no task-selection or
post-hoc budget/seed overrides. Register a separate protocol through the service
API for a different experiment.

Both tasks use explicit linear natural-log retention equations, fixed synthetic
FWHM of 0.15 minutes and the existing Gaussian finite-centroid profile. The
column declaration gives a 0.5-minute dead time at the fixed 0.5 mL/min flow.
The first task has parallel solvent-response slopes; the second has crossing
slopes. They are constructed, unfiltered cases, not estimates from physical
standards or samples selected for a policy's observed advantage.

The search coordinates are strong-solvent fraction 0.1–0.9 and acquisition
duration 2–10 minutes. Both arms use the same predeclared initial coordinates,
but each owns and counts its own evaluations. The scalar GP fits the declared
aggregate source-profile utility; it has no privileged access to latent model
parameters or unselected results. Analytical detection, resolution availability,
strict acceptance and utility remain separately inspectable.

## Evidence and interpretation

Each evaluation retains the exact proposed method, actual simulator attempt,
retention prediction, Gaussian results, analytical assessment, frozen snapshot
and campaign consumption. The report provides both of these cumulative series:

- Strict analytical acceptance under the declared observation contract.
- Finite-resolution source-profile utility at least 2.0.

Both series use the full six-campaign denominator per policy. Unknown outcomes
form an explicit upper bound; that range is not a confidence interval. A
campaign that never has a confirmed event retains a null first-success index
and its status at the budget. Paired task/seed tables do not drop unsuccessful
campaigns or estimate a population effect from these six pairs.

Cost, simulated acquisition duration and declared solvent volume include
completed simulator attempts whose observations were not consumed. Separate
consumed totals and coverage counts expose that difference. These are declared
synthetic quantities, not monetary cost, measured solvent use or physical
instrument time. Persisted optimizer proposal wall time includes the optional
child-process launch and is reported independently, even if a later observation
stage fails. Raw retention/censor states, finite resolutions, detection counts,
runtime preference and source identities remain available per evaluation.

The flat bundle includes readable Markdown/JSON reports, per-evaluation and
campaign JSONL, CSV trajectory/pair tables, the complete protocol/task inputs,
and a SQLite evidence backup. `complete.json` seals every core file by SHA256
and byte count. Checksums identify files; they do not by themselves establish
scientific correctness. Existing evidence directories are never overwritten.

## Recover an interrupted comparison

Use an explicitly dedicated data directory. A new store receives a persistent
runner/input marker. Existing unmarked, mismatched or mixed stores are rejected
before they can be changed or exported. The runner verifies ownership again
before making its evidence backup; it never exports the normal user service
database.

Original retry keys are derived from the frozen input and logical task, seed,
policy, ordinal and operation. A resumed run reuses the original records and
receipts. If a consumption committed but its acknowledgement was lost, the
runner reconciles that exact durable consumption rather than executing another
attempt or losing a successful observation from its trajectory.

If a partial report already exists, preserve it and use a new output location:

```sh
python -m measure_service.benchmarks run-synthetic-campaigns \
  --data-dir /tmp/measure-paired-synthetic-store \
  --output-dir /tmp/measure-paired-synthetic-report-resumed
```

Backend or numerical failure is reported with its stage. Other planned
campaigns still run, future unattempted slots remain unknown, and the report's
`completed` field stays false until all twelve campaigns exhaust their budgets.
No random fallback, replacement score or successful event is invented.

## Relation to MoSeS

This is an executable ideal integration comparison. It differs from the paper's
filtered 50-mixture, three-segment, 100-evaluation experiment and its surrogate
settings. It does not qualify physical model fits, noisy traces, peak picking,
chemical correspondence, instrument operation or multi-fidelity execution.
The [archived-data reconciliation](moses-metric-reconciliation.md) and
[paper-protocol issues](https://github.com/JamesHWade/measure/issues/108)
retain their separate evidence and unresolved requirements.
