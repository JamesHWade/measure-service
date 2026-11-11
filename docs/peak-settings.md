# Choosing chromatogram thresholds and baseline settings

`measurement_analyze_chromatogram` applies preprocessing, peak detection, and
area integration in sequence. Thresholds operate on the signal after the
selected baseline correction and smoothing. Peak height and prominence use the
declared `signal_unit`; minimum distance uses the `axis_unit`.

| Setting | Default and accepted range | Effect |
| --- | --- | --- |
| `baseline` | `"none"`; `"none"` or `"als"` | Applies no ALS preprocessing or subtracts an asymmetric least-squares baseline. |
| `baseline_lambda` | `1e6`; `0 < value <= 1e12` | ALS smoothness penalty on second differences of the sample-index signal. Larger values generally give a smoother estimated baseline. |
| `baseline_p` | `0.01`; `0 < value < 1` | Unitless ALS weight for residuals above the fitted baseline. Residuals below it receive weight `1 - p`; smaller values downweight peaks more strongly. |
| `smoothing` | `"none"`; `"none"` or `"gaussian"` | Leaves the signal unsmoothed or applies Gaussian smoothing on the sample-index grid. |
| `smoothing_sigma` | `1`; `0 < value <= 100` sample points | Standard deviation of the Gaussian kernel in sample points, not axis units. |
| `min_height` | `0`; `value >= 0` in `signal_unit` | Minimum processed signal value at a detected apex. Zero disables height filtering. |
| `min_prominence` | `0`; `value >= 0` in `signal_unit` | Minimum prominence for the service's prominence detector. Zero disables prominence filtering. |
| `min_distance` | `0`; `value >= 0` in `axis_unit` | Minimum separation between retained peak locations. Zero disables distance filtering. |
| `integration_baseline` | `"local"`; `"local"`, `"none"`, or `"global"` | Baseline rule used for observed-signal area calculation. |

## Select detection thresholds from the trace

The default zero thresholds are valid. They leave height, prominence and
distance filtering disabled; when all three are zero, the R worker adds an
advisory warning that noise may be retained. It does not change the values or
choose thresholds.

Compare the signal range and noise fluctuations with the peaks you intend to
retain. A nonzero `min_height` sets an absolute processed-signal floor. A
nonzero `min_prominence` requires a candidate to stand above its surrounding
contour by the requested signal amount. Use `min_distance` when nearby
candidates closer than a meaningful axis spacing should not both be retained;
the prominence detector keeps stronger candidates first when applying that
filter. A distance threshold can also remove a real nearby peak.

When suitable controls are available, inspect blanks, references, and
replicates alongside the trace. Choose values in the units shown in the
measurement metadata and record them with the method. The service does not
estimate a noise floor, infer instrument resolution, or propose instrument
specific thresholds. No numeric threshold is universal across detectors,
sampling grids, samples, or signal processing choices.

## Keep preprocessing and area baselines separate

`baseline="none"` leaves the acquired signal unchanged before smoothing and
detection. Background can therefore affect processed peak heights and
prominence. This setting is different from `integration_baseline`:

- `integration_baseline="local"` subtracts the line between the recorded left
  and right integration bounds.
- `integration_baseline="none"` integrates the signal to zero, so background
  signal in the interval contributes to area.
- `integration_baseline="global"` subtracts the minimum signal within the
  peak interval.

The current observed-signal integrator clips negative corrected values to
zero before trapezoidal integration. Areas remain signal times axis; they are
not concentrations or compound identities. See the [method overview](analysis.md)
for broader limits and the [peak boundary guide](peak-boundaries.md) for
overlap and historical-result behavior.

## Read prominence and integration bounds correctly

The prominence detector first evaluates candidate peaks using their
prominence contours. After prominence, height, and distance filters have
removed candidates, the current `measure` implementation uses separate
integration bounds for the retained peak rows. For adjacent retained peaks,
it clips their bounds to the lowest sampled signal value between their apexes.
A candidate removed by a threshold does not split the remaining peaks'
integration ranges. A remaining range can consequently include background or
signal from a filtered-out peak.

