# Analysis methods

All numerical tools invoke the packaged R worker with bounded JSON input.
Results are stored with the method, input digest, backend versions, warnings
and code fingerprints. The initial checks use synthetic traces with known
expectations. They do not establish performance on every instrument or matrix.

## Chromatogram processing

`measurement_analyze_chromatogram` supports no baseline correction or asymmetric
least squares (ALS), followed by no smoothing or Gaussian smoothing. Peak
selection uses height, prominence and minimum distance. Peak areas use
trapezoidal integration with the selected local, global or no-baseline rule.
The stored result contains peaks and the processed trace.

ALS and Gaussian smoothing use sample indices. `smoothing_sigma` is in sample
points; `min_distance` is in the declared axis unit. On irregular grids, the
physical width of index-based preprocessing varies and the service returns a
warning. Integration uses the elution axis. Inspect the trace, peak boundaries
and baseline assumptions before interpreting areas. The tool does not resolve
coeluting components or assign chemical identities.

## Peak quantitation

`measurement_quantify_peak` takes a stored chromatogram analysis, one of its
peak IDs and caller-supplied response standards. The fit is linear with an
optional zero intercept and weighting of none, `1/x` or `1/x2`. At least three
distinct concentration levels are required. Inverse-concentration weights
require strictly positive concentrations. The fitted slope must be positive
and finite.

The caller declares the analyte identity and that standards and the unknown
use comparable integration, response units and detector conditions. Output
includes concentration, slope, intercept, fit diagnostics and the standard
range. Extrapolation produces a warning. Internal-standard correction,
dilution factors, LOD/LOQ calculations and uncertainty intervals are not
applied by this tool, even where an R backend has other validation functions.

## Relative SEC

`measurement_analyze_sec` requires a trace marked `SEC` with time in `min` or
volume in `mL`. The caller supplies standards in g/mol, their locations and a
standard description, and explicitly asserts that the input is baseline
corrected and proportional to weight concentration.

The backend fits log10 molecular weight against elution location using a
linear, quadratic or cubic calibration. Those choices require at least two,
three or four distinct locations, respectively. Integration range boundaries
select existing sampled points; boundary values are not interpolated. At least
two usable positive-weight points are needed. The configured extrapolation
policy determines treatment outside the calibration domain.

Output includes relative Mn, Mw, Mz, peak molecular weight, dispersity,
calibration diagnostics and a distribution. These depend on the supplied
standards, detector response and integration domain. They are not absolute
molecular weights, a universal calibration or a light-scattering fit.

`measurement_compare_sec` compares two to eight distinct stored SEC analyses
against the first. It reports numeric deltas, warnings and equality checks for
method digests, installed R-code fingerprints, and detector/channel/unit
metadata. Matching flags do not prove equivalent samples or instrument
conditions, and deltas do not establish statistical significance.

## Provenance and interpretation

Each saved analysis records SHA-256 digests of normalized input, method and
worker code, plus the original text digest when available. Backend metadata
include R/package versions and MD5 fingerprints of the installed `measure`
and `measure.sec` R code databases. Optional operator-declared source
revisions are recorded separately.

These fingerprints help distinguish builds; they are not a complete dependency
lock, software bill of materials, authentication proof or validation record.
Warnings and assumptions remain part of the scientific result. The
`qualification: experimental` field describes implementation status. The
`artifact: true` field is a client display hint; it does not upload a file or
create a managed asset.
