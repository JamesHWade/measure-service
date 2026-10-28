# Experiment and result contracts

The registry records what a caller reports and links it to stored measurements.
It requires no R backend. The executable [registry example](../examples/registry.py)
shows the tool sequence over MCP.

## Records and ownership

An **experiment** is an immutable intended design. It contains a title,
caller-declared source, intended conditions and optional external/proposal
references. A proposal reference is a label, not a validated optimizer campaign.
Correcting a design currently means creating another experiment; design
supersession is not implemented.

An **attempt** belongs to one experiment and records actual conditions,
reported status, measurements, evaluator declarations and optional timestamps,
instrument/method references and replicate identifiers. Dataset/trace links
must belong to the same principal. The service resolves them and snapshots
sample, injection, detector, units and input digest.

An **outcome** belongs to an exact attempt revision. It contains a metric,
caller-declared unit, optional analyte label, outcome status and value or
bounds. Chemical identity and unit compatibility are not established by the
registry. No conversion is applied.

Numeric conditions need a finite value and an explicit unit (`"1"` for
dimensionless). Categorical and boolean conditions carry no unit. Names must
be unique within each intended or actual condition set. Types are retained;
a string such as `"0.4"` is not treated as a numeric condition.

## Reported lifecycle and revisions

Attempt statuses are `planned`, `running`, `completed`, `cancelled`, `failed`
and `unknown`. The last three require explanations. Timestamps must include a
timezone and are normalized to UTC; end must not precede start. Planned
attempts have no actual timestamps; running attempts have no end timestamp.
The registry does not invent timestamps or enforce an execution transition graph.

`measurement_revise_attempt` requires the expected current revision, a complete
replacement specification, a reason and a new idempotency key. It appends a
snapshot; it does not patch fields. Earlier revisions remain readable.
Recording a state does not start or stop equipment. A physical retry is a
separate attempt, while a record correction is a revision.

Replicate kind/index require a replicate group. They are caller assignments,
not a statistical model. Evaluator kinds are `manual`, `instrument`, `simulator`
and `replay`; `manual` is the default. Evaluator/version, training-snapshot
reference and fidelity are caller-declared. Fidelity is a coordinate in [0, 1],
not measured accuracy. These declarations are retained in outcome provenance.

## Outcome states

| Status | Value or bound | Meaning recorded |
| --- | --- | --- |
| `observed` | Finite number, including zero | A reported numeric observation |
| `not_detected` | No value or bounds | Caller reports no detection |
| `below_limit` | Upper bound only | Caller reports a value below a declared limit |
| `censored` | At least one bound; lower < upper if both | Caller reports a bounded observation |
| `missing` | No value or bounds | Result unavailable |
| `failed` | No value or bounds | Measurement or evaluation failed |
| `infeasible` | No value or bounds | Caller reports infeasibility |

Every non-observed outcome needs an explanation. None is converted to zero.
The registry does not infer non-detection or feasibility from an analysis.

For an observed outcome, supply either a manual `value` or an `analysis_id`
and direct numeric `summary_field`, omitting `value` in the latter case. The
analysis must belong to a dataset/trace linked to the selected attempt revision.
The service copies the stored number, backend provenance and warnings. It
rejects caller overrides, nested field paths and executable transformations.
An optional analysis link on a non-observed outcome is supporting evidence for
the caller's classification.

Provenance distinguishes `caller_reported` from `stored_analysis_summary`
values and marks units as `caller_declared`. A copied value is evidence-linked;
its metric, unit and analyte assignment still need to make scientific sense.

## Corrections, retries and transactions

One current outcome is allowed per attempt/metric/analyte-label combination.
A correction supplies `supersedes_result_id` and an explanation. It may change
the value, unit, metric or analyte label within the same attempt, provided it
does not collide with another current outcome. Old records remain readable;
competing corrections of the same current result cannot both succeed.

Every registry write needs an idempotency key scoped to the principal across
all registry operations. Repeating the same normalized request with the same
key returns its original response, including after later revisions. Reusing a
key for a different request fails. Keep the original key and request when
retrying after a lost response. A new logical operation needs a new key.

Results require `expected_attempt_revision`; stale revisions fail without
writing. Result batches commit all records and their retry receipt in one
SQLite transaction. One invalid entry rolls back the whole batch. These are
single-node database guarantees, not distributed or instrument guarantees.
Dataset registration and numerical analysis calls have no retry key and create
new records when repeated.

Limits are 64 conditions per set, 32 measurement links per attempt, 50 outcomes
per batch and 50 records per retrieval page. Records and registry
request/response batches are limited to 64,000 serialized UTF-8 bytes. List
operations use offset pagination and `next_offset`; they are not immutable
campaign snapshots. See [deployment](deployment.md) for persistence limits.
