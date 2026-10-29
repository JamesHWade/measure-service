# Reconcile archived MoSeS metrics

This audit identifies what the public LCBO source defines, compares those
definitions with the fields in the checksum-verified result archive, and keeps
the historical gap explicit. The archive’s serialized summaries are the
reproduction inputs for its recorded scalar scores. Their agreement does not
identify the code that produced them or establish that they are valid
measurements. The selected contract for later optimization is in
[`moses-observation-contract.md`](moses-observation-contract.md).

## Source history and definitions

The Zenodo archive `results.zip` has SHA256
`880533ded926f766ce9504d4f164e9c5195b775e40061c18d70ae79497f03f71` and MD5
`25b40b0f56467d83ee5ba434a8872cbb` ([record 22109729](https://doi.org/10.5281/zenodo.22109729)).
The audit read all 146,405 observations through the bounded archive reader and
literal-only record decoder described in the [reproduction protocol](moses-reproduction.md).

As inspected on 2026-10-28, GitHub exposed one LCBO branch (`main` at
`35098fb88e19cdc0786ed416dec92aa9f833cc2f`) and no tags. The file histories
for the experiment metric class, experiment routine, and campaign serializer
each begin at the initial public-release commit `66b761f4aadf149db409444991f57bdc52a87375`;
the public history contains no earlier implementation of these routines
([branches](https://api.github.com/repos/fsk-lab/lcbo/branches?per_page=100),
[tags](https://api.github.com/repos/fsk-lab/lcbo/git/matching-refs/tags/),
[metric history](https://api.github.com/repos/fsk-lab/lcbo/commits?path=lcbo%2Fhplc_utils%2Fhplc_experiment.py&per_page=100),
[routine history](https://api.github.com/repos/fsk-lab/lcbo/commits?path=lcbo%2Froutines%2Fexperiment.py&per_page=100),
[campaign history](https://api.github.com/repos/fsk-lab/lcbo/commits?path=lcbo%2Fdata%2Fcampaign.py&per_page=100)).
This describes the reachable public history, not unpublished source.

At the reviewed LCBO revision, the definitions are:

| Path | Public source definition |
| --- | --- |
| Simulated observation | Sort latent retention times. For adjacent analytes, `R[i,i+1] = (t[i+1] - t[i]) / (2 * (sigma[i] + sigma[i+1]))`. Each analyte receives the smaller of its left and right adjacent-pair values; an absent boundary neighbor is `+Inf`. The wrapper constructs these values from the simulator’s latent retention times and sigmas. ([source, lines 47–82](https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/lcbo/routines/experiment.py#L47-L82)) |
| Instrument analysis | Calls `peaks.peak_resolutions(..., fitted=True)` from the external `moses` package. If a requested target peak is absent, it records retention time `+Inf` and resolution `NaN`. This wrapper does not define the upstream fitted-peak equation or version. ([source, lines 153–184](https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/lcbo/routines/experiment.py#L153-L184); [upstream link in LCBO README](https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/README.md#L13-L19)) |
| `num_peaks` | Count finite per-analyte resolutions. ([source, lines 53–60](https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/lcbo/hplc_utils/hplc_experiment.py#L53-L60)) |
| `num_resolved_peaks` | Count finite resolutions at or above the supplied threshold; the method default is `1.5`. ([source, lines 58–60](https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/lcbo/hplc_utils/hplc_experiment.py#L58-L60)) |
| `critical_resolution` | Minimum finite per-analyte resolution, or `0.0` when none is finite. ([source, lines 62–67](https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/lcbo/hplc_utils/hplc_experiment.py#L62-L67)) |
| Scoring inputs | `NumPeaks-CritRes` passes `num_peaks` and `critical_resolution`; `NumPeaks-NumResPeaks` passes `num_peaks` and the configured resolution-threshold count; `SinglePeak-Resolution` passes the selected target’s elution flag and resolution. ([source, lines 465–501](https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/lcbo/data/config.py#L465-L501)) |

There is a serialization gap. The public `HPLCExperiment.to_dict()` stores the
underlying arrays but omits `num_peaks`, `num_resolved_peaks`, and
`critical_resolution`; the campaign serializer writes those experiment
dictionaries directly. ([experiment serializer, lines 69–80](https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/lcbo/hplc_utils/hplc_experiment.py#L69-L80);
[campaign serializer, lines 311–331](https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/lcbo/data/campaign.py#L311-L331)). The archive nevertheless contains those three summary fields, and they
often disagree with the arrays. This means the public source defines a clear
array-derived reference calculation, but it does not identify how the
archived summary fields were generated. The README describes `botier == 1.0.0`
as the development version; the archive does not record a run-specific
environment or LCBO revision. ([README, lines 13–19](https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/README.md#L13-L19)). A numerical match to a named BoTier profile does not establish which
software version was installed when the archive was produced.

## Archive comparisons

Count comparisons below are exact integer equality. For real-valued metrics,
“exact” means binary64 equality; the tolerance column uses both absolute and
relative tolerance `1e-12`. Rows lacking a reported summary are excluded from
the candidate comparison. There are 146,343 rows with global summary fields and
62 targeted rows with no global-summary candidate.

The explicit candidates are `count(isfinite(Rs))`,
`count(isfinite(RT) & RT <= run_time)`,
`count(isfinite(Rs) & Rs >= threshold)`, and `min(Rs[isfinite(Rs)])` (with
the public source's `0.0` fallback when no finite resolution exists). For the
simulation array reconstruction, sort finite RTs and use
`R[i,i+1] = (RT[i+1] - RT[i]) / (2 * (sigma[i] + sigma[i+1]))`, where
`sigma = FWHM / 2.3548`; each analyte's array entry is the minimum of its
adjacent pair values. The resolved-count comparison at each observation's
configured threshold has the same counts as using the source method default
`1.5`.

| Archived summary compared with candidate | Compared | Exact matches | Within `1e-12` | Outside `1e-12` | Candidate unavailable |
| --- | ---: | ---: | ---: | ---: | ---: |
| `num_peaks` vs. count of finite `peak_resolutions` | 146,343 | 142,399 | 142,399 | 3,944 | 0 |
| `num_peaks` vs. count of finite retention times `<= run_time` | 146,343 | 142,496 | 142,496 | 3,847 | 0 |
| `num_resolved_peaks` vs. finite resolutions `>=` configured threshold | 146,343 | 131,845 | 131,845 | 14,498 | 0 |
| `critical_resolution` vs. minimum finite `peak_resolutions` | 146,343 | 27,264 | 27,572 | 118,771 | 0 |
| `critical_resolution` vs. adjacent-pair minimum reconstructed from RT and FWHM using the source sigma equation | 145,848 | not separately counted | 27,027 | 118,821 | 495 |

For `critical_resolution`, 119,079 summary values are exactly unequal to the
array minimum; 308 of those are still within `1e-12`, leaving 118,771 outside
the tolerance. The retained source summary is not simply the minimum of the
archived per-analyte array.

The two `num_peaks` alternatives differ by 97 exact matches. Across finite
retention-time values, none equals or exceeds its recorded run endpoint, so
the inclusive and exclusive run-window candidates have the same result. The
archive has 97 simulated observations with exactly one finite retention time
but no finite resolution. Under the public adjacent-neighbor rule, a singleton
has `+Inf` resolution; the finite-resolution count therefore excludes it even
though a retention time exists. That is a concrete reason these metrics cannot
be called equivalent measures of analyte presence.

In the 146,325 simulated observations, the source equation reconstructed from
stored `FWHM` using `sigma = FWHM / 2.3548` matches all 1,429,994 finite
per-analyte resolution values within `1e-12`. Retention-time ties occur in
26,863 observations (183,330 tied pairs); the reconstruction still matches the
finite resolution entries. This validates the archived simulation array
construction against the reviewed wrapper. It does not recover the archived
summary algorithm or validate instrument analysis.

The instrument-analysis cohort contains 80 observations. The 50 rows with a
reported critical-resolution summary all agree exactly with the minimum finite
resolution array. The Gaussian adjacent-pair reconstruction from stored
retention times and FWHMs matches 0 of those 50 rows. This is consistent with
the public instrument wrapper delegating fitted resolution to external
`moses`; the public LCBO source alone cannot establish the historical fitted
resolution formula.

At the per-analyte value level, simulated records contain 32,962 positive
infinite and 15 NaN retention times; 495 positive infinite and 32,579 NaN
resolutions; and 32,977 NaN FWHMs. Of the simulated observations, 495 have
fewer than two finite retention times, so an adjacent-pair reconstruction is
unavailable: 97 have exactly one finite RT and 398 have none. For the 80
instrument observations, RT has 777 finite and 113 positive-infinite values;
Rs has 777 finite and 113 NaN values; FWHM has 775 finite and 115 NaN values.
No finite retention time in the archive lies at or beyond its recorded run
endpoint.

## Score replay and disposition

For the untargeted observations, the archived scalar scores match the explicit
published BoTier 1.0.0 profile evaluated from the serialized count and
resolution summaries for all 146,343 rows at `abs_tol=rel_tol=1e-10`. The other
62 observations use `SinglePeak-Resolution` and the selected target input, as
the LCBO scorer specifies. Evaluating all observed scores from the raw
per-analyte arrays under the same profile matches 33,848 of 146,405 archived
scores within tolerance and changes 856 `score >= 2` classifications. The
summary-input score fit identifies a useful immutable reproduction baseline;
it does not identify the historical summary implementation or establish
analytical correctness.

The source-supported interpretation is therefore limited to two statements:
the public LCBO code defines the array-derived reference formulas above, and
the archived scalar scores are reproduced by the stored summary inputs (or the
selected target input for targeted scores). No exact historical implementation
of the global summary fields was identified in the public LCBO history or by
tested candidates. Preserve that uncertainty; do not silently substitute one
candidate for the other. The separate
[`moses-observation-contract.md`](moses-observation-contract.md) declares the
inputs and acceptance rules selected for subsequent optimization.


## Reproduce the metric-candidate audit

From a source checkout with the service installed, use the verified archive and
original replay as inputs, and write a separate bundle:

```sh
python scripts/audit_moses_metrics.py \
  --archive /tmp/moses-data/moses-record-22109729-results.zip \
  --replay-dir /tmp/moses-replay \
  --output-dir /tmp/moses-metric-audit
```

The source ZIP must match its published size and MD5 and the reviewed full
SHA256. Every replay observation is checked against all regenerated source
fields, with campaign identity, scientific configuration and source digests
verified. The command safely interprets bounded data records; it neither
executes pickle constructors nor extracts the ZIP. Existing source/replay
files are unchanged and existing output is preserved.

`report.json` records candidate agreement, exact and tolerance counts,
nonfinite/availability statuses, origin-specific results and actual
implementation hashes. `report.md` provides the comparison table;
`complete.json` records output sizes and SHA256 values. Targeted objectives are
explicitly excluded from global-summary comparisons. The report contains
aggregate metrics, not the original per-analyte arrays. Inputs and software
are rechecked before atomic publication.

The Gaussian reconstruction qualifies the simulation array equation only.
Agreement with an archived summary or scalar score does not resolve the
historical producer or validate a physical analytical method.


The audit delegates numerical candidates to the packaged pure backend:

```python
from measure_methods import chromatography_metric_candidates

metrics = chromatography_metric_candidates(
    num_compounds=2,
    retention_times=[1, 3],
    peak_resolutions=[0.5, 0.5],
    fwhms=[2.3548, 2.3548],
    run_time_min=4,
)
print(metrics["candidates"]["reported_critical_vs_sigma_pair_adjacent_min"])
```

All time and FWHM values use minutes. The backend shares its Gaussian
calculation with `measure_methods.chromatography.gaussian_pairwise_resolutions`;
it has no archive, MCP, R or storage dependency. The script owns source binding,
normalization of archived fields and reporting. The method requires a positive
declared analyte count; empty arrays for that nonempty experiment remain
unavailable, while present nonfinite entries retain the source zero sentinel.
