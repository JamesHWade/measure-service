# Reproduce the archived MoSeS results

The first reproduction layer reads the authors' shared observations, recomputes
raw chromatographic metrics, and reconstructs their recorded score trajectories.
It covers 24 simulated benchmark configurations and the known-mixture and
target-focused experimental campaigns. It uses no optimizer, instrument SDK,
Torch, or NumPy dependency.

The scientific target is Haddadnia et al., [MoSeS Enables Autonomous
Chromatography Operation through Programmable Analytical
Expertise](https://doi.org/10.26434/chemrxiv.15008042/v1), ChemRxiv v1. This is an
HPLC-MS study. SEC/GPC capabilities are separate from this reproduction.

## Run the archive replay

From a source checkout, install the service, then explicitly fetch the authors' 1.49 GB ZIP into a
cache outside this repository:

```sh
python -m pip install .
python -m measure_service.benchmarks fetch-moses --cache-dir /tmp/moses-data
python -m measure_service.benchmarks replay-moses \
  --archive /tmp/moses-data/moses-record-22109729-results.zip \
  --output-dir /tmp/moses-replay
```

An existing archive can be passed directly. Replay verifies its exact size and
published MD5 before reading members, and records its observed SHA256. Ordinary
tests do not download it. The output directory must be absent or empty;
successful outputs are published together, without overwriting previous results.

Read `report.md` for the numerical summary. The machine-readable evidence is:

| File | Contents |
| --- | --- |
| `report.json` | Named protocol, source and implementation digests, scientific settings and configuration results |
| `archive-manifest.json` | Every member name, byte count and archive checksum |
| `campaigns.csv` | Stable source identities, snapshot reconciliation, budget/completion state, first satisfactory score and costs |
| `observations.csv` | Every observation, fidelity role, origin, recomputed metrics, missingness and discrepancy diagnostics |
| `success-trajectories.csv` | Full-cohort fractions and unknown-outcome bounds at each evaluation budget |
| `complete.json` | Byte count and SHA256 of every emitted evidence file |

For a standalone publication figure, use the supplied plotting script with R,
jsonlite and ggplot2 installed:

```sh
Rscript scripts/plot_moses_replay.R /tmp/moses-replay
```

It writes `success-trajectories.png` and `.svg` beside the evidence. The shaded
area represents outcomes not identified by the archived histories; it is not a
confidence interval. Plotting is optional and has no effect on the CSV results.

## Protocol and scientific interpretation

Profile `moses-zenodo-v1-archived-observations/1` retains the archived BoTier
scalar score and declares `score >= 2` satisfactory. It does not independently
recalculate that scalarizer. Initial-design evaluations count toward the budget.
The source's high-fidelity threshold is `fidelity_level >= 0.99`; fidelity is a
role within each campaign. High-fidelity observations in simulated benchmarks
are still simulations. Low-fidelity observations inside experimental campaigns
are also labelled simulated, rather than inheriting an instrument origin from
their containing file.

Raw metrics follow the source's finite-resolution counting convention:
`num_peaks` counts finite per-analyte resolutions, resolved peaks satisfy the
configured threshold inclusively, and critical resolution is the minimum finite
value. When a present array has no finite resolutions, the source's zero
sentinel is retained with an explicit status. An absent array produces an
unavailable metric. Finite retention times within the run window are counted
separately. Missing measurements, non-eluting analytes, observed zeros and
unavailable scores remain distinct. Source convenience summaries are checked
against recomputed values and disagreements are reported without repairing data.

The optional Gaussian helper in `measure_methods.chromatography` uses
`Rs = delta_t / (2 * (sigma_i + sigma_j))`, with `FWHM = 2.3548 * sigma`, matching
the reviewed simulation wrapper. It is not a general resolution definition for
asymmetric fitted peaks.

Program volume integrates the supplied time/flow rows. Rounded source solvent
fractions are not normalized to invent solvent-specific consumption. Reported
volume excludes equilibration and flushing. Declared low-fidelity cost produces
an evaluation-cost proxy, not measured instrument time or expenditure.

## Reconcile the actual archive

The verified ZIP has 4,066 members, 50 ten-analyte input sets and 24 grouped
benchmark result records. Most grouped results contain all 50 tasks. One
compoundwise multi-fidelity result contains only 24 completed campaigns and
compacts their positions. Treating group zero as analyte set zero is incorrect.
Its first completed group corresponds to labelled analyte set 1.

The replay matches grouped observations to labelled `CmpdSet` histories by
scientific configuration and normalized raw-observation prefixes. It retains
the longest consistent history, including unfinished campaigns. Both original
member digests remain in the report. Derived convenience fields and gradient
names are not identity evidence. The documented legacy defaults
`compound_idx=None` and `mixed_fidelity=False` are used only for configuration
matching. Conflicting or ambiguous histories stop with a diagnostic.

That multi-fidelity archive retains 49 labelled histories; all 50 input mixtures
remain in the denominator. Missing and incomplete outcomes remain unknown, and
the trajectory file gives bounds rather than silently dropping them. The
random-design baseline has only a complete grouped snapshot; its source-driver
order interpretation is explicitly marked instead of claiming labelled records
verified it.

The data reader interprets a bounded grammar of literal pickle records and
numeric NumPy scalar byte encodings. It never invokes serialized constructors,
imports their globals or uses `pickle.load`. Unsupported representations,
duplicate keys, cycles and excessive work stop explicitly. Archive members are
read into bounded memory; they are not extracted or executed.

## Independently qualify scalar scores

Keep the replay directory intact and write a separate score report:

```sh
python -m measure_service.benchmarks qualify-moses-scores \
  --archive /tmp/moses-data/moses-record-22109729-results.zip \
  --replay-dir /tmp/moses-replay \
  --output-dir /tmp/moses-score-qualification
```

The qualifier verifies all six core replay files against `complete.json`,
verifies the exact ZIP and its recorded manifest, reconnects every observation
to its safely decoded source, and checks every replay observation cell against
that source. Optional plot files are ignored and are not claimed as verified
replay inputs. Missing, duplicate, reordered or altered observations stop the
run. Publication is atomic into an absent or empty directory; the original
replay is never rewritten. Source, replay and implementation integrity are
checked again before publication.

Profile `moses-score-qualification/1` evaluates the two
[named hierarchy rules](hierarchy-scores.md) against two separate input policies:

- `raw_resolution_array` derives count and critical resolution from the
  per-analyte arrays using the reviewed LCBO experiment properties. Targeted
  observations use the selected peak, including zero-based target index 0.
- `serialized_summary_hypothesis` evaluates the stored count/resolution
  summaries. It tests a historical-input hypothesis and does not substitute
  summaries into the raw-array calculation. Targeted observations have no
  summary candidate. Missing arrays or summaries remain explicitly unscorable.

The default source scorer builds cumulative observed objectives even when its
surrogate is labelled compoundwise. All calculations use binary64. Time-based
records use the final absolute gradient endpoint; segment-based records round
each interval to four decimal places using the source's scale/nearest-even/
unscale convention, then sum them. The declared runtime is compared separately.
Units remain minutes. The fixed comparison uses
`math.isclose(abs_tol=1e-10, rel_tol=1e-10)`; errors are retained even when they
fall within tolerance. Raw-versus-summary discrepancy counts compare exact
values; their absolute differences show whether a mismatch exceeds numerical
rounding.

Read the new `report.md` and `report.json` for per-profile/input-policy
agreement, maximum score errors, classification differences, runtime
diagnostics and raw-array versus stored-summary differences. Its
`observations.csv` has four candidate rows per original observation with source
identity, score decomposition and explicit unavailable statuses. The new
`complete.json` checksums all three numerical evidence files. Original
per-analyte arrays are not copied into these outputs.

Analytical acceptance remains separate from the scalar cutoff. The report
compares archived and native `score >= 2` with explicit count/resolution
thresholds, and separately includes runtime. For example, ten peaks all at
resolution 1.49 with a one-minute run produce a published-profile score above
2.6 despite failing the declared resolution threshold of 1.5. Smooth tier gates
make these different questions.

An optional publication figure reads only the derived aggregates:

```sh
Rscript scripts/plot_moses_score_qualification.R /tmp/moses-score-qualification
```

It writes `score-agreement.png` and `.svg`. Plotting does not change any of the
checksummed numerical results. Numerical agreement with a source rule and
input hypothesis establishes neither the historical installed software nor the
validity of discrepant source inputs. A fresh optimizer comparison and raw
signal analysis still require independent evidence.

## What remains for the full paper

This layer reproduces the archived-observation calculations and accounting. A
fresh optimization comparison, qualified retention models, DAD/MS processing,
cross-run tracking and instrument execution require their own evidence. The reviewed simulation wrapper reads latent
analyte outputs and ignores its `advanced_peak_detection` argument, so an
advanced/basic flag comparison cannot qualify peak deconvolution.

The archive-derived target profile also differs from the manuscript-table
profile: it records 18 analytes, target index 14, resolution threshold 2.5,
maximum simulation fidelity 0.8 and five declared initial runs. Keep those
settings separate from SI Table S9. [The protocol
audit](https://github.com/JamesHWade/measure/issues/108) retains the equation,
unit, initialization and benchmark-sampling discrepancies; [the capability
audit](https://github.com/JamesHWade/measure/issues/105) maps the remaining work.

## Sources and provenance

- [Author dataset and checksum](https://zenodo.org/records/22109729), DOI
  `10.5281/zenodo.22109729`; expected size 1,485,278,454 bytes and MD5
  `25b40b0f56467d83ee5ba434a8872cbb`.
- Observed complete-archive SHA256:
  `880533ded926f766ce9504d4f164e9c5195b775e40061c18d70ae79497f03f71`.
- [Reviewed author code](https://github.com/fsk-lab/lcbo/tree/35098fb88e19cdc0786ed416dec92aa9f833cc2f),
  especially `data/campaign.py`, `hplc_utils/hplc_experiment.py` and
  `routines/experiment.py`.
- [MoSeS software](https://gitlab.com/aspuru-guzik-group/self-driving-lab/instruments/moses).

Source revisions and input digests identify reviewed evidence; they impose no
fixed revision requirement on runtime package installation. The original data
remain in the external cache. The Zenodo record has no displayed dataset
license and the experiment-code repository has no license file; this package
does not redistribute those data or source files. Tests use synthetic records.
