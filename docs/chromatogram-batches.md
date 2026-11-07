# Resume a declared chromatogram batch

A batch freezes a bounded selection of already registered traces and one
normalized chromatogram method. Run calls process at most two pending items
using the same input validation, R worker, spacing warnings and result format
as `measurement_analyze_chromatogram`. Successful results remain ordinary
analyses: read their evidence and export reports with the
[chromatogram evidence client](chromatogram-evidence.md).

## Try the restart and retry example

[Inspect a retained synthetic run](reference-data/chromatogram-batch-example/summary.html)
or read its [evidence and scope](chromatogram-batch-results.md).

From an installed service environment with compatible R backends, run:

```sh
python examples/chromatogram_batch.py \
  --output-dir /tmp/chromatogram-batch-report \
  --data-dir /tmp/chromatogram-batch-store
```

Both paths must be new, separate and non-nested. The optional `--data-dir` retains a new local store;
omitting it uses a temporary store. The example never opens an existing store.
It respects `MEASURE_MCP_R` and the caller's R library configuration.

Four synthetic 501-point UV traces have explicit reference, blank, QC and
unknown roles. Two complete, a third deliberately fails with an unavailable R
executable, and the fourth is cancelled while pending. After selected retry,
all four complete. Process restarts separate those stages. Replaying the
original success and failure requests with R unavailable performs no new work;
the first two analysis IDs remain fixed and the original failed run stays
failed. Cancellation remains visible in retained control history.

Open `summary.html` for links to four evidence reports. `workflow_receipt.json`
retains the transitions, bounded public reads and report hashes. Four CSV tables
support analysis without flattening distinct grains:

| Table | One row represents | Constructed example rows |
| --- | --- | --- |
| `items.csv` | Selected input, metadata, current state and retained result | 4 |
| `attempts.csv` | Actual numerical execution attempt | 5 |
| `control-events.csv` | Cancellation or selected-retry event | 2 |
| `control-items.csv` | One item affected by a control event | 3 |

The deliberate failed attempt has unknown R runtime evidence; blank CSV runtime
cells mean unavailable, while the blank trace's zero detected peaks remains
numeric zero. CSV exports escape spreadsheet formula prefixes and control
characters in text; JSON retains original values. This example exercises local
stdio MCP restarts and retained outcomes. It does not simulate instrument
acquisition or prove physical method performance. Explicit expired-claim
recovery is covered separately by the public subprocess crash contract test.

Batch declarations, progress and attempt history are available without R.
Executing a pending item requires compatible `measure` and `recipes`
backends (`measure.sec` is required for SEC tools). This workflow performs numerical processing of existing traces;
it does not acquire a sample or control an instrument.

## Declare inputs and settings

Register each dataset through the existing [input tools](inputs.md), then
call `measurement_create_chromatogram_batch` with its owned dataset/trace IDs:

```json
{
  "batch": {
    "schema_version": "1.0",
    "profile": "measure-chromatogram-batch/1",
    "title": "Reference and sample preview",
    "method": {
      "baseline": "als",
      "smoothing": "gaussian",
      "smoothing_sigma": 2,
      "min_prominence": 0.3,
      "integration_baseline": "local"
    },
    "inputs": [
      {
        "dataset_id": "owned-reference-dataset",
        "trace_id": "uv-254",
        "role": "reference"
      },
      {
        "dataset_id": "owned-sample-dataset",
        "trace_id": "uv-254",
        "role": "unknown",
        "internal_standard_references": ["declared-IS-A"]
      }
    ]
  },
  "idempotency_key": "preview-batch-1"
}
```

Each input requires an explicit role: `unknown`, `blank`, `standard`, `qc` or
`reference`. These are caller declarations, kept separately from the source
trace's original `sample_type`. Internal-standard labels establish no peak
assignment, correction factor or concentration. The manifest retains the
original sample, injection, channel, units, import mapping and source identity.
Selecting the same dataset/trace twice is rejected.

The stored method includes all normalized defaults. The schema/profile version
defines its interpretation; arbitrary recipes, executable settings and server
paths are not accepted. The manifest identifies inputs by their recorded
content digests. Read it with `measurement_get_chromatogram_batch`; it contains
metadata and references, without copying raw arrays into every item response.

## Run a bounded group

Read the current batch revision, then call
`measurement_run_chromatogram_batch`:

```json
{
  "batch_id": "owned-batch-id",
  "idempotency_key": "preview-run-1",
  "expected_revision": 1,
  "max_items": 2
}
```

