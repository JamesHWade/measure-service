# MCP tool reference

Generated from the registered tools by `python scripts/render_reference.py --write`.
Do not edit the tables by hand. MCP `tools/list` supplies complete JSON schemas,
including nested record models, defaults and validation limits. For a local JSON
export, run `python scripts/render_reference.py --json`.

All tools return structured JSON. Check `isError` before reading
`structuredContent`. Tool annotations describe effects, not scientific validity.
Registry writes use required retry keys. Dataset and analysis writes create
a new record each time. No tool executes an instrument command.

The current catalog contains 29 tools.

| Tool | Required arguments | Effect | Retry behavior |
| --- | --- | --- | --- |
| `measurement_analyze_chromatogram` | `dataset_id`, `trace_id`, `method` | write | creates new record |
| `measurement_analyze_sec` | `dataset_id`, `trace_id`, `method` | write | creates new record |
| `measurement_capabilities` | — | read | idempotent |
| `measurement_compare_sec` | `analysis_ids` | read | idempotent |
| `measurement_create_experiment` | `spec`, `idempotency_key` | write | idempotent |
| `measurement_get_analysis` | `analysis_id` | read | idempotent |
| `measurement_get_attempt` | `attempt_id` | read | idempotent |
| `measurement_get_experiment` | `experiment_id` | read | idempotent |
| `measurement_get_method` | `method_id` | read | idempotent |
| `measurement_get_result` | `result_id` | read | idempotent |
| `measurement_inspect_dataset` | `dataset_id` | read | idempotent |
| `measurement_list_attempts` | `experiment_id` | read | idempotent |
| `measurement_list_experiments` | — | read | idempotent |
| `measurement_list_methods` | — | read | idempotent |
| `measurement_list_results` | `attempt_id` | read | idempotent |
| `measurement_method_capabilities` | — | read | idempotent |
| `measurement_quantify_peak` | `analysis_id`, `peak_id`, `method` | write | creates new record |
| `measurement_read_analysis_data` | `analysis_id`, `kind` | read | idempotent |
| `measurement_read_source` | `dataset_id` | read | idempotent |
| `measurement_record_attempt` | `experiment_id`, `spec`, `idempotency_key` | write | idempotent |
| `measurement_record_results` | `attempt_id`, `expected_attempt_revision`, `results`, `idempotency_key` | write | idempotent |
| `measurement_register_csv` | `title`, `csv_text`, `axis_column`, `signal_column`, `metadata` | write | creates new record |
| `measurement_register_dataset` | `dataset` | write | creates new record |
| `measurement_register_long_csv` | `title`, `csv_text`, `axis_column`, `signal_column`, `group_columns`, `groups` | write | creates new record |
| `measurement_register_method` | `title`, `declaration`, `idempotency_key` | write | idempotent |
| `measurement_register_wide_csv` | `title`, `csv_text`, `axis_column`, `channels` | write | creates new record |
| `measurement_registry_capabilities` | — | read | idempotent |
| `measurement_revise_attempt` | `attempt_id`, `expected_revision`, `spec`, `reason`, `idempotency_key` | write | idempotent |
| `measurement_validate_method` | `declaration` | read | idempotent |

## Tool descriptions

### `measurement_analyze_chromatogram`

Process one trace and detect/integrate peaks using measure; peak areas are not compound identities or concentrations.

### `measurement_analyze_sec`

Calculate relative SEC molecular-weight moments from a baseline-corrected concentration trace and explicit standards.

### `measurement_capabilities`

Report installed R backends and the experimental operations available.

### `measurement_compare_sec`

Compare stored SEC moments to the first analysis; report method and detector differences without assigning pass/fail.

### `measurement_create_experiment`

Record an immutable experiment design with intended conditions; does not execute it or create an optimizer campaign.

### `measurement_get_analysis`

Retrieve a compact analysis record, method, diagnostics and provenance.

### `measurement_get_attempt`

Read the latest or a specific historical attempt revision, including sample/injection metadata from its linked traces.

### `measurement_get_experiment`

Retrieve an experiment's intended conditions and caller-declared origin.

### `measurement_get_method`

Retrieve an owned immutable executable method and its declared limits.

### `measurement_get_result`

Retrieve an immutable outcome with evidence provenance, attempt revision and any later superseding result.

### `measurement_inspect_dataset`

Inspect available traces, sample/injection identities, axes and units without returning raw arrays.

### `measurement_list_attempts`

List a bounded page of attempts and latest reported statuses for an owned experiment.

### `measurement_list_experiments`

List a bounded page of the current owner's experiments.

### `measurement_list_methods`

List compact method identities in a bounded owner-scoped page.

### `measurement_list_results`

List compact current outcome heads or the full result history for an owned attempt; no missing outcome is converted to zero.

### `measurement_method_capabilities`

Describe executable LC method validation and storage without an R backend.

### `measurement_quantify_peak`

Quantify an explicitly assigned chromatogram peak with supplied linear response standards; does not establish peak identity.

### `measurement_read_analysis_data`

Read a bounded page of peaks, processed trace points, or SEC distribution points.

### `measurement_read_source`

Read a bounded character page of the retained original CSV/TSV text and its import mapping.

### `measurement_record_attempt`

Record one attempted experiment, its reported status/actual conditions and owned dataset/trace links; no instrument action occurs.

### `measurement_record_results`

Atomically record outcomes against an exact attempt revision; copy numeric analysis-summary values or record explicit caller-reported outcomes. Units are caller-declared.

### `measurement_register_csv`

Register one explicitly mapped numeric trace from bounded CSV/TSV text; metadata are the same as for a Trace, excluding axis and signal.

### `measurement_register_dataset`

Register bounded one-dimensional traces and their sample, injection, detector and unit metadata.

### `measurement_register_long_csv`

Register row-oriented traces grouped by explicit column values, with a separate axis and metadata for each group.

### `measurement_register_method`

Retain an immutable method that meets its declared constraints, with original units, canonical program and digests; does not run an instrument.

### `measurement_register_wide_csv`

Register multiple explicitly mapped detector columns that share one numeric axis; retain the source text and mapping.

### `measurement_registry_capabilities`

Describe the local experiment registry without requiring an R backend.

### `measurement_revise_attempt`

Append a corrected or updated reported attempt snapshot with an explanation and optimistic version check; earlier revisions remain available.

### `measurement_validate_method`

Normalize a declared LC method, calculate time and solvent use, and report feasibility; performs no execution or storage.

## Related contracts

- [Input layouts and limits](inputs.md)
- [Numerical methods](analysis.md)
- [Registry semantics](experiments.md)
- [HTTP identity and storage](deployment.md)
