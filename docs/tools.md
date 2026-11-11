# MCP tool reference

Generated from the registered tools by `python scripts/render_reference.py --write`.
Do not edit the tables by hand. MCP `tools/list` supplies complete JSON schemas,
including nested record models, defaults and validation limits. For a local JSON
export, run `python scripts/render_reference.py --json`.

All tools return structured JSON. Check `isError` before reading
`structuredContent`. Tool annotations describe effects, not scientific validity.
Registry writes use required retry keys. Dataset and analysis writes create
a new record each time. No tool executes an instrument command.

The current catalog contains 76 tools.

| Tool | Required arguments | Effect | Retry behavior |
| --- | --- | --- | --- |
| `measurement_abandon_campaign_suggestion_attempt` | `suggestion_attempt_id`, `expected_start_sha256`, `reason`, `idempotency_key` | write | idempotent |
| `measurement_analyze_chromatogram` | `dataset_id`, `trace_id`, `method` | write | creates new record |
| `measurement_analyze_sec` | `dataset_id`, `trace_id`, `method` | write | creates new record |
| `measurement_cancel_chromatogram_batch` | `batch_id`, `idempotency_key`, `expected_revision` | write | idempotent |
| `measurement_capabilities` | — | read | idempotent |
| `measurement_compare_sec` | `analysis_ids` | read | idempotent |
| `measurement_consume_campaign_snapshot` | `campaign_id`, `proposal_id`, `snapshot_id`, `expected_revision`, `idempotency_key` | write | idempotent |
| `measurement_create_campaign` | `protocol_id`, `task_id`, `seed`, `policy`, `idempotency_key` | write | idempotent |
| `measurement_create_chromatogram_batch` | `batch`, `idempotency_key` | write | idempotent |
| `measurement_create_experiment` | `spec`, `idempotency_key` | write | idempotent |
| `measurement_create_observation_snapshot` | `contract_id`, `observation_ids`, `idempotency_key` | write | idempotent |
| `measurement_evaluate_observation` | `contract_id`, `attempt_id`, `expected_attempt_revision`, `spec`, `idempotency_key` | write | idempotent |
| `measurement_export_chromatogram_batch` | `batch_id`, `expected_revision`, `mode` | read | returns a new capture |
| `measurement_get_analysis` | `analysis_id` | read | idempotent |
| `measurement_get_analytical_import` | `import_id` | read | idempotent |
| `measurement_get_attempt` | `attempt_id` | read | idempotent |
| `measurement_get_campaign` | `campaign_id` | read | idempotent |
| `measurement_get_campaign_consumption` | `consumption_id` | read | idempotent |
| `measurement_get_campaign_proposal` | `proposal_id` | read | idempotent |
| `measurement_get_campaign_protocol` | `protocol_id` | read | idempotent |
| `measurement_get_campaign_suggestion_attempt` | `suggestion_attempt_id` | read | idempotent |
| `measurement_get_chromatogram_batch` | `batch_id` | read | idempotent |
| `measurement_get_chromatogram_batch_run` | `run_id` | read | idempotent |
| `measurement_get_chromatogram_evidence` | `analysis_id` | read | idempotent |
| `measurement_get_experiment` | `experiment_id` | read | idempotent |
| `measurement_get_experiment_design_lineage` | `experiment_id` | read | idempotent |
| `measurement_get_gaussian_observation` | `run_id` | read | idempotent |
| `measurement_get_gaussian_producer` | `producer_id` | read | idempotent |
| `measurement_get_method` | `method_id` | read | idempotent |
| `measurement_get_observation` | `observation_id` | read | idempotent |
| `measurement_get_observation_contract` | `contract_id` | read | idempotent |
| `measurement_get_observation_snapshot` | `snapshot_id` | read | idempotent |
| `measurement_get_result` | `result_id` | read | idempotent |
| `measurement_get_result_table_import` | `import_id` | read | idempotent |
| `measurement_get_retention_model` | `model_id` | read | idempotent |
| `measurement_get_retention_prediction` | `prediction_id` | read | idempotent |
| `measurement_import_result_table` | `spec`, `idempotency_key` | write | idempotent |
| `measurement_inspect_dataset` | `dataset_id` | read | idempotent |
| `measurement_list_attempts` | `experiment_id` | read | idempotent |
| `measurement_list_campaign_suggestion_attempts` | `campaign_id` | read | idempotent |
| `measurement_list_experiments` | — | read | idempotent |
| `measurement_list_methods` | — | read | idempotent |
| `measurement_list_results` | `attempt_id` | read | idempotent |
| `measurement_method_capabilities` | — | read | idempotent |
| `measurement_predict_retention` | `model_id`, `method_id`, `idempotency_key` | write | idempotent |
| `measurement_preview_result_table` | `spec` | read | idempotent |
| `measurement_quantify_peak` | `analysis_id`, `peak_id`, `method` | write | creates new record |
| `measurement_read_analysis_data` | `analysis_id`, `kind` | read | idempotent |
| `measurement_read_chromatogram_batch_attempts` | `batch_id`, `ordinal` | read | idempotent |
| `measurement_read_chromatogram_batch_control_events` | `batch_id` | read | idempotent |
| `measurement_read_chromatogram_batch_export` | `snapshot_sha256` | read | idempotent |
| `measurement_read_chromatogram_batch_items` | `batch_id` | read | idempotent |
| `measurement_read_chromatogram_evidence` | `analysis_id`, `kind`, `evidence_sha256` | read | idempotent |
| `measurement_read_result_table_rows` | `import_id` | read | idempotent |
| `measurement_read_result_table_source` | `import_id` | read | idempotent |
| `measurement_read_source` | `dataset_id` | read | idempotent |
| `measurement_record_attempt` | `experiment_id`, `spec`, `idempotency_key` | write | idempotent |
| `measurement_record_gaussian_observation` | `producer_id`, `prediction_id`, `attempt_id`, `expected_attempt_revision`, `idempotency_key` | write | idempotent |
| `measurement_record_results` | `attempt_id`, `expected_attempt_revision`, `results`, `idempotency_key` | write | idempotent |
| `measurement_recover_chromatogram_batch` | `batch_id`, `idempotency_key`, `expected_revision` | write | idempotent |
| `measurement_register_campaign_protocol` | `title`, `spec`, `idempotency_key` | write | idempotent |
| `measurement_register_csv` | `title`, `csv_text`, `axis_column`, `signal_column`, `metadata` | write | creates new record |
| `measurement_register_dataset` | `dataset` | write | creates new record |
| `measurement_register_gaussian_producer` | `title`, `retention_model_id`, `spec`, `idempotency_key` | write | idempotent |
| `measurement_register_long_csv` | `title`, `csv_text`, `axis_column`, `signal_column`, `group_columns`, `groups` | write | creates new record |
| `measurement_register_method` | `title`, `declaration`, `idempotency_key` | write | idempotent |
| `measurement_register_observation_contract` | `title`, `spec`, `idempotency_key` | write | idempotent |
| `measurement_register_retention_model` | `title`, `spec`, `idempotency_key` | write | idempotent |
| `measurement_register_wide_csv` | `title`, `csv_text`, `axis_column`, `channels` | write | creates new record |
| `measurement_registry_capabilities` | — | read | idempotent |
| `measurement_retry_chromatogram_batch_items` | `batch_id`, `ordinals`, `idempotency_key`, `expected_revision` | write | idempotent |
| `measurement_revise_attempt` | `attempt_id`, `expected_revision`, `spec`, `reason`, `idempotency_key` | write | idempotent |
| `measurement_run_chromatogram_batch` | `batch_id`, `idempotency_key`, `expected_revision` | write | idempotent |
| `measurement_suggest_campaign` | `campaign_id`, `expected_revision`, `idempotency_key` | write | idempotent |
| `measurement_supersede_experiment` | `experiment_id`, `expected_input_sha256`, `spec`, `reason`, `idempotency_key` | write | idempotent |
| `measurement_validate_method` | `declaration` | read | idempotent |

