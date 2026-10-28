# measure-service

MCP tools for processing analytical traces and tracking experiment results.
The service stores measurements, analysis methods, results and their provenance.
It delegates numerical analysis to the R packages
[measure](https://github.com/JamesHWade/measure) and
[measure.sec](https://github.com/JamesHWade/measure.sec).

**Status: research prototype.** Local workflows are tested. A hosted deployment,
BO integration and instrument control are not implemented or qualified.

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

Registry operations, import and record retrieval work without R. Numerical
analysis and `measurement_capabilities` require R; use
`measurement_registry_capabilities` for discovery without it.

The service does not yet implement the full MoSeS or MOCCA2 workflow. LC-MS,
GC-MS, DAD deconvolution, cross-run analyte tracking, retention models, BO
suggestions and instrument adapters are [roadmap work](docs/roadmap.md).

## Documentation

- [Local setup and examples](docs/quickstart.md)
- [Input formats and limits](docs/inputs.md)
- [Scientific methods and interpretation](docs/analysis.md)
- [Chromatography method declarations](docs/methods.md)
- [Experiment and result contracts](docs/experiments.md)
- [MCP tool reference](docs/tools.md)
- [Architecture and package boundaries](docs/architecture.md)
- [HTTP deployment, identity and storage](docs/deployment.md)
- [Roadmap and acceptance criteria](docs/roadmap.md)
- [Development and checks](CONTRIBUTING.md)

Data live in a local SQLite database. Stdio uses one `local` principal. HTTP
requires a trusted authentication proxy; parsing its identity header is not
authentication. Read the [deployment limits](docs/deployment.md) before hosting.

## Origin and license

Extracted from the service and registry proposed in
[measure #83](https://github.com/JamesHWade/measure/pull/83) and
[measure #109](https://github.com/JamesHWade/measure/pull/109).
The [migration record](docs/migration.md) identifies the source revisions and
compatibility decisions. Licensed under [MIT](LICENSE).