The service publishes `left_base` and `right_base` as integration bounds; they
are not a promise that the prominence contours had those same coordinates.
The core package may retain separate contour-base columns, but the service's
fixed peak-row contract omits additional backend columns. The installed R code
fingerprint identifies the backend used by each analysis. Historical peak
areas and bounds are retained as recorded; a report may show a separate
view-only overlap diagnostic without changing its evidence files or digest.

## Interpret ALS settings on the sample grid

The current ALS implementation penalizes second differences of the signal
vector. It receives signal values but does not use the trace's axis values in
the smoothness calculation. `baseline_lambda` therefore has no physical axis
unit: its effect depends on the sample grid and on the baseline, peak, and
noise pattern. The same numeric lambda should not be assumed to yield the same
physical smoothness after changing the sampling grid. The API default of
`1e6` is a convenience default, not a universal or instrument-specific
recommendation. The asymmetry setting `baseline_p` also changes how peaks
above the fitted baseline are weighted.

The service applies ALS with the R step's default iteration limit of 20. The
step checks the maximum baseline change across the signal against a global
tolerance and otherwise returns the latest estimate at the iteration limit.
It does not report the iteration count, a convergence flag, or
endpoint-specific convergence diagnostics, and the service does not infer
such diagnostics from the returned trace.

ALS and Gaussian smoothing both use sample indices. On irregular axes, their
effective width varies along the axis and the worker returns a warning. Inspect
the raw trace, processed trace, and chosen settings together. The service does
not select lambda or qualify a baseline for an instrument or method.

The following fixed synthetic study records the evaluated ALS implementation
over a predeclared grid of lambda values, sample counts, noise levels, and
seeds. The three sample grids are nested subsets of each seed's 1601-point
noise realization, and each noise level scales the same seed-specific vector.
The median lines are paired descriptive summaries, not independent-density
inference. All three noise panels use the same vertical range, starting at
zero. The figure plots medians across the five declared seeds; all 315 fit rows
and all 45 signal inputs are available with their hashes. It describes only
that synthetic grid and does not select a best setting.

The recorded `baseline_rmse_peak_free` column name refers to the declared
low-peak windows away from the Gaussian centers. Gaussian tails remain in
those windows, so this metric is not a measure over strictly peak-free data.
The endpoint columns report baseline error in the declared endpoint windows;
they are not ALS convergence diagnostics.

![Three panels show median baseline RMSE across the fixed lambda grid for noise standard deviations 0.01, 0.03, and 0.06. Each panel compares 101-, 401-, and 1601-point sample grids over five declared seeds; the figure describes only this synthetic study and does not select a general-purpose lambda.](reference-data/als-sampling-study/lambda-sampling-sensitivity.svg)

- [Study declaration](reference-data/als-sampling-study/declaration.json)
- [Input hashes](reference-data/als-sampling-study/input_manifest.csv)
- [All synthetic input samples](reference-data/als-sampling-study/inputs.csv)
- [All 315 fit metrics](reference-data/als-sampling-study/fits.csv)
- [Seed summaries](reference-data/als-sampling-study/summary.csv)
- [Run and runtime provenance](reference-data/als-sampling-study/run.json)

To prepare and rerun the fixed grid in a fresh output directory, use an
installed R environment containing `measure` and `recipes`. The preparation
command writes the declared inputs and hashes without fitting. The run command
uses the R binary selected by `MEASURE_MCP_R` and records its actual installed
package versions, code fingerprint, and library paths. It does not require the
reference runtime and does not choose a lambda.

```sh
python scripts/qualify_als_sampling_study.py \
  --prepare --output-dir /tmp/als-study-new
MEASURE_MCP_R="$(command -v R)" \
  python scripts/qualify_als_sampling_study.py \
  --run --output-dir /tmp/als-study-new
```

For optional source provenance, also pass `--measure-source-dir PATH`; a
non-Git source archive can supply its recorded head with
`--source-head-file PATH`. Source provenance is recorded when provided but is
not required to fit the declared inputs.

The study has synthetic software scope. It does not qualify an instrument,
establish a method, rank settings for user data, or claim a universal lambda.