## Tool descriptions

### `measurement_abandon_campaign_suggestion_attempt`

Explicitly reconcile an unresolved owned suggestion as abandoned_unknown; this does not assert failure, kill a process or invent timing, and any late computation cannot create a proposal.

### `measurement_analyze_chromatogram`

Process one trace and detect/integrate peaks using measure; peak areas are not compound identities or concentrations.

### `measurement_analyze_sec`

Calculate relative SEC molecular-weight moments from a baseline-corrected concentration trace and explicit standards.

### `measurement_cancel_chromatogram_batch`

Cancel pending items using a revision check; preserve successes and allow already running numerical work to finish.

### `measurement_capabilities`

Report installed R backends and the experimental operations available.

### `measurement_compare_sec`

Compare stored SEC moments to the first analysis; report method and detector differences without assigning pass/fail.

### `measurement_consume_campaign_snapshot`

Consume exactly one frozen complete generated observation for the pending proposal; preserve analytical unknowns and source-profile utility separately, with atomic budget/revision updates and retry receipts.

### `measurement_create_campaign`

Create one owned sequential synthetic task/seed/policy arm from a frozen protocol; all initialization consumes its declared evaluation budget.

### `measurement_create_chromatogram_batch`

Freeze owned trace selections, caller roles, import identities and versioned chromatogram settings; does not start analysis or verify assignments.

### `measurement_create_experiment`

Record an immutable experiment design with intended conditions; does not execute it or create an optimizer campaign.

### `measurement_create_observation_snapshot`

