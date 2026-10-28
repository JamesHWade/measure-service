"""Qualify reconstructed MoSeS scores against an integrity-checked replay."""

from __future__ import annotations

import csv
import hashlib
import importlib
import json
import math
import os
import platform
import re
import shutil
import stat
import tempfile
from collections import Counter
from dataclasses import dataclass
from importlib import metadata
from itertools import chain
from pathlib import Path
from typing import Any

from measure_methods.moses_objective import (
    MOSES_SCORE_PROFILES,
    moses_observation_score,
    moses_summary_score,
)

from .benchmarks import moses_replay
from .benchmarks.moses_archive import inspect_archive, read_member
from .benchmarks.pickle_records import decode_archive_record

_REPLAY_FILES = {
    "archive-manifest.json",
    "campaigns.csv",
    "observations.csv",
    "report.json",
    "report.md",
    "success-trajectories.csv",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ABS_TOLERANCE = 1e-10
_REL_TOLERANCE = 1e-10
_SCORE_POLICIES = ("raw_resolution_array", "serialized_summary_hypothesis")
_COMPOUND_SET_MEMBER = re.compile(
    r"results/compound_sets/(\d+)x(\d+)C_20min_(\d+)/compound_set_(\d+)\.json"
)
_OBJECTIVE_MODULES = (
    "measure_methods.objectives",
    "measure_methods.moses_objective",
)


@dataclass(frozen=True)
class _SourceObservation:
    expected_replay_row: dict[str, Any]
    observation: dict[str, Any]
    config: dict[str, Any]
    campaign_id: str
    configuration_id: str
    source_member: str
    source_sha256: str
    sequence: int
    analyte_set_index: int | None
    repeat_index: int


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, *, maximum_bytes: int = 64 * 1024 * 1024) -> Any:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
        raise ValueError(f"replay metadata is not a bounded regular file: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid replay JSON file: {path.name}") from error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _software_metadata() -> dict[str, Any]:
    replay_software = moses_replay._implementation_metadata()
    return {
        "distribution": "measure-service",
        "version": metadata.version("measure-service"),
        "python": platform.python_version(),
        "replay_software": replay_software,
        "replay_module_sha256": _sha256_file(Path(moses_replay.__file__)),
        "qualification_module_sha256": _sha256_file(Path(__file__)),
        "score_kernel_sha256": {
            name: _sha256_file(Path(importlib.import_module(name).__file__))
            for name in _OBJECTIVE_MODULES
        },
    }


def _verify_replay_artifacts(
    replay_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        root_metadata = replay_dir.lstat()
    except FileNotFoundError as error:
        raise ValueError("replay directory does not exist") from error
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("replay directory must be a real directory")

    complete_path = replay_dir / "complete.json"
    complete = _load_json(complete_path, maximum_bytes=16 * 1024 * 1024)
    if not isinstance(complete, dict) or complete.get("schema_version") != 1:
        raise ValueError("unsupported replay complete.json schema")
    files = complete.get("files")
    if not isinstance(files, dict) or set(files) != _REPLAY_FILES:
        raise ValueError("replay complete.json must list every v1 evidence file exactly once")
    directory_entries = list(replay_dir.iterdir())
    required_entries = _REPLAY_FILES | {"complete.json"}
    if not required_entries <= {item.name for item in directory_entries}:
        raise ValueError("replay directory is missing required core evidence files")
    for item in directory_entries:
        metadata = item.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("replay directory may contain only regular core or optional files")
        if item.name not in required_entries and (
            Path(item.name).name != item.name or item.name in {".", ".."}
        ):
            raise ValueError("replay directory contains an unsafe optional filename")

    for name, record in files.items():
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("replay manifest contains an unsafe file name")
        if not isinstance(record, dict):
            raise ValueError(f"replay checksum entry for {name!r} must be an object")
        expected_digest = record.get("sha256")
        expected_size = record.get("size_bytes")
        if not isinstance(expected_digest, str) or not _SHA256.fullmatch(expected_digest):
            raise ValueError(f"replay checksum for {name!r} is invalid")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise ValueError(f"replay size for {name!r} is invalid")
        path = replay_dir / name
        try:
            metadata = path.lstat()
        except FileNotFoundError as error:
            raise ValueError(f"replay evidence file is missing: {name}") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"replay evidence file must be a regular file: {name}")
        if metadata.st_size != expected_size:
            raise ValueError(f"replay evidence size mismatch: {name}")
        if _sha256_file(path) != expected_digest:
            raise ValueError(f"replay evidence SHA256 mismatch: {name}")

    replay_report = _load_json(replay_dir / "report.json")
    archived_manifest = _load_json(replay_dir / "archive-manifest.json")
    if not isinstance(replay_report, dict) or replay_report.get("schema_version") != 1:
        raise ValueError("unsupported replay report schema")
    if not isinstance(archived_manifest, dict) or archived_manifest.get("schema_version") != 1:
        raise ValueError("unsupported archived ZIP manifest schema")
    return complete, replay_report, archived_manifest


def _validate_output_target(output_dir: Path, replay_dir: Path) -> None:
    output_resolved = output_dir.resolve()
    replay_resolved = replay_dir.resolve()
    if (
        output_resolved == replay_resolved
        or output_resolved.is_relative_to(replay_resolved)
        or replay_resolved.is_relative_to(output_resolved)
    ):
        raise ValueError("qualification output must be separate from the original replay directory")

    if output_dir.exists() or output_dir.is_symlink():
        metadata = output_dir.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("output path must be a directory")
        if any(output_dir.iterdir()):
            raise ValueError("output directory must be empty; existing results are preserved")


