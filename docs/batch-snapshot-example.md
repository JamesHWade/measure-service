# Reproduce the batch snapshot workflow

The executable example captures a partially completed analytical batch, imports
its evidence into a new store, and explicitly runs the new pending batch. It
compares the two originally successful traces with their fresh results.

Install the service and the compatible R backends described in the
[quickstart](quickstart.md). From a source checkout, run with three new,
separate directories:

```sh
MEASURE_MCP_R="$(command -v R)" python examples/batch_snapshot.py \
  --output-dir /tmp/measure-snapshot-output \
  --source-dir /tmp/measure-snapshot-source \
  --restore-dir /tmp/measure-snapshot-restored
```

Existing directories, files, symlinks and nested targets reject before the
workflow starts. The output contains two ZIP archives, six HTML/SVG/JSON/CSV
report sets, `analysis-index.csv`, `summary.html` and `workflow_receipt.json`.
The source and restored SQLite stores remain available at the supplied paths.
The receipt records IDs, content hashes, runtime observations, comparison
tolerances and the workflow's intermediate states.

## Read the comparison at its actual scope

| Stage | Retained evidence |
| --- | --- |
| Input | One wide CSV, five traces and 2,505 raw points; four selected traces and 2,004 selected points |
| Source batch | Two successes, one controlled R-start failure, cancellation and selected retry; frozen with two completed and two pending items |
| Archive | Complete dataset closure including the unselected trace; two historical analyses, three attempts and two control events |
| Import | New dataset/analysis IDs, two copied historical analyses, four new pending items; no active historical runs, attempts or controls |
| Explicit fresh execution | Four new analyses from two bounded R-backed runs |
| Numerical comparison | Only the two originally successful traces; the original unresolved outcomes remain unresolved |

The source uses explicit reference, blank, QC and unknown roles. These labels
are caller declarations. The deliberate failed attempt retains unknown R
runtime evidence; it is not filled from a later successful run.

Historical report generation and both snapshot exports run with R unavailable.
The references-only archive rejects before creating an import target. The
self-contained archive retains original text, mappings, results and scientific
provenance. Separate import-origin records explain the new local IDs.

For the two supported comparisons, numeric values use absolute tolerance
`1e-10` or relative tolerance `1e-8`. Trace/method identity, categorical values,
missing values and row order remain exact. The comparison covers raw and
processed coordinate grids, signals, stored peak rows and result metadata.
Two matching historical traces do not establish four historical successes.

Package versions, installed-code fingerprints and R versions are execution
observations. The receipt records both builds separately; they are not an
exact-equality gate on numerical reproduction. Agreement within the declared
tolerance applies to these compared results and does not qualify every build
or establish physical equivalence.

The fresh QC and unknown results establish availability of new computations;
their original source outcomes stay unknown. This is a synthetic single-trace
workflow check, without compound identification, physical method validation,
instrument execution, MOCCA2 interoperability or a new optimizer study. See
the [snapshot contract](batch-snapshots.md) for import limits and the distinction
between retained evidence and fresh execution.
