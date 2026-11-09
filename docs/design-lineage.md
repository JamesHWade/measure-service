# Replace an intended design without moving its evidence

An experiment is an immutable intended design. A replacement creates a new
experiment ID with a complete specification and an explicit reason. A bounded
lineage connects the versions; each attempt still names the exact experiment
against which it was recorded.

The registry library exposes `design_lineage.supersede_experiment()` and
`design_lineage.get_experiment_design_lineage()`. They use the same local store,
owner boundary and retry-key namespace as the existing experiment registry.
These library calls do not authenticate an HTTP principal; deployment identity
is supplied by the service boundary.

## Public MCP and verified collection

Use `measurement_supersede_experiment` for the retry-safe write and
`measurement_get_experiment_design_lineage` for bounded reads. The read tool
accepts any owned member ID; its `selected_experiment_id` and
`selected_revision` describe that selection, while `current_experiment_id` and
`current_revision` describe the discovered tip. Both operations require no R.

The Python client helper collects and verifies the entire bounded history:

```python
from measure_service.design_lineage_client import collect_experiment_design_lineage

# client is an initialized mcp.ClientSession; root_id is an owned experiment ID.
history = await collect_experiment_design_lineage(client, root_id)
current_id = history["current_experiment_id"]
response = await client.call_tool(
    "measurement_get_experiment", {"experiment_id": current_id}
)
if response.isError or not isinstance(response.structuredContent, dict):
    raise RuntimeError(response.content)
current = response.structuredContent

# Explicitly copy every retained requested field before changing the design.
replacement = {**current["spec"], "description": "Revised scouting design"}
response = await client.call_tool(
    "measurement_supersede_experiment",
    {
        "experiment_id": current_id,
        "expected_input_sha256": current["input_sha256"],
        "spec": replacement,
        "reason": "Correct the intended scouting description",
        "idempotency_key": "scouting-design-replacement-1",
    },
)
if response.isError or not isinstance(response.structuredContent, dict):
    raise RuntimeError(response.content)
replacement_id = response.structuredContent["entry"]["experiment_id"]
```

Use a fresh key for a different intended replacement. A no-op or a concurrent
head change rejects this request; it does not silently select another design.
The collector verifies every entry digest, predecessor, root, selected/current
identity and ordered page. If the head or count changes between pages, it fails
and the caller can start a new collection. Appending after the last returned
page does not change the verified historical view just collected.

Both public collectors use the shared exact JSON preflight before serialization.
Escaped control characters, Unicode, punctuation and aggregate values count
against their canonical byte limits. The lineage collector returns a separate
assembled envelope with `offset=0`, `limit=count` (up to 50), and
`next_offset=null`; its 160,000-byte bound is not the 20-entry wire-page bound.

## Replacement and concurrency

Supply the current experiment ID, its exact `input_sha256`, the complete new
`ExperimentSpec`, a reason and an idempotency key. Each replacement chooses its
method, observation contract and producer explicitly. Omitted optional links
are absent from the replacement; they are not inherited from its predecessor.
A changed observation contract does not change the contract used by an earlier
attempt or assessment.

Only the current tip can be superseded. Competing replacements have one winner;
a stale caller must retrieve the lineage and choose a complete new request.
The root entry, replacement experiment, version, head update and retry receipt
share one transaction. An invalid link, ownership conflict, stale ID/digest,
no-op or limit violation leaves no partial replacement or successful receipt.

A reason alone does not create a new design. No-op detection compares canonical
requested specifications after normalizing legacy absent defaults in a temporary
comparison value. Numeric `1`, `1.0` and boolean `true` remain distinct. The
original retained payload and its digest are never normalized or rewritten.

Repeating a successful key returns its original receipt, including after later
supersession and server restart. Reusing it for different arguments fails.
The new operation verifies the receipt against the owned historical entry and
its complete request. Older operations retain their existing receipt contracts.

## Read history and current discovery

Select any owned member to inspect the same root and ordered history. The page
identifies the selected revision and the current tip separately. Entries retain
experiment IDs and input digests, predecessor IDs and entry digests, exact
reasons and server-recorded creation times. Version 1 uses the original design's
creation time. Its identity is the same before and after the first replacement.

Read checks every member's canonical payload, experiment binding, predecessor,
owner and contiguous revision. A separate current-head record detects missing
versions, including accidental tail deletion. Unexpected inbound successor
references fail instead of making another chain's design appear current.
These content digests detect inconsistent records; they are not signatures or
proof against coherent rewriting of an entire store.

Reading a standalone design returns a virtual singleton without creating
lineage tables or rows. The optional schema is created only by a successful
replacement, and is validated before unrelated schema initialization. A partial
or unsupported component fails closed.

## Historical recording stays explicit

A current pointer is discovery information. It does not schedule work or move
attempts, measurements, results, assessments, snapshots or campaign evidence.
They continue to reference their original IDs and content digests.

Existing attempts on an older design can be revised, and late historical
attempts can still be recorded explicitly against that older ID. Use the new
experiment ID when an attempt actually follows the replacement. The service
does not infer that choice from a timestamp, result correction or current tip.
Replacements do not themselves run an analysis, evaluate an observation or
consume optimizer evidence.

## Limits

A lineage has at most 50 versions, including its initial design. Page limits
are 1–20 entries, with nonnegative integer offsets; booleans and fractional
counters are rejected. An offset at or beyond the count returns an empty page.
A reason must contain non-whitespace text, exclude NUL, and fit 2,000 UTF8 bytes.
The returned entry, including its digest, must fit 3,000 canonical JSON UTF8
bytes. Both limits apply: JSON escaping can make a reason exceed the entry
budget even when its raw text fits.

Each wire page or receipt fits 64,000 canonical UTF8 bytes. The complete
50-entry client collection can fit 160,000 bytes and is distinct from a wire
page. No values, units, cohort membership, model-training evidence or physical
qualification are inferred from a design replacement.
