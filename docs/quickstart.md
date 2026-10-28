# Local setup

Run commands from the repository root. Python 3.10+ and Git are required; the initial
extraction was checked on Python 3.12. The Python package has no R installation
step or dependency on the R packages at install time.

```sh
python -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python examples/registry.py
```

On Windows, use `.venv\Scripts\python.exe` in place of `.venv/bin/python`.
There is no published-package installation assumed here.

## Connect an MCP client

Replace the paths below with absolute paths on the service host. The Python
module works from any working directory after installation.

```json
{
  "mcpServers": {
    "measure": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "measure_service"],
      "env": {
        "MEASURE_MCP_DATA_DIR": "/absolute/path/to/private-measure-data"
      }
    }
  }
}
```

Call `measurement_registry_capabilities`, then follow the
[registry example](../examples/registry.py). A stdio client owns the server
process. Starting `measure-service` in a terminal waits for MCP messages;
it does not open a web interface.

## Enable numerical analysis

Install R and the backend packages into the library visible to the service
process. `jsonlite`, `tibble`, `recipes` and `measure` are required. Relative
SEC also requires the corrected `measure.sec` implementation with calibrated
molecular-weight columns. The worker checks this capability at runtime.

Use the current R release and CRAN dependencies. Until the SEC correction in
[measure.sec #61](https://github.com/JamesHWade/measure.sec/pull/61) is merged,
install that active branch. `measure` follows its default development branch.
These commands use branch HEADs rather than fixed commit constraints:

```sh
Rscript -e 'install.packages(c("remotes", "jsonlite", "tibble", "recipes"), repos = "https://cloud.r-project.org")'
Rscript -e 'options(repos = c(CRAN = "https://cloud.r-project.org")); remotes::install_github("JamesHWade/measure@main", dependencies = NA)'
Rscript -e 'options(repos = c(CRAN = "https://cloud.r-project.org")); remotes::install_github("JamesHWade/measure.sec@fix/separations-sec-correctness", dependencies = FALSE)'
Rscript -e 'for (p in c("measure", "measure.sec")) {d <- packageDescription(p); cat(p, d$Version, d$RemoteRef, d$RemoteSha, "\n")}'
```

The SEC install leaves the selected core package in place. After #61 is merged,
use `JamesHWade/measure.sec` without the temporary branch override. Record the
actually installed `RemoteSha` with analytical evidence: development version
strings alone do not identify source code. CI also records the branch heads at
job start and checks the installed packages' source metadata and required SEC
API before numerical tests. A moving branch can resolve to a later revision
between those two observations; neither observation becomes an install pin.

The original fixed-source integration evidence remains in the
[extraction PR](https://github.com/JamesHWade/measure-service/pull/14). Current
CI tests R release, released CRAN `recipes`, and live `measure`/corrected SEC
branches in isolated libraries.

Set `MEASURE_MCP_R` to the R executable, not `Rscript`. If R is on `PATH`, the
default is `R`. Include `R_LIBS_USER` in the client environment when the backend
uses a non-default library. Optional `MEASURE_MCP_MEASURE_REVISION` and
`MEASURE_MCP_SEC_REVISION` record operator-declared source revisions in results;
the service does not verify those declarations against the installed packages.

```sh
MEASURE_MCP_R="$(command -v R)" .venv/bin/python examples/analyze_trace.py
```

This example registers a synthetic LC trace, detects a peak, and records its
peak count as an outcome linked to the stored analysis. It prints analysis and
result IDs and uses a temporary database. It is a transport and calculation
smoke test, not a reference-material validation.

In an MCP client, add `MEASURE_MCP_R` to the configuration and call
`measurement_capabilities` before requesting numerical tools. Then register,
inspect and analyze a trace; inspect warnings and provenance with every result.
Use [input guidance](inputs.md) for real data and
[method limits](analysis.md) when interpreting it.

## Common failures

| Failure | Check |
| --- | --- |
| Module cannot be imported | Install the checkout into the interpreter configured in the client |
| R worker cannot start or times out | Check `MEASURE_MCP_R`, R library visibility and the 90-second worker limit |
| SEC reports missing calibrated-curve support | Install the corrected backend revision; its version string may match an older checkout |
| HTTP reports local identity is stdio-only | Configure a verified proxy deployment; see [HTTP setup](deployment.md) |
| A retry key conflicts | Reuse the original arguments or choose a new key for a different registry operation |
