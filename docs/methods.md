# Chromatography methods

`measurement_method_capabilities` describes the independent method contract; it works without R. `measurement_validate_method` accepts a versioned `declaration` and optional `constraints`. It retains the declared units, normalizes a copy to minutes and mL/min, and returns `feasible` plus individual constraint violations with values, limits and units. Invalid structure or unsupported units produce an error. A well-formed method that exceeds a declared limit is returned as infeasible.

The contract covers LC isocratic programs, absolute-time gradients (`gradient_absolute`) and segment-duration gradients (`gradient_duration`). Gradients interpolate both flow and each solvent fraction linearly between endpoints. All named solvents must occur in every state and fractions must sum to one. Holds, increases and decreases are allowed. Absolute programs start exactly at analytical time zero; duration programs retain an initial state and positive durations. Equivalent absolute and duration programs produce the same canonical gradient.

This validation call describes a two-minute gradient in seconds and microlitres per minute:

```json
{
  "declaration": {
    "schema_version": "1.0",
    "time_unit": "s",
    "flow_unit": "uL/min",
    "solvents": ["A", "B"],
    "program": {
      "kind": "gradient_absolute",
      "points": [
        {"time": 0, "state": {"flow": 1000, "solvent_fractions": {"A": 0.9, "B": 0.1}}},
        {"time": 120, "state": {"flow": 1000, "solvent_fractions": {"A": 0.1, "B": 0.9}}}
      ]
    }
  },
  "constraints": {"max_flow_ml_min": 1.5, "max_acquisition_duration_min": 5}
}
```

Its canonical acquisition duration is 2 min, with 1 mL each of A and B. The same declaration can be registered with `measurement_register_method`, adding `title` and a fresh `idempotency_key`. Registration requires feasibility under the supplied limits. Retrieve it with `measurement_get_method`; list compact identities with `measurement_list_methods` (maximum 50 records per page). Methods are immutable: register a new method to change the program or limits. The registry retains the original declaration, normalized program, constraints, calculation results, creation time and two SHA-256 digests. `input_sha256` identifies the complete normalized registration request, including title and constraints; `canonical_program_sha256` identifies the normalized numerical schedule. Equipment declarations are retained in the input identity and are not part of the numerical-program digest.

Both `ExperimentSpec` and `AttemptSpec` accept `executable_method_id`. An experiment links its intended method; each attempt links its separately reported method. The registry verifies ownership and captures the method's immutable ID and digests in `executable_method` alongside the record. Revising an attempt preserves the earlier method link and digest. There is no automatic inheritance from intended to actual: callers must report what was attempted. Existing free-text `method_reference` values remain external references and are not treated as validated methods.

Optional constant `pre_acquisition` and `post_acquisition` phases use the declaration's time and flow units. They contribute to occupancy and total solvent use, while analytical points retain their own zero. Solvent use is the exact integral of flow multiplied by fraction within each linear segment; multiplying the mean flow by the mean fraction is generally incorrect when both vary. For example, a 2 min segment with flow 1 → 3 mL/min and A fraction 0.2 → 0.8 uses 2.2 mL of A and 1.8 mL of B. Constant overhead phases are added separately. Acquisition duration, occupancy and solvent use are reported as different quantities.

Constraint values carry canonical units in their field names. Supported limits include minimum segment duration, maximum acquisition and occupancy duration, flow, solvent-fraction ranges, absolute fraction slopes and declared categorical equipment compatibility. Constraints describe the caller's permitted method; they do not establish pressure feasibility, solvent chemical identity, column suitability or hardware qualification. Instrument-specific temperature, pressure prediction, nonlinear vendor curves, optimizer search-space parameterization and initial-design sampling remain future work. A registered method never starts an acquisition or establishes that an instrument followed the program. The complete stored record/request is limited to 64,000 UTF-8 bytes; the schema also bounds points, solvents and metadata.

The method schema permits up to 16 analytical points and 6 solvents; its bounded violation list has at most 256 entries. Fractions must sum to one within an absolute tolerance of `1e-9`. Limit comparisons allow floating-point differences within relative `1e-9` or absolute `1e-12` tolerance; input values and reported quantities are retained rather than rounded onto a coarser schedule. Missing or `unknown` equipment compatibility does not establish suitability, even when the numerical method is feasible under its declared limits.

## Run the example

After installing this checkout, run `python examples/methods.py` from the repository root. The example uses stdio MCP and a temporary database without R. It validates and retains a two-minute gradient, records separate intended and reported method links, and checks an idempotent retry. The reported attempt is labeled as a simulator record; the example performs no acquisition.

## Numerical backend and provenance

Solvent-use integration runs in the independent Python package
[measure-methods](https://github.com/JamesHWade/measure-methods). The service
validates declarations and constraints, converts to canonical units, calls the
backend and retains the results. It contains no integration kernel.

Each newly calculated evaluation records `calculation_backend`: the installed
package version and, when available, the source revision/ref supplied by pip's
installed VCS metadata. Missing source metadata is returned as `null`; a version
string does not establish a source revision. Older stored evaluations retain
unknown backend provenance. A retry returns the original stored evaluation
rather than relabeling it with the currently installed backend.

Until the backend's initial PR is merged, installation follows its
`codex/linear-program-usage` branch HEAD. Git is required by pip for this source
dependency. Resolved source IDs are evidence rather than install constraints.
