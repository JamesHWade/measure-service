"""Project an owned native array into a portable diode-array detector view."""

from __future__ import annotations

import math
from typing import Any

from . import acquisition_store, analysis_runtime
from .storage import canonical, sha256

PROFILE = "measure-dad-plane/1"
REGION_PROFILE = "measure-dad-region/1"
MAX_PLANE_BYTES = 2_000_000


def validate_range(value: Any, *, wavelength: bool = False) -> list[float] | None:
    """Normalize two inclusive bounds in the units declared by the native axis."""
    if value is None:
        return None
    name = "wavelength_range" if wavelength else "time_range"
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} requires two bounds")
    for bound in value:
        if type(bound) not in (int, float) or not (-math.inf < bound < math.inf):
            raise ValueError(f"{name} requires finite numeric bounds")
    try:
        low, high = map(float, value)
    except OverflowError as error:
        raise ValueError(f"{name} exceeds the native coordinate range") from error
    if low != value[0] or high != value[1]:
        raise ValueError(f"{name} bounds must be representable as native coordinates")
    if low > high or (wavelength and low <= 0):
        raise ValueError(f"Invalid {name}: supply ordered bounds in the declared units")
    return [low, high]


def read_dad_plane(
    owner: str,
    acquisition_id: str,
    array_id: str,
    time_axis: int,
    *,
    time_range: list[float] | None = None,
    wavelength_range: list[float] | None = None,
) -> dict[str, Any]:
    """Read the array as supplied, preserving its point order and source-row mapping."""
    if type(time_axis) is not int or time_axis not in (1, 2):
        raise ValueError("Select native axis 1 or 2 as the time axis")
    selection = {
        "time_range": validate_range(time_range),
        "wavelength_range": validate_range(wavelength_range, wavelength=True),
    }
    ranges = {key: bounds for key, bounds in selection.items() if bounds is not None}
    files = acquisition_store.acquisition_files(owner, acquisition_id)
    result = analysis_runtime.run_worker(
        {
            "operation": "read_dad_plane",
            "files": files,
            "array_id": array_id,
            "time_axis": time_axis,
            **ranges,
        }
    )
    if result["array_id"] != array_id:
        raise ValueError("The worker returned a different native array")
    evidence = {
        "profile": REGION_PROFILE if ranges else PROFILE,
        "acquisition_id": acquisition_id,
        "input_sha256": sha256(files),
        "time_axis": time_axis,
        **{
            key: result[key]
            for key in (
                "array_id",
                "stream_id",
                "injection_id",
                "channel_id",
                "source_id",
                "time_unit",
                "wavelength_unit",
                "value_unit",
                "points",
            )
        },
        "provenance": {
            "worker_sha256": result.worker_sha256,
            "packages": result["packages"],
            "installed_r_code_md5": result["installed_r_code_md5"],
            "r_version": result["r_version"],
        },
        "warnings": result["warnings"],
    }
    if ranges:
        evidence.update(selection=selection, native_point_count=result["native_point_count"])
    evidence["evidence_sha256"] = sha256(evidence)
    if len(canonical(evidence).encode("utf-8")) > MAX_PLANE_BYTES:
        raise ValueError("The selected array exceeds the 2 MB portable evidence limit")
    return evidence
