# Evaluate hierarchical objective scores

`measure_methods` evaluates an ordered set of analytical objectives without R,
MCP, NumPy, Torch or an optimizer. Each criterion declares its name, direction,
clipping bounds and satisfaction threshold. Values must match criterion names
exactly. The result retains each normalized value, gate and score contribution.

```python
from measure_methods import Criterion, hierarchical_score

criteria = [
    Criterion("recovery", "maximize", 0, 100, 95),
    Criterion("run_time_min", "minimize", 1, 20, 5),
]
result = hierarchical_score(
    {"recovery": 98, "run_time_min": 4},
    criteria,
    final_objective="recovery",
    final_rule="excess",
)
print(result["score"])
print(result["criteria"])
```

Bounds use the units of each supplied value. Clipping is inclusive; the
threshold must lie within the bounds and strictly improve on the worst bound.
An objective's normalized value is `(clipped_value - worst) / (threshold - worst)`.
Its threshold is therefore 1, independent of direction. Order expresses
priority, so rearranging criteria changes the meaning of a score.

For normalized objective `g`, the gate is `logistic(k * (g - 1))`. Its tier
contribution is the exponential-weighted arithmetic mean of `g` and 1, weighted
by all preceding gates. This smooth minimum is not a log-sum-exp minimum.
After all tiers, the selected final objective adds either its normalized value
(`final_rule="value"`) or its excess above 1 (`"excess"`), multiplied by every
gate. The default smoothing is 100 and the default final objective is the first
criterion. The default rule is `"excess"`; callers comparing reference software
should select the rule explicitly. Stable exponential calculations avoid
unnecessary overflow. Nonfinite inputs and unrepresentable results fail rather
than producing nonfinite JSON.

This is a scalar evaluation interface. It does not supply gradients,
acquisition functions, a surrogate model or optimizer suggestions. Its smooth
gates also mean a score cutoff is not equivalent to a conjunction of hard
analytical thresholds near a boundary. Assess those thresholds separately.

## MoSeS observation profiles

The reference-specific wrapper takes per-analyte resolutions and a run time in
minutes. A profile is required; there is no automatic software-version guess.

```python
from measure_methods.moses_objective import moses_observation_score

result = moses_observation_score(
    profile="moses-botier-1.0.0-binary64/1",
    num_compounds=3,
    peak_resolutions=[1.7, 2.1, 1.8],
    run_time_min=4,
    max_run_time_min=20,
)
print(result["score"])
print(result["analytical_thresholds_satisfied"])
```

The profiles identify arithmetic and the source rule:

| Profile | Final term |
| --- | --- |
| `moses-botier-1.0.0-binary64/1` | Full normalized final value, matching published BoTier 1.0.0 |
| `moses-botier-1.1.0-binary64/1` | Excess above threshold, matching the BoTier v1.1.0 source tag |

These implement the observed-experiment scorer in the reviewed LCBO source,
which always uses cumulative objectives even with a compoundwise surrogate.
The three supported modes are `NumPeaks-CritRes`, `NumPeaks-NumResPeaks` and
`SinglePeak-Resolution`. A target index is zero-based and required only for the
last mode. Untargeted count bounds are `[0, 10]`, including mixtures of a
different size; targeted count bounds are `[0, 1]`. Resolution bounds are
`[0, 5]`; run-time bounds are `[1, max_run_time_min]`. The default thresholds
are 0.5 missing analytes, resolution 1.5 and 5 minutes. Supplied configuration
values are validated, not silently replaced with defaults.

Detection counts finite resolution entries; critical resolution is their
minimum, or the source's zero sentinel for a present array with no finite
entries. An absent or empty array has an unavailable score. Null, NaN,
infinite and finite entries retain separate status counts. The result separates
`score_at_least_two`, `analytical_thresholds_satisfied` and
`all_objective_thresholds_satisfied` (including run time). None is substituted
for another.

`moses_summary_score` evaluates explicitly supplied count/resolution summaries
under the same profiles. It labels its input policy
`serialized_summary_hypothesis`: serialized convenience fields can disagree
with the per-analyte arrays. Score agreement from those fields does not prove
that they were the inputs used by the historical software. Targeted scoring
requires the selected array entry instead. Original archived scores remain
unaltered by either function.

## Reference evidence

The normalization and hierarchy equations were independently implemented from
[BoTier v1.0.0](https://github.com/fsk-lab/botier/tree/v1.0.0) and
[v1.1.0](https://github.com/fsk-lab/botier/tree/v1.1.0).
[PyPI's published 1.0.0 wheel](https://pypi.org/project/botier/1.0.0/#files) has
SHA256 `9177990c1f650373925b19dafa366506f145bb415629bcf6860ed0ae839b7fb6`.
The profile adapter follows
[LCBO's observed-experiment scorer](https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/lcbo/data/config.py)
and [experiment metrics](https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/lcbo/hplc_utils/hplc_experiment.py).
Those revisions identify reviewed scientific evidence; the runtime package has
no BoTier or Torch dependency and imposes no installation revision constraint.
