# Inspect failed and interrupted campaign suggestions

Every new suggestion computation has a durable claim before candidate
computation starts. Its terminal record retains success, a rejected candidate
or a known failure. A transport retry with the same owner, idempotency key and
request replays that result; it does not call the optimizer again. This applies
to common initialization and random sampling as well as the optional BO-MCP
runtime.

Suggestion attempts are separate from analytical experiment attempts. A failed
suggestion spends no analytical evaluation budget and creates no experiment or
observation. Its failure is never a zero-valued training outcome. The service
retains the exact ordered consumed observations and snapshot identities that
were available when it claimed the suggestion.

## Observe a failure and retry it after restart

Install the service, then run the source-checkout or source-archive example:

```sh
python examples/suggestion_recovery.py
```

The example starts the installed stdio MCP service in an isolated temporary
store and deliberately removes its optional BO runtime setting. It acquires
and consumes three real synthetic initial observations. The fourth suggestion
records the missing-runtime failure. After a server restart, the same key and
revision replay the same failure and unchanged evidence hashes, with three
consumed observations and one remaining evaluation slot.

This exercises storage, MCP, synthetic observation production and restart
behavior. It launches no optimizer or instrument and does not qualify a GP or
an optimization policy. The [real backend setup](campaign-runtime.md) remains a
separate requirement.

## Read records at the suggestion-attempt grain

Use these MCP tools:

- `measurement_list_campaign_suggestion_attempts(campaign_id, limit=10,
  after=None)` returns summaries in `(started_at, suggestion_attempt_id)` order.
  `next_after` is either the last ID needed for the next page or null. Limits
  are integers from 1 through 20; a cursor must belong to the same owned
  campaign.
- `measurement_get_campaign_suggestion_attempt(suggestion_attempt_id)` returns
  the frozen request and context, its hashes, the terminal outcome if present,
  and any separately retained late result. Public records exclude the raw retry
  key and execution token.
- `measurement_get_campaign` continues to report analytical campaign state,
  budget and pending proposal. Suggestion coordination has its own state; a
  campaign can be analytically ready while a suggestion claim is unresolved.

The record profile is `campaign-suggestion-attempt/1`. Its
`suggestion_attempt_id` identifies one computation reservation. The claim
binds the campaign, expected revision, ordinal, protocol, policy, seed,
requested optimizer settings and source identities, ordered training records
and immutable consumption/snapshot references. Public reads revalidate that
context against the owned protocol and consumed prefix, including typed
canonical JSON rather than treating false, zero and unavailable as equivalent.

There is one active claim per campaign, across all retry keys. The claim
reserves the global owner/key namespace, so another registry operation cannot
steal it while the optimizer runs. Candidate computation happens outside the
SQLite write transaction; unrelated campaigns and registry operations can
continue. Finalization checks the same claim token, start/context identity,
revision, history and pending state before accepting a proposal.

Existing successful suggestion responses retain their previous shape and
receipt behavior. Historical successful receipts can still replay without
creating a claim. Earlier failures without a durable claim remain unrecorded;
the service cannot infer their calls or timings. Reissuing an unrecorded key
can therefore start new work. The previous frozen synthetic results have not
been resumed, reclassified or backfilled.

## Interpret terminal outcomes

| State | Meaning | Analytical proposal or spend |
| --- | --- | --- |
| `claimed` | Execution was reserved; dispatch and completion are unresolved | None established |
| `accepted` | The returned candidate passed validation and policy | A pending proposal; spend follows actual outcome consumption |
| `rejected` | Returned data, source identity, feasibility, policy or retention bounds prevented admission | No new proposal or analytical spend |
| `failed` | A known candidate-generation, runtime or transport failure occurred | No new proposal or analytical spend |
| `abandoned_unknown` | The owner explicitly reconciled an unresolved claim | No inference that the optimizer failed or never ran |

A rejected candidate that fits the record bound retains its coordinates and
verified backend evidence. Source contradictions are checked **before** any
proposal or success receipt commits. Contradictory returned metadata is boxed
as `untrusted_returned_evidence`, not presented as verified provenance, and
cannot create a proposal. This keeps the rejection readable without granting
trust to the data it rejected.

The original request is bounded at 64,000 bytes, context at 128,000, terminal
or late-event data at 64,000, and a full public record at 256,000. If complete
returned metadata cannot fit the terminal envelope, the rejection retains the
available candidate, failure and timings plus explicit `retention` hashes and
byte counts for omitted data. A hash identifies omitted bytes; it does not
make them available. No success is committed with incomplete required evidence.

Ordinary errors, including an exception with no message, receive bounded,
nonempty failure details. Runtime diagnostics retain categories, requested
source/request hashes, return code, byte counts and response hash where
available. They do not retain raw stdout, stderr or local executable paths.

## Keep timing scopes and missingness separate

| Field | Scope |
| --- | --- |
| `candidate_generation_elapsed_seconds` | Host wall time around candidate computation and validation, outside claim/final database writes |
| `adapter_subprocess_elapsed_seconds` | Child-call wall time retained from the installed adapter, when available |
| `backend.runtime.elapsed_seconds` | Existing proposal provenance's service adapter-call wall time; its legacy meaning is preserved |

These intervals overlap. Do not add them together or interpret them as CPU or
instrument time. A known zero remains zero. An unavailable value is null.
Missing-runtime resolution has no child-process interval; a timeout can retain
its observed child-call interval while the child result remains unavailable.
A claim alone is not proof that the child process started. A process can die
between reserving execution and launching the child.

Owner/key failures are terminal and immutable. To deliberately try a new
computation after a recorded rejection or failure, submit a **new** idempotency
key with the current campaign revision. The earlier attempt remains in the
ledger. This is distinct from a transport retry and is not an automatic
fallback, jitter or optimizer retry.

## Reconcile an unresolved claim explicitly

There is no lease that silently redispatches work. If a server process or its
client disappears, inspect the claim and reconcile only when the owner intends
to fence that computation:

```text
measurement_abandon_campaign_suggestion_attempt(
  suggestion_attempt_id,
  expected_start_sha256,
  reason,
  idempotency_key
)
```

The start hash must match and the reason must be nonempty, at most 500
characters. Reconciliation uses its own retry key. It freezes
`abandoned_unknown` with unavailable candidate and timing; it does not kill a
process or invent a failure. A new key can then reserve another computation.

If the original live worker finishes later, its token cannot resurrect the
claim, create a proposal or overwrite the authoritative reconciliation. One
bounded immutable late-result event may retain the available computation data,
clearly separate from the authoritative outcome. Identical late-finalization
retries are idempotent; conflicting rewrites are refused. The service promises
durable visibility and controlled admission, not exactly-once external process
execution across a crash.

## Preserve scientific and operational boundaries

Claims, terminal outcomes and late events are included in owned-store closure
checks and SQLite backups. An unresolved claim is a valid typed root even
before a successful registry receipt exists. Unknown tables, foreign owners,
off-plan keys, orphan evidence and source-inconsistent records are refused.

This layer changes evidence and retry behavior. It does not change either
existing protocol's duplicate policy, manufacture analytical outcomes, qualify
physical replicates, or replace the [recorded partial study](synthetic-ablation-results.md).
The [follow-up issue](https://github.com/JamesHWade/measure-service/issues/45)
also requires explicit new-profile replication semantics and sequential
real-backend qualification before another optimization comparison.
