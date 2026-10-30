# Reproduce Gaussian centroid qualification

The installed offline command checks the synthetic Gaussian producer against
43 independently calculated numerical cases and ten separate expected-failure
checks. It preserves complete cohorts, statuses, pair identities and unavailable
values. Qualification makes no network requests and needs no author archive,
Torch, mpmath or R installation.

```sh
python -m measure_service.benchmarks qualify-gaussian-producer \
  --output-dir gaussian-qualification
```

The reviewed reference ships in both wheel and source archive. Optional
`--reference-json` selects another copy of those exact reviewed bytes; a changed
reference needs a separately reviewed baseline. These scientific input digests
do not restrict production dependency versions.

A Python caller can run the same operation:

```python
from pathlib import Path
from measure_service.benchmarks.gaussian_qualification import qualify_gaussian_producer

report = qualify_gaussian_producer(Path("gaussian-qualification"))
print(report["qualified"])
```

The output directory must be new or empty. The command stages all output and
publishes its completion manifest only after verifying reference integrity
again. Existing evidence bundles are preserved.

## What is compared

The reference was generated independently with 70-digit mpmath arithmetic and
exact parsed binary64 inputs, preserving decimal strings, hexadecimal values
and exact rationals. It contains every cohort size from one through 32, plus
censoring, singleton, equal-centroid, unequal-width, acquisition-boundary,
permutation, common-time-scaling and extreme-range cases. A farther neighbor
with a wider peak can determine the minimum incident resolution.

The named producer retains the source convention `sigma = FWHM / 2.3548`.
That approximate constant is part of the declared profile. The reference uses
its exact parsed binary64 value rather than substituting the exact mathematical
Gaussian factor. Comparison uses absolute tolerance **zero** and relative
tolerance `1e-12`; tiny positive resolutions cannot pass after replacement by
zero. Exact ties, nulls, IDs, scope and statuses must agree.

The producer keeps ordinary source arithmetic unchanged and uses an exact
rational fallback when an intermediate width denominator overflows or rounds
to zero despite a representable final ratio. A genuinely unrepresentable ratio
or positive resolution underflow raises. Negative widths, inconsistent event
statuses, incomplete/duplicate identities and oversized cohorts are distinct
input failures. None becomes a fabricated observed zero or censored result.

## Output and interpretation

| File | Contents |
| --- | --- |
| `report.md` | Human-readable numerical result and scientific limits |
| `report.json` | Separate numerical and failure-check results, exact reference/source/runtime evidence and declared tolerances |
| `cases.csv` | 43 numerical rows and ten failure-check rows identified by `case_kind` |
| `complete.json` | Byte counts and SHA256 digests of the three core files |

Every expected analyte, adjacent pair and nullable field is checked, not just a
numeric summary. The report distinguishes the number of cases from the number
of copied-input, resolution and status fields. Numeric/status disagreements
publish `qualified: false` evidence. Malformed, unbound, changed or truncated
reference data abort before publication. Checksum integrity does not imply a
signature or historical installed-software provenance.

The [producer contract](gaussian-observation-producer.md) states the qualified
scope: supplied ideal centroids, assumed FWHM widths and adjacency among finite
centroids. This calculation excludes amplitudes, noise, Gaussian tails,
after-window neighbors, peak finding, deconvolution and chemical correspondence.
The reference does not fit or qualify physical widths, signals, instruments or
an optimizer. Fresh BO readiness remains false.

From the source checkout or source archive, with R, ggplot2, jsonlite and digest
available, figures can be rendered from the
completed report without recalculating resolution:

```sh
Rscript scripts/plot_gaussian_qualification.R gaussian-qualification
```
