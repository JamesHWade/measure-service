# Compare campaign success without changing the observations

A campaign's success trajectory depends on the observation and acceptance
rules used to score it. Re-evaluating the same archived history measures that
sensitivity; it does not simulate the optimizer decisions that a different
rule would have produced. Keep the original archived-score replay intact.

## Account for one campaign

The pure `measure_methods` API accepts explicit high/low fidelity roles and
three-valued outcomes: true, false or unavailable. It is independent of MCP,
R, storage and the MoSeS data format.

```python
from measure_methods import (
    SuccessObservation,
    aggregate_success_trajectories,
    summarize_campaign_success,
)

campaign = summarize_campaign_success(
    [
        SuccessObservation(1, "high", False, program_volume_ml=1.2),
        SuccessObservation(2, "low", True, program_volume_ml=99),
        SuccessObservation(3, "high", True, program_volume_ml=1.8),
    ],
    budget=3,
    low_fidelity_cost=0.25,
)
print(campaign["first_confirmed_success"])
cohort = aggregate_success_trajectories([campaign], planned_count=2, budget=3)
print(cohort)
```

The first confirmed success occurs at high-fidelity evaluation 2 and overall
sequence 3. Its declared evaluation-cost proxy is 2.25. Its high-fidelity
program volume is 3 mL; the low-fidelity row's volume is excluded. The planned
second campaign has no retained history, so the cohort's success fraction at
budget 2 has lower bound 0.5 and upper bound 1.0.

Budgets count high-fidelity evaluations, including every initial-design run.
A successful low-fidelity prediction cannot confirm a high-fidelity outcome.
The role is explicit: high-fidelity rows in a simulation benchmark remain
simulations, and their program volume is a calculation rather than measured
instrument consumption. The cost proxy is dimensionless, not money or elapsed
time. Program volume excludes any unrecorded equilibration and flushing.

Zero volume and a declared zero simulation cost are valid observations. A
missing cost declaration makes the event cost unknown if low-fidelity rows
precede success; a missing high-fidelity volume makes its cumulative program
volume unknown. Neither is silently replaced with zero.

## Preserve unknown outcomes

Each high-fidelity budget has one of three states:

| State | Meaning |
| --- | --- |
| `confirmed_success` | A retained high-fidelity observation meets the selected acceptance rule within this budget |
| `observed_no_success` | The retained history covers this budget and no qualifying high-fidelity outcome is unavailable or successful |
| `unknown` | An outcome is unavailable, or the retained history does not cover this budget and no success is already confirmed |

An unavailable result before a later successful observation means the first
possible success can precede the first confirmed one. Both indices remain
visible. Success after the declared budget is retained in the event record and
excluded from success within that budget. The API does not infer failed
experiments from an unfinished history.

Cohort aggregation retains the full planned denominator. Omitted campaigns
are unknown at every budget. The lower bound counts confirmed successes; the
upper bound additionally permits success among unknown outcomes. These are
identified bounds, not confidence intervals. A confirmed success remains a
success even when the retained campaign ends before a later budget point.

## Scientific interpretation

Compare paired campaigns under separately named inputs and acceptance rules.
Keep the archived scalar cutoff, recomputed raw-array scalar cutoffs, hard
count/resolution acceptance and runtime-inclusive acceptance distinct. A smooth
hierarchical score above 2 does not logically establish hard analytical
acceptance. The [hierarchy profiles](hierarchy-scores.md) retain the selected
version and input policy.

A difference in these curves cannot establish that one optimizer is better:
all curves reuse the same suggestions and observations. Fresh optimization
must make suggestions using its explicitly selected observation, utility and
acceptance contract, and requires its own qualified retention or signal model.


## Compare the qualified MoSeS histories

After the [immutable replay and scalar-score qualification](moses-reproduction.md),
run the comparison into a separate output directory:

```sh
python -m measure_service.benchmarks compare-moses-trajectories \
  --replay-dir /tmp/moses-replay \
  --qualification-dir /tmp/moses-score-qualification \
  --output-dir /tmp/moses-trajectory-sensitivity
```

The command reads only the two checksum-verified evidence bundles. It does not
open the original ZIP or rerun scalarization. Every original observation is
joined by campaign identity, sequence, member digest and archived score to all
four qualified score candidates. A missing, duplicate, reordered or divergent
row stops publication. The archived-score series must reproduce every cell of
the preserved success-trajectories table. Both input bundles and implementation
hashes are rechecked before atomic publication.

Seven explicit rules are compared: archived scores; published and excess-rule
array scores; published and excess-rule serialized-summary scores; hard array
count/resolution acceptance; and hard array count/resolution/runtime acceptance.
For targeted experiments only, the two summary-or-target rules use the qualified
selected-target array entry. They do not fabricate global summaries or fill
missing summaries for other objectives. The target gap remains recorded.

| Output | Contents |
| --- | --- |
| `report.json`, `report.md` | Definitions, source and software digests, descriptive final-state counts and paired state matrices |
| `campaigns.csv` | First possible/confirmed success, event costs and volumes, and budget state for each retained campaign and rule |
| `success-trajectories.csv` | All configurations, rules and HF budgets, retaining the full planned denominator and unknown bounds |
| `configuration-summary.csv` | Final states and largest curve differences for every configuration and rule |
| `paired-comparisons.csv` | Campaign-level state changes and first-confirmation differences from archived scores |
| `complete.json` | Sizes and SHA256 digests of all numerical evidence files |

A known success gain requires candidate-confirmed success versus an observed
no-success archived state. The reverse is a known loss. A change involving an
unknown state remains a separate indeterminate comparison. Planned campaigns
with no history contribute unknown/unknown to each paired matrix, even though
no observation row can be emitted for them.

Earlier/later first-confirmation counts describe only pairs for which both
rules confirm success within the declared budget. They are conditional
comparisons, not evidence that a rule improves optimization. Global final-state
counts combine different configuration-specific budgets for descriptive
accounting; use the configuration tables and curves for individual comparisons.

The optional source-checkout plotting script creates both a complete simulated
configuration atlas and a compact summary:

```sh
Rscript scripts/plot_moses_sensitivity.R /tmp/moses-trajectory-sensitivity
```

Plots are additional presentation files; they do not change the checksummed
numerical results. The shaded curves show unknown-outcome bounds, not confidence
intervals. Read the [metric audit](moses-metric-reconciliation.md) and selected
[subsequent observation contract](moses-observation-contract.md) before using
these results to design fresh optimization.
