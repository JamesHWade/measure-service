# Predict ideal retention from explicit model inputs

Register a retention model, select an owned executable method, and retain an
immutable prediction. The `ideal-lc-retention-zeta/1` profile implements three
solvent-strength laws and a continuous constant-flow retention reference. It
requires no R, Torch, SciPy or optimizer at runtime.

Run the installed MCP example:

```sh
python examples/retention.py
```

The example declares a 0.1 m column with effective cross-section
`2.5e-6 m²`, constant flow `0.5 mL/min` and post-column volume `0.1 mL`.
The mobile-phase dead time is 0.5 min and post-column transit is 0.2 min.
An isocratic synthetic analyte with `k=3` elutes at 2.2 min; one with `k=1000`
is right-censored at the ten-minute acquisition limit. Neither prediction
contains a peak width, resolution, scalar utility or physical validation claim.

## Declare the model, geometry and clock

`measurement_register_retention_model` takes a title, specification and
idempotency key. The specification requires:

- `gradient_coordinate: "column_inlet_at_analyte_entry"`. Method times describe
  the inlet profile with time zero at analyte entry. Pump-clock commands need a
  separately established correction; this service does not infer one.
- One to 32 unique analyte IDs, each with a model (`linear`, `quadratic` or
  `neue-kuss`), `ln_k0`, `S1` and, for the latter two models, `S2`. `ln_k0` is a
  natural logarithm; coefficients and solvent fractions are dimensionless.
  `S1` and `S2` are nonnegative. A linear model omits `S2`.
- Positive `column_length_m` and `effective_area_m2`, and nonnegative
  `post_column_volume_ml`. Effective area defines the mobile-phase column volume
  with length; a separate porosity is not inferred. Post-column volume is the
  outlet-to-detector transit volume, not a gradient delay or method phase.
- `strong_solvent`, identifying which solvent fraction enters the law, plus
  `parameter_origin` (`synthetic` or `caller_supplied`) and a parameter reference.
  The reference is retained text; it is not fetched or executed.

Numbers must be finite numeric inputs; percentages, decimal strings, booleans,
negative dimensions and unsupported fields are rejected. The source fitting
ranges are advisory warnings, with no clipping or silent replacement. A
coefficient outside a fitting search range can remain mathematically valid;
being inside that range does not establish fit quality.

The numerical interface normalizes inputs to binary64. Endpoint decisions use
those supplied numeric values. A high-precision decimal construction that
lands exactly on a boundary can move to either side when converted to
binary64; the tolerance does not conceal that input change. Qualification
oracles must use the same input representation. These numerical decisions do
not account for uncertainty in measured flow, geometry or timing.

An optional `training_snapshot_id` resolves an owned immutable observation
snapshot and its exact contract/content lineage. Declared model analytes must
belong to that contract's cohort. The link records which evidence a caller
names as training material. The service does not fit coefficients or assess
residuals, coverage, uncertainty, identifiability or extrapolation merely
because that link exists.

## Use a supported method and preserve its identity

`measurement_predict_retention` takes model ID, method ID and idempotency key.
The method must be owned, binary and constant-flow, and include the declared
strong solvent. Its canonical times/fractions supply the inlet profile;
seconds and supported flow units are already normalized by the method layer.
Equivalent absolute-time and duration-segment declarations therefore share the
same canonical input and prediction.

Variable flow and pre-acquisition phases are rejected. A post-acquisition phase
is explicitly excluded from the retention calculation and retained as such in
provenance. It never becomes the physical post-column volume. These exclusions
are specific to this ideal reference; method validation does not establish
that a program was delivered at a physical column inlet.

Predictions retain exact model/method input digests, the canonical program,
geometry, clock declaration, installed distribution evidence, source hashes,
solver settings and per-analyte results. `measurement_get_retention_model` and
`measurement_get_retention_prediction` return those immutable records.

## Interpret elution, censoring and numerical failure separately

The model solves `integral(1/k, 0, zeta) = t0` and reports detector time
`zeta + t0 + Vpost/Q`. The available intrinsic-time horizon is acquisition
end minus dead time and post-column transit. Detector time at the acquisition
endpoint is included; a peak after it is censored. The requested numerical
accuracy does not widen the acquisition window.

An `eluted` result contains a finite retention time and a numerical error
estimate. A `not_eluted` result has `retention_time_min: null` and the explicit
right-censor bound. It establishes only that this ideal model does not predict
an event within the acquisition window. It does not prove non-elution under
all future methods or confirm an analytical non-detection on an instrument.

The service requests absolute tolerance `1e-7 min` and allows at most 20,000
integrand evaluations per analyte. The numerical backend uses bounded adaptive
integration and root brackets, with roundoff/error estimates. These are
numerical diagnostics, not confidence intervals or calibrated model uncertainty.
Unresolved endpoint classification, numerical precision failures and exhausted
budgets raise rather than manufacturing an elution or a censored result.

Writes and retry receipts use the same owner-scoped SQLite transaction as the
other registries. Invalid models, methods or computations leave no prediction
or receipt. An identical retry returns its original prediction, including its
original implementation evidence. Model/prediction envelopes are bounded to
64,000 UTF-8 bytes. Stored results are local, synchronous calculations; they do
not implement an asynchronous instrument queue or optimizer campaign.

## Qualification boundary

The [source and unit audit](retention-model-qualification.md) explains the
selected natural-log signs, intrinsic clock and volume/flow relationships.
This ideal reference deliberately differs from the source's sampled time grid
and Gaussian retention mask. The source's plate-height and diffusion units,
peak widths, fit quality, original model assignment, measured gradient delays,
signal processing and hardware remain separate qualification work. Retention
predictions alone cannot establish separation or hard analytical acceptance.
