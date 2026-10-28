"""Explicit mathematical profiles for the archived MoSeS scalar score.

Reference definitions: fsk-lab/lcbo ``HPLCOptimizationConfig.calculate_experiment_score``
at 35098fb88e19cdc0786ed416dec92aa9f833cc2f, and the BoTier v1.0.0/v1.1.0
tags. These profiles evaluate reported per-analyte resolutions, not surrogate
predictions or a new chromatogram analysis. Time inputs are in minutes.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from numbers import Real

from .objectives import Criterion, hierarchical_score

MOSES_SCORE_PROFILES = {
    "moses-botier-1.0.0-binary64/1": "value",
    "moses-botier-1.1.0-binary64/1": "excess",
}


def _finite(value, name, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite real number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return result


def moses_observation_score(
    *,
    profile: str,
    num_compounds: int,
    peak_resolutions: Sequence[Real | None] | None,
    run_time_min: Real,
    max_run_time_min: Real,
    objective: str = "NumPeaks-CritRes",
    compound_idx: int | None = None,
    count_threshold: Real = 0.5,
    resolution_threshold: Real = 1.5,
    run_time_threshold_min: Real = 5.0,
) -> dict:
    """Score one observation under a caller-selected, versioned source profile.

    Detection follows finite per-analyte resolutions, as in the author code.
    An absent resolution array is unscorable; a present array with no finite
    values retains the source's zero-count/zero-critical-resolution convention.
    No missing scalar score is substituted with zero.

    BoTier v1.0.0 continues the final normalized objective from zero; v1.1.0
    continues only its excess over its threshold. Both rules are exposed,
    without inferring which software produced an archived score. All arithmetic
    uses Python binary64. This API supplies values, not autodifferentiation.
    """
    criteria, runtime, count_limit, resolution_limit, time_limit = _profile_criteria(
        profile,
        num_compounds,
        objective,
        compound_idx,
        run_time_min,
        max_run_time_min,
        count_threshold,
        resolution_threshold,
        run_time_threshold_min,
    )
    targeted = objective == "SinglePeak-Resolution"
    if isinstance(peak_resolutions, (str, bytes)):
        raise ValueError("peak_resolutions must be a numeric sequence")
    if peak_resolutions is None or (
        isinstance(peak_resolutions, Sequence) and len(peak_resolutions) == 0
    ):
        return {
            "profile": profile,
            "status": "unavailable",
            "score": None,
            "reason": "resolution_array_unavailable",
        }
    if (
        isinstance(peak_resolutions, (str, bytes))
        or not isinstance(peak_resolutions, Sequence)
        or len(peak_resolutions) != num_compounds
    ):
        raise ValueError("peak_resolutions must contain one numeric value per declared analyte")
    values, statuses = [], []
    for item in peak_resolutions:
        if item is None:
            values.append(None)
            statuses.append("missing")
        elif isinstance(item, bool) or not isinstance(item, Real):
            raise ValueError("peak_resolutions must contain real numbers, null or NaN")
        else:
            try:
                value = float(item)
            except (ValueError, OverflowError) as error:
                raise ValueError("peak resolution is not representable as binary64") from error
            if math.isnan(value):
                values.append(None)
                statuses.append("unavailable")
            elif math.isinf(value):
                values.append(None)
                statuses.append("nonfinite_resolution")
            else:
                values.append(value)
                statuses.append("finite")
    finite = [value for value in values if value is not None]
    if targeted:
        selected = values[compound_idx]
        detected = int(selected is not None)
        critical = selected if selected is not None else 0.0
        resolved = int(selected is not None and selected >= resolution_limit)
        scoring_count = 1
    else:
        detected = len(finite)
        critical = min(finite) if finite else 0.0
        resolved = sum(value >= resolution_limit for value in finite)
        scoring_count = num_compounds
    result = _score_metrics(
        profile=profile,
        criteria=criteria,
        objective=objective,
        scoring_count=scoring_count,
        detected=detected,
        resolved=resolved,
        critical=critical,
        runtime=runtime,
        count_limit=count_limit,
        resolution_limit=resolution_limit,
        time_limit=time_limit,
    )
    result.update(
        input_policy="raw_resolution_array",
        reference_detection_rule="finite per-analyte resolution",
        resolution_status_counts=dict(Counter(statuses)),
        num_compounds=num_compounds,
        scoring_compound_count=scoring_count,
        compound_idx=compound_idx,
    )
    return result


def _score_metrics(
    *,
    profile,
    criteria,
    objective,
    scoring_count,
    detected,
    resolved,
    critical,
    runtime,
    count_limit,
    resolution_limit,
    time_limit,
):
    resolved_count_mode = objective == "NumPeaks-NumResPeaks"
    raw = {"missing_analytes": scoring_count - detected, "run_time_min": runtime}
    if resolved_count_mode:
        raw["unresolved_analytes"] = scoring_count - resolved
    else:
        raw["critical_resolution"] = critical
    result = hierarchical_score(
        raw, criteria, smoothing=100.0, final_rule=MOSES_SCORE_PROFILES[profile]
    )
    count_met = scoring_count - detected <= count_limit
    resolution_met = (
        scoring_count - resolved <= count_limit
        if resolved_count_mode
        else critical >= resolution_limit
    )
    result.update(
        profile=profile,
        status="scored",
        arithmetic="binary64",
        objective=objective,
        detected_count=detected,
        resolved_count=resolved,
        critical_resolution=critical,
        analytical_thresholds_satisfied=count_met and resolution_met,
        all_objective_thresholds_satisfied=count_met and resolution_met and runtime <= time_limit,
        score_at_least_two=result["score"] >= 2.0,
    )
    return result


def moses_summary_score(
    *,
    profile: str,
    num_compounds: int,
    detected_count: int,
    critical_resolution: Real,
    run_time_min: Real,
    max_run_time_min: Real,
    resolved_count: int | None = None,
    objective: str = "NumPeaks-CritRes",
    count_threshold: Real = 0.5,
    resolution_threshold: Real = 1.5,
    run_time_threshold_min: Real = 5.0,
) -> dict:
    """Evaluate explicitly supplied summary inputs under a named score profile.

    This tests a historical-input hypothesis when serialized summaries disagree
    with per-analyte arrays. Agreement does not establish that these summaries
    were the original scorer's inputs. Targeted scores require the selected
    analyte's array entry and use :func:`moses_observation_score` instead.
    """
    criteria, runtime, count_limit, resolution_limit, time_limit = _profile_criteria(
        profile,
        num_compounds,
        objective,
        None,
        run_time_min,
        max_run_time_min,
        count_threshold,
        resolution_threshold,
        run_time_threshold_min,
    )
    for name, value in (("detected_count", detected_count), ("resolved_count", resolved_count)):
        if value is None and name == "resolved_count" and objective != "NumPeaks-NumResPeaks":
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= num_compounds:
            raise ValueError(f"{name} must be an integer within the declared analyte count")
    if resolved_count is not None and resolved_count > detected_count:
        raise ValueError("resolved_count cannot exceed detected_count")
    critical = _finite(critical_resolution, "critical_resolution")
    result = _score_metrics(
        profile=profile,
        criteria=criteria,
        objective=objective,
        scoring_count=num_compounds,
        detected=detected_count,
        resolved=resolved_count,
        critical=critical,
        runtime=float(run_time_min),
        count_limit=float(count_threshold),
        resolution_limit=float(resolution_threshold),
        time_limit=float(run_time_threshold_min),
    )
    result.update(
        input_policy="serialized_summary_hypothesis",
        num_compounds=num_compounds,
        scoring_compound_count=num_compounds,
        compound_idx=None,
    )
    return result


def _profile_criteria(
    profile,
    num_compounds,
    objective,
    compound_idx,
    run_time_min,
    max_run_time_min,
    count_threshold,
    resolution_threshold,
    run_time_threshold_min,
):
    if not isinstance(profile, str) or profile not in MOSES_SCORE_PROFILES:
        raise ValueError(f"profile must be one of {tuple(MOSES_SCORE_PROFILES)}")
    if isinstance(num_compounds, bool) or not isinstance(num_compounds, int) or num_compounds < 1:
        raise ValueError("num_compounds must be a positive integer")
    if objective not in {"NumPeaks-CritRes", "NumPeaks-NumResPeaks", "SinglePeak-Resolution"}:
        raise ValueError("unsupported MoSeS scoring objective")
    targeted = objective == "SinglePeak-Resolution"
    if targeted:
        if (
            isinstance(compound_idx, bool)
            or not isinstance(compound_idx, int)
            or not 0 <= compound_idx < num_compounds
        ):
            raise ValueError("SinglePeak-Resolution requires a valid zero-based compound_idx")
    elif compound_idx is not None:
        raise ValueError("compound_idx is only supported for SinglePeak-Resolution")
    runtime = _finite(run_time_min, "run_time_min", minimum=0)
    maximum = _finite(max_run_time_min, "max_run_time_min", minimum=1)
    count_limit = _finite(count_threshold, "count_threshold", minimum=0)
    resolution_limit = _finite(resolution_threshold, "resolution_threshold", minimum=0)
    time_limit = _finite(run_time_threshold_min, "run_time_threshold_min", minimum=1)
    count_bound = 1.0 if targeted else 10.0
    criteria = [Criterion("missing_analytes", "minimize", 0.0, count_bound, count_limit)]
    resolved_count_mode = objective == "NumPeaks-NumResPeaks"
    if resolved_count_mode:
        criteria.append(Criterion("unresolved_analytes", "minimize", 0.0, count_bound, count_limit))
    else:
        criteria.append(Criterion("critical_resolution", "maximize", 0.0, 5.0, resolution_limit))
    criteria.append(Criterion("run_time_min", "minimize", 1.0, maximum, time_limit))
    return criteria, runtime, count_limit, resolution_limit, time_limit
