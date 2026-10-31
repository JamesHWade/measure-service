# Matched synthetic campaign declarations

Register `synthetic-isocratic-paired/1` before running either policy. The
declaration fixes the search domain, synthetic task identities, seeds,
initialization, evaluation budget, cost and stopping rule. A later change
requires another immutable protocol.

The separate [ablation profile](ablation-protocol.md) declares optimizer target
and inferred-noise prior as controlled factors. It preserves this first
profile's stored shapes and candidate sequences.

The first profile uses an isocratic mixture of solvent A and strong solvent B
at constant flow. Its two continuous coordinates are
`strong_solvent_fraction` (unitless B fraction; A is exactly `1 - B`) and
`acquisition_duration_min` (minutes). There is no method overhead or instrument
execution. Both coordinate intervals are closed; their endpoints must differ.
The realized initial design must occupy all declared strata without repeated
points at binary64 precision for every predeclared seed. Intervals too narrow
to support that design are rejected. The full budget's declared cost must also
remain finite. Every candidate is checked using the existing executable-method evaluator.
All four domain corners must satisfy the declared constraints before a
protocol can be registered. For this isocratic template, acquisition time,
occupancy, fixed flow and solvent use reach their extrema at those corners;
fraction slopes are zero. This does not establish feasibility for a general
gradient or a physical pressure model.

## Bind the task before suggesting a method

Each task names an owned [Gaussian producer](gaussian-observation-producer.md)
and an owned [observation contract](observation-evidence.md). Registration
resolves and retains the exact producer, synthetic retention-model and contract
identities. Their analyte cohorts and resolution definitions must match. At
least two analytes are required. The model must declare solvent B as strong;
the contract's maximum acquisition duration must equal the domain maximum.
Caller-provided physical models cannot enter this synthetic profile.

The installed MCP example `python examples/gaussian_observations.py` registers
the model, producer, contract and protocol, retrieves the protocol, and exercises
immutable generated evidence. It needs no R installation or BO runtime and
does not execute a campaign.

After registering those records, call `measurement_register_campaign_protocol`:

```json
{
  "title": "Predeclared synthetic comparison",
  "spec": {
    "domain": {
      "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
      "acquisition_duration_min": {"lower": 2.0, "upper": 10.0}
    },
    "flow_ml_min": 0.5,
    "constraints": {"max_acquisition_duration_min": 10.0},
    "tasks": [{
      "task_id": "constructed-mixture",
      "producer_id": "REPLACE_WITH_OWNED_PRODUCER_ID",
      "observation_contract_id": "REPLACE_WITH_OWNED_CONTRACT_ID"
    }],
    "seeds": [17, 29, 43],
    "evaluation_budget": 10,
    "initial_design_size": 3,
    "evaluation_cost": 1.0,
    "evaluation_cost_unit": "synthetic_evaluation"
  },
  "idempotency_key": "matched-protocol"
}
```

The response retains normalized declarations, resolved task bindings, installed
implementation evidence and input/output digests. The output digest covers the
bound task identities and provenance as well as the specification. Retrieval
verifies those digests. Reusing the same owner-scoped request key returns the
original response; changing its request fails. Another owner cannot read or
bind the protocol or its inputs.

## Budget and stochastic design

The policies are `random` and `bo_mcp_gp_logei`. Each policy and seed owns its
own simulation records and counts every initial evaluation against its total
budget. The first three or more candidate points form a shared stratified
Latin-hypercube design. Each coordinate occupies each of the equally sized
strata once; coordinate permutations and within-stratum draws use separate
standard-library random streams. This is the declared baseline design, not the
author software's Sobol or polytope initialization.

The reusable numerical functions are `measure_methods.design.latin_hypercube_design`
and `uniform_random_point`. They accept explicit bound pairs and one integer
stream seed per dimension, return coordinate tuples, and import no service,
storage, schemas or identity code. The service prepares identity-derived seeds,
normalizes the protocol and checks executable-method feasibility. Protocol
provenance records the numerical design module's source hash separately.

Seeds are predeclared integers from 0 through 2^31 - 1. The full protocol identity
includes owned record IDs. Candidate random streams instead use a design
digest that replaces opaque producer and contract IDs with logical task IDs.
Registering identical scientific declarations in another database therefore
preserves the initial and random candidate sequence. Every later random draw
also includes its evaluation ordinal and coordinate, so resuming does not
depend on mutable RNG state. Record the Python implementation/version when
comparing bitwise output across environments.

All campaigns run their complete predeclared budget, including campaigns that
never reach satisfactory separation. No initialization, rejected proposal or
low-fidelity expenditure is hidden in a physical-experiment count. This first
profile has only simulator-latent observations at fidelity 1.0. Declared cost
uses its own unit; summed acquisition duration is simulated method time,
separate from optimizer wall time and physical instrument occupancy.

## Scientific interpretation

The utility is the existing versioned
`moses-botier-1.0.0-binary64/1` / `finite-resolution-reference/1` calculation.
Its finite-resolution count remains distinct from analytical detection. Strict
analytical acceptance, unavailable singleton resolution, acquisition feasibility
and runtime preference remain separate fields in the observation contract.

The packaged
[synthetic declaration](../src/measure_service/benchmarks/reference_data/synthetic-isocratic-paired-v1.json)
predeclares two constructed, unfiltered three-analyte tasks, three seeds,
ten evaluations per campaign and three initial evaluations. It includes all
tasks and seeds regardless of their outcomes. The original declaration's SHA256
is `0728c6b2e03132d2889fef030c57a5331cae24067ca3fc5e6c40256f1238b38e`.

These are noiseless ideal retention laws and supplied synthetic widths. They
do not reproduce the manuscript's filtered mixtures, three-segment domain,
50-task/100-evaluation protocol, physical model fits or measured chromatograms.
The optional optimizer must be exercised and its actual model/acquisition
recorded before making a fresh optimization claim. A registered protocol alone
does not establish that an optimizer ran.
