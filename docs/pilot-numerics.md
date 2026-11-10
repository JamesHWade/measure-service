# Check the installed numerical path for a pilot

`measure_service.pilot_numerics.qualify_numerics` accepts an initialized MCP
client and runs a fixed synthetic trace through the public service tools. It
creates a new CSV dataset and chromatogram/SEC analyses. It returns private
records for later retrieval checks, independent numeric comparisons, actual
backend capabilities and warnings. It does not require access to the service
filesystem. Use the installed pilot CLI when qualifying a remote endpoint.

The reference has 101 evenly spaced points from 1 to 3 min with response
`exp(-0.5 * ((time - 2) / 0.16)^2)`, reported in arbitrary RI response units.
Input declarations explicitly state SEC separation, baseline correction and
signal proportionality to weight concentration. The client verifies retained
CSV text, mapping, source digest and metadata, then checks one peak at 2 min
and integrated area against `sqrt(2 * pi) * 0.16` with relative tolerance 0.001.
Finite acquisition-window error is included in that declared tolerance.

Five synthetic standards at 1, 1.5, 2, 2.5 and 3 min follow
`log10(M / (g/mol)) = 6 - 0.4 * time + 0.03 * time^2`. The service fits the
quadratic relative calibration. Independently, the client computes molecular
weights directly from this known equation, assigns trapezoidal weights to the
input signal, and calculates:

- Weight average: `Mw = sum(weight * M) / sum(weight)`.
- Number average: `Mn = sum(weight) / sum(weight / M)`.
- Dispersity: `Mw / Mn`.

Moment comparisons use relative tolerance `1e-8`. The normalized distribution
integral uses relative tolerance `1e-5` with cumulative endpoints at zero and
one. Density must be nonnegative, cumulative values bounded and monotone, and
the log-mass grid must match the known calibration range. Installed code
fingerprints must be present, and retained method declarations/digests must
match the actual submitted methods. Nonfinite values, unavailable numerical capabilities, tool errors or
inconsistent source/provenance bindings fail the checks. Python reference
arithmetic uses the standard library and does not call the service's method
kernel or reuse R calculations as the expected answer.

These checks establish only this installed synthetic path. Declared detector
response, baseline correction and standards are fixture assumptions, not
measured qualifications. Peak identity, real standard validity, physical SEC
calibration and performance on researcher measurements remain separate.
