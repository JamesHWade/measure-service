# Change the optimizer target and inferred-noise prior independently

Register `synthetic-isocratic-ablation/1` to declare two controlled factors on
the existing isocratic synthetic campaign. It uses the same owned task,
producer, observation contract and immutable outcome consumption as the
[first campaign profile](synthetic-campaigns.md). The original
`synthetic-isocratic-paired/1` declarations retain their serialization,
identities and candidate sequences.

The new profile requires both fields:

| Field | Values | Meaning |
| --- | --- | --- |
| `optimizer_target` | `historical_utility`, `confirmed_acceptance_reward` | Scalar training value derived from the exact consumed observation |
| `surrogate_noise_prior` | `default`, `low_noise` | Predeclared Gamma prior on inferred Gaussian observation-noise variance |

The full protocol digest includes both factors. Its coordinate-design digest
excludes the two factors and opaque producer/contract IDs. Otherwise identical
declarations therefore share initial and random coordinates across treatments,
while retaining distinct immutable protocol identities. Every arm owns and
counts its own initial evaluations. Changing the task cohort, bounds, budget or
another scientific setting changes the design identity.

## Preserve the analytical outcome beside the optimization reward

`historical_utility` trains on the existing
`moses-botier-1.0.0-binary64/1` finite-resolution score. Its count, resolution
and runtime preferences remain distinct from hard analytical acceptance.

`confirmed_acceptance_reward` uses
`confirmed-acceptance-lower-bound/1`:

| Immutable analytical assessment | Confirmation reward | Analytical status retained |
| --- | ---: | --- |
| `accepted` | 1 | `accepted` |
| `not_accepted` | 0 | `not_accepted` |
| `unknown` | 0 | `unknown` |
| No completed, qualified assessment | Unavailable | No invented assessment |

Zero reward means that this observation has no confirmed acceptance reward.
An unknown chemical outcome stays unknown. Censoring and acquisition duration
can affect confirmation by limiting available evidence. Missing executions,
numerical failures and absent assessments never become zero-valued training
observations.

The mapping deliberately removes resolution margin, runtime preference and
within-class utility information. A scalar Gaussian regression model on this
binary reward is a search heuristic. Its posterior is not a calibrated
acceptance probability; it is neither clipped to a probability nor used to
rewrite the analytical assessment. Campaigns continue through their declared
budget after their first success.

New-profile consumptions retain the selected `optimizer_target` beside the
original utility, raw metrics and three-valued assessment. Ordered training
replay uses the selected finite value from the exact consumed snapshots.
Retries return the original consumption; later evidence corrections do not
rewrite it. The original profile continues to train on its original utility.

The installed MCP example exercises this distinction with a deliberately
right-censored synthetic cohort:

```sh
python examples/confirmed_campaign.py
```

Configure the [optional BO-MCP runtime](campaign-runtime.md#optional-real-bo-mcp-runtime)
first. The example counts three initial evaluations, restarts the service,
obtains a real low-noise-prior GP suggestion from the three zero confirmation
rewards, and verifies immutable consumption after another restart. Its
analytical outcomes remain unknown. This is a component/recovery example with
its own temporary store, separate from the declared study's evaluation budget.

## Change regularization through the public backend

Both settings use the public BO-MCP `OptimizationSpec.noise_prior_params`
route with a scalar `SingleTaskGP`, ARD RBF covariance, Gaussian likelihood,
outcome standardization and qLogExpectedImprovement. The adapter supplies the
declared pair explicitly for this profile:

| Setting | Gamma concentration | Gamma rate | Nominal mean | Nominal mode |
| --- | ---: | ---: | ---: | ---: |
| `default` | 1.1 | 0.05 | 22 | 2 |
| `low_noise` | 1.1 | 100 | 0.011 | 0.001 |

These are standardized target-variance units. `default` names the reference
default observed when this comparison was declared; explicitly supplying it
keeps the treatment defined if a later backend changes its own defaults.
The reviewed likelihood has a nominal variance floor of 0.0001. Effective
floating-point tensors can differ slightly from these decimal declarations.
Constant target columns use the standardization transform's constant-column
behavior, rather than acquiring empirical unit variance.

Both models infer noise. The 2000-fold change in prior mean and mode is a
regularization stress test, not fixed observation noise or measured physical
uncertainty. A prior override does not initialize the model at its prior mode.
No per-observation uncertainty, likelihood-floor override, target jitter,
class balancing or hidden utility tie-breaker is added.

The backend records the requested target, configured prior, actual software
versions, source identities, acquisition settings, warnings and proposal wall
time. Configured prior parameters are not fitted noise-variance estimates.
Tests inspect the actual public model-construction route; a separate diagnostic
refit does not establish the exact surrogate used for a suggestion.

Source observations and capability checks identify a working runtime. They do
not impose dependency revision restrictions. Unsupported or degraded routes
fail explicitly instead of substituting another algorithm. The current profile
has no compoundwise surrogate, Matérn selector, classifier or multi-fidelity
policy.

The inspected public [specification](https://github.com/AccelerationConsortium/bo-mcp/blob/56d590b91ac120d0f22a3e41e770843d271808cd/packages/bo-engine/src/bo_engine/types.py)
and [model factory](https://github.com/AccelerationConsortium/bo-mcp/blob/56d590b91ac120d0f22a3e41e770843d271808cd/packages/bo-engine/src/bo_engine/models.py)
identify the API and noise-prior route used to define this profile. These links
are source evidence for the design, rather than installation constraints.

## Freeze the experiment before execution

The packaged
[follow-up declaration](../src/measure_service/benchmarks/reference_data/synthetic-isocratic-ablation-v1.json)
has SHA256
`0c607fa590c3c5682018765f25b37c4c9820a2ef36fc3d57bb205d0f1aa31094`.
It specifies a shared random control and four GP arms crossing the two factors.
Four tasks, three seeds and ten evaluations per arm give 60 campaigns and 600
planned evaluations, including three common initial coordinates per seed.

The two original tasks remain regression cases. Two separately generated
held-out tasks were frozen without any optimizer or separation screening;
both draws are retained. Their seed, distributions, draw order and exact
coefficients are recorded in the declaration. This is a prospective follow-up
motivated by the first comparison. It does not replace that comparison.

Before study execution, the declared preflight requires the actual backend to
generate finite feasible model-guided candidates for zero, one and mixed binary
histories at lengths three and nine under both priors, plus nonconstant utility
histories under both priors. All 16 component cases retain their inputs,
warnings, provenance and time separately from analytical evaluations. A failed
preflight blocks study execution; it does not trigger random fallback or
outcome-dependent parameter changes.

Report regression and held-out cohorts separately. Compare targets within each
prior and priors within each target, retaining interactions and every planned
task/seed denominator. The shared random control is one comparator reused in
paired tables, not extra independent replication. These constructed tasks
support descriptive comparisons. Manuscript-filtered mixtures, physical
retention fits, processed signals, instruments and multi-fidelity execution
retain their separate qualification requirements.
