# Suggest and consume synthetic campaign outcomes

Create one campaign for a task, seed and policy from a
[frozen protocol](synthetic-campaigns.md). The campaign starts at revision 1.
Each successful suggestion and consumption increments that revision once.
Supply `expected_revision` and a distinct owner-scoped retry key for each new
operation. A transport retry with the original request/key returns its original
response, even after later transitions or corrections.

```text
ready_to_suggest → proposal_pending → ready_to_suggest
                       ↓ exact one-observation snapshot
                consumed_evaluations += 1
                       ↓ final declared evaluation
                  budget_complete
```

Only one proposal can be pending. Concurrent requests serialize in SQLite;
revision checks and unique ledger constraints prevent duplicate proposal or
physical-attempt consumption. A proposal persists its candidate, feasible
executable method, training-input digest, exact consumed snapshot references
and actual policy/backend evidence. Use its retained method ID when creating
the experiment and completed simulator attempt. Rebuilding a numerically
similar method under another record ID does not satisfy this first profile.

## Complete the evidence path

For each proposal, create an experiment prebound to its task's Gaussian producer
and observation contract. Record a completed simulator attempt at fidelity 1.0
using the exact proposed method. Retain the prediction, generated results,
analytical assessment and a snapshot selecting exactly that observation.
Then call `measurement_consume_campaign_snapshot`.

Consumption verifies the complete generated cohort and both result references
per analyte against the immutable producer run. An empty or partial assessment
cannot borrow other latent results merely because they exist for the attempt.
The exact producer, retention model, method, contract, attempt revision and
snapshot identities must agree. Caller-supplied numbers cannot replace this
evidence. Censored retention and unavailable resolution remain explicit;
they are not measurement successes or invented event times.

The service recomputes the predeclared finite-resolution utility from those
verified raw arrays and stores it separately from strict analytical acceptance
and runtime preference. The source utility may score a complete array whose
entries are unavailable under its existing zero-count convention. That is not
an analytical detection claim. The consumed record also retains raw statuses,
times, resolutions, declared cost and simulated acquisition time.

A later result correction or attempt revision does not rewrite an earlier
frozen observation, snapshot, consumption or optimizer training set. This
profile deliberately consumes the selected historical evidence. To use a
different experimental outcome, declare a new campaign evaluation rather than
silently replacing a consumed row. One attempt can count only once in one
campaign. Separate policy arms own and count their own simulation evaluations.

`measurement_get_campaign` returns compact history and pending references.
`measurement_get_campaign_proposal`, `measurement_get_campaign_consumption`,
and existing observation/result retrieval tools expose full immutable records
without duplicating them in every campaign response. Records retain the
64,000-byte envelope; this applies at the maximum supported evaluation budget
as well as in the small example.

## Optional real BO-MCP runtime

The `random` arm needs no optimizer dependency. After its common initialization,
`bo_mcp_gp_logei` invokes the public
[`bo-engine` BoTorchBackend](https://github.com/AccelerationConsortium/bo-mcp/tree/main/packages/bo-engine)
through a separate Python interpreter. The current public package requires
Python 3.13 or later; the analysis service retains Python 3.10 or later support.
Heavy optimizer dependencies are optional.

Run `python examples/synthetic_campaign.py` for a complete four-evaluation
random campaign, including server restart and original receipt verification.
With the optional runtime configured, use
`python examples/synthetic_campaign.py --policy bo_mcp_gp_logei` to fit a real
GP after the three initial observations and consume its generated outcome.
The separate CI backend job repeats that actual MCP workflow in fresh stores;
the ordinary Python jobs require no optimizer installation.

From the service checkout, install the same service build into the separate
runtime and install the public backend:

```sh
python3.13 -m venv /tmp/measure-bo-runtime
/tmp/measure-bo-runtime/bin/python -m pip install .
git clone https://github.com/AccelerationConsortium/bo-mcp.git ../bo-mcp
/tmp/measure-bo-runtime/bin/python -m pip install ../bo-mcp/packages/bo-engine
export MEASURE_BO_PYTHON=/tmp/measure-bo-runtime/bin/python
```

`MEASURE_BO_PYTHON` is administrator configuration; a tool request cannot choose
an executable or arbitrary acquisition settings. The child uses its own
installed packages, ignores inherited `PYTHONPATH`, and checks adapter/protocol
source identities against the caller. Reinstall both service environments when
updating this code. Backend dependencies are not restricted to a Git revision;
their observed versions and imported source-tree digest are recorded as
execution evidence. A local source install without VCS metadata reports a null
revision, rather than inferring one from an unrelated checkout.

Every BO call supplies the full ordered consumed training history. The backend
fits its `SingleTaskGP` and explicitly selects expected improvement, mapped by
the reviewed public implementation to `qLogExpectedImprovement`. The current
continuous model uses its RBF default, Normalize/Standardize transforms and
inferred Gaussian noise. It is not the manuscript's Matern-5/2 or compoundwise
surrogate. Three or more initial rows meet its two-dimensional minimum-data
requirement. A returned warmup or fallback proposal is rejected.

The response records the actual generation method, model/acquisition, installed
versions, source hashes, seed, iteration, acquisition-optimizer settings and
warnings. No hidden optimizer state is needed on restart: the ordered evidence
history is replayed. Identical input and seed were tested to repeat in one
recorded runtime; bitwise output across different Torch versions or devices is
not promised. Backend startup, fitting, timeout, infeasibility or repeated
candidate failure leaves no proposal, receipt or budget expenditure. The
service does not silently substitute random sampling. The child has a
60-second call limit; local registry mutations serialize while a suggestion is
computed.

## Scope

This is a sequential, single-fidelity synthetic campaign loop. It implements
neither instrument actuation nor generic mixed/categorical, asynchronous,
multiobjective or cost-aware multi-fidelity BO. Authentication/deployment,
physical model fitting, signal processing and manuscript reproduction retain
their separate acceptance requirements.