Freeze explicitly selected observation identities and digests for one contract, retaining at most one observation per physical attempt; does not generate optimizer suggestions.

### `measurement_evaluate_observation`

Assess exact owned retention/resolution result records under the experiment's predeclared contract; retain immutable evidence, explicit unknowns and separate fidelity/origin declarations.

### `measurement_export_chromatogram_batch`

Capture owned quiescent batch evidence at a revision without R; explicitly choose metadata references or self-contained source assets, then read immutable byte pages.

### `measurement_get_analysis`

Retrieve a compact analysis record, method, diagnostics and provenance.

### `measurement_get_analytical_import`

Read the owned receipt mapping an imported evidence snapshot to new local datasets, historical analysis copies and a fresh pending batch; import is not fresh computation.

### `measurement_get_attempt`

Read the latest or a specific historical attempt revision, including sample/injection metadata from its linked traces.

### `measurement_get_campaign`

Inspect an owned synthetic campaign's revision, pending proposal, budget and compact immutable consumption history.

### `measurement_get_campaign_consumption`

Retrieve the owned immutable consumption record, exact source digests, raw metrics, utility, analytical acceptance and declared expenditure.

### `measurement_get_campaign_proposal`

Read an owned immutable proposal with its executable method, exact consumed training references and actual policy/backend provenance.

### `measurement_get_campaign_protocol`

Read an owned immutable matched protocol, complete task bindings, budgets, seeds and provenance.

### `measurement_get_campaign_suggestion_attempt`

Read an owned suggestion claim, immutable terminal or late evidence, exact requested training and available timing; a claim does not prove the optimizer started.

### `measurement_get_chromatogram_batch`

Read an owned batch's frozen metadata manifest, dataset references, current revision and per-state counts without R; this response omits raw arrays.

### `measurement_get_chromatogram_batch_run`

Read a retained run's fixed item membership and current durable outcomes without execution or R; claim tokens remain private.

### `measurement_get_chromatogram_evidence`

Capture owned stored chromatogram evidence and return its manifest/digest for bounded immutable paging; does not reanalyze or return large arrays.

### `measurement_get_experiment`

Retrieve an experiment's intended conditions and caller-declared origin.

### `measurement_get_experiment_design_lineage`

Read verified ordered design versions and a separate current pointer without moving historical evidence or creating lineage state for a standalone design.

### `measurement_get_gaussian_observation`

Read an immutable owned Gaussian producer run and its exact prediction, assumed widths and generated result identities.

### `measurement_get_gaussian_producer`

Retrieve the owned immutable latent-producer declaration, assumed FWHM widths and retention-model identity.

### `measurement_get_method`

Retrieve an owned immutable executable method and its declared limits.

### `measurement_get_observation`

Retrieve an immutable analytical observation, source evidence and decision even after later corrections.

### `measurement_get_observation_contract`

Read an owned observation contract and its exact input digest; declarations do not qualify the measurement producer.

### `measurement_get_observation_snapshot`

Read immutable owned snapshot membership; later result corrections or attempt revisions cannot change its contents.

### `measurement_get_result`

Retrieve an immutable outcome with evidence provenance, attempt revision and any later superseding result.

### `measurement_get_result_table_import`

Read an owned immutable table-import receipt and verify its source, result bindings and historical outcome evidence without R.

### `measurement_get_retention_model`

Read an owned immutable retention model declaration, input checks, warnings and source evidence.

### `measurement_get_retention_prediction`

Retrieve an immutable ideal prediction and its exact model, method, solver and backend evidence.

### `measurement_import_result_table`

Atomically import mapped outcomes across owned exact attempt revisions with retained source-row provenance and explicit corrections; no observation is evaluated or consumed automatically.

### `measurement_inspect_dataset`

Inspect available traces, sample/injection identities, axes and units without returning raw arrays.

### `measurement_list_attempts`

List a bounded page of attempts and latest reported statuses for an owned experiment.

### `measurement_list_campaign_suggestion_attempts`

List bounded owned suggestion-attempt summaries in stable order; after is a suggestion-attempt ID from the same campaign, and historical missing telemetry stays absent.

### `measurement_list_experiments`

List a bounded page of the current owner's experiments.

### `measurement_list_methods`

List compact method identities in a bounded owner-scoped page.

### `measurement_list_results`

List compact current outcome heads or the full result history for an owned attempt; no missing outcome is converted to zero.

### `measurement_method_capabilities`

Describe executable LC method validation and storage without an R backend.

### `measurement_predict_retention`

Calculate and retain ideal retention times for an owned constant-flow binary inlet program; missing within-window events remain censored, with no peak-width, resolution or instrument claim.

### `measurement_preview_result_table`

Parse explicitly mapped CSV outcomes without writing or resolving target records; preview does not reserve current attempt revisions. No values or statuses are inferred.

