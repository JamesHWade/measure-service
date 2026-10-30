# Produce synthetic observations with declared Gaussian widths

Declare a synthetic retention model and per-analyte full widths at half maximum
(FWHM), bind that producer and an analytical observation contract to an
experiment, then record generated results from an exact retained prediction.
The `ideal-gaussian-centroid/1` profile supplies an inspectable latent-data
producer for reference experiments. It does not process chromatograms, fit
physical peak widths or execute an optimizer.

Run the complete installed MCP example:

```sh
python examples/gaussian_observations.py
```

The example records three synthetic analytes under a seven-minute method,
assesses the generated retention/resolution results, and freezes an observation
snapshot. A shorter acquisition preserves censored times and unavailable
resolution as unknown evidence. Both predictions use the same predeclared model
and widths. The snapshot and original retry remain unchanged.

## Declare the producer before attempts

`measurement_register_gaussian_producer` takes a title, an owned
`retention_model_id`, a specification and an idempotency key. The retention
model must declare `parameter_origin: "synthetic"`. This restriction describes
the supported reference workflow; a caller's synthetic label does not establish
physical provenance.

The specification declares a complete unique analyte cohort and positive finite
`fwhm_min` values, with `width_origin: "synthetic_assumption"` and a nonblank
`width_reference`. Widths remain assumptions in minutes. The producer does not
infer them from retention time, the source archive's H field, measured plate
height or parameter fitting. Each immutable producer binds the exact model
input digest and width declaration. A changed assumption needs a new producer.

Create an experiment with both `observation_contract_id` and
`observation_producer_id`. Its required cohort must match the producer's full
model cohort, and `resolution_definition` must be
`gaussian-baseline-4sigma-finite-centroids/1`. The experiment retains the producer
identity before attempts; it cannot acquire a different producer retroactively.

## Record an exact prediction as latent evidence

Use the existing ideal retention interface to retain a prediction for an owned
method. Then call `measurement_record_gaussian_observation` with producer ID,
prediction ID, attempt ID, expected current revision and retry key. The attempt
must be completed, declare a simulator evaluator and a finite fidelity
coordinate, and name the exact actual method used by the prediction. The model,
method, canonical program, producer and prediction digests must agree.

The service delegates all calculations to the pure `measure_methods` backend.
A single transaction writes the immutable producer run, generated retention and
resolution results, and retry receipt. Failures leave none of those records.
The response supplies exact result IDs and an evidence specification for the
existing `measurement_evaluate_observation` tool. Analytical assessment and
explicit snapshot membership remain separate operations.

Generated results record `stored_ideal_gaussian_producer` origin and exact run,
producer and prediction identities. A producer-bound experiment rejects
caller-entered replacements and results from another run. Caller identity
assignments can remain ambiguous or unassigned; a confirmed latent assignment
refers to the declared model analyte, not a structural chemical identification.

## Interpret widths, adjacency and censoring

The calculation reuses the qualified source-convention Gaussian resolution
helper. Its explicit conversion is `sigma = FWHM / 2.3548`, and adjacent-pair
resolution is `delta_time / (2 * (sigma_left + sigma_right))`. The constant
`2.3548` approximates the exact Gaussian FWHM factor; it is retained in the
profile and independent reference rather than silently replaced.

Finite predicted centroids within the inclusive acquisition window are sorted
by time. Each analyte receives the minimum resolution of its incident adjacent
pairs. With unequal widths, the limiting pair need not be the closest in time.
Equal centroids produce observed resolution zero. A singleton finite subset has
unsupported resolution, represented as null rather than infinity or zero.

A model event beyond acquisition remains right-censored, with a null event time
and the acquisition bound. Its resolution remains unavailable. Generic result
records use a missing-resolution outcome while the producer artifact preserves
the specific unsupported or censored reason. Every required analyte remains in
the analytical assessment, so censoring cannot turn a partial separation into
all-cohort acceptance.

Adjacency is restricted to finite predicted centroids. Potential after-window
neighbors, Gaussian tails, amplitudes, noise, detector thresholds, peak picking,
deconvolution and correspondence errors are outside this profile. Numerical
agreement for this declared latent calculation does not establish physical
separation or qualify a processed-signal producer.

## Retries, corrections and limits

The original retry returns its stored response after later attempt revisions or
result corrections. Retrieval preserves the producer artifact and generated
result identities. A different retry key cannot silently regenerate or
supersede current results for the same attempt. Explicit corrections in the
generic result registry preserve history; they do not become qualified
producer evidence merely because their numbers match a prediction.

Records and complete response envelopes are bounded to 64,000 UTF-8 bytes.
Ownership is checked on every link. The producer supports the bounded
32-analyte retention cohort and finite positive widths; numerical range failures
raise without fabricated results. No source file, executable payload or URL is
accepted as a producer.

The [observation contract](observation-evidence.md),
[ideal retention inputs](retention-predictions.md) and
[scientific unit audit](retention-model-qualification.md) define the surrounding
boundaries. Fresh optimization still needs a declared matched campaign protocol,
initialization/budget accounting and a qualified optimizer consumption path.

Producer-bound analytical observations retain compact result references after
validation: each contains the exact `result_id`, full outcome `input_sha256`,
status, raw unit/value and normalized value, with `outcome_included: false`.
`measurement_get_result` retrieves the full immutable outcome; its digest must
match that reference. Shared producer/run/prediction evidence is retained once
in the observation, and full source/implementation evidence is retained on the
immutable producer run. This avoids copying the same provenance for every
metric while preserving a traceable bounded 32-analyte assessment.
