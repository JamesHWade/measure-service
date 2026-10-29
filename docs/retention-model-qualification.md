# MoSeS retention model: inputs and qualification boundary

This note records the conventions needed to compare the public MoSeS simulator
with a dimensionally explicit retention-time reference. The source snapshot
inspected is MoSeS commit
[`5818c698b61652bbe6e718881ca99c04a3d497bd`](https://gitlab.com/aspuru-guzik-group/self-driving-lab/instruments/moses/-/tree/5818c698b61652bbe6e718881ca99c04a3d497bd).
That revision identifies the inspected evidence; it is not a runtime version
pin. The SI is Haddadnia et al., *MoSeS Enables Autonomous Chromatography
Operation through Programmable Analytical Expertise*, ChemRxiv v1, especially
pp. 15–20, Fig. S8 and equations (4)–(14)
([article](https://chemrxiv.org/doi/full/10.26434/chemrxiv.15008042/v1),
[supplementary information, p. 16](https://chemrxiv.org/doi/suppl/10.26434/chemrxiv.15008042/v1/suppl_file/lcbo_si_aug26.pdf#page=16),
[p. 17](https://chemrxiv.org/doi/suppl/10.26434/chemrxiv.15008042/v1/suppl_file/lcbo_si_aug26.pdf#page=17),
[p. 18](https://chemrxiv.org/doi/suppl/10.26434/chemrxiv.15008042/v1/suppl_file/lcbo_si_aug26.pdf#page=18)).

## Convention for a new ideal retention reference

The selected `ideal-lc-retention-zeta/1` reference takes a gradient already
expressed at the **column inlet**, with time zero at analyte entry into the
column. It models constant-flow ideal plug transport and retention time only.
The existing implementation documents the input profile and limits in
[`retention.py`](../src/measure_methods/retention.py#L1-L15).

| Input | Unit and meaning |
| --- | --- |
| `flow_rate_ml_min` | Constant volumetric flow, mL/min. For equations in SI, `Q = flow_rate_ml_min × 10⁻⁶` m³/min. |
| `column_length_m` | Column length `L`, m. |
| `effective_area_m2` | Effective column cross-section `A`, m². The declared mobile volume is `L A`; no separate porosity is inferred. |
| `gradient_times_min` | Strictly increasing minutes, from 0 through the acquisition endpoint. |
| `gradient_fractions` | Eluent-B fraction `φ`, dimensionless in [0, 1], piecewise linear between gradient knots. The profile is the one at the column inlet, not an uncorrected pump command. |
| `post_column_volume_ml` | Optional volume from column outlet to detector, mL. Its transit delay is `Vpost / Q`; it does not change on-column retention. |
| retention coefficients | Dimensionless log-model coefficients. `ln_k0` is the natural log of `k` at `φ = 0`; the logarithm base is `e`. |

With `u = Q/A` m/min, the mobile-phase dead time is
`t0 = L/u = L A/Q` minutes. For a retention factor `k(φ)`, solve

```text
integral from 0 to ζR of 1 / k(φ(ζ)) dζ = t0
```

and report detector time `tR = ζR + t0 + Vpost/Q`. This time is relative to
analyte entry into the column; injector-to-column transit is not represented.
If the integral has not reached `t0` by the available acquisition time, report
right censoring at that window. The selected interface does not apply a hidden
gradient delay, fit an instrument dwell volume, infer an injection-front
correction, or calculate peak width. A caller with pump-clock gradient data
must provide a separately declared and measured clock/delay correction to
produce the column-inlet profile. The parameter `post_column_volume_ml` is
only the column-outlet to detector volume; do not map the upstream
`pre_column_volume` into it or into a gradient delay without independent
evidence.

The three empirical solvent-strength laws are:

```text
linear:     ln(k) = ln_k0 - S1 φ
quadratic:  ln(k) = ln_k0 - S1 φ - S2 φ²
Neue–Kuss:  ln(k) = ln_k0 + 2 ln(1 + S2 φ) - S1 φ / (1 + S2 φ)
```

These match the code convention in the pinned
[`retention_models.py`](https://gitlab.com/aspuru-guzik-group/self-driving-lab/instruments/moses/-/blob/5818c698b61652bbe6e718881ca99c04a3d497bd/src/moses/simulator/retention_models.py#L35-165).
SI equations (12)–(13) print plus-signed linear and quadratic coefficients;
for the same curve, their signed coefficients are `S1_SI = -S1_code` and
`S2_SI = -S2_code`. SI equation (14) and the source Neue–Kuss expression use
the same signs. `φ` is a fraction, not percent. The source `log_k0` is a
natural logarithm because the implementation evaluates `exp(log_k0 - …)`;
do not pass `log10(k0)`. The source's bounded positive fit ranges are
optimization choices, not physical units or instrument calibration. The
selected reference keeps the equation convention but does not impose those
fit bounds.

The public numerical interface uses binary64 inputs. Independent high-precision
checks must evaluate the exact parsed binary64 values, including geometry and
the acquisition endpoint. A decimal identity need not survive that conversion;
retain that distinction when classifying a peak at a hard acquisition limit.
The solver tolerance describes numerical accuracy, not physical input
uncertainty or an extension of the observation window.

## What the pinned simulator actually does

`LCGradient` stores times in minutes, B proportions and flow in mL/min; the
simulator uses `numpy.interp` on a fixed time grid, assumes constant flow, and
converts the first flow value to m³/min. It computes `t0 = L A/Q`. Its
retention root is a cumulative sum of `Δt/k` against that dead time. The
gradient setter explicitly leaves `pre_injection_volume` unimplemented.
After solving, `pre_column_volume/Q` is added to the reported retention time.
([gradient definition](https://gitlab.com/aspuru-guzik-group/self-driving-lab/instruments/moses/-/blob/5818c698b61652bbe6e718881ca99c04a3d497bd/src/moses/datatypes/lc_gradient.py#L33-53),
[interpolation and delay](https://gitlab.com/aspuru-guzik-group/self-driving-lab/instruments/moses/-/blob/5818c698b61652bbe6e718881ca99c04a3d497bd/src/moses/simulator/simulator.py#L105-155),
[retention root and offset](https://gitlab.com/aspuru-guzik-group/self-driving-lab/instruments/moses/-/blob/5818c698b61652bbe6e718881ca99c04a3d497bd/src/moses/simulator/simulator.py#L228-253)).

The SI defines `V0` from pump mixer to injector, `V1` from injector to column,
and `V2` from column to detector. It combines `V1 + V2` into `Ṽ` for the
detector-time correction and says `V0` corrects the gradient. The source
implementation does not implement `V0` in its gradient profile, has one
`pre_column_volume` offset, and does not distinguish `V1` from `V2`. Thus its
`pre_column_volume` cannot be treated as a measured post-column volume or as a
fully specified dwell correction. The selected ideal reference intentionally
accepts an already column-inlet-corrected profile and a separate post-column
volume; injection and front delays remain outside its contract.

There are two dimensional inconsistencies in the printed SI that must remain
visible. The SI defines `ζ = t - z/u`, an intrinsic time coordinate in the
transport derivation, not a pump-command clock or a dwell correction.
Integrating `dz/dζ = u/k` to the column end and dividing by `u` gives
`integral dζ/k = t0`. Equation (6) as printed instead has `u/k` inside the
time integral, whose units are length. I therefore use the dimensionally
consistent equation, which is also implemented by the source's `Δt/k`
accumulation, while retaining the printed-equation discrepancy. Equation (10) prints
`Ṽ/u`, but `Ṽ` is in m³ and `u` is in m/min. A volume transit time uses
volumetric flow, `Ṽ/Q`; the simulator likewise adds `pre_column_volume / Q`.
The equations as printed are not silently treated as executable unit
definitions.

## Peak width and the fitted plate-height parameter

SI equations (8)–(11) define an effective plate height `H = 2D/u` in metres,
then a spatial second moment `μ2`, and time width
`σR = sqrt(μ2) (1 + kR) / u`. In that physical form, with
`I = integral from 0 to ζR of (1 + k)²/k³ dζ`,
`σR = kR sqrt(H I/u)`. An independent isocratic limit is

```text
ζR = k t0
I = (1 + k)² t0 / k²
σR = (1 + k) sqrt(H L) / u
```

With no transit offset, the last expression is also
`tR / sqrt(L/H)`, the plate-count limit for `N = L/H`. It is a validation
target for a future width model; the current selected retention reference
does not calculate `σ`, FWHM, or `H`.

The pinned simulator's width code inserts volumetric `Q` where SI equations
(9) and (11) use linear `u`: it calculates a moment with `H_code × Q × I`
and divides its square root by `Q`. Consequently its parameter declared and
fitted as `plate_height [m]` is not established as a physical plate height.
Algebraically, the final width expression could coincide with the physical
expression if one hypothesized `H_code = H_physical × A` (so `H_code` has
units m³), but this contradicts the source declaration and does not make its
intermediate `μ2` a spatial variance. Treat `H_physical = H_code/A` only as
an unverified conversion hypothesis, not a calibration or accepted result.
The additional fitted `pre_column_diffusion` term also lacks independent
dimensional and experimental qualification: the code annotates it in metres,
but adds `pre_column_diffusion × pre_column_volume / Q`, which has units m·min
under those declarations, not minutes.

`Simulator.from_data` requires each run to supply a gradient and equal-length,
fixed-order retention-time, FWHM and peak-area vectors. It converts FWHM to
`σ`; NaN retention times are ignored and positive infinity is treated as
elution after the gradient. It fits global geometry/volume parameters and
compound retention parameters from times, then fits `plate_height` and
`pre_column_diffusion` from widths. Those are fitting inputs, not proof that
the inputs are identified, measured, or physically qualified. Although
`from_data` lists `pre_injection_volume` among fit parameters, the gradient
setter does not use it, so the parameter has no implemented effect on the
retention calculation ([fit contract and parameter
ranges](https://gitlab.com/aspuru-guzik-group/self-driving-lab/instruments/moses/-/blob/5818c698b61652bbe6e718881ca99c04a3d497bd/src/moses/simulator/simulator.py#L348-463)).

## What the archived artifacts can establish

The archived simulator rows can test byte integrity, reproduce source outputs,
and compare a source-compatible numerical replay. The LCBO simulation wrapper
builds simulated arrays from latent retention times and Gaussian widths; this
is evidence about that simulation path, not independent instrument
measurements. Experiment-labelled archive rows may serve as measured outcomes
after original trace, assignment and condition provenance is established; a
stored gradient or fit parameter does not itself show that physical column
geometry, flow, clock alignment, or system volumes were independently
measured. Agreement with simulated outputs cannot qualify a column geometry,
pump-to-injector delay, injection front, analyte assignment, fitted `H`,
peak-width model, or instrument. A fresh physical qualification needs
identified standards or independently assigned peaks, measured/declared run
gradients and clock origin, flow, actual column geometry, independently
measured relevant system volumes, and residual checks on repeated experimental
runs. See the [archive's scope and
limits](moses-reproduction.md#what-remains-for-the-full-paper) and the
[subsequent observation contract](moses-observation-contract.md).

For an independent linear-ramp check in the column-inlet coordinate, set
`φ(ζ) = φ0 + g ζ` and `ln(k) = c - b ζ`, where
`c = ln_k0 - S1 φ0` and `b = S1 g`. Then
`integral(0, ζ, 1/k dζ) = exp(-c) × (exp(b ζ) - 1) / b` for `b ≠ 0`,
so `ζR = ln(1 + b t0 exp(c)) / b` when that root exists. Its `b → 0` limit
is `k(φ0) t0`.

Independent numerical checks should also include the limit `k → 0+`, where
`tR → t0 + Vpost/Q`, and a controlled increase `ΔVpost`, which must shift
detector time by exactly `ΔVpost/Q` without changing `t0` or `ζR` when the
acquisition window remains sufficient. These qualify calculations under the
stated ideal assumptions, not an instrument or a fitted empirical model.

## Source and redistribution scope

MoSeS source inspection used the pinned public GitLab files linked above. The
[README](https://gitlab.com/aspuru-guzik-group/self-driving-lab/instruments/moses/-/blob/5818c698b61652bbe6e718881ca99c04a3d497bd/README.md)
advertises MIT and
[`pyproject.toml`](https://gitlab.com/aspuru-guzik-group/self-driving-lab/instruments/moses/-/blob/5818c698b61652bbe6e718881ca99c04a3d497bd/pyproject.toml)
refers to a `LICENSE` file, but the
[repository tree](https://gitlab.com/aspuru-guzik-group/self-driving-lab/instruments/moses/-/tree/5818c698b61652bbe6e718881ca99c04a3d497bd)
at the inspected revision did not contain that file. The source-code
redistribution terms therefore remain unverified from the actual license text;
this note includes no copied implementation or author data. The ChemRxiv
[article record](https://chemrxiv.org/doi/full/10.26434/chemrxiv.15008042/v1)
identifies CC BY 4.0 terms for the paper and SI
([license](https://creativecommons.org/licenses/by/4.0/)). The author dataset
has a separate license status documented in the
[archive provenance note](moses-reproduction.md#sources-and-provenance); this
repository does not copy the dataset into this document.
