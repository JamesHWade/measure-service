# measure-service

MCP tools for processing analytical traces and tracking experiment results.
The service stores measurements, analysis methods, results and their provenance.
Method resource calculations use the R-free Python package `measure_methods`,
which ships inside the `measure-service` distribution. It has no dependency on
MCP, storage or R. The service delegates trace analysis to the R packages
[measure](https://github.com/JamesHWade/measure) and
[measure.sec](https://github.com/JamesHWade/measure.sec).

**Status: research prototype.** Local analytical workflows and a bounded
synthetic campaign loop are tested. BO-MCP execution uses an optional separate
runtime. Hosted deployment and instrument control are not qualified.

## Try it

From this checkout, with Python 3.10 or later:

```sh
python -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python examples/registry.py
```

The example starts an MCP server, records an experiment and an attempt, saves
an observed zero and a below-limit result, and retrieves both. It uses a
temporary database and needs no R installation. It prints record IDs and exits.

For an MCP client, use the installed `measure-service` command or
`python -m measure_service`. See [local setup](docs/quickstart.md) for a client
configuration and a numerical analysis example.

## Current scope

| Workflow | Available behavior | Main limit |
| --- | --- | --- |
| Import | JSON traces; single, wide or grouped-long CSV/TSV with explicit mappings | Scalar numeric traces; no vendor files or spectra |
| Chromatograms | Baseline correction, smoothing, peak detection and area integration | No compound identification or overlapping-peak deconvolution |
| Peak quantitation | Linear response calibration from supplied standards | Caller assigns the peak; no automatic dilution, LOD or uncertainty |
| Relative SEC | Calibration, molecular-weight averages and distributions | Requires corrected `measure.sec`; results are relative to supplied standards |
| Comparison | SEC deltas and method/backend/unit comparability flags | No statistical equivalence claim |
| Method declarations | Isocratic/linear gradients, explicit units, flow/solvent use, declared feasibility and immutable method identities | No optimizer-domain sampling, pressure model or acquisition |
| Experiment tracking | Intended and actual conditions, attempt revisions, evidence-linked outcomes and explicit corrections | Reported records; no execution or optimizer state machine |
| Analytical acceptance | Predeclared analyte cohorts, evidence-bound three-valued decisions and immutable observation snapshots | Assignments and producer qualifications remain separate |
| Ideal retention | Explicit linear, quadratic and Neue–Kuss laws, constant-flow inlet programs and immutable predictions | No fitting, gradient-delay inference, widths or physical qualification |
| Latent Gaussian observations | Predeclared synthetic FWHM widths, prediction-bound generated results and immutable producer evidence | Finite-centroid reference; no traces, physical widths or signal processing |
| Synthetic campaigns | Frozen isocratic protocols, sequential proposals and exact immutable outcome consumption; optional BO-MCP GP suggestions | Single-fidelity latent reference; no general BO or physical-instrument claim |

Registry operations, import and record retrieval work without R. Numerical
analysis and `measurement_capabilities` require R; use
`measurement_registry_capabilities` for discovery without it.

The service does not yet implement the full MoSeS or MOCCA2 workflow. LC-MS,
GC-MS, DAD deconvolution, cross-run analyte tracking, retention-model fitting,
broader BO policies and instrument adapters are [roadmap work](docs/roadmap.md).

## Documentation

- [Local setup and examples](docs/quickstart.md)
- [Input formats and limits](docs/inputs.md)
- [Scientific methods and interpretation](docs/analysis.md)
- [Chromatography method declarations](docs/methods.md)
- [Experiment and result contracts](docs/experiments.md)
- [Analytical observation evidence and snapshots](docs/observation-evidence.md)
- [Ideal retention declarations and predictions](docs/retention-predictions.md)
- [Generate synthetic Gaussian observation evidence](docs/gaussian-observation-producer.md)
- [Reproduce the independent Gaussian reference checks](docs/gaussian-reference-benchmark.md)
- [Declare matched synthetic campaign protocols](docs/synthetic-campaigns.md)
- [Declare controlled optimizer-target and noise-prior comparisons](docs/ablation-protocol.md)
- [Run durable synthetic campaigns with optional BO-MCP](docs/campaign-runtime.md)
- [Reproduce the fixed paired synthetic comparison](docs/synthetic-comparison.md)
- [Inspect the first checked synthetic comparison](docs/synthetic-reference-results.md)
- [Retention equations, units and qualification scope](docs/retention-model-qualification.md)
- [Reproduce archived retention input qualification](docs/retention-reference-benchmark.md)
- [MCP tool reference](docs/tools.md)
- [Architecture and package boundaries](docs/architecture.md)
- [HTTP deployment, identity and storage](docs/deployment.md)
- [Roadmap and acceptance criteria](docs/roadmap.md)
- [Hierarchical objective scores](docs/hierarchy-scores.md)
- [MoSeS metric reconciliation](docs/moses-metric-reconciliation.md)
- [Subsequent observation contract](docs/moses-observation-contract.md)
- [Campaign success accounting](docs/moses-trajectory-sensitivity.md)
- [MoSeS archived-result reproduction](docs/moses-reproduction.md)
- [Development and checks](CONTRIBUTING.md)

Data live in a local SQLite database. Stdio uses one `local` principal. HTTP
requires a trusted authentication proxy; parsing its identity header is not
authentication. Read the [deployment limits](docs/deployment.md) before hosting.

## Origin and license

Extracted from the service and registry proposed in
[measure #83](https://github.com/JamesHWade/measure/pull/83) and
[measure #109](https://github.com/JamesHWade/measure/pull/109).
The [migration record](docs/migration.md) identifies the source revisions and
compatibility decisions. Code is licensed under [MIT](LICENSE). Packaged
[derived source-law reference data](src/measure_service/benchmarks/reference_data/README.md)
carry the author dataset's CC BY 4.0 attribution.