def _validate_report_source(
    report: dict[str, Any],
    archived_manifest: dict[str, Any],
    current_manifest: dict[str, Any],
) -> None:
    if report.get("profile") != moses_replay.PROFILE:
        raise ValueError("replay report profile is not the supported archived-observation profile")
    if archived_manifest != current_manifest:
        raise ValueError("source ZIP manifest or digest differs from the verified replay source")
    if report.get("archive_source") != current_manifest.get("source"):
        raise ValueError("replay report source digest does not match its archive manifest")
    archive = current_manifest.get("archive")
    if not isinstance(archive, dict):
        raise ValueError("verified source ZIP manifest is missing archive metadata")
    expected_summary = {key: value for key, value in archive.items() if key != "members"}
    if report.get("archive_summary") != expected_summary:
        raise ValueError("replay report archive summary does not match its source manifest")


def _json_bytes_value(value: Any) -> Any:
    if not isinstance(value, (dict, list)):
        return value
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _csv_cell(value: Any) -> str:
    serialized = _json_bytes_value(value)
    if serialized is None:
        return ""
    return str(serialized)


def _source_score(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("archived source score is not numeric")
    result = float(value)
    return result if math.isfinite(result) else None


def _archive_analyte_sets(
    archive_path: Path, members: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result = []
    for item in members:
        name = item["name"]
        match = _COMPOUND_SET_MEMBER.fullmatch(name)
        if match is None:
            continue
        payload = read_member(archive_path, name)
        compounds = json.loads(payload, object_pairs_hook=_unique_pairs)
        if not isinstance(compounds, list) or len(compounds) != int(match[2]):
            raise ValueError("analyte set length disagrees with its declared cohort")
        result.append(
            {
                "source_member": name,
                "source_sha256": hashlib.sha256(payload).hexdigest(),
                "cohort_size": int(match[1]),
                "num_compounds": int(match[2]),
                "compound_set_hash": match[3],
                "analyte_set_index": int(match[4]),
            }
        )
    return result


def _required_config_value(config: dict[str, Any], name: str) -> Any:
    if name not in config:
        raise ValueError(f"scoring config is missing required field {name!r}")
    return config[name]


def _validate_scoring_config(config: Any) -> dict[str, Any]:
    config = moses_replay._object(config, "campaign config")
    num_compounds = moses_replay._integer(
        _required_config_value(config, "num_compounds"), "config num_compounds"
    )
    objective = _required_config_value(config, "objective")
    if not isinstance(objective, str):
        raise ValueError("config objective must be a supported string")
    max_run_time = moses_replay._number(
        _required_config_value(config, "max_run_time"), "config max_run_time", minimum=1
    )
    count_threshold = moses_replay._number(
        _required_config_value(config, "count_threshold"), "config count_threshold"
    )
    resolution_threshold = moses_replay._number(
        _required_config_value(config, "resolution_threshold"), "config resolution_threshold"
    )
    run_time_threshold = moses_replay._number(
        _required_config_value(config, "run_time_threshold"), "config run_time_threshold", minimum=1
    )
    segment_based = _required_config_value(config, "segment_based")
    if not isinstance(segment_based, bool):
        raise ValueError("config segment_based must be a boolean")
    compound_idx = config.get("compound_idx")

    # Let the method kernel validate the profile-specific objective thresholds.
    # A missing input array returns an explicit unavailable result and never
    # manufactures a score from these validation-only arguments.
    for profile in MOSES_SCORE_PROFILES:
        check = moses_observation_score(
            profile=profile,
            num_compounds=num_compounds,
            peak_resolutions=None,
            run_time_min=0.0,
            max_run_time_min=max_run_time,
            objective=objective,
            compound_idx=compound_idx,
            count_threshold=count_threshold,
            resolution_threshold=resolution_threshold,
            run_time_threshold_min=run_time_threshold,
        )
        if check.get("status") != "unavailable" or check.get("score") is not None:
            raise ValueError("score config validation unexpectedly produced a score")

    return {
        "num_compounds": num_compounds,
        "objective": objective,
        "compound_idx": compound_idx,
        "max_run_time_min": max_run_time,
        "count_threshold": count_threshold,
        "resolution_threshold": resolution_threshold,
        "run_time_threshold_min": run_time_threshold,
        "segment_based": segment_based,
    }


def _runtime_details(observation: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    gradient = moses_replay._object(observation.get("gradient"), "gradient")
    declared = moses_replay._number(gradient.get("run_time"), "gradient run_time")
    details = {
        "declared_run_time_min": declared,
        "derived_run_time_min": None,
        "runtime_derivation": None,
        "runtime_abs_error_min": None,
        "runtime_within_tolerance": None,
        "runtime_status": "unavailable",
    }

    header = gradient.get("header_gradient_table")
    table = gradient.get("gradient_table")
    if not isinstance(header, (list, tuple)) or not isinstance(table, (list, tuple)):
        details["runtime_reason"] = "gradient_time_table_unavailable"
        return details
    if header.count("time") != 1:
        details["runtime_reason"] = "gradient_time_column_unavailable_or_ambiguous"
        return details
    if len(table) < 2:
        details["runtime_reason"] = "gradient_time_table_has_fewer_than_two_rows"
        return details

    time_index = header.index("time")
    times = []
    for index, row in enumerate(table):
        if not isinstance(row, (list, tuple)) or len(row) != len(header):
            raise ValueError("gradient table rows must match their declared header")
        times.append(moses_replay._number(row[time_index], f"gradient time row {index}"))
    if times[0] != 0.0 or any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError("gradient time points must start at zero and increase strictly")

    if config["segment_based"]:
        durations = [_round_segment_duration(right - left) for left, right in zip(times, times[1:])]
        if any(duration <= 0.0 for duration in durations):
            raise ValueError("rounded gradient segment durations must remain positive")
        derived = math.fsum(durations)
        derivation = "sum_of_rounded_4_decimal_segment_durations"
    else:
        derived = times[-1]
        derivation = "last_gradient_time_endpoint"
    if not math.isfinite(derived):
        raise ValueError("derived scoring runtime is not finite")
    difference = abs(declared - derived)
    agrees = math.isclose(declared, derived, rel_tol=_REL_TOLERANCE, abs_tol=_ABS_TOLERANCE)
    details.update(
        derived_run_time_min=derived,
        runtime_derivation=derivation,
        runtime_abs_error_min=difference,
        runtime_within_tolerance=agrees,
        runtime_status="agrees" if agrees else "disagrees",
    )
    return details


def _round_segment_duration(duration: float) -> float:
    """Match NumPy's decimal rounding path used by the source parameterizer."""
    scaled = duration * 10_000.0
    if not math.isfinite(scaled):
        raise ValueError("gradient segment duration cannot be rounded to four decimals")
    return round(scaled) / 10_000.0


def _iter_source_observations(
    archive_path: Path,
    manifest: dict[str, Any],
    audit: dict[str, Any],
):
    members = manifest["archive"]["members"]
    audit["analyte_sets"] = _archive_analyte_sets(archive_path, members)
    selected = [
        item["name"]
        for item in members
        if ("/in_silico_bo_benchmarks/" in item["name"] and item["name"].endswith("/results.pkl"))
        or (
            item["name"].endswith("/campaign_summary.json")
            and any(
                part in item["name"]
                for part in ("/known_mixture_optimization/", "/laser_optimization/")
            )
        )
    ]
    if not selected:
        raise ValueError("archive has no supported benchmark results or campaign summaries")

    for member in selected:
        payload = read_member(archive_path, member)
        digest = hashlib.sha256(payload).hexdigest()
        simulated = member.endswith(".pkl")
        record = decode_archive_record(payload) if simulated else moses_replay._json_object(payload)
        record = moses_replay._object(record, "record")
        audit["record_sources"].append(
            {
                "member": member,
                "sha256": digest,
                "size_bytes": len(payload),
                "format": "data-only-pickle" if simulated else "json",
            }
        )
        group_id = member.rsplit("/", 1)[0]

        if simulated:
            cohort = moses_replay._object(record.get("config"), "benchmark config")
            matching = [
                analyte_set
                for analyte_set in audit["analyte_sets"]
                if analyte_set["compound_set_hash"] == str(cohort.get("compound_set_hash"))
                and analyte_set["num_compounds"] == cohort.get("num_compounds")
            ]
            input_sizes = {analyte_set["cohort_size"] for analyte_set in matching}
            if len(input_sizes) != 1:
                raise ValueError("benchmark input cohort is missing or ambiguous")
            input_size = input_sizes.pop()
            if {item["analyte_set_index"] for item in matching} != set(range(input_size)):
                raise ValueError("benchmark lacks the complete declared archived analyte set")
            campaigns_by_id, extra_sources = moses_replay._reconcile_campaign_records(
                record,
                group_id,
                {item["name"] for item in members},
                lambda name: read_member(archive_path, name),
            )
            audit["record_sources"].extend(extra_sources)
            source_digests = {item["member"]: item["sha256"] for item in audit["record_sources"]}
            repeats = moses_replay._integer(
                cohort.get("iterations_per_compound_set"), "iterations_per_compound_set"
            )
            if any(index >= input_size or repeat >= repeats for index, repeat in campaigns_by_id):
                raise ValueError(
                    "individual campaign identity is outside the archived input cohort"
                )
            if (
                all(
                    item[2] == "serialized_complete_cohort_order"
                    for item in campaigns_by_id.values()
                )
                and len(campaigns_by_id) != input_size * repeats
            ):
                raise ValueError("unlabelled grouped identities require a complete input cohort")
            campaigns = [
                (index, repeat, *item) for (index, repeat), item in sorted(campaigns_by_id.items())
            ]
        else:
            campaigns = [(0, 0, record, member, "experimental_summary")]
            source_digests = {member: digest}

        for analyte_set_index, repeat_index, campaign, chosen_source, _snapshot_status in campaigns:
            campaign_id = f"{group_id}/set-{analyte_set_index}/repeat-{repeat_index}"
            if chosen_source not in source_digests:
                raise ValueError("reconciled campaign source is absent from the source digest list")
            config = _validate_scoring_config(campaign.get("config"))
            result = moses_replay.replay_campaign(
                campaign,
                campaign_id=campaign_id,
                source_member=chosen_source,
                source_sha256=source_digests[chosen_source],
                observation_origin=(
                    "simulator-latent-reference" if simulated else "archived-instrument-analysis"
                ),
            )
            replay_rows = result.pop("observations")
            observations = campaign.get("experiments")
            if not isinstance(observations, list) or len(observations) != len(replay_rows):
                raise ValueError("source campaign observations cannot be reconciled to replay rows")
            for sequence, (observation, replay_row) in enumerate(zip(observations, replay_rows), 1):
                yield _SourceObservation(
                    expected_replay_row=replay_row,
                    observation=moses_replay._object(observation, "experiment"),
                    config=config,
                    campaign_id=campaign_id,
                    configuration_id=group_id,
                    source_member=chosen_source,
                    source_sha256=source_digests[chosen_source],
                    sequence=sequence,
                    analyte_set_index=analyte_set_index if simulated else None,
                    repeat_index=repeat_index,
                )


def _summary_candidate(
    profile: str,
    source_observation: _SourceObservation,
    runtime: float,
) -> dict[str, Any]:
    config = source_observation.config
    observation = source_observation.observation
    objective = config["objective"]
    if objective == "SinglePeak-Resolution":
        return {
            "profile": profile,
            "input_policy": "serialized_summary_hypothesis",
            "status": "unavailable",
            "score": None,
            "reason": "targeted objective requires the selected raw resolution entry",
        }

    detected = observation.get("num_peaks")
    critical = observation.get("critical_resolution")
    resolved = observation.get("num_resolved_peaks")
    if isinstance(detected, bool) or not isinstance(detected, int):
        return {
            "profile": profile,
            "input_policy": "serialized_summary_hypothesis",
            "status": "unavailable",
            "score": None,
            "reason": "serialized num_peaks summary is unavailable or invalid",
        }
    if (
        isinstance(critical, bool)
        or not isinstance(critical, (int, float))
        or not math.isfinite(critical)
    ):
        return {
            "profile": profile,
            "input_policy": "serialized_summary_hypothesis",
            "status": "unavailable",
            "score": None,
            "reason": "serialized critical_resolution summary is unavailable or invalid",
        }
    if objective == "NumPeaks-NumResPeaks" and (
        isinstance(resolved, bool) or not isinstance(resolved, int)
    ):
        return {
            "profile": profile,
            "input_policy": "serialized_summary_hypothesis",
            "status": "unavailable",
            "score": None,
            "reason": "serialized num_resolved_peaks summary is unavailable or invalid",
        }
    return moses_summary_score(
        profile=profile,
        num_compounds=config["num_compounds"],
        detected_count=detected,
        critical_resolution=critical,
        resolved_count=resolved if objective == "NumPeaks-NumResPeaks" else None,
        run_time_min=runtime,
        max_run_time_min=config["max_run_time_min"],
        objective=objective,
        count_threshold=config["count_threshold"],
        resolution_threshold=config["resolution_threshold"],
        run_time_threshold_min=config["run_time_threshold_min"],
    )


def _score_comparison(archived: float | None, candidate: dict[str, Any]) -> dict[str, Any]:
    candidate_score = candidate.get("score")
    if archived is None or candidate_score is None:
        return {
            "absolute_error": None,
            "relative_error": None,
            "within_tolerance": None,
            "classification_disagreement": None,
        }
    difference = abs(candidate_score - archived)
    if not math.isfinite(difference):
        return {
            "absolute_error": None,
            "relative_error": None,
            "within_tolerance": False,
            "classification_disagreement": (candidate_score >= 2.0) != (archived >= 2.0),
            "comparison_status": "absolute_error_not_representable",
        }
    relative = difference / abs(archived) if archived != 0 else (0.0 if difference == 0 else None)
    return {
        "absolute_error": difference,
        "relative_error": relative if relative is None or math.isfinite(relative) else None,
        "within_tolerance": math.isclose(
            candidate_score,
            archived,
            rel_tol=_REL_TOLERANCE,
            abs_tol=_ABS_TOLERANCE,
        ),
        "classification_disagreement": (candidate_score >= 2.0) != (archived >= 2.0),
    }


def _safe_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Retain score decomposition and metrics without copying input arrays."""
    keys = (
        "profile",
        "input_policy",
        "status",
        "reason",
        "score",
        "criteria",
        "detected_count",
        "resolved_count",
        "critical_resolution",
        "analytical_thresholds_satisfied",
        "all_objective_thresholds_satisfied",
        "score_at_least_two",
        "resolution_status_counts",
    )
    return {key: candidate[key] for key in keys if key in candidate}


def _qualify_row(source: _SourceObservation, runtime: dict[str, Any]) -> list[dict[str, Any]]:
    config = source.config
    archived = _source_score(source.expected_replay_row["archived_score"])
    raw_resolution = source.observation.get("peak_resolutions")
    raw_scores = {}
    for profile in MOSES_SCORE_PROFILES:
        if runtime["derived_run_time_min"] is None:
            raw_scores[profile] = {
                "profile": profile,
                "input_policy": "raw_resolution_array",
                "status": "unavailable",
                "score": None,
                "reason": runtime.get("runtime_reason", "derived scoring runtime is unavailable"),
            }
        else:
            raw_scores[profile] = moses_observation_score(
                profile=profile,
                num_compounds=config["num_compounds"],
                peak_resolutions=raw_resolution,
                run_time_min=runtime["derived_run_time_min"],
                max_run_time_min=config["max_run_time_min"],
                objective=config["objective"],
                compound_idx=config["compound_idx"],
                count_threshold=config["count_threshold"],
                resolution_threshold=config["resolution_threshold"],
                run_time_threshold_min=config["run_time_threshold_min"],
            )
        raw_scores[profile].setdefault("input_policy", "raw_resolution_array")

    raw_reference = next(iter(raw_scores.values()))
    raw_analytical = raw_reference.get("analytical_thresholds_satisfied")
    raw_all_thresholds = raw_reference.get("all_objective_thresholds_satisfied")

    results = []
    for profile in MOSES_SCORE_PROFILES:
        raw_candidate = raw_scores[profile]
        if runtime["derived_run_time_min"] is None:
            summary_candidate = {
                "profile": profile,
                "input_policy": "serialized_summary_hypothesis",
                "status": "unavailable",
                "score": None,
                "reason": runtime.get("runtime_reason", "derived scoring runtime is unavailable"),
            }
        else:
            summary_candidate = _summary_candidate(profile, source, runtime["derived_run_time_min"])

        raw_metrics = raw_candidate if raw_candidate.get("status") == "scored" else {}
        summary_metrics = summary_candidate if summary_candidate.get("status") == "scored" else {}
        detected_disagreement = (
            raw_metrics.get("detected_count") != summary_metrics.get("detected_count")
            if "detected_count" in raw_metrics and "detected_count" in summary_metrics
            else None
        )
        critical_difference = None
        if "critical_resolution" in raw_metrics and "critical_resolution" in summary_metrics:
            critical_difference = abs(
                raw_metrics["critical_resolution"] - summary_metrics["critical_resolution"]
            )
        resolved_disagreement = (
            raw_metrics.get("resolved_count") != summary_metrics.get("resolved_count")
            if "resolved_count" in raw_metrics and "resolved_count" in summary_metrics
            else None
        )

        for candidate in (raw_candidate, summary_candidate):
            comparison = _score_comparison(archived, candidate)
            candidate_class = (
                candidate["score"] >= 2.0 if candidate.get("score") is not None else None
            )
            analytical = candidate.get("analytical_thresholds_satisfied")
            all_thresholds = candidate.get("all_objective_thresholds_satisfied")
            hard_disagreement = (
                (archived >= 2.0) != analytical
                if archived is not None and isinstance(analytical, bool)
                else None
            )
            candidate_hard_disagreement = (
                candidate_class != analytical
                if isinstance(candidate_class, bool) and isinstance(analytical, bool)
                else None
            )
            results.append(
                {
                    "campaign_id": source.campaign_id,
                    "configuration_id": source.configuration_id,
                    "sequence": source.sequence,
                    "source_member": source.source_member,
                    "source_sha256": source.source_sha256,
                    "analyte_set_index": source.analyte_set_index,
                    "repeat_index": source.repeat_index,
                    "objective": config["objective"],
                    "profile": candidate["profile"],
                    "input_policy": candidate["input_policy"],
                    "status": candidate.get("status"),
                    "reason": candidate.get("reason"),
                    "arithmetic": "binary64",
                    "absolute_tolerance": _ABS_TOLERANCE,
                    "relative_tolerance": _REL_TOLERANCE,
                    "archived_score": archived,
                    "candidate_score": candidate.get("score"),
                    **comparison,
                    "archived_score_at_least_two": archived >= 2.0
                    if archived is not None
                    else None,
                    "candidate_score_at_least_two": candidate_class,
                    "score_classification_disagreement": comparison["classification_disagreement"],
                    "analytical_thresholds_satisfied": analytical,
                    "all_objective_thresholds_satisfied": all_thresholds,
                    "archived_vs_analytical_threshold_disagreement": hard_disagreement,
                    "candidate_vs_analytical_threshold_disagreement": candidate_hard_disagreement,
                    "raw_analytical_thresholds_satisfied": raw_analytical,
                    "raw_all_objective_thresholds_satisfied": raw_all_thresholds,
                    "archived_vs_raw_analytical_threshold_disagreement": (
                        (archived >= 2.0) != raw_analytical
                        if archived is not None and isinstance(raw_analytical, bool)
                        else None
                    ),
                    "archived_vs_raw_all_objectives_threshold_disagreement": (
                        (archived >= 2.0) != raw_all_thresholds
                        if archived is not None and isinstance(raw_all_thresholds, bool)
                        else None
                    ),
                    "declared_run_time_min": runtime["declared_run_time_min"],
                    "derived_run_time_min": runtime["derived_run_time_min"],
                    "runtime_derivation": runtime["runtime_derivation"],
                    "runtime_abs_error_min": runtime["runtime_abs_error_min"],
                    "runtime_within_tolerance": runtime["runtime_within_tolerance"],
                    "runtime_status": runtime["runtime_status"],
                    "runtime_reason": runtime.get("runtime_reason"),
                    "raw_array_detected_count": raw_metrics.get("detected_count"),
                    "serialized_detected_count": summary_metrics.get("detected_count"),
                    "raw_vs_serialized_detected_count_disagreement": detected_disagreement,
                    "raw_array_critical_resolution": raw_metrics.get("critical_resolution"),
                    "serialized_critical_resolution": summary_metrics.get("critical_resolution"),
                    "raw_vs_serialized_critical_resolution_abs_difference": critical_difference,
                    "raw_array_resolved_count": raw_metrics.get("resolved_count"),
                    "serialized_resolved_count": summary_metrics.get("resolved_count"),
                    "raw_vs_serialized_resolved_count_disagreement": resolved_disagreement,
                    "candidate_criteria": candidate.get("criteria"),
                    "candidate_metrics": _safe_candidate(candidate),
                }
            )
    return results


def _empty_aggregate() -> dict[str, Any]:
    return {
        "row_count": 0,
        "scored_count": 0,
        "comparable_count": 0,
        "within_tolerance_count": 0,
        "score_classification_disagreement_count": 0,
        "archived_vs_analytical_threshold_disagreement_count": 0,
        "candidate_vs_analytical_threshold_disagreement_count": 0,
        "archived_vs_raw_analytical_threshold_disagreement_count": 0,
        "archived_vs_raw_all_objectives_threshold_disagreement_count": 0,
        "max_absolute_error": None,
        "max_relative_error": None,
    }


def _update_aggregate(aggregate: dict[str, Any], row: dict[str, Any]) -> None:
    aggregate["row_count"] += 1
    if row["candidate_score"] is not None:
        aggregate["scored_count"] += 1
    if row["absolute_error"] is not None:
        aggregate["comparable_count"] += 1
        if row["within_tolerance"]:
            aggregate["within_tolerance_count"] += 1
        current = aggregate["max_absolute_error"]
        aggregate["max_absolute_error"] = (
            row["absolute_error"] if current is None else max(current, row["absolute_error"])
        )
    if row["relative_error"] is not None:
        current = aggregate["max_relative_error"]
        aggregate["max_relative_error"] = (
            row["relative_error"] if current is None else max(current, row["relative_error"])
        )
    for source, target in (
        ("score_classification_disagreement", "score_classification_disagreement_count"),
        (
            "archived_vs_analytical_threshold_disagreement",
            "archived_vs_analytical_threshold_disagreement_count",
        ),
        (
            "candidate_vs_analytical_threshold_disagreement",
            "candidate_vs_analytical_threshold_disagreement_count",
        ),
        (
            "archived_vs_raw_analytical_threshold_disagreement",
            "archived_vs_raw_analytical_threshold_disagreement_count",
        ),
        (
            "archived_vs_raw_all_objectives_threshold_disagreement",
            "archived_vs_raw_all_objectives_threshold_disagreement_count",
        ),
    ):
        if row[source] is True:
            aggregate[target] += 1


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# MoSeS archived score qualification",
        "",
        "This report compares reconstructed binary64 scores with archived scores from a checksum-verified replay. It does not rerun optimization or claim manuscript parity.",
        "",
        f"- Source ZIP SHA256: `{report['source_zip_sha256']}`",
        f"- Matched observations: {report['observation_count']}",
        f"- Comparison tolerance: absolute {report['tolerance']['absolute']:.1e}, relative {report['tolerance']['relative']:.1e}",
        "- Each observation is evaluated under both named score profiles and both input policies.",
        "- `raw_resolution_array` uses per-analyte raw resolution values. `serialized_summary_hypothesis` separately evaluates reported `num_peaks`, `critical_resolution`, and, when applicable, `num_resolved_peaks`; it does not establish which inputs produced the archived score.",
        "- Targeted single-analyte objectives have no serialized-summary candidate. Missing raw resolution arrays remain unscorable under the raw-array policy.",
        "- The derived CSV contains metrics and score decompositions only; it does not copy source arrays.",
        "",
        "## Score comparisons",
        "",
        "| Profile | Input policy | Compared | Within tolerance | Max absolute error | Score ≥2 disagreements | Archived vs analytical threshold disagreements |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for profile in report["profiles"]:
        for policy in report["input_policies"]:
            aggregate = report["aggregates"][profile][policy]
            error = aggregate["max_absolute_error"]
            lines.append(
                f"| {profile} | {policy} | {aggregate['comparable_count']} | {aggregate['within_tolerance_count']} | {error if error is not None else '—'} | {aggregate['score_classification_disagreement_count']} | {aggregate['archived_vs_analytical_threshold_disagreement_count']} |"
            )
    hard = report["raw_hard_threshold_confusion"]
    runtime = report["runtime_diagnostics"]
    lines.extend(
        [
            "",
            "## Archived score versus raw hard acceptance",
            "",
            "The following counts compare the archived `score >= 2` classification with the raw-array `all_objective_thresholds_satisfied` flag, which includes the runtime threshold. This separates input-policy disagreement from differences between a smooth score and hard acceptance.",
            "",
            f"- Archived below 2 and raw hard false: {hard['true_negative_archived_below_two_raw_hard_false']}",
            f"- Archived at least 2 and raw hard true: {hard['true_positive_archived_at_least_two_raw_hard_true']}",
            f"- Archived at least 2 and raw hard false: {hard['false_positive_archived_at_least_two_raw_hard_false']}",
            f"- Archived below 2 and raw hard true: {hard['false_negative_archived_below_two_raw_hard_true']}",
            "",
            "## Runtime diagnostics",
            "",
            f"Derived runtime agrees with the replay's declared `run_time` for {runtime['agree_count']} rows, differs for {runtime['disagree_count']}, and is unavailable for {runtime['unavailable_count']}. Time-based records use the final gradient time endpoint; segment-based records sum durations rounded to four decimal places before scoring.",
            "",
            "## Input discrepancies",
            "",
            f"For non-targeted objectives with both inputs available, serialized `critical_resolution` differs from the minimum finite raw resolution array for {report['raw_summary_disagreements']['critical_resolution_disagreement_count']} observations. Serialized and raw detected counts differ for {report['raw_summary_disagreements']['detected_count_disagreement_count']} observations.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )


def qualify_moses_scores(
    archive_path: str | os.PathLike[str],
    replay_dir: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    """Verify a replay, reconnect every row to its source, and qualify scores.

    The replay's ``complete.json`` hashes, complete source ZIP manifest and
    archive SHA256 are verified before observations are matched. Every CSV
    observation cell must match a row regenerated from the corresponding
    safely decoded source record. Qualification scores are emitted for both
    named BoTier profiles and two explicit input hypotheses. Existing replay
    files are never modified.
    """
    archive = Path(archive_path)
    replay = Path(replay_dir)
    target = Path(output_dir)
    _validate_output_target(target, replay)

    complete_before, replay_report, prior_manifest = _verify_replay_artifacts(replay)
    source_manifest = inspect_archive(archive)
    _validate_report_source(replay_report, prior_manifest, source_manifest)
    software = _software_metadata()

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".moses-score-qualification-", dir=target.parent))
    aggregates = {
        profile: {policy: _empty_aggregate() for policy in _SCORE_POLICIES}
        for profile in MOSES_SCORE_PROFILES
    }
    runtime_statuses: Counter[str] = Counter()
    raw_hard_confusion: Counter[tuple[bool, bool]] = Counter()
    raw_analytical_confusion: Counter[tuple[bool, bool]] = Counter()
    raw_summary_disagreements: Counter[str] = Counter()
    runtime_max_error = None
    observation_count = 0
    campaign_ids: set[str] = set()
    configuration_ids: set[str] = set()
    audit: dict[str, Any] = {"record_sources": [], "analyte_sets": []}

    try:
        with (replay / "observations.csv").open("r", newline="", encoding="utf-8") as source_csv:
            reader = csv.reader(source_csv, strict=True)
            header = next(reader, None)
            if not header or len(header) != len(set(header)):
                raise ValueError("replay observations.csv has a missing or duplicate header")

            source_rows = iter(_iter_source_observations(archive, source_manifest, audit))
            first_source = next(source_rows, None)
            if first_source is None:
                raise ValueError("source archive produced no observation rows")
            expected_header = list(first_source.expected_replay_row)
            if header != expected_header:
                raise ValueError(
                    "replay observations.csv columns differ from regenerated source rows"
                )

            output_path = stage / "observations.csv"
            with output_path.open("w", newline="", encoding="utf-8") as output_csv:
                fieldnames = [
                    "campaign_id",
                    "configuration_id",
                    "sequence",
                    "source_member",
                    "source_sha256",
                    "analyte_set_index",
                    "repeat_index",
                    "objective",
                    "profile",
                    "input_policy",
                    "status",
                    "reason",
                    "arithmetic",
                    "absolute_tolerance",
                    "relative_tolerance",
                    "archived_score",
                    "candidate_score",
                    "absolute_error",
                    "relative_error",
                    "within_tolerance",
                    "comparison_status",
                    "archived_score_at_least_two",
                    "candidate_score_at_least_two",
                    "score_classification_disagreement",
                    "analytical_thresholds_satisfied",
                    "all_objective_thresholds_satisfied",
                    "archived_vs_analytical_threshold_disagreement",
                    "candidate_vs_analytical_threshold_disagreement",
                    "raw_analytical_thresholds_satisfied",
                    "raw_all_objective_thresholds_satisfied",
                    "archived_vs_raw_analytical_threshold_disagreement",
                    "archived_vs_raw_all_objectives_threshold_disagreement",
                    "declared_run_time_min",
                    "derived_run_time_min",
                    "runtime_derivation",
                    "runtime_abs_error_min",
                    "runtime_within_tolerance",
                    "runtime_status",
                    "runtime_reason",
                    "raw_array_detected_count",
                    "serialized_detected_count",
                    "raw_vs_serialized_detected_count_disagreement",
                    "raw_array_critical_resolution",
                    "serialized_critical_resolution",
                    "raw_vs_serialized_critical_resolution_abs_difference",
                    "raw_array_resolved_count",
                    "serialized_resolved_count",
                    "raw_vs_serialized_resolved_count_disagreement",
                    "candidate_criteria",
                    "candidate_metrics",
                ]
                writer = csv.DictWriter(output_csv, fieldnames=fieldnames)
                writer.writeheader()
                seen_keys: set[tuple[str, int]] = set()
                for source in chain((first_source,), source_rows):
                    actual_values = next(reader, None)
                    if actual_values is None:
                        raise ValueError(
                            "replay observations.csv is missing source observation rows"
                        )
                    if len(actual_values) != len(header):
                        raise ValueError("replay observations.csv has a malformed row")
                    actual = dict(zip(header, actual_values))
                    key = (actual["campaign_id"], int(actual["sequence"]))
                    if key in seen_keys:
                        raise ValueError(f"replay observations.csv has a duplicate identity: {key}")
                    seen_keys.add(key)
                    expected = source.expected_replay_row
                    if key != (source.campaign_id, source.sequence):
                        raise ValueError(
                            "replay observation identity does not match its raw source record"
                        )
                    for name in header:
                        if actual[name] != _csv_cell(expected[name]):
                            raise ValueError(
                                f"replay observation field {name!r} differs from raw source at {key}"
                            )

                    runtime = _runtime_details(source.observation, source.config)
                    runtime_statuses[runtime["runtime_status"]] += 1
                    if runtime["runtime_abs_error_min"] is not None:
                        current = runtime_max_error
                        runtime_max_error = (
                            runtime["runtime_abs_error_min"]
                            if current is None
                            else max(current, runtime["runtime_abs_error_min"])
                        )
                    candidate_rows = _qualify_row(source, runtime)
                    archived = _source_score(source.expected_replay_row["archived_score"])
                    raw_hard = candidate_rows[0]["raw_all_objective_thresholds_satisfied"]
                    raw_analytical = candidate_rows[0]["raw_analytical_thresholds_satisfied"]
                    if archived is not None and isinstance(raw_hard, bool):
                        raw_hard_confusion[(archived >= 2.0, raw_hard)] += 1
                    if archived is not None and isinstance(raw_analytical, bool):
                        raw_analytical_confusion[(archived >= 2.0, raw_analytical)] += 1
                    for candidate_row in candidate_rows:
                        profile = candidate_row["profile"]
                        policy = candidate_row["input_policy"]
                        _update_aggregate(aggregates[profile][policy], candidate_row)
                        writer.writerow(
                            {
                                key: _json_bytes_value(value)
                                for key, value in candidate_row.items()
                                if key in fieldnames
                            }
                        )
                    raw_critical = next(
                        (
                            row["raw_array_critical_resolution"]
                            for row in candidate_rows
                            if row["input_policy"] == "raw_resolution_array"
                        ),
                        None,
                    )
                    raw_detected = next(
                        (
                            row["raw_array_detected_count"]
                            for row in candidate_rows
                            if row["input_policy"] == "raw_resolution_array"
                        ),
                        None,
                    )
                    serialized_critical = source.observation.get("critical_resolution")
                    serialized_detected = source.observation.get("num_peaks")
                    summary_available = any(
                        row["input_policy"] == "serialized_summary_hypothesis"
                        and row["status"] == "scored"
                        for row in candidate_rows
                    )
                    if (
                        source.config["objective"] != "SinglePeak-Resolution"
                        and summary_available
                        and raw_critical is not None
                        and isinstance(serialized_critical, (int, float))
                        and not isinstance(serialized_critical, bool)
                        and math.isfinite(serialized_critical)
                        and raw_critical != serialized_critical
                    ):
                        raw_summary_disagreements["critical_resolution_disagreement_count"] += 1
                    if (
                        source.config["objective"] != "SinglePeak-Resolution"
                        and summary_available
                        and raw_detected is not None
                        and isinstance(serialized_detected, int)
                        and not isinstance(serialized_detected, bool)
                        and raw_detected != serialized_detected
                    ):
                        raw_summary_disagreements["detected_count_disagreement_count"] += 1
                    observation_count += 1
                    campaign_ids.add(source.campaign_id)
                    configuration_ids.add(source.configuration_id)

                if next(reader, None) is not None:
                    raise ValueError(
                        "replay observations.csv contains rows without source observations"
                    )

        if audit["record_sources"] != replay_report.get("record_sources"):
            raise ValueError("replay record source digests do not match current raw ZIP members")
        if audit["analyte_sets"] != replay_report.get("analyte_sets"):
            raise ValueError("replay analyte-set identities do not match current raw ZIP members")
        if replay_report.get("observation_count") != observation_count:
            raise ValueError("replay observation count differs from regenerated source rows")
        if replay_report.get("campaign_count") != len(campaign_ids):
            raise ValueError("replay campaign count differs from regenerated source identities")
        if replay_report.get("configuration_count") != len(configuration_ids):
            raise ValueError(
                "replay configuration count differs from regenerated source identities"
            )

        report = {
            "schema_version": 1,
            "profile": "moses-score-qualification/1",
            "source_zip_sha256": source_manifest["source"]["observed"]["sha256"],
            "source_zip_md5": source_manifest["source"]["observed"]["md5"],
            "source_zip_size_bytes": source_manifest["source"]["observed"]["size_bytes"],
            "source_zip_manifest_sha256": hashlib.sha256(
                json.dumps(
                    source_manifest, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
            ).hexdigest(),
            "source_zip_member_count": source_manifest["archive"]["member_count"],
            "observation_count": observation_count,
            "candidate_count": observation_count * len(MOSES_SCORE_PROFILES) * len(_SCORE_POLICIES),
            "profiles": list(MOSES_SCORE_PROFILES),
            "input_policies": list(_SCORE_POLICIES),
            "arithmetic": "binary64",
            "tolerance": {"absolute": _ABS_TOLERANCE, "relative": _REL_TOLERANCE},
            "aggregation": "Per-row math.isclose(archived_score, candidate_score, rel_tol=1e-10, abs_tol=1e-10).",
            "hard_score_threshold": 2.0,
            "replay_profile": replay_report["profile"],
            "replay_observation_count": replay_report["observation_count"],
            "aggregates": aggregates,
            "raw_hard_threshold_confusion": {
                "classification": "archived score >= 2.0 versus raw-resolution-array all_objective_thresholds_satisfied, including runtime",
                "rows_with_both_values": sum(raw_hard_confusion.values()),
                "true_negative_archived_below_two_raw_hard_false": raw_hard_confusion[
                    (False, False)
                ],
                "true_positive_archived_at_least_two_raw_hard_true": raw_hard_confusion[
                    (True, True)
                ],
                "false_positive_archived_at_least_two_raw_hard_false": raw_hard_confusion[
                    (True, False)
                ],
                "false_negative_archived_below_two_raw_hard_true": raw_hard_confusion[
                    (False, True)
                ],
            },
            "raw_analytical_threshold_confusion": {
                "classification": "archived score >= 2.0 versus raw-resolution-array analytical_thresholds_satisfied (count and resolution; excludes runtime)",
                "rows_with_both_values": sum(raw_analytical_confusion.values()),
                "true_negative_archived_below_two_raw_analytical_false": raw_analytical_confusion[
                    (False, False)
                ],
                "true_positive_archived_at_least_two_raw_analytical_true": raw_analytical_confusion[
                    (True, True)
                ],
                "false_positive_archived_at_least_two_raw_analytical_false": raw_analytical_confusion[
                    (True, False)
                ],
                "false_negative_archived_below_two_raw_analytical_true": raw_analytical_confusion[
                    (False, True)
                ],
            },
            "runtime_diagnostics": {
                "agree_count": runtime_statuses["agrees"],
                "disagree_count": runtime_statuses["disagrees"],
                "unavailable_count": runtime_statuses["unavailable"],
                "max_abs_error_min": runtime_max_error,
                "tolerance": {"absolute": _ABS_TOLERANCE, "relative": _REL_TOLERANCE},
                "time_based_derivation": "last gradient_table time endpoint",
                "segment_based_derivation": "sum of adjacent gradient_table time differences rounded individually to four decimal places",
            },
            "raw_summary_disagreements": {
                "comparison_scope": "non-targeted objectives with both raw-array and serialized-summary candidates available",
                "critical_resolution_disagreement_count": raw_summary_disagreements[
                    "critical_resolution_disagreement_count"
                ],
                "detected_count_disagreement_count": raw_summary_disagreements[
                    "detected_count_disagreement_count"
                ],
            },
            "integrity": {
                "replay_complete_schema_version": complete_before["schema_version"],
                "replay_file_count": len(complete_before["files"]),
                "optional_replay_files_ignored": sorted(
                    item.name
                    for item in replay.iterdir()
                    if item.name not in _REPLAY_FILES | {"complete.json"}
                ),
                "source_manifest_verified": True,
                "source_records_verified": True,
                "rows_matched_to_source": observation_count,
            },
            "software": {
                "original_replay": replay_report.get("software"),
                "qualification": software,
            },
            "scope": "Archived scalar scores are compared with explicit profile and input-policy hypotheses. Summary agreement does not establish the historical scorer inputs. Raw arrays are not copied to qualification outputs.",
        }
        json.dumps(report, allow_nan=False)
        _write_json(stage / "report.json", report)
        (stage / "report.md").write_text(_markdown_report(report), encoding="utf-8")
        output_files = {
            item.name: {
                "sha256": _sha256_file(item),
                "size_bytes": item.stat().st_size,
            }
            for item in sorted(stage.iterdir())
            if item.is_file()
        }
        (stage / "complete.json").write_text(
            json.dumps({"schema_version": 1, "files": output_files}, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        complete_after, report_after, manifest_after = _verify_replay_artifacts(replay)
        if complete_after != complete_before or report_after != replay_report:
            raise ValueError("replay evidence changed during score qualification")
        if manifest_after != prior_manifest or inspect_archive(archive) != source_manifest:
            raise ValueError("source archive changed during score qualification")
        if _software_metadata() != software:
            raise ValueError("score qualification implementation changed during scoring")
        if target.exists() and any(target.iterdir()):
            raise ValueError(
                "output directory changed during qualification; existing files are preserved"
            )
        os.replace(stage, target)
        return report
    finally:
        shutil.rmtree(stage, ignore_errors=True)
