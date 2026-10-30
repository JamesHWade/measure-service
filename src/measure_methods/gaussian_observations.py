"""Pure ideal-Gaussian centroid resolution observations.

This module compares caller-supplied retention centroids and synthetic FWHM
values. It does not generate traces, add noise, infer widths, model plate
height, or qualify a physical instrument. Resolution uses adjacent finite
eluted centroids under the ``gaussian-baseline-4sigma-finite-centroids/1``
definition.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from typing import Any, Literal

from .chromatography import gaussian_pairwise_resolutions

RESOLUTION_DEFINITION = "gaussian-baseline-4sigma-finite-centroids/1"
_FWHM_PER_SIGMA = 2.3548
_MAX_ANALYTES = 32
_MAX_ANALYTE_ID_LENGTH = 100


def _finite_real(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a finite real number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _positive_real(name: str, value: object) -> float:
    number = _finite_real(name, value)
    if number <= 0.0:
        raise ValueError(f"{name} must be greater than zero")
    return number


@dataclass(frozen=True, slots=True)
class GaussianAnalyte:
    """One supplied ideal retention centroid and synthetic Gaussian width.

    An eluted analyte requires a finite nonnegative retention time. A
    not-eluted analyte omits its retention time; censoring is reported at the
    acquisition duration by :func:`simulate_gaussian_centroids`.
    """

    analyte_id: str
    retention_status: Literal["eluted", "not_eluted"]
    retention_time_min: float | None
    fwhm_min: float

    def __post_init__(self) -> None:
        if not isinstance(self.analyte_id, str):
            raise TypeError("analyte_id must be a string")
        if not self.analyte_id.strip():
            raise ValueError("analyte_id must not be blank")
        if len(self.analyte_id) > _MAX_ANALYTE_ID_LENGTH:
            raise ValueError(f"analyte_id must be at most {_MAX_ANALYTE_ID_LENGTH} characters")
        if self.retention_status not in ("eluted", "not_eluted"):
            raise ValueError("retention_status must be 'eluted' or 'not_eluted'")
        width = _positive_real("fwhm_min", self.fwhm_min)
        object.__setattr__(self, "fwhm_min", width)
        if self.retention_status == "not_eluted":
            if self.retention_time_min is not None:
                raise ValueError("not_eluted analytes must omit retention_time_min")
            return
        if self.retention_time_min is None:
            raise ValueError("eluted analytes require retention_time_min")
        retention_time = _finite_real("retention_time_min", self.retention_time_min)
        if retention_time < 0.0:
            raise ValueError("retention_time_min must be nonnegative")
        object.__setattr__(self, "retention_time_min", retention_time)


def _validate_analytes(analytes: Sequence[GaussianAnalyte]) -> list[GaussianAnalyte]:
    if isinstance(analytes, (str, bytes, bytearray, Mapping)) or not isinstance(analytes, Sequence):
        raise TypeError("analytes must be a sequence of GaussianAnalyte values")
    if not 1 <= len(analytes) <= _MAX_ANALYTES:
        raise ValueError(f"analytes must contain between 1 and {_MAX_ANALYTES} values")
    rows = list(analytes)
    if any(not isinstance(item, GaussianAnalyte) for item in rows):
        raise TypeError("every analyte must be a GaussianAnalyte")
    identifiers = [item.analyte_id for item in rows]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("analyte_id values must be unique")
    return rows


def simulate_gaussian_centroids(
    *,
    analytes: Sequence[GaussianAnalyte],
    acquisition_duration_min: Real,
) -> dict[str, Any]:
    """Return ideal Gaussian resolution summaries for supplied centroids.

    Adjacent pairs are calculated only among finite, eluted centroids, ordered
    by retention time then analyte ID. Pair identity arrays and output rows are
    sorted canonically by ID, independent of input order. Censored analytes
    remain in the cohort and carry a right-censor bound; they are never turned
    into zero-resolution observations.

    Widths are declared synthetic model inputs. This operation does not
    simulate measurements or certify a chromatographic method or instrument.
    """
    duration = _positive_real("acquisition_duration_min", acquisition_duration_min)
    rows = _validate_analytes(analytes)
    for item in rows:
        if (
            item.retention_status == "eluted"
            and item.retention_time_min is not None
            and item.retention_time_min > duration
        ):
            raise ValueError(f"eluted analyte {item.analyte_id!r} exceeds acquisition_duration_min")

    ordered = sorted(rows, key=lambda item: item.analyte_id)
    finite_by_time = sorted(
        (item for item in rows if item.retention_status == "eluted"),
        key=lambda item: (item.retention_time_min, item.analyte_id),
    )
    predicted = gaussian_pairwise_resolutions(
        [item.retention_time_min for item in finite_by_time],
        [item.fwhm_min for item in finite_by_time],
        time_unit="min",
        fwhm_unit="min",
    )
    if finite_by_time and predicted.get("fwhm_per_sigma") != _FWHM_PER_SIGMA:
        raise ValueError("Gaussian helper conversion differs from the declared producer profile")

    per_analyte_resolution: dict[str, float] = {}
    pairs: list[dict[str, object]] = []
    for pair in predicted["pairs"] or []:
        left_index = pair["left_analyte_index"]
        right_index = pair["right_analyte_index"]
        left_id = finite_by_time[left_index].analyte_id
        right_id = finite_by_time[right_index].analyte_id
        delta = pair["retention_time_delta"]
        resolution = pair["resolution"]
        if delta < 0.0 or resolution < 0.0 or not math.isfinite(resolution):
            raise ValueError("Gaussian pair calculation returned an invalid resolution")
        if delta > 0.0 and resolution == 0.0:
            raise OverflowError("positive retention-time separation underflowed to zero resolution")
        if delta == 0.0 and resolution != 0.0:
            raise ValueError("exactly tied retention times must have zero resolution")
        per_analyte_resolution[left_id] = min(
            resolution,
            per_analyte_resolution.get(left_id, math.inf),
        )
        per_analyte_resolution[right_id] = min(
            resolution,
            per_analyte_resolution.get(right_id, math.inf),
        )
        pairs.append(
            {
                "analyte_ids": sorted((left_id, right_id)),
                "resolution_status": "observed",
                "resolution": resolution,
                "right_censor_bound_min": None,
            }
        )
    pairs.sort(key=lambda pair: tuple(pair["analyte_ids"]))

    analyte_results: list[dict[str, object]] = []
    for item in ordered:
        if item.retention_status == "not_eluted":
            resolution_status = "censored"
            resolution = None
            resolution_reason = "retention_not_eluted_within_acquisition_duration"
            right_censor_bound = duration
        elif item.analyte_id in per_analyte_resolution:
            resolution_status = "observed"
            resolution = per_analyte_resolution[item.analyte_id]
            resolution_reason = None
            right_censor_bound = None
        else:
            resolution_status = "unsupported"
            resolution = None
            resolution_reason = "no_adjacent_finite_centroid"
            right_censor_bound = None
        analyte_results.append(
            {
                "analyte_id": item.analyte_id,
                "retention_status": item.retention_status,
                "retention_time_min": item.retention_time_min,
                "fwhm_min": item.fwhm_min,
                "resolution_status": resolution_status,
                "resolution": resolution,
                "right_censor_bound_min": right_censor_bound,
                "resolution_reason": resolution_reason,
            }
        )

    return {
        "profile": "ideal-gaussian-centroid/1",
        "resolution_definition": RESOLUTION_DEFINITION,
        "fwhm_per_sigma": _FWHM_PER_SIGMA,
        "acquisition_duration_min": duration,
        "analytes": analyte_results,
        "pairs": pairs,
        "scope": {
            "adjacency": "adjacent_finite_eluted_centroids_ordered_by_retention_time_then_analyte_id",
            "analyte_resolution": "minimum_of_incident_adjacent_pair_resolutions",
            "widths": "caller_supplied_synthetic_fwhm; fwhm_per_sigma_approximation_2.3548",
            "interpretation": "ideal_latent_model_only_not_trace_simulation_or_physical_qualification",
        },
    }
