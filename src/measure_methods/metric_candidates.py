"""Pure candidate metrics for archived chromatographic observations.

The candidates count finite retention times and resolutions separately, then
compare alternative resolution summaries. Retention times, FWHMs, and run time
are supplied in minutes; this function does not convert units. Pairwise
Gaussian resolution uses the same source convention as
``gaussian_pairwise_resolutions``. This module does not read archives, inspect
provenance, or depend on the service or MCP layer.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any

from measure_methods.chromatography import (
    _gaussian_pair_resolution,
    gaussian_pairwise_resolutions,
)

_MAX_PAIR_COMPOUNDS = 500
_ABSOLUTE_TOLERANCE = 1e-12
_RELATIVE_TOLERANCE = 1e-12
_PAIR_CANDIDATES = (
    "sigma_pair_adjacent_min",
    "sigma_pair_all_min",
    "fwhm_delta_over_width_sum_adjacent_min",
    "fwhm_delta_over_width_sum_all_min",
    "fwhm_2delta_over_width_sum_adjacent_min",
    "fwhm_2delta_over_width_sum_all_min",
    "fwhm_1_1774delta_over_width_sum_adjacent_min",
    "fwhm_1_1774delta_over_width_sum_all_min",
)


def _classify_array(
    name: str,
    values: object,
) -> tuple[list[float | None] | None, dict[str, int]]:
    if values is None:
        return None, {"array_unavailable": 1}
    if isinstance(values, (str, bytes, bytearray, Mapping)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be a numeric sequence or None")
    if not values:
        return None, {"array_unavailable": 1}

    result: list[float | None] = []
    counts: Counter[str] = Counter()
    for index, value in enumerate(values):
        if value is None:
            result.append(None)
            counts["null"] += 1
            continue
        if isinstance(value, bool) or not isinstance(value, Real):
            result.append(None)
            counts["invalid"] += 1
            continue
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            result.append(None)
            counts["invalid"] += 1
            continue
        if math.isnan(number):
            result.append(None)
            counts["nan"] += 1
        elif math.isinf(number):
            if number < 0:
                raise ValueError(f"{name}[{index}] must not be negative infinity")
            result.append(None)
            counts["positive_infinity"] += 1
        elif number < 0:
            raise ValueError(f"{name}[{index}] must be nonnegative")
        else:
            result.append(number)
            counts["finite"] += 1
    return result, dict(counts)


def _finite_optional(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite_min(values: list[float | None] | None) -> float | None:
    if values is None:
        return None
    finite = [value for value in values if value is not None]
    return min(finite) if finite else 0.0


def _candidate_template() -> dict[str, float | int | None]:
    return {
        "reported_num_peaks_vs_finite_resolution_count": None,
        "reported_num_peaks_vs_finite_retention_time_count": None,
        "reported_num_peaks_vs_finite_rt_within_runtime_inclusive": None,
        "reported_num_peaks_vs_finite_rt_within_runtime_exclusive": None,
        "reported_num_peaks_vs_finite_rt_and_resolution_within_runtime": None,
        "reported_critical_vs_min_finite_resolution": None,
        "reported_critical_vs_min_resolution_with_finite_rt": None,
        "reported_critical_vs_min_resolution_within_runtime": None,
        "reported_num_resolved_vs_finite_resolution_threshold_count": None,
        "reported_num_resolved_vs_finite_resolution_default_1_5": None,
        **{f"reported_critical_vs_{name}": None for name in _PAIR_CANDIDATES},
    }


def _pair_resolution_from_oracle(
    left_time: float,
    right_time: float,
    left_width: float | None,
    right_width: float | None,
) -> tuple[float, float] | None:
    """Calculate one adjacent pair through the established Gaussian API."""
    result = gaussian_pairwise_resolutions(
        [left_time, right_time],
        [left_width, right_width],
        time_unit="min",
        fwhm_unit="min",
    )
    pairs = result["pairs"]
    if not pairs:
        return None
    pair = pairs[0]
    return float(pair["retention_time_delta"]), float(pair["resolution"])


def chromatography_metric_candidates(
    *,
    num_compounds: int,
    retention_times: Sequence[object] | None,
    peak_resolutions: Sequence[object] | None,
    fwhms: Sequence[object] | None,
    run_time_min: object,
    resolution_threshold: object = 1.5,
) -> dict[str, Any]:
    """Calculate independent chromatographic metric candidates.

    ``retention_times``, ``fwhms``, and ``run_time_min`` use minutes; no unit
    conversion occurs. Empty or absent measurement arrays are unavailable.
    Invalid and nonfinite elements remain visible in per-array status counts
    and do not contribute to finite-value candidates. Negative finite values
    and negative infinity are rejected. Pairwise alternatives are calculated
    only when the declared cohort and the available arrays are at most 500
    compounds, which bounds the all-pairs calculation.

    The returned mapping contains numeric ``candidates``, per-array ``arrays``
    status counts, ``shape_status``, ``pair_counts``, and a ``pair_array_comparison``
    summary. It intentionally excludes archived summary values and provenance.
    """
    if isinstance(num_compounds, bool) or not isinstance(num_compounds, int):
        raise TypeError("num_compounds must be a positive integer")
    if num_compounds < 1:
        raise ValueError("num_compounds must be positive")

    retention, retention_status = _classify_array("retention_times", retention_times)
    resolutions, resolution_status = _classify_array("peak_resolutions", peak_resolutions)
    widths, width_status = _classify_array("fwhms", fwhms)
    runtime = _finite_optional(run_time_min)
    threshold = _finite_optional(resolution_threshold)

    finite_rt = [] if retention is None else [value for value in retention if value is not None]
    finite_rs = [] if resolutions is None else [value for value in resolutions if value is not None]
    count_rt = None if retention is None else len(finite_rt)
    count_rs = None if resolutions is None else len(finite_rs)
    count_rt_le = (
        None
        if retention is None or runtime is None
        else sum(value <= runtime for value in finite_rt)
    )
    count_rt_lt = (
        None
        if retention is None or runtime is None
        else sum(value < runtime for value in finite_rt)
    )
    count_rt_r_le = None
    if (
        retention is not None
        and resolutions is not None
        and runtime is not None
        and len(retention) == len(resolutions)
    ):
        count_rt_r_le = sum(
            time is not None and resolution is not None and time <= runtime
            for time, resolution in zip(retention, resolutions, strict=True)
        )

    min_rs = _finite_min(resolutions)
    min_rs_finite_rt = None
    min_rs_within_runtime = None
    if retention is not None and resolutions is not None and len(retention) == len(resolutions):
        paired = [
            resolution
            for time, resolution in zip(retention, resolutions, strict=True)
            if time is not None and resolution is not None
        ]
        min_rs_finite_rt = min(paired) if paired else 0.0
        if runtime is not None:
            within = [
                resolution
                for time, resolution in zip(retention, resolutions, strict=True)
                if time is not None and resolution is not None and time <= runtime
            ]
            min_rs_within_runtime = min(within) if within else 0.0

    resolved_threshold = (
        None
        if resolutions is None or threshold is None
        else sum(value >= threshold for value in finite_rs)
    )
    resolved_default = None if resolutions is None else sum(value >= 1.5 for value in finite_rs)

    candidate = _candidate_template()
    candidate.update(
        {
            "reported_num_peaks_vs_finite_resolution_count": count_rs,
            "reported_num_peaks_vs_finite_retention_time_count": count_rt,
            "reported_num_peaks_vs_finite_rt_within_runtime_inclusive": count_rt_le,
            "reported_num_peaks_vs_finite_rt_within_runtime_exclusive": count_rt_lt,
            "reported_num_peaks_vs_finite_rt_and_resolution_within_runtime": count_rt_r_le,
            "reported_critical_vs_min_finite_resolution": min_rs,
            "reported_critical_vs_min_resolution_with_finite_rt": min_rs_finite_rt,
            "reported_critical_vs_min_resolution_within_runtime": min_rs_within_runtime,
            "reported_num_resolved_vs_finite_resolution_threshold_count": resolved_threshold,
            "reported_num_resolved_vs_finite_resolution_default_1_5": resolved_default,
        }
    )

    arrays = {
        "retention_times": retention_status,
        "peak_resolutions": resolution_status,
        "fwhms": width_status,
    }
    if retention is None or resolutions is None or widths is None:
        shape_status = "array_unavailable"
    elif len(retention) == len(resolutions) == len(widths) == num_compounds:
        shape_status = "aligned_arrays"
    else:
        shape_status = "length_mismatch"

    pair_counts: Counter[str] = Counter()
    pair_comparison = {"compared": 0, "mismatches": 0, "max_abs_error": 0.0}
    if retention is not None and resolutions is not None:
        for time, resolution in zip(retention, resolutions):
            pair_counts["finite_resolution_without_finite_rt"] += int(
                resolution is not None and time is None
            )
            pair_counts["finite_rt_without_finite_resolution"] += int(
                time is not None and resolution is None
            )
    if retention is not None:
        rt_positions = [index for index, value in enumerate(retention) if value is not None]
        pair_counts["observations_with_fewer_than_two_finite_rt"] += int(len(rt_positions) < 2)
        pair_counts["source_style_boundary_singleton_rows"] += int(len(rt_positions) == 1)
        ties = Counter(retention[index] for index in rt_positions)
        duplicate_pairs = sum(count * (count - 1) // 2 for count in ties.values())
        pair_counts["observations_with_retention_time_ties"] += int(duplicate_pairs > 0)
        pair_counts["duplicate_retention_time_pairs"] += duplicate_pairs
        if runtime is not None:
            pair_counts["retention_time_equal_runtime"] += sum(
                value == runtime for value in finite_rt
            )
            pair_counts["retention_time_after_runtime"] += sum(
                value > runtime for value in finite_rt
            )

    pair_values: dict[str, list[float]] = defaultdict(list)
    per_analyte_neighbours: list[list[float]] | None = None
    max_pair_size = max(
        num_compounds,
        len(retention) if retention is not None else 0,
        len(widths) if widths is not None else 0,
    )
    if (
        retention is not None
        and widths is not None
        and len(retention) == len(widths)
        and max_pair_size <= _MAX_PAIR_COMPOUNDS
    ):
        ordered_positions = sorted(
            (index for index, value in enumerate(retention) if value is not None),
            key=lambda index: (retention[index], index),
        )
        per_analyte_neighbours = [[] for _ in range(max(num_compounds, len(retention)))]
        valid_adjacent: list[tuple[int, int, dict[str, float]]] = []

        for left_position in range(len(ordered_positions)):
            for right_position in range(left_position + 1, len(ordered_positions)):
                left_index = ordered_positions[left_position]
                right_index = ordered_positions[right_position]
                left_width = widths[left_index]
                right_width = widths[right_index]
                if left_width is None or right_width is None or left_width <= 0 or right_width <= 0:
                    continue
                delta: float
                sigma_pair: float
                if right_position == left_position + 1:
                    try:
                        gaussian_pair = _pair_resolution_from_oracle(
                            retention[left_index],
                            retention[right_index],
                            left_width,
                            right_width,
                        )
                    except ValueError:
                        gaussian_pair = None
                    if gaussian_pair is None:
                        continue
                    delta, sigma_pair = gaussian_pair
                else:
                    try:
                        delta, sigma_pair = _gaussian_pair_resolution(
                            retention[left_index],
                            retention[right_index],
                            left_width,
                            right_width,
                        )
                    except ValueError:
                        continue

                width_total = left_width + right_width
                if not math.isfinite(width_total) or width_total <= 0:
                    continue
                values = {
                    "sigma_pair": sigma_pair,
                    "fwhm_delta_over_width_sum": delta / width_total,
                    "fwhm_2delta_over_width_sum": 2 * delta / width_total,
                    "fwhm_1_1774delta_over_width_sum": 1.1774 * delta / width_total,
                }
                if any(not math.isfinite(value) for value in values.values()):
                    continue
                for base, value in values.items():
                    pair_values[f"{base}_all_min"].append(value)
                if right_position == left_position + 1:
                    valid_adjacent.append((left_index, right_index, values))
                    pair_counts["source_style_usable_adjacent_pairs"] += 1
                    if values["sigma_pair"] == 0:
                        pair_counts["zero_resolution_adjacent_pairs"] += 1
                    per_analyte_neighbours[left_index].append(values["sigma_pair"])
                    per_analyte_neighbours[right_index].append(values["sigma_pair"])

        for left, right in zip(valid_adjacent, valid_adjacent[1:]):
            pair_counts["source_style_adjacent_pair_ties"] += int(
                left[2]["sigma_pair"] == right[2]["sigma_pair"]
            )
        pair_counts["source_style_all_pairs"] += len(pair_values["sigma_pair_all_min"])
        pair_counts["source_style_adjacent_pairs"] += len(valid_adjacent)
        pair_counts[
            "observations_with_finite_rt_pairs_but_no_positive_finite_width_adjacent_pair"
        ] += int(len(ordered_positions) >= 2 and not valid_adjacent)
        for base in (
            "sigma_pair",
            "fwhm_delta_over_width_sum",
            "fwhm_2delta_over_width_sum",
            "fwhm_1_1774delta_over_width_sum",
        ):
            pair_values[f"{base}_adjacent_min"] = [item[2][base] for item in valid_adjacent]
        for name in _PAIR_CANDIDATES:
            values = pair_values.get(name, [])
            candidate[f"reported_critical_vs_{name}"] = min(values) if values else None

        if resolutions is not None:
            reconstructed = [min(items) if items else None for items in per_analyte_neighbours]
            paired = [
                (derived, archived)
                for derived, archived in zip(reconstructed, resolutions)
                if derived is not None and archived is not None
            ]
            pair_comparison["compared"] = len(paired)
            for derived, archived in paired:
                error = abs(derived - archived)
                if math.isfinite(error) and pair_comparison["max_abs_error"] is not None:
                    pair_comparison["max_abs_error"] = max(pair_comparison["max_abs_error"], error)
                else:
                    pair_comparison["max_abs_error"] = None
                if not math.isclose(
                    derived,
                    archived,
                    rel_tol=_RELATIVE_TOLERANCE,
                    abs_tol=_ABSOLUTE_TOLERANCE,
                ):
                    pair_comparison["mismatches"] += 1
    return {
        "candidates": candidate,
        "arrays": arrays,
        "shape_status": shape_status,
        "pair_counts": dict(pair_counts),
        "pair_array_comparison": pair_comparison,
    }
