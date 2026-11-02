# Declare how a campaign handles repeated candidates

`synthetic-isocratic-ablation/2` requires `duplicate_policy` alongside the
optimizer target and noise prior. Choose `reject` or `replicate` before
execution. Omitting the policy is invalid. The paired/1 and ablation/1 profiles
keep their original declarations, hashes, streams and rejection behavior.

| Policy | Candidate already proposed in this campaign | Analytical budget |
| --- | --- | --- |
| `reject` | Retain a rejected suggestion attempt; create no proposal | Unchanged |
| `replicate` | Create a new proposal and executable method at the next ordinal | Charge a new consumed execution |

Equality uses the exact normalized candidate digest. There is no distance
tolerance, jitter, averaging, substitution or automatic retry. A backend can
return the same boundary point repeatedly. A rejection is a policy outcome;
it does not establish that the backend returned an invalid point or that the
campaign completed its budget.

## Register an explicit protocol

Use the [ablation declaration](ablation-protocol.md) with these three fields:

```json
{
  "profile": "synthetic-isocratic-ablation/2",
  "optimizer_target": "confirmed_acceptance_reward",
  "surrogate_noise_prior": "default",
  "duplicate_policy": "replicate"
}
```

This fragment must be combined with the domain, flow, constraints, owned task
bindings, seeds, budget and declared cost required by the full protocol.
Protocol identity includes the policy. For this new profile, coordinate-design
identity excludes duplicate policy, target, prior and opaque task binding IDs.
Otherwise equal declarations therefore share initial and random coordinates
across those factors. The new profile does not claim the older profile's
coordinate identity.

## Each repeated evaluation owns fresh evidence

A new-profile proposal contains `repetition`: its policy, whether it repeats
an earlier candidate, the first matching proposal ID (or null), and an ordered
`matching_prior_proposals` list of proposal IDs and ordinals. Retrieval derives
that relationship again from the owned ordered history. Campaign history and
pending-proposal summaries contain `matching_prior_proposal_count` in place of
the full list, keeping the campaign response bounded through its maximum
50-evaluation budget. The proposal and consumption retain the full lineage.
Reject-policy history is checked on reads as well as writes, including records
created before this version.

Individual proposal, consumption, method and campaign records remain bounded
at 64,000 bytes. New-profile suggestion and consumption responses combine two
such records and allow at most 128,000 bytes; successful suggestion retry
receipts use the same bound. Request and terminal-outcome limits remain
64,000 bytes. The capability response advertises the composite response bound.

A repeated candidate receives a fresh proposal, executable method, experiment,
attempt, prediction, generated results, analytical observation, snapshot and
consumption. Reusing an experiment for another new-profile consumption is
invalid even if it has another attempt. The model, producer and observation
contract remain the declared task's shared inputs. Every consumed evaluation
counts toward the budget and declared cost, including initialization and
repetitions. Ordered optimizer training retains every consumed row.

The latent simulator is deterministic. These are separately executed equal
inputs with separately charged spend; their results do not measure chemical
repeatability, independent measurement noise or physical uncertainty.

The [suggestion ledger](suggestion-attempt-evidence.md) counts optimizer calls
separately from analytical spend. A same-key retry replays the exact prior
success or failure and does not create a replicate. A new key after a terminal
failure explicitly requests another computation. Interrupted claims require
owner reconciliation; elapsed age alone never permits redispatch.

## Existing live stores migrate once

Service startup recognizes the supported proposal schema and removes its
candidate uniqueness constraint using a transactional table rebuild. Ordinal
uniqueness, one pending proposal per campaign, foreign keys, exact row values,
receipts and identities remain enforced. A nonunique candidate lookup supports
policy checks. Unknown or partially changed schema shapes fail explicitly.
The migration restores foreign-key enforcement and verifies referential
integrity before accepting the migrated connection. Tests cover populated
stores, rollback, repeated and concurrent startup, views and triggers.

The procedure follows SQLite's documented
[table rebuild](https://www.sqlite.org/lang_altertable.html#otheralter).
Migrating a live database changes its database-file bytes. Sealed historical
artifacts must remain read-only and must not be passed through service startup.
Use a separate new store for qualification. The original 543-observation
[reference study](synthetic-ablation-results.md), its unknown slots and
unavailable historical rejected-call timing remain the reference.

Registration and mock-based contract tests establish policy behavior. Real
sequential backend qualification and any later optimization comparison require
separate frozen declarations, retained failures and full planned denominators.
