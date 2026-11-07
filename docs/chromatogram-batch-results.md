# A retained chromatogram batch

[Open the four-result summary](reference-data/chromatogram-batch-example/summary.html).
This is a constructed workflow example with explicit reference, blank, QC and
unknown roles. It demonstrates what the service remembers when numerical
processing fails, pending work is cancelled, and selected items are retried.

Each input has 501 synthetic UV samples on a 0–5 minute axis. The method uses
an ALS baseline, Gaussian smoothing with sigma 2, minimum prominence 0.3 and
local-baseline integration. Reference, QC and unknown inputs each produce two
detected peak rows; the zero-signal blank produces zero. These labels and peak
rows establish no chemical identity, concentration, purity or physical method
performance.

## What happened

1. The reference and blank completed, retaining their ordinary analysis IDs.
2. After a process restart with R deliberately unavailable, the QC attempt
   failed. Its R runtime evidence stayed unknown. The still-pending unknown
   item was explicitly cancelled.
3. Explicit retry selected only QC and unknown. A restarted service with R
   available completed both. The first two analyses remained unchanged.
4. With R unavailable again, the original success and failure requests replayed
   without numerical execution. The failed run remained failed while its item
   was now completed. Cancellation remained in the durable control history.

| Evidence | Retained outcome |
| --- | --- |
| Selected items | Four completed analyses with four distinct IDs |
| Numerical attempts | Four successful attempts and one original failure |
| Control history | One pending cancellation and one selected retry |
| Affected-item history | One cancellation row plus two retry rows |
| Raw and processed signals | All 2,004 input samples retained, with corresponding processed values |
| Failure runtime | Unknown, without copying a previous successful R build |

The [workflow receipt](reference-data/chromatogram-batch-example/workflow_receipt.json)
contains transitions, public reads and byte hashes. Four separate CSV tables
retain the item, attempt, control-event and affected-item grains. Each analysis
also has an HTML report, SVG plot, complete evidence JSON, trace CSV and peak
CSV. Empty runtime fields represent unavailable evidence; the blank trace's
zero peak count remains zero.

## Reproduce and interpret

Run [the executable example](../examples/chromatogram_batch.py) using the
[new-directory instructions](chromatogram-batches.md). The service observes
compatible R builds at execution; it does not require exact revision pins.
The published runtime identifies the particular builds used for this run.
Analysis IDs, timestamps and metadata hashes will differ on a new run.

This example uses local stdio MCP and existing synthetic traces. Public HTTP
ownership and actual process crash/recovery are tested separately. Recovery
uses an expired-lease fixture in that subprocess test; this example does not
claim a wall-clock timeout or interrupted R computation. No optimizer study,
instrument acquisition, portable source restoration or MOCCA2 interoperability
is established here. Service #11 remains open for those remaining snapshot and
interoperability requirements.
