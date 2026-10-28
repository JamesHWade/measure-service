# Roadmap

The first implementation covers scalar-trace analysis and experiment evidence.
It does not claim feature parity with MoSeS or MOCCA2. The detailed
[capability audit in measure #105](https://github.com/JamesHWade/measure/issues/105)
maps the manuscript, supplement and source findings to the existing issues.
Those scientific findings remain the reference while service ownership moves
here.

## Next service increments

| Increment | Deliverable | Acceptance evidence | Existing planning |
| --- | --- | --- | --- |
| Independent package | Installed MCP service, packaged worker, executable examples and explicit deployment limits | Wheel installation outside the checkout; contract and R integration tests; Air/Jarl checks | [#82](https://github.com/JamesHWade/measure/issues/82), [#86](https://github.com/JamesHWade/measure/issues/86) |
| BO adapter | Versioned campaign/objective contracts and an optimizer adapter using immutable outcome snapshots | Replay a small campaign; duplicate delivery does not consume an outcome twice; failures/censoring and evaluator origins survive round trips | [#90](https://github.com/JamesHWade/measure/issues/90), [#93](https://github.com/JamesHWade/measure/issues/93), [#95](https://github.com/JamesHWade/measure/issues/95) |
| Durable campaign loop | Persisted proposal, attempt, analysis and consumption states, with recovery | Restart between each stage; reconcile partial failures; distinguish transport retries from new physical attempts | [#96](https://github.com/JamesHWade/measure/issues/96) |
| Data and batch infrastructure | Authenticated asset ingestion, bounded jobs and campaign snapshots | Ownership checks on every artifact link; stable snapshot contents; restart/cancellation behavior documented and tested | [#87](https://github.com/JamesHWade/measure/issues/87), [#103](https://github.com/JamesHWade/measure/issues/103), [#107](https://github.com/JamesHWade/measure/issues/107) |
| Evidence views | Inspectable methods, traces, assignments, warnings and exportable lineage | Trace an exported objective back to exact input, analysis and outcome revisions | [#102](https://github.com/JamesHWade/measure/issues/102) |
| Optional execution adapters | Device-specific command/observation adapters kept separate from scientific methods | Simulator/fake-device tests followed by named instrument qualification; no claim based only on a mock | [#101](https://github.com/JamesHWade/measure/issues/101) |

The BO adapter is the next functional increment. The optimizer retains its
surrogate/acquisition state. The service retains evidence and records exactly
which versioned outcomes were sent to the optimizer. A source label or
`proposal_reference` is not enough: consumption needs a durable identifier,
retry behavior and an explicit objective definition.

A first adapter should support caller-provided numeric/categorical spaces,
objective directions and units, constraints, campaign creation, suggestions,
and outcome submission through one selected optimizer backend. It should
reject unsupported censored/failed observations explicitly rather than invent
numbers. Multi-fidelity policies, scheduling and richer diagnostics follow
after the basic replay/recovery contract works.

## Separations coverage

This table assigns work to the layer that should own it. Names such as LC and
MS packs describe proposed boundaries; no new technique package is implied to
exist merely because it appears here.

| Area | Backend responsibility | Service responsibility | Existing planning |
| --- | --- | --- | --- |
| Scalar LC, GC and CE | Peak/batch methods, response calibration and method-specific QC | Import mappings, bounded operations and complete evidence | [#84](https://github.com/JamesHWade/measure/issues/84) |
| SEC/GPC | Correct calibration, integration, relative/absolute models and detector assumptions | Invoke supported methods and preserve comparability limits | [measure.sec #61](https://github.com/JamesHWade/measure.sec/pull/61) |
| DAD and MOCCA2 | Baseline/peak processing, spectral deconvolution, purity and concentration methods with reference cases | DAD records, adapters, arrays and interpretable diagnostics | [#88](https://github.com/JamesHWade/measure/issues/88) |
| LC-MS and GC-MS | Spectral data, extraction, annotation and detector alignment | Optional backend discovery, large artifacts and provenance | [#85](https://github.com/JamesHWade/measure/issues/85), [#99](https://github.com/JamesHWade/measure/issues/99) |
| Cross-run identity | UV/MS/rank/area matching, correspondence graphs and ambiguous assignments | Versioned assignment evidence and correction history | [#98](https://github.com/JamesHWade/measure/issues/98) |
| Retention models | LSS/QSS/Neue–Kuss, transport/width models, fitting and identifiability checks | Named model profiles, training snapshots and simulator provenance | [#100](https://github.com/JamesHWade/measure/issues/100), [#108](https://github.com/JamesHWade/measure/issues/108) |
| Chromatography objectives | Resolution, analyte count, runtime, feasibility and method constraints | Versioned extraction rules and objective-to-evidence links | [#92](https://github.com/JamesHWade/measure/issues/92), [#93](https://github.com/JamesHWade/measure/issues/93) |
| Cost-aware multi-fidelity BO | Qualified simulation and discrepancy diagnostics | Cost/fidelity accounting, physical-data refits and optimizer protocol | [#106](https://github.com/JamesHWade/measure/issues/106) |
| Exchange and native inputs | Explicit readers/writers and unit-aware gradient models | Authenticated ingestion and round-trip provenance | [#107](https://github.com/JamesHWade/measure/issues/107) |
| Knowledge-guided diagnostics | Scientific failure modes and interpretable model diagnostics | Evidence-backed suggestions with stated assumptions | [#97](https://github.com/JamesHWade/measure/issues/97) |
| Benchmarks and demonstrations | Ground truth, representative mixtures and reference tolerances | Replayable campaign records, baselines and complete failures | [#94](https://github.com/JamesHWade/measure/issues/94), [#104](https://github.com/JamesHWade/measure/issues/104) |

Native NPZ persistence, mzML exchange and a usable mzML reader are separate
capabilities. Analytical target-method development and preparative fraction
collection are also separate claims. The source audit records paper/code
protocol and unit discrepancies in
[#108](https://github.com/JamesHWade/measure/issues/108); named benchmark
profiles must resolve those discrepancies before claiming reproduction.

## Completion criteria

A roadmap item is complete when its public contract, executable example,
reference data/tolerances, provenance and failure behavior are documented and
tested. Local synthetic tests establish only the behavior they exercise.
Real-instrument and manuscript-reproduction claims need named datasets,
protocol versions, baselines and reported limitations. A merged adapter or
matching function name is not evidence of scientific equivalence.

The source references are the
[MoSeS manuscript](https://chemrxiv.org/doi/pdf/10.26434/chemrxiv.15008042/v1?redirectToLatest=false),
[its supplement](https://chemrxiv.org/action/downloadSupplement?doi=10.26434%2Fchemrxiv.15008042%2Fv1&file=lcbo_si_aug26.pdf),
[MoSeS repository](https://gitlab.com/aspuru-guzik-group/self-driving-lab/instruments/moses),
[lcbo code](https://github.com/fsk-lab/lcbo) and
[MOCCA2 code](https://github.com/bayer-group/MOCCA).
The full audit identifies the reviewed versions; this document does not claim
that unreviewed upstream revisions have identical behavior.
