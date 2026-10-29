# Reproduce the retention input qualification

The installed command compares all 500 archived parameter rows with the
packaged source-law reference and exercises 18 independently calculated
continuous-retention cases. It verifies input identities before joining any
records, retains disagreements and numerical failures, and publishes an atomic
report. It does not fit parameters or start an optimizer.

## Run from an installed package

Download the author archive explicitly once, or use the already verified ZIP:

```sh
python -m measure_service.benchmarks fetch-moses --cache-dir reference-data
python -m measure_service.benchmarks qualify-retention-inputs \
  --archive reference-data/moses-record-22109729-results.zip \
  --output-dir retention-qualification
```

The download is about 1.49 GB. Qualification performs no network requests,
extracts no files and loads no serialized Python objects. It reads only bounded
safe compound-set JSON members after verifying the full source archive. The
source-law and independent timing references ship in both wheel and source
archive, with [data attribution](../src/measure_service/benchmarks/reference_data/README.md).
Torch and mpmath are reference-generation tools; the qualification runtime uses
neither.

A Python caller can use the same interface:

```python
from pathlib import Path
from measure_service.benchmarks.retention_qualification import qualify_retention_inputs

report = qualify_retention_inputs(
    Path("reference-data/moses-record-22109729-results.zip"),
    Path("retention-qualification"),
)
print(report["qualified"])
```

Existing output is preserved. Select a new output directory for another run.
Optional `--law-oracle-json` and `--trajectory-oracle-json` arguments select
copies of the reviewed reference files; their digests and schemas must match.
A different baseline needs its own reviewed reference/profile update. These
are immutable scientific inputs, not production dependency-version pins.

## Read the results and their limits

The verified source has 50 compound-set files with ten analytes each. Each row
is evaluated conditionally under linear, quadratic and Neue–Kuss laws at 21
compositions: **31,500 pointwise comparisons**. The JSON files do not identify
which model was selected for each original analyte. Their member and canonical
parameter digests bind every comparison without copying coefficient arrays
into the output.

All 500 rows are mathematically in-domain under each conditional model; 497
lie inside each model's declared source fitting bounds. The other three have
`S1 > 250`. Fitting ranges are advisory metadata, with no clipping or claim
that a bounded parameter has a good fit. Relative law-comparison tolerance is
`1e-12`, with zero absolute tolerance to avoid hiding errors in tiny positive
retention factors.

The independent timing reference uses 70-digit calculations on the exact
parsed binary64 inputs. It includes geometry scaling, piecewise gradients,
nonmonotone Neue–Kuss retention factors, post-column transit, right censoring,
a decimal-rounding counterexample and a binary-exact inclusive endpoint.
The predictor requests `1e-9 min` absolute numerical tolerance for this
comparison. The report separately states its comparison tolerances and retains
actual absolute/relative errors, statuses and unavailable values.

| File | Contents |
| --- | --- |
| `report.md` | Human-readable qualification summary and scientific boundary |
| `report.json` | Counts, declared tolerances, exact input/source/implementation evidence and readiness flags |
| `law-comparisons.csv` | 1,500 analyte/law rows with source/parameter/reference digests, numerical differences and fitting-range diagnostics |
| `retention-cases.csv` | 18 synthetic status/time comparisons, including explicit calculation-error rows |
| `complete.json` | SHA256 and byte counts for all four core files |

Numeric/status disagreements set `qualified` to false while retaining the
comparison. Nonfinite or nonpositive calculated factors, exhausted solver
budgets and unresolved numerical classifications remain failed calculations;
they do not acquire zero errors or fabricated elution states. Malformed,
unbound, incomplete or mutated inputs abort before publishing a result bundle.
Input checksums establish integrity, not signatures or historical installed
software provenance.

The [source/unit audit](retention-model-qualification.md) and
[ideal prediction contract](retention-predictions.md) define the qualified
scope. Original model assignment, physical parameter provenance, fitted
plate-height/diffusion units, widths, measured delays, signal processing,
instruments and fit quality remain separate work. The report keeps
`fresh_bo_readiness` false. New suggestions still need a declared matched
protocol and qualified observation producer.

## Render figures from completed tables

From the source checkout or unpacked source archive, with R, ggplot2 and
jsonlite available:

```sh
Rscript scripts/plot_retention_qualification.R retention-qualification
```

The script renders PNG and SVG law-agreement and timing-error figures from the
completed report/tables. It performs no retention calculation or reclassification.
