# Import, correct and replay a reported table

Run the installed public-MCP example to create a new synthetic experiment,
two declared technical-replicate attempts, two successful CSV imports and a
retained observation snapshot:

```sh
python examples/result_tables.py \
  --output-dir /tmp/measure-result-table-output \
  --data-dir /tmp/measure-result-table-store
```

Both paths must be new, separate and non-nested. The example validates every
target before creating output and rejects existing paths, including dangling
symlinks. Omitting `--data-dir` uses a temporary store. An
`EXAMPLE_INCOMPLETE` marker remains in the output if execution fails.

The [executed report and downloads](reference-data/result-table-example/report.html)
retain the recorded 11-row import, correction, old snapshot and full result
history. The [independent audit](reference-data/result-table-example/independent-review.json)
verified 522 source-to-store/artifact checks without invoking service validators
or recomputing analytical values. The [publication manifest](reference-data/result-table-example/publication.json)
binds these original output bytes to their recorded execution source and wheel.
The displayed `report.html` uses the current renderer with those retained
receipts and rows. It changes label spacing and phone wrapping only; the original
executed `index.html` and its original manifest remain byte-identical. Renderer
identity and inputs are recorded separately in the publication manifest.

The workflow uses the installed service over two successive stdio MCP sessions.
It makes no direct database or registry calls and requires no R backend.
The [table importer](result-tables.md) still operates on existing owned attempts;
the example creates its own attempts before preparing the mapped sources.

## What is reported

The first attempt declares a seven-minute method and a predeclared two-analyte
contract. Caller-confirmed constructed labels A/B have retention results of
2 minutes and 240 seconds, and dimensionless resolution results of 2 and 1.5.
The contract's inclusive resolution threshold is 1.5. Assignment evidence and
fidelity are caller declarations; these are constructed manual records.

The second attempt supplies one result in every supported outcome state:
observed zero, not detected, below limit, interval censored, missing, failed and
infeasible. It is not silently assessed as a complete analytical observation.
Its plate/notes columns remain raw source metadata, including CRLF, a quoted
multiline note and Unicode text.

| Stage | Imported rows | Retained results | Analytical evidence |
| --- | ---: | ---: | --- |
| Initial table | 11 across two attempts | 11 | First attempt explicitly assessed and snapshotted |
| Rejected table | 0 of two rows | 11 | No new result or successful import receipt |
| Explicit correction | 1 | 12, including the superseded result | Prior observation/snapshot remain unchanged |
| Explicit reassessment | No import | 12 | New assessment uses corrected result IDs |
| Restart and retry | No new rows | 12 | Original receipt, raw source and row bindings replay unchanged |

The rejected source starts with a valid new metric and ends with an incorrect
expected attempt revision. Both rows roll back. A later correction explicitly supersedes
B's original resolution with observed zero. The original analytical assessment
stays accepted in its snapshot; the new explicit assessment is not accepted.
No analytical decision is created automatically by importing a table.

The published CSV files retain that execution's existing attempt/result IDs.
Run the executable workflow to create new targets and source mappings. New
IDs and timestamps change file hashes while retaining the demonstrated
contracts; the frozen reference remains unchanged.

## Inspect the output

Open `index.html` for the source/import/history summary, status counts and
per-row values with original units and censoring bounds. Tables have named,
focusable scroll regions. The report links:

- `initial.csv`, `correction.csv` and `rejected.csv`: exact submitted sources.
- `initial-import.json` and `correction-import.json`: verified receipts and
  original normalized source-row/result bindings.
- `results.json`: twelve full immutable result records, including the
  superseded result and separate current successor.
- `observations.json` and `snapshot.json`: old/corrected assessments and the
  preserved original snapshot.
- `outcome-rows.csv`: one row per imported result, with source coordinates,
  states, values, units, bounds, result IDs and content digests.
- `workflow_receipt.json` and `manifest.json`: public-call verification,
  declared scope and output-file hashes.

All seven outcome states remain distinct. Missing numeric values stay absent,
observed zeros stay zero, and one-sided/interval bounds retain their units.
The current result head changes only through the explicit correction; the
original source's result membership does not change.

## Scope

This validates software ingestion, correction, analytical evidence history and
restart behavior for constructed scalar outcomes. It does not acquire data,
establish chemical identity, validate units or calibration, fit a model,
qualify an instrument or run an optimizer campaign. Declared technical
replicates do not establish statistical independence. Service #4 remains open
for design supersession, further historical workflows and broader lineage.
