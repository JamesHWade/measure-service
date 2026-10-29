# Retain the exact evidence used for analytical acceptance

Register an observation contract before creating an experiment, bind the
experiment to that contract, and evaluate exact stored retention/resolution
results from an attempt. The service retains an immutable observation with its
contract, method, attempt revision, assignment declarations and source results.
An explicit observation snapshot freezes membership for a subsequent consumer.
It does not generate suggestions or consume optimizer outcomes.

The [installed example](../examples/observations.py) exercises this workflow
over stdio MCP without R:

```sh
python examples/observations.py
```

## Declare the cohort and rules before attempting a method

`measurement_register_observation_contract` accepts a title, specification and
idempotency key. The profile is `moses-observed-analytical/1`. Declare one to 32
unique required analyte IDs, the named resolution definition, an inclusive
resolution threshold, maximum acquisition duration and separate runtime
preference. Times use minutes; resolution is dimensionless.

The contract retains the published `moses-botier-1.0.0-binary64/1` utility
definition, including its criterion order, bounds, thresholds, smoothing and
continuation objective. This definition names the source's finite-resolution
reference metric. It does not relabel that metric as analyte detection or turn
the hierarchy score into hard analytical acceptance. Utility declarations are
stored before outcomes; this boundary evaluates analytical acceptance rather
than manufacturing scalar utilities from unavailable observations.

Supply `observation_contract_id` when calling
`measurement_create_experiment`. The experiment retains the contract ID and
input digest. An existing experiment cannot acquire a contract after its
attempts by changing a label. Correcting the predeclared cohort or rules means
creating a new contract and experiment.

Each evaluated attempt must reference its actual immutable executable method.
The service derives the acquisition duration from that method's canonical
program. An intended method alone is insufficient, and callers cannot override
the duration in the observation request. These are retained method declarations;
the service does not verify instrument readback or equipment execution.

## Select exact outcome records and assignments

`measurement_evaluate_observation` takes the contract ID, attempt ID, expected
current attempt revision, evidence specification and idempotency key. Each
analyte entry names an assignment state, assignment evidence, and optional
retention and resolution result IDs. Results must belong to the same owner,
attempt revision and analyte, and must be current when first evaluated.

The retention metric is `retention_time` in `min` or `s`; seconds are normalized
to minutes while original values and units remain in the evidence. The
resolution metric is `resolution` with unit `1`. Boolean, negative, nonfinite,
wrong-metric and incompatible-unit observations are rejected. Missing links
remain missing. A result cannot serve two roles or two analytes.

Assignments are `confirmed`, `ambiguous` or `unassigned`. Their evidence is a
caller declaration, not automatic compound identification. A linked stored
analysis can establish the origin of a number without validating the chemical
assignment or a fitted-resolution definition. A processed-signal origin needs
supporting analysis evidence; it does not certify that analysis scientifically.

| Evidence | Detection | Resolution decision |
| --- | --- | --- |
| Confirmed assignment, observed retention within the acquisition window | Confirmed, even without a neighboring peak | Separate observed resolution or unknown |
| Confirmed assignment, retention after acquisition or explicit non-detection | Known absence within this window | Analytical non-acceptance already established |
| Ambiguous/unassigned identity | Unknown | Unknown |
| Missing, failed or censored measurement | Unknown | Unknown |
| Observed resolution zero | Detection remains separate | Known non-acceptance for a positive threshold |
| Observed resolution exactly at the threshold | Detection remains separate | Satisfied |

Missing required analytes remain in the declared cohort as unknown. A confirmed
absence or below-threshold observed resolution dominates unknown evidence in
the conjunction. A singleton with confirmed detection and unavailable
resolution is therefore analytically unknown rather than absent.

Analytical acceptance, maximum-duration feasibility and runtime preference are
separate outputs. An analytically accepted seven-minute run can miss a
five-minute preference while remaining feasible under a 20-minute maximum.

## Keep fidelity, origin and physical qualification separate

Declare `fidelity_role` and `observation_origin` independently. A high-fidelity
simulated observation is still simulated. A low-fidelity instrument observation
is still instrument-origin. If an attempt declares a fidelity coordinate, the
role must agree with its high/low boundary at 0.99. Missing coordinates remain
unavailable declarations. Origin must agree with the attempt's evaluator kind.

These records assess supplied evidence. They do not establish instrument
qualification, physical success, calibrated prediction uncertainty, or the
validity of a retention/signal producer. The immutable provenance retains the
installed backend evidence without inferring a source revision from the
checkout. The service sends no instrument commands.

## Freeze membership and preserve corrections

`measurement_create_observation_snapshot` selects up to 50 owned observation
IDs under one contract. Its deterministic membership and digest reference exact
immutable observations, which remain separately retrievable. Selection order
does not alter the content identity. A snapshot cannot include multiple
observations from one physical attempt, including different revisions of that
attempt. Reprocessing a result is not another physical experiment.

Later result supersession, attempt corrections and new observations leave an
existing observation and snapshot unchanged. A new snapshot explicitly selects
the corrected evidence. Snapshot identity alone does not implement optimizer
consumption, suggestion persistence, or double-delivery recovery in an external
optimizer; those remain separate campaign responsibilities.

Writes share the registry's owner-scoped idempotency keys and transactional
retry receipts. Retrying an identical request returns its original response
even after evidence is superseded. A changed request needs a new key; an invalid
request rolls back its records and receipt. Contract/observation records and
snapshot responses are bounded to 64,000 UTF-8 bytes. SQLite persistence and
local concurrency do not establish a distributed acquisition guarantee.
