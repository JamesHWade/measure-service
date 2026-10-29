# Development

Use a feature branch and a reviewable pull request. The service is an installed
Python package; tests must not depend on adding source directories to
`PYTHONPATH`. The pure-Python method resource kernel is the importable
`measure_methods` package under `src/` and is included in the same distribution
as the MCP service. Keep its reference tests with the kernel and add service
contract tests when the adapter changes. R calculations remain in their R
backend packages.

## Set up and check

```sh
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/air format --check .
.venv/bin/jarl check .
.venv/bin/python scripts/render_reference.py --check
.venv/bin/python scripts/check_docs.py
.venv/bin/python -m pytest -q -m 'not r_backend'
```

Before a PR, run both Air and Jarl across the repository, including the R
worker. Fix new findings. This repository starts with no accepted Air/Jarl
baseline. To apply formatting, use `ruff format .` and `air format .`; use
`ruff check . --fix` for supported Python fixes and review the diff.

For R-backed numerical integration, install the R packages described in the
[quickstart](docs/quickstart.md), then run:

```sh
MEASURE_MCP_TEST_R="$(command -v R)" .venv/bin/python -m pytest -q
MEASURE_MCP_R="$(command -v R)" .venv/bin/python examples/analyze_trace.py
```

Use `MEASURE_MCP_TEST_R` for the R-backed contract test and `MEASURE_MCP_R` for
the example/runtime. Python-only checks do not establish that numerical
analysis works. CI keeps those checks in a separate job from R integration. The
R job uses current R/CRAN dependencies and live `measure` and `measure.sec`
branches. It logs installed source revisions and checks required SEC
capabilities; those observations do not impose fixed revision constraints.

## Check the distribution

Build both a source archive and a wheel:

```sh
.venv/bin/python -m build
```

Install the wheel in a separate environment, then run tests and examples from
outside this checkout. Check that both `measure_service` and `measure_methods`
are in the wheel, along with `measure_service/worker.R`. Run the installed tests
without installing a separate `measure-methods` distribution. The source archive
must include both packages as well. The CI distribution job performs these
checks and exercises the uninstall/force-reinstall upgrade sequence with local
wheel files and no package index. A successful editable install alone does not
verify packaging.

## Documentation standard

Write for a scientist or developer who did not read the design conversation.
Start with the operation and resulting behavior. Give complete commands and
examples, then state assumptions that affect interpretation.

- Describe implemented behavior in the present tense. Put proposed work in the
  roadmap with acceptance evidence.
- Use concrete nouns and verbs. Omit marketing claims, filler, repeated
  summaries and claims that a method is robust without supporting evidence.
- Keep units, ownership, revisions, retry semantics and failure states explicit.
  Distinguish caller declarations from verified or computed facts.
- Use synthetic/public examples. Do not include private client names, repository
  links, sample records, credentials or internal deployment details.
- Change the relevant contract and example in the same PR as behavior changes.
  Regenerate [the tool reference](docs/tools.md) from registered schemas.
- State exactly what a check covers. A local HTTP identity simulation does not
  validate a real proxy, and a synthetic chromatogram does not qualify an
  instrument workflow.

`scripts/check_docs.py` checks local Markdown links and JSON examples. The
reference check detects catalog drift. Executable examples are exercised over
MCP in tests. These checks supplement review; they do not judge scientific
claims or prose quality.

## PR evidence

Describe the concrete behavior change, why it matters and the checks that ran.
Include installed R backend revisions for trace-analysis results and explain
material limits. Method resource calculations report the containing
`measure-service` distribution and its PEP 610 source revision when available.
CI reports the checkout revision separately; it does not infer installed
provenance from the checkout.
Do not label an item complete because it has a stub, a schema or a mock.
Do not merge without maintainer review.
