# Observation and acceptance contract for subsequent optimization

The archive reconciliation selects explicit inputs, utility and acceptance
rules for the next optimization milestone. The pure acceptance kernel and
[immutable service evidence](observation-evidence.md) implement this boundary.
They do not qualify new physical measurements or implement an optimizer.

## Keep reproduction and new decisions identifiable

The archived-score trajectory remains the reproduction baseline. The
`moses-botier-1.0.0-binary64/1` profile with serialized summaries, or selected
peak input for a targeted experiment, explains the recorded scalar scores. It
does not establish how the historical summaries were obtained. Do not use a
serialized summary as the default input to a new run simply because its score
matches the archive.

For source-code comparisons, retain the explicit raw-array profiles and their
hard count/resolution flags. Their finite-resolution detection convention is a
reference calculation. It is not a general physical definition of whether an
analyte eluted: a single finite retention time can have no finite neighboring
resolution. The [metric audit](moses-metric-reconciliation.md) records the
observed scope and unresolved historical definitions.

The selected subsequent-run contract is `moses-observed-analytical/1`:

| Concern | Selected rule |
| --- | --- |
| Identity | Declare the required analytes or selected target before suggestion; preserve each observation's assignment evidence |
| Detection | Determine observed presence from a qualified retention-time/assignment result within the acquisition window, independently of resolution availability |
| Resolution | Use an explicitly named and qualified resolution definition; missing, nonfinite or unsupported results remain unavailable rather than proving separation |
| Analytical acceptance | Every required analyte must have confirmed detection and meet the declared inclusive resolution threshold; confirmed absence or an observed resolution below the threshold fails acceptance, and unavailable evidence remains unknown unless non-acceptance is already established |
| Feasibility | Check the declared maximum acquisition time, method constraints and units separately from analytical acceptance |
| Runtime preference | The five-minute hierarchy threshold is an optimization preference; it is not automatically a hard analytical-acceptance condition |
| Utility | Declare the scalarizer version, criterion order, bounds, thresholds, continuation rule and binary64 arithmetic before the campaign; retain their decomposition |
| Missingness | Preserve unavailable, non-eluting, censored and observed-zero statuses; neither a missing outcome nor a stopped campaign becomes a known failure |
| Fidelity | Record the fidelity role and observation origin separately; a simulated prediction cannot establish a physical result |
| Accounting | Count initial-design runs; retain suggested/attempted/observed identities, declared costs and actual program resources without inventing unrecorded equilibration or flushing |

For a newly generated **reference simulation** intended to compare with the
reviewed LCBO objective, the initial utility is the published full-value
continuation profile `moses-botier-1.0.0-binary64/1` with explicit array-derived
metrics. Its finite-resolution count remains named as a reference metric, not
relabeled as observed analyte detection. Record analytical acceptance under the
separate selected contract. An excess-rule comparison is a separately named
campaign, rather than a silent dependency-driven change in utility.

For physical or signal-derived observations, the detection and resolution
producer must establish the required assignment and measurement evidence
before those values can drive a new optimizer. The archived simulation arrays
and their Gaussian reconstruction do not supply that qualification.

## Evidence required before fresh optimization

The observation boundary distinguishes accepted, known non-acceptance and
unknown. Failed measurement or evaluation attempts provide unavailable evidence;
they do not establish analytical non-acceptance. A scalar score cutoff alone is
insufficient. Qualification of the selected retention or signal producer still
requires independent numerical and dimensional checks.

New suggestions must be generated using the selected utility and the same
recorded evidence snapshot. Reclassifying the existing archive is a paired
sensitivity analysis; it cannot estimate which suggestions a changed optimizer
would have made. Compare new campaigns with the same declared cohort,
initialization, high/low fidelity budget accounting and stopping conditions.

Remaining retention-model conventions, physical units, simulation-cap behavior
and instrument or raw-signal work stay open in the
[protocol audit](https://github.com/JamesHWade/measure/issues/108) and
[optimization milestone](https://github.com/JamesHWade/measure-service/issues/5).