### `measurement_quantify_peak`

Quantify an explicitly assigned chromatogram peak with supplied linear response standards; does not establish peak identity.

### `measurement_read_analysis_data`

Read up to 1,000 rows and 48,000 compact UTF-8 JSON bytes of stored analysis data; continue from the returned next_offset.

### `measurement_read_chromatogram_batch_attempts`

Read one owned item's attempt history with failures, interruption and observed or unavailable runtime evidence; ordinals start at zero.

### `measurement_read_chromatogram_batch_control_events`

Read owned immutable cancellation, recovery and selected-retry outcomes, including controls that affected no items; history survives later retries.

### `measurement_read_chromatogram_batch_export`

Read an owner/store-bound immutable export byte page as base64; restart or eviction requires a new capture, without silently changing the snapshot.

### `measurement_read_chromatogram_batch_items`

Read bounded owned batch-item metadata, states and retained analysis links; does not return raw arrays or execution tokens.

### `measurement_read_chromatogram_evidence`

Read a bounded page from an owned immutable capture selected by its manifest digest; request a new manifest after cache eviction or server restart.

### `measurement_read_result_table_rows`

Page immutable source-record coordinates and exact result identities; later corrections do not replace these import members.

### `measurement_read_result_table_source`

Page exact retained UTF8 source bytes as base64 using byte offsets, including original quoting, line endings and unmapped columns; no filesystem path is read.

### `measurement_read_source`

Read a bounded character page of the retained original CSV/TSV text and its import mapping.

### `measurement_record_attempt`

Record one attempted experiment, its reported status/actual conditions and owned dataset/trace links; no instrument action occurs.

### `measurement_record_gaussian_observation`

Atomically retain latent Gaussian results for a prebound completed simulator attempt and its exact actual-method prediction; returns result IDs for separate analytical assessment and snapshot selection.

### `measurement_record_results`

Atomically record outcomes against an exact attempt revision; copy numeric analysis-summary values or record explicit caller-reported outcomes. Units are caller-declared.

### `measurement_recover_chromatogram_batch`

Mark expired owned claims interrupted and fence late results; does not kill a process, repeat analysis or automatically retry.

### `measurement_register_campaign_protocol`

Freeze a bounded matched synthetic isocratic protocol and owned model, Gaussian producer and observation-contract identities before campaign execution; initialization counts against each arm's budget.

### `measurement_register_csv`

Register one explicitly mapped numeric trace from bounded CSV/TSV text; metadata are the same as for a Trace, excluding axis and signal.

### `measurement_register_dataset`

Register bounded one-dimensional traces and their sample, injection, detector and unit metadata.

### `measurement_register_gaussian_producer`

Declare immutable synthetic Gaussian widths for an owned synthetic retention model before an experiment; does not fit physical peak widths or identify chemicals.

### `measurement_register_long_csv`

Register row-oriented traces grouped by explicit column values, with a separate axis and metadata for each group.

### `measurement_register_method`

Retain an immutable method that meets its declared constraints, with original units, canonical program and digests; does not run an instrument.

### `measurement_register_observation_contract`

Declare an immutable required analyte cohort, analytical acceptance and separate feasibility/runtime preferences before creating experiments.

### `measurement_register_retention_model`

Retain a declared ideal retention reference with natural-log coefficients, explicit geometry/clock/units and optional immutable training lineage; does not fit or physically validate parameters.

### `measurement_register_wide_csv`

Register multiple explicitly mapped detector columns that share one numeric axis; retain the source text and mapping.

### `measurement_registry_capabilities`

Describe the local experiment registry without requiring an R backend.

### `measurement_retry_chromatogram_batch_items`

Reset explicitly selected failed, interrupted or cancelled items to pending with a revision check; preserve attempt history and never reset completed items.

### `measurement_revise_attempt`

Append a corrected or updated reported attempt snapshot with an explanation and optimistic version check; earlier revisions remain available.

### `measurement_run_chromatogram_batch`

Run at most two pending items with shared bounded R capacity and atomic result retention; replaying the same request key never executes more items.

### `measurement_suggest_campaign`

Durably claim and finalize one synthetic suggestion with exact training evidence and failure timing; same-key retries never rerun a claimed computation, and no fallback replaces a failed candidate.

### `measurement_supersede_experiment`

Replace only a current owned design with a complete new immutable experiment and exact expected digest; historical attempts, observations, snapshots and campaign evidence keep their original IDs.

### `measurement_validate_method`

Normalize a declared LC method, calculate time and solvent use, and report feasibility; performs no execution or storage.

## Related contracts

- [Input layouts and limits](inputs.md)
- [Numerical methods](analysis.md)
- [Registry semantics](experiments.md)
- [HTTP identity and storage](deployment.md)
