# Keep reported work attached to its intended design

Run the installed public-MCP workflow in new, separate directories:

```sh
python examples/design_lineage.py \
  --output-dir /tmp/new-design-lineage-report \
  --data-dir /tmp/new-design-lineage-store
```

Both paths must be absent, separate and non-nested. The example rejects existing
paths and dangling symlinks before creating either target. Omitting `--data-dir`
uses a temporary store. An `EXAMPLE_INCOMPLETE` marker remains if execution fails.
No R backend is used. All service calls pass through two successive stdio MCP
sessions; the example does not access the registry or SQLite directly.

The [executed report and downloads](reference-data/design-lineage-example/index.html)
retain the three designs, all history and the original snapshot. The
[independent audit](reference-data/design-lineage-example/independent-review.json)
verified 789 source/store/artifact checks without rerunning the workflow or
using service validators. The [publication manifest](reference-data/design-lineage-example/publication.json)
binds every retained public file to its original execution source and wheel.

## The reported sequence

Three owned isocratic method declarations and one A/B observation contract are
created before the first experiment. The contract requires both labels, with
an inclusive dimensionless resolution threshold of 1.5. Labels, assignments,
units, actual conditions and fidelity are caller declarations, not physical
qualification or measured prediction accuracy.

| Design | Intended flow (mL/min) | Declared duration (min) | Retained work |
| --- | ---: | ---: | --- |
| v1 | 0.4 | 7 | Completed attempt reports 0.395 mL/min; its accepted observation and original snapshot remain fixed |
| v2 | 0.45 | 8 | Attempt is planned while v2 is current; its completion is recorded after v3 exists, still against v2, reporting 0.44 mL/min |
| v3 | 0.5 | 9 | Current design has no recorded attempt or assessment |

Two explicit replacements produce three immutable experiment IDs. A stale
request against v1 fails, leaving exactly three designs. Replaying the first
successful replacement returns its original receipt after v3 and again after
server restart. The [lineage client](design-lineage.md) verifies the complete
ordered chain and keeps current discovery separate from selected history.

The first v1 attempt reports A/B retention times of 2 and 4 minutes, with
resolutions of 2 and 1.5. Its explicit assessment is accepted and snapshotted.
A later metadata revision and a separately recorded late v1 attempt still name
v1. Completing the planned v2 attempt does not move it to v3. Its new reported
B resolution is observed zero, so its separate assessment is not accepted.
This is a constructed illustration, not a causal comparison of flow settings.
The separately recorded late v1 attempt has no result records and is not
assessed. Its reported completed status does not supply missing measurements.

The report retains three experiments, three attempts, five attempt revisions,
eight results, two explicit assessments and one snapshot. No operation infers
that the current design is measured, best or ready for instrument execution.
The call order records software events; it does not establish a physical run
order or prove independence of attempts.

## Inspect the evidence

Open `index.html` for the ordered lineage, attempt revisions, unit-bearing
results and both assessments. Its named table regions support keyboard focus
and horizontal scrolling. Local links provide:

- `designs.json`: all three complete immutable experiment records.
- `lineage.json`: verified versions, predecessor hashes and the discovered tip.
- `replacement-v1-v2.json` and `replacement-v2-v3.json`: immutable write receipts.
- `attempts.json`: five revision views, three latest views and before/after
  discovery metadata for the two revised attempts.
- `results.json`: eight exact result records and their attempt-revision bindings.
- `observations.json` and `snapshot.json`: both assessments and the original
  snapshot, including unchanged historical records after later work.
- `history.csv`: a typed audit log; `record_type` distinguishes design, attempt
  revision and result rows. Values keep their declared units and observed zero.
- `workflow_receipt.json` and `manifest.json`: complete retained responses,
  verification results, declared scope, file sizes and hashes.

An old `get_attempt(revision=1)` response can show `latest_revision=2` after a
metadata correction. Its exact revision-1 specification, digest, method, reason
and creation time remain unchanged. The immutable result and snapshot bindings
continue to identify revision 1; the latest-discovery field does not rewrite them.

New runs produce new IDs and timestamps. Choose new paths and use their returned
IDs. Never substitute a current lineage pointer into a historical snapshot or
optimizer input. Design supersession, measured-data qualification, model-training
lineage, campaign consumption and physical execution have separate contracts.
The broader service #4 remains open for the remaining work.
