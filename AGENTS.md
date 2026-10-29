# Repository instructions

Read README.md and CONTRIBUTING.md before editing. Work on a feature branch.
Keep numerical method calculations in `src/measure_methods`, shipped in the
service distribution. Its calculation interface stays independent of MCP,
storage and identity. The service owns schemas, normalization, feasibility and
retention. For method packaging or provenance changes, read `docs/architecture.md`.

Before a PR, run repository-wide Ruff, Air and Jarl, the documentation checks
and the Python-only suite. Run the R-backed suite for worker, method, packaging
or numerical adapter changes. Test the installed distribution outside the
checkout when changing packaging or entrypoints. Report skipped checks.

Keep public text and fixtures free of private application names, private links,
credentials and internal data. Use synthetic examples. Document current limits
and caller-declared fields; do not claim manuscript parity from schemas or mocks.

Documentation should state concrete behavior, units, provenance, ownership,
revision and retry semantics. Keep commands executable and links relative.
Regenerate docs/tools.md with scripts/render_reference.py instead of hand edits.