`max_items` is an upper bound. A run reserves only the immediately available
capacity and fixes its selected item membership. At most two R subprocesses
run per service process across batches, previews, SEC, quantitation and backend
discovery. If no permit is available, the call returns a capacity error without
reserving the request key. There is no detached worker queue. Additional
processes have independent limits; multi-node scheduling is not implemented.

One run call waits for its selected work. A disconnected or timed-out client
does not establish that execution stopped. Repeat the **same request key and
arguments** to read that retained run without executing more items, even while
capacity is busy. `measurement_get_chromatogram_batch_run` also reads a run by
ID. Use a new request key and a fresh batch revision to request the next group.
Changing the arguments of a retained key is rejected.

Run membership refers to particular attempts. A failed attempt remains failed
in its original run after a later retry completes the item. The item read shows
its current state and retained analysis link; the attempt read preserves the
earlier failure and subsequent success.

## Read progress, cancel and recover

`measurement_read_chromatogram_batch_items` pages current item states.
`measurement_read_chromatogram_batch_attempts` pages the history of one
zero-based item ordinal. `measurement_read_chromatogram_batch_control_events`
pages immutable cancellation, recovery and selected-retry outcomes. Cancellation
remains visible there after the affected item is retried or completed, even if
the client missed its original response. These reads enforce ownership and omit
execution tokens and raw arrays. Use the returned `next_offset` until it is `null`. Pages contain at most 20
rows. Batch `active` means incomplete, not necessarily executing: read the
per-state counts to distinguish pending work, running work and items awaiting
explicit retry. Control-event numbers increase independently of item attempts;
a no-op recovery is retained without inventing a numerical attempt.

| Item state | Meaning |
| --- | --- |
| `pending` | Eligible for a new explicit run request |
| `running` | Reserved attempt; computation or its outcome may still be unresolved |
| `completed` | An ordinary analysis and its item completion committed together |
| `failed` | A bounded failure record was retained |
| `interrupted` | Explicit recovery fenced an expired claim without a committed result |
| `cancelled` | Pending work was stopped before execution |

`measurement_cancel_chromatogram_batch` requires the current revision and an
idempotency key. It cancels pending items and keeps successful results.
Already running numerical work may still complete or fail; cancellation does
not promise immediate R-process termination.

Each claim has a 150-second lease; an R call normally has a 90-second timeout.
If the service dies or cannot commit an outcome, the claim stays explicit.
After its lease expires, call `measurement_recover_chromatogram_batch` with
the current revision and a new request key. Recovery marks expired claims
`interrupted` and fences late results. Expiry never grants permission to run
again, and an expired claim cannot commit even before recovery.

To retry, call `measurement_retry_chromatogram_batch_items` with an explicit
nonempty list of `ordinals`, the current revision and a new key. Only failed,
interrupted or cancelled items are eligible, and no items may still be running.
Completed items are never reset. Retried work needs a further explicit run
request; history remains intact and each item allows at most eight attempts.

A retry may repeat pure numerical computation when interruption left no
committed result. The service guarantees one retained successful analysis per
item, not exactly-once computation. Independent processes or orphaned children
after a crash are outside the process-local concurrency bound.

## Interpret runtime evidence

Each attempt records its pre-execution service/worker fingerprints. Successful
analysis provenance also retains the R version, package versions and installed
R-code evidence returned by that execution. If failure or interruption prevents
that observation, R runtime evidence stays explicitly unknown; it is never
filled from an earlier capability probe.

Compatible runtime updates can change between attempts. Their observed builds
remain visible rather than imposing exact revision pins. This contract does
not establish numerical equivalence across those builds or qualify a physical
method. Peak IDs and caller labels establish no chemical identity or purity.

## Limits and remaining scope

A declaration accepts at most 32 items and 500,000 total selected points.
Existing per-trace and dataset limits still apply. Normalized declaration/mutation requests are limited to 64,000 bytes;
manifests are limited to 256 KiB; their response envelope adds at most 16 KiB.
Public pages are limited to 256 KiB. The manifest stores dataset references.
`measurement_read_source` pages retained CSV/TSV source text when available;
successful analyses expose numeric trace pages through the evidence tools.
The SQLite batch schema is additive and version checked; unsupported
versions fail rather than being guessed or silently reinterpreted.

The batch manifest is an inspectable declaration with local references. Use
the separate [portable evidence snapshot](batch-snapshots.md) to retain complete
datasets, source assets, results and history, or to import that evidence into a
new store with a fresh pending batch. Import does not recreate active historical
claims or establish fresh numerical reproduction.

This is the single-trace slice of service #11. MOCCA2 interoperability, DAD/MS,
cross-run component assignments and calibration revisions remain separate work.
Optimizer campaign state and consumed observation snapshots are not modified
by analytical batch retry, recovery or local evidence import.
