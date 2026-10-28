"""Read-only metrics for archived chromatographic observations.

The reference-observation counts follow ``HPLCExperiment.num_peaks``,
``num_resolved_peaks`` and ``critical_resolution`` in the archived lcbo source
at https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/lcbo/hplc_utils/hplc_experiment.py.
In particular, those counts use finite peak resolutions; they do not infer
elution from retention time. The separate runtime count below uses finite
retention times less than or equal to the caller-declared run time.

The optional Gaussian oracle follows the theoretical-compound calculation in
https://github.com/fsk-lab/lcbo/blob/35098fb88e19cdc0786ed416dec92aa9f833cc2f/lcbo/routines/experiment.py:
FWHM = 2.3548 * sigma and adjacent-pair resolution = delta_t / (2 * (sigma_i + sigma_j)).
It is a source-specific Gaussian check, not a general fitted-peak resolution model.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any

_FWHM_PER_SIGMA = 2.3548


def _finite_real(name: str, value: object, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a finite real number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be at least {minimum:g}")
    return result


def _declared_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("num_compounds must be a nonnegative integer")
    if value < 0:
        raise ValueError("num_compounds must be nonnegative")
    return value


def _unit(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a nonempty unit string")
    if not value or value.strip() != value:
        raise ValueError(f"{name} must be a nonempty unit string without surrounding whitespace")
    return value


def _time_units(
    retention_time_unit: object,
    fwhm_unit: object,
    run_time_unit: object,
) -> tuple[str, str, str]:
    retention_unit = _unit("retention_time_unit", retention_time_unit)
    width_unit = _unit("fwhm_unit", fwhm_unit)
    runtime_unit = _unit("run_time_unit", run_time_unit)
    if not (retention_unit == width_unit == runtime_unit):
        raise ValueError(
            "retention_time_unit, fwhm_unit, and run_time_unit must match; values are not converted"
        )
    return retention_unit, width_unit, runtime_unit


def _numeric_vector(
    name: str,
    values: object,
    expected_length: int,
) -> tuple[list[tuple[float | None, str]] | None, str]:
    """Parse one JSON-like numeric vector without collapsing missingness."""
    if values is None:
        return None, "array_unavailable"
    if isinstance(values, (str, bytes, bytearray, Mapping)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be a numeric sequence or None")

    if len(values) == 0 and expected_length > 0:
        # Historical HPLCExperiment.from_dict used [] when resolution data was
        # absent. Treat an empty vector as missing for nonempty experiments.
        return None, "array_unavailable"
    if len(values) != expected_length:
        raise ValueError(
            f"{name} must contain exactly num_compounds values "
            f"({expected_length}); got {len(values)}"
        )

    parsed: list[tuple[float | None, str]] = []
    for index, value in enumerate(values):
        if value is None:
            parsed.append((None, "missing"))
            continue
        if isinstance(value, bool) or not isinstance(value, Real):
            raise TypeError(f"{name}[{index}] must be a real number, None, or NaN")
        try:
            number = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"{name}[{index}] is outside the supported numeric range") from exc
        if math.isnan(number):
            parsed.append((None, "unavailable"))
        elif math.isinf(number):
            if number < 0:
                raise ValueError(f"{name}[{index}] must not be negative infinity")
            parsed.append(
                (None, "non_eluting" if name == "retention_times" else "positive_infinity")
            )
        else:
            parsed.append((number, "finite"))

    if name == "retention_times":
        for index, (number, status) in enumerate(parsed):
            if status == "finite" and number is not None and number < 0:
                raise ValueError(f"retention_times[{index}] must be nonnegative")
    if name == "fwhms":
        for index, (number, status) in enumerate(parsed):
            if status == "finite" and number is not None and number < 0:
                raise ValueError(f"fwhms[{index}] must be nonnegative")
    if name == "peak_resolutions":
        for index, (number, status) in enumerate(parsed):
            if status == "finite" and number is not None and number < 0:
                raise ValueError(f"peak_resolutions[{index}] must be nonnegative")
    return parsed, "present"


def _serialise_vector(
    values: list[tuple[float | None, str]] | None,
    availability: str,
) -> dict[str, Any]:
    if values is None:
        return {"availability": availability, "values": None}
    return {
        "availability": availability,
        "values": [{"value": value, "status": status} for value, status in values],
    }


def _finite_values(values: list[tuple[float | None, str]] | None) -> list[float] | None:
    if values is None:
        return None
    return [value for value, status in values if status == "finite" and value is not None]


def _diagnostic(
    code: str, analyte_index: int | None = None, **details: object
) -> dict[str, object]:
    result: dict[str, object] = {"code": code}
    if analyte_index is not None:
        result["analyte_index"] = analyte_index
    result.update(details)
    return result


def summarize_chromatography_observation(
    *,
    num_compounds: int,
    retention_times: Sequence[object] | None,
    peak_resolutions: Sequence[object] | None,
    fwhms: Sequence[object] | None,
    areas: Sequence[object] | None,
    run_time: Real,
    retention_time_unit: str,
    fwhm_unit: str,
    run_time_unit: str,
    area_unit: str,
    resolution_threshold: Real = 1.5,
) -> dict[str, Any]:
    """Summarize caller-supplied archived values without inventing measurements.

    All time-like values use an identical caller-declared unit. Area values
    retain the caller's unit label, which is provenance metadata and does not
    qualify a detector response as a physical quantity. ``None`` or an empty
    vector for a nonempty experiment marks that whole array unavailable; a
    length mismatch otherwise raises. A NaN element is retained as an
    unavailable observation, while positive-infinite retention time is marked
    non-eluting. JSON values for all non-finite numbers are ``null`` plus a
    status.

    Reference metrics reproduce the finite-resolution rules from the pinned
    HPLCExperiment implementation. Runtime elution is an independent count of
    finite retention times <= ``run_time`` (inclusive). Missing resolution
    arrays make the reference metrics unavailable rather than fabricating
    zero-valued observations.
    """
    count = _declared_count(num_compounds)
    retention_unit, width_unit, runtime_unit = _time_units(
        retention_time_unit,
        fwhm_unit,
        run_time_unit,
    )
    declared_area_unit = _unit("area_unit", area_unit)
    runtime = _finite_real("run_time", run_time, minimum=0)
    threshold = _finite_real("resolution_threshold", resolution_threshold, minimum=0)

    parsed = {
        "retention_times": _numeric_vector("retention_times", retention_times, count),
        "peak_resolutions": _numeric_vector("peak_resolutions", peak_resolutions, count),
        "fwhms": _numeric_vector("fwhms", fwhms, count),
        "areas": _numeric_vector("areas", areas, count),
    }
    retention, retention_availability = parsed["retention_times"]
    resolutions, resolution_availability = parsed["peak_resolutions"]
    widths, width_availability = parsed["fwhms"]
    areas_parsed, area_availability = parsed["areas"]

    finite_resolutions = _finite_values(resolutions)
    if finite_resolutions is None:
        reference_metrics: dict[str, object] = {
            "status": "unavailable",
            "num_peaks": None,
            "num_resolved_peaks": None,
            "critical_resolution": None,
            "critical_resolution_status": "unavailable",
            "resolution_threshold": threshold,
        }
    else:
        critical_status = (
            "minimum_finite_resolution" if finite_resolutions else "source_default_no_finite_values"
        )
        reference_metrics = {
            "status": "available" if finite_resolutions else "no_finite_resolution_values",
            "num_peaks": len(finite_resolutions),
            "num_resolved_peaks": sum(value >= threshold for value in finite_resolutions),
            "critical_resolution": min(finite_resolutions) if finite_resolutions else 0.0,
            "critical_resolution_status": critical_status,
            "resolution_threshold": threshold,
        }

    finite_retention_times = _finite_values(retention)
    if finite_retention_times is None:
        time_window_metrics: dict[str, object] = {
            "status": "unavailable",
            "num_eluted_within_run_time": None,
            "comparison": "retention_time <= run_time (inclusive)",
        }
    else:
        time_window_metrics = {
            "status": "available" if finite_retention_times else "no_finite_retention_times",
            "num_eluted_within_run_time": sum(value <= runtime for value in finite_retention_times),
            "comparison": "retention_time <= run_time (inclusive)",
        }

    diagnostics: list[dict[str, object]] = []
    for name, availability in (
        ("retention_times", retention_availability),
        ("peak_resolutions", resolution_availability),
        ("fwhms", width_availability),
        ("areas", area_availability),
    ):
        if availability == "array_unavailable":
            diagnostics.append(_diagnostic(f"{name}_array_unavailable"))

    if retention is None and finite_resolutions:
        diagnostics.append(_diagnostic("finite_resolutions_without_retention_time_array"))

    if retention is not None:
        for index, (retention_value, retention_status) in enumerate(retention):
            resolution_status = resolutions[index][1] if resolutions is not None else None

            if retention_status == "finite" and retention_value is not None:
                if retention_value > runtime:
                    diagnostics.append(_diagnostic("retention_time_after_run_time", index))
                if (
                    retention_value <= runtime
                    and resolution_status is not None
                    and resolution_status != "finite"
                ):
                    diagnostics.append(
                        _diagnostic("eluted_within_run_time_without_finite_resolution", index)
                    )
            elif resolution_status == "finite":
                diagnostics.append(
                    _diagnostic("finite_resolution_without_finite_retention_time", index)
                )

            if retention_status == "non_eluting":
                finite_companions = []
                for name, vector in (
                    ("peak_resolution", resolutions),
                    ("fwhm", widths),
                    ("area", areas_parsed),
                ):
                    if vector is not None and vector[index][1] == "finite":
                        finite_companions.append(name)
                if finite_companions:
                    diagnostics.append(
                        _diagnostic(
                            "non_eluting_with_finite_measurements",
                            index,
                            fields=finite_companions,
                        )
                    )

    if areas_parsed is not None:
        for index, (area_value, area_status) in enumerate(areas_parsed):
            if area_status == "finite" and area_value is not None and area_value < 0:
                diagnostics.append(_diagnostic("negative_reported_area", index))
    if widths is not None:
        for index, (width_value, width_status) in enumerate(widths):
            if width_status == "finite" and width_value == 0:
                diagnostics.append(_diagnostic("zero_fwhm", index))

    return {
        "num_compounds": count,
        "run_time": {"value": runtime, "unit": runtime_unit},
        "units": {
            "retention_time": retention_unit,
            "fwhm": width_unit,
            "run_time": runtime_unit,
            "area": declared_area_unit,
            "peak_resolution": "dimensionless",
        },
        "measurements": {
            "retention_times": _serialise_vector(retention, retention_availability),
            "peak_resolutions": _serialise_vector(resolutions, resolution_availability),
            "fwhms": _serialise_vector(widths, width_availability),
            "areas": _serialise_vector(areas_parsed, area_availability),
        },
        "reference_resolution_metrics": reference_metrics,
        "time_window_elution_metrics": time_window_metrics,
        "diagnostics": diagnostics,
    }


def gaussian_pairwise_resolutions(
    retention_times: Sequence[object] | None,
    fwhms: Sequence[object] | None,
    *,
    time_unit: str,
    fwhm_unit: str,
) -> dict[str, Any]:
    """Calculate source-convention Rs for adjacent observed Gaussian peaks.

    Finite peaks are ordered by retention time; pair records retain original
    indices. A NaN/None retention time makes full-set adjacency uncertain, so
    computed pairs are explicitly scoped to the known finite-time subset.
    Non-eluting peaks are excluded with a status. Missing/invalid widths never
    cause the helper to bridge across a known-retention peak.
    """
    retention_unit, width_unit, _ = _time_units(time_unit, fwhm_unit, time_unit)
    if retention_times is None and fwhms is None:
        return {
            "status": "unavailable",
            "pairs": None,
            "excluded_analytes": [],
            "diagnostics": [_diagnostic("retention_times_and_fwhms_unavailable")],
            "units": {"retention_time": retention_unit, "fwhm": width_unit},
        }
    if retention_times is None or fwhms is None:
        return {
            "status": "unavailable",
            "pairs": None,
            "excluded_analytes": [],
            "diagnostics": [_diagnostic("required_pairwise_array_unavailable")],
            "units": {"retention_time": retention_unit, "fwhm": width_unit},
        }

    if (
        isinstance(retention_times, (str, bytes, bytearray, Mapping))
        or not isinstance(retention_times, Sequence)
        or isinstance(fwhms, (str, bytes, bytearray, Mapping))
        or not isinstance(fwhms, Sequence)
    ):
        raise TypeError("retention_times and fwhms must be numeric sequences or None")
    if len(retention_times) != len(fwhms):
        raise ValueError("retention_times and fwhms must have equal lengths")
    count = len(retention_times)
    if count == 0:
        return {
            "status": "no_analytes",
            "pairs": [],
            "excluded_analytes": [],
            "diagnostics": [],
            "units": {"retention_time": retention_unit, "fwhm": width_unit},
        }

    retention, retention_status = _numeric_vector("retention_times", retention_times, count)
    widths, width_status = _numeric_vector("fwhms", fwhms, count)
    assert retention is not None and widths is not None
    orderable: list[tuple[float, int, float | None, str]] = []
    excluded: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    missing_retention = False
    for index, ((retention_value, rt_status), (width_value, width_status_value)) in enumerate(
        zip(retention, widths, strict=True)
    ):
        if rt_status == "non_eluting":
            excluded.append({"analyte_index": index, "status": "non_eluting"})
            continue
        if rt_status != "finite" or retention_value is None:
            excluded.append({"analyte_index": index, "status": rt_status})
            missing_retention = True
            continue
        if width_status_value != "finite" or width_value is None:
            orderable.append((retention_value, index, None, width_status_value))
            excluded.append({"analyte_index": index, "status": f"fwhm_{width_status_value}"})
            continue
        if width_value <= 0:
            raise ValueError(f"fwhms[{index}] must be greater than zero for Gaussian resolution")
        orderable.append((retention_value, index, width_value, "finite"))

    orderable.sort(key=lambda item: (item[0], item[1]))
    pairs: list[dict[str, object]] = []
    for left, right in zip(orderable, orderable[1:]):
        left_time, left_index, left_fwhm, left_width_status = left
        right_time, right_index, right_fwhm, right_width_status = right
        if left_width_status != "finite" or right_width_status != "finite":
            diagnostics.append(
                _diagnostic(
                    "adjacent_pair_missing_fwhm",
                    left_index,
                    other_analyte_index=right_index,
                )
            )
            continue
        assert left_fwhm is not None and right_fwhm is not None
        left_sigma = left_fwhm / _FWHM_PER_SIGMA
        right_sigma = right_fwhm / _FWHM_PER_SIGMA
        sigma_sum = left_sigma + right_sigma
        denominator = 2.0 * sigma_sum
        delta = right_time - left_time
        if not math.isfinite(denominator) or denominator <= 0 or not math.isfinite(delta):
            raise ValueError(
                "Gaussian resolution inputs produce an unrepresentable denominator or time delta"
            )
        resolution = delta / denominator
        if not math.isfinite(resolution):
            raise ValueError("Gaussian resolution is not finite")
        pairs.append(
            {
                "left_analyte_index": left_index,
                "right_analyte_index": right_index,
                "retention_time_delta": delta,
                "resolution": resolution,
                "neighbor_scope": "adjacent_among_observed_finite_retention_times",
            }
        )

    if missing_retention:
        diagnostics.append(_diagnostic("full_set_adjacency_unknown_due_to_missing_retention_time"))
    complete = not missing_retention and not any(item[3] != "finite" for item in orderable)
    return {
        "status": "complete" if complete else "partial",
        "pairs": pairs,
        "excluded_analytes": excluded,
        "diagnostics": diagnostics,
        "units": {"retention_time": retention_unit, "fwhm": width_unit},
        "fwhm_per_sigma": _FWHM_PER_SIGMA,
    }
