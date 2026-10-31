# Run the controlled target and noise-prior comparison

The fixed `synthetic-isocratic-ablation-study/1` comparison runs one random
control and four GP arms. The GP arms cross historical utility versus
confirmed-acceptance reward with reference-default versus low-noise Gamma
priors. The [protocol guide](ablation-protocol.md) defines those treatments and
their limits. The original [paired reference](synthetic-reference-results.md)
remains a separate completed experiment.

The [recorded five-arm result](synthetic-ablation-results.md) retains 543
consumed evaluations and 57 unknown future slots after 19 duplicate-candidate
stops. Inspect the reported execution status as well as the completion seal;
sealing an evidence bundle does not mean every campaign finished its budget.

Install the service and its [optional backend runtime](campaign-runtime.md#optional-real-bo-mcp-runtime),
then run from outside the checkout:

```sh
python -m measure_service.benchmarks run-synthetic-ablations \
  --data-dir /tmp/measure-ablation-store \
  --output-dir /tmp/measure-ablation-report
```

The command verifies the packaged declaration's SHA256
`0c607fa590c3c5682018765f25b37c4c9820a2ef36fc3d57bb205d0f1aa31094`
before setting up its dedicated store. It offers no task, seed, objective,
prior or budget overrides. A changed scientific experiment needs a separate
declaration and identity. After installation the calculation needs neither R
nor a network connection, and executes no instrument commands.

## Keep all five arms and both cohorts

The two original constructed tasks are regression cases. Two new tasks were
generated and frozen without optimizer or separation screening; neither was
rejected or redrawn. Each task has three analytes with explicit ideal linear
retention parameters and synthetic FWHM assumptions. These four tasks each use
seeds 17, 29 and 43, giving twelve task/seed campaigns per arm.

Every campaign owns ten evaluation slots, including three identical initial
coordinates shared across arms. Initialization counts separately against each
arm's budget. The complete plan has 60 campaigns and 600 analytical evaluation
slots. All campaigns retain their full budgets after first success. Missing
and failed executions remain represented rather than receiving invented zero
rewards or disappearing from a denominator.

The report separates the regression and held-out cohorts, each with six
task/seed blocks per arm. It also labels a combined descriptive view with
twelve blocks per arm. The two held-out tasks provide a prospective check;
they do not support a general population ranking of optimizers.

## Qualify the runtime before new proposals

The runner first verifies the shared initial design and qualifies the actual
backend on all sixteen declared component cases. Those fabricated histories
are not observations from the four analytical tasks. Their requests,
candidates, warnings, source evidence and recorded time are reported separately
from the 600 evaluation slots.

A failed preflight blocks new proposals. An initial failure leaves all study
outcomes unknown. On an interrupted study, previously acquired and consumed
outcomes remain available while future slots remain unknown. No random
fallback, target jitter, prior retuning or replacement analytical result is
introduced to complete a failed route.

Actual study proposals must match the successfully qualified backend source
and settings. This protects experiment consistency without restricting which
compatible dependency revisions can be installed. The report identifies the
software and scientific inputs actually used.

## Inspect evidence and controlled contrasts

Each evaluation retains the exact proposed method, actual simulator attempt,
retention prediction, generated Gaussian results, analytical assessment,
snapshot and immutable campaign consumption. Historical utility, original
three-valued analytical acceptance and the selected optimization reward remain
separate. A known zero confirmation reward can accompany an unknown analytical
assessment; an absent execution has no reward.

Trajectory tables retain strict analytical success and historical utility >=2
for every arm. An unknown-outcome upper bound is not a confidence interval or
an additional confirmed success. Paired tables compare targets within each
prior and priors within each target. Comparisons against the shared random
control reuse that control's original task/seed outcomes; they do not create
extra independent replicates.

Declared cost, acquisition duration and solvent volume include completed
simulator attempts even if consumption failed. Consumed totals remain separate.
Optimizer and component-preflight timings are recorded computational quantities;
random timing is unavailable when it was not measured. Declared synthetic
resources do not establish physical instrument time, measured solvent use or
monetary cost.

Readable and machine-readable reports, full campaign/evaluation records,
cohort trajectories, paired contrasts, preflight records and an owned SQLite
backup form the evidence bundle. `complete.json` seals the core files by
SHA256 and byte count. File identity supplements independent numerical and
evidence review; it does not establish scientific correctness by itself.

## Resume from the original owned store

Keep any existing output directory and choose a new one when resuming:

```sh
python -m measure_service.benchmarks run-synthetic-ablations \
  --data-dir /tmp/measure-ablation-store \
  --output-dir /tmp/measure-ablation-report-resumed
```

The persistent store marker binds this runner and the exact declaration.
Unmarked, mismatched and mixed stores are rejected before mutation or export.
The runner verifies ownership again before creating its backup, and never
exports the normal user service database.

Original operation keys recover the same proposal, attempt and consumption
receipts. A committed outcome whose acknowledgement was lost is reconciled
without repeating its analytical attempt. A completed study reuses its saved
qualified preflight and stored evidence; it can produce a fresh report without
an optimizer runtime or new proposals.

This controlled study tests a scalar target and an inferred-noise regularizer.
Compoundwise surrogates, Matérn selection, manuscript mixture filtering and
three-segment programs, physical fits, processed signals, instruments and
multi-fidelity execution remain separate work.
