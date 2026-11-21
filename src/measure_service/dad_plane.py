"""Project an owned native array into a portable diode-array detector view."""

from __future__ import annotations

from typing import Any

from . import acquisition_store, analysis_runtime
from .storage import canonical, sha256

PROFILE = "measure-dad-plane/1"
MAX_PLANE_BYTES = 2_000_000


def read_dad_plane(
    owner: str, acquisition_id: str, array_id: str, time_axis: int
) -> dict[str, Any]:
    """Read the array as supplied, preserving its point order and source-row mapping."""
    if type(time_axis) is not int or time_axis not in (1, 2):
        raise ValueError("Select native axis 1 or 2 as the time axis")
    files = acquisition_store.acquisition_files(owner, acquisition_id)
    result = analysis_runtime.run_worker(
        {
            "operation": "read_dad_plane",
            "files": files,
            "array_id": array_id,
            "time_axis": time_axis,
        }
    )
    if result["array_id"] != array_id:
        raise ValueError("The worker returned a different native array")
    evidence = {
        "profile": PROFILE,
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
    evidence["evidence_sha256"] = sha256(evidence)
    if len(canonical(evidence).encode("utf-8")) > MAX_PLANE_BYTES:
        raise ValueError("The selected array exceeds the 2 MB portable evidence limit")
    return evidence
