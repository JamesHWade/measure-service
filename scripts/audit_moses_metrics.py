"""Audit archived MoSeS metric summaries against independent candidates.

Example::

    python scripts/audit_moses_metrics.py --archive results.zip \
      --replay-dir /tmp/moses-replay --output-dir /tmp/moses-metric-audit

The script reads archive members with the bounded literal-only decoder. It
writes aggregate comparisons and hashes only; it does not copy source arrays or
machine paths into the result bundle.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import stat
from collections import Counter, defaultdict
from itertools import chain
from pathlib import Path
from typing import Any

from measure_service import moses_score_qualification as score_qualification
from measure_service.benchmarks import evidence, moses_archive, moses_replay, pickle_records
from measure_service.benchmarks.evidence import staged_bundle

AUDIT_PROFILE = "moses-metric-source-audit/1"
EXPECTED_ARCHIVE_SHA256 = "880533ded926f766ce9504d4f164e9c5195b775e40061c18d70ae79497f03f71"
ABSOLUTE_TOLERANCE = 1e-12
RELATIVE_TOLERANCE = 1e-12
_MAX_PAIR_COMPOUNDS = 500
_REPLAY_FILES = {
    "archive-manifest.json",
    "campaigns.csv",
    "observations.csv",
    "report.json",
    "report.md",
    "success-trajectories.csv",
}
_IMPLEMENTATION_FILES = {
    "audit_script": None,
    "measure_service.moses_score_qualification": score_qualification,
    "measure_service.benchmarks.moses_replay": moses_replay,
    "measure_service.benchmarks.moses_archive": moses_archive,
    "measure_service.benchmarks.pickle_records": pickle_records,
    "measure_service.benchmarks.evidence": evidence,
    "measure_methods.chromatography": moses_replay.chromatography,
    "measure_methods.volume": moses_replay.volume,
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_hashes() -> dict[str, str]:
    result = {"audit_script": _sha256_file(Path(__file__))}
    for name, module in _IMPLEMENTATION_FILES.items():
        if name != "audit_script":
            result[name] = _sha256_file(Path(module.__file__))
    return result


def _safe_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _classify_array(values: object) -> tuple[list[float | None] | None, dict[str, int]]:
    if not isinstance(values, list) or not values:
        # All supported source campaigns declare at least one analyte. The
        # historical empty-list representation denotes unavailable measurements.
        return None, {"array_unavailable": 1}
    result: list[float | None] = []
    counts: Counter[str] = Counter()
    for value in values:
        if value is None:
            result.append(None)
            counts["null"] += 1
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
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
            result.append(None)
            counts["positive_infinity" if number > 0 else "negative_infinity"] += 1
        else:
            result.append(number)
            counts["finite"] += 1
    return result, dict(counts)


def _finite_min(values: list[float | None] | None) -> float | None:
    if values is None:
        return None
    finite = [value for value in values if value is not None]
    return min(finite) if finite else 0.0


def _new_metric() -> dict[str, Any]:
    return {
        "compared": 0,
        "exact_matches": 0,
        "matches_abs_1e-12_rel_1e-12": 0,
        "mismatches": 0,
        "candidate_unavailable": 0,
        "reported_unavailable": 0,
        "excluded_targeted": 0,
        "max_abs_error": 0.0,
        "examples": [],
    }


def _compare(
    name: str,
    candidate: object,
    reported: object,
    *,
    targeted: bool,
    overall: dict[str, dict[str, Any]],
    by_origin: dict[str, dict[str, dict[str, Any]]],
    origin: str,
) -> None:
    groups = (overall[name], by_origin[origin][name])
    if targeted:
        for group in groups:
            group["excluded_targeted"] += 1
        return
    candidate_number = _safe_number(candidate)
    reported_number = _safe_number(reported)
    for group in groups:
        if candidate_number is None:
            group["candidate_unavailable"] += 1
            continue
        if reported_number is None:
            group["reported_unavailable"] += 1
            continue
        group["compared"] += 1
        if candidate_number == reported_number:
            group["exact_matches"] += 1
        error = abs(candidate_number - reported_number)
        if math.isfinite(error) and group["max_abs_error"] is not None:
            group["max_abs_error"] = max(group["max_abs_error"], error)
        else:
            group["max_abs_error"] = None
        if math.isclose(
            candidate_number,
            reported_number,
            rel_tol=RELATIVE_TOLERANCE,
            abs_tol=ABSOLUTE_TOLERANCE,
        ):
            group["matches_abs_1e-12_rel_1e-12"] += 1
        else:
            group["mismatches"] += 1


def _candidate_values(observation: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    retention, retention_status = _classify_array(observation.get("retention_times"))
    resolutions, resolution_status = _classify_array(observation.get("peak_resolutions"))
    widths, width_status = _classify_array(observation.get("fwhms"))
    declared = config["num_compounds"]
    runtime = _safe_number(observation.get("gradient", {}).get("run_time"))
    threshold = _safe_number(config.get("resolution_threshold"))

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
            for time, resolution in zip(retention, resolutions)
        )

    min_rs = _finite_min(resolutions)
    min_rs_finite_rt = None
    min_rs_within_runtime = None
    if retention is not None and resolutions is not None and len(retention) == len(resolutions):
        paired = [
            resolution
            for time, resolution in zip(retention, resolutions)
            if time is not None and resolution is not None
        ]
        min_rs_finite_rt = min(paired) if paired else 0.0
        if runtime is not None:
            within = [
                resolution
                for time, resolution in zip(retention, resolutions)
                if time is not None and resolution is not None and time <= runtime
            ]
            min_rs_within_runtime = min(within) if within else 0.0

    resolved_threshold = (
        None
        if resolutions is None or threshold is None
        else sum(value >= threshold for value in finite_rs)
    )
    resolved_default = None if resolutions is None else sum(value >= 1.5 for value in finite_rs)

    candidate: dict[str, float | int | None] = {
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

    value_status_counts = {
        "retention_times": retention_status,
        "peak_resolutions": resolution_status,
        "fwhms": width_status,
    }
    if retention is None or resolutions is None or widths is None:
        shape_status = "array_unavailable"
    elif len(retention) == len(resolutions) == len(widths) == declared:
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
    if (
        retention is not None
        and widths is not None
        and len(retention) == len(widths)
        and declared <= _MAX_PAIR_COMPOUNDS
    ):
        ordered_positions = sorted(
            (index for index, value in enumerate(retention) if value is not None),
            key=lambda index: (retention[index], index),
        )
        per_analyte_neighbours = [[] for _ in range(declared)]
        valid_adjacent = []

        for left_position in range(len(ordered_positions)):
            for right_position in range(left_position + 1, len(ordered_positions)):
                left_index = ordered_positions[left_position]
                right_index = ordered_positions[right_position]
                left_width = widths[left_index]
                right_width = widths[right_index]
                if left_width is None or right_width is None or left_width <= 0 or right_width <= 0:
                    continue
                delta = retention[right_index] - retention[left_index]
                sigma_total = left_width / 2.3548 + right_width / 2.3548
                if not math.isfinite(delta) or not math.isfinite(sigma_total) or sigma_total <= 0:
                    continue
                width_total = left_width + right_width
                if not math.isfinite(width_total) or width_total <= 0:
                    continue
                values = {
                    "sigma_pair": delta / (2 * sigma_total),
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
        pair_candidate_names = (
            "sigma_pair_adjacent_min",
            "sigma_pair_all_min",
            "fwhm_delta_over_width_sum_adjacent_min",
            "fwhm_delta_over_width_sum_all_min",
            "fwhm_2delta_over_width_sum_adjacent_min",
            "fwhm_2delta_over_width_sum_all_min",
            "fwhm_1_1774delta_over_width_sum_adjacent_min",
            "fwhm_1_1774delta_over_width_sum_all_min",
        )
        for name in pair_candidate_names:
            pair_name = name.removeprefix("reported_critical_vs_")
            values = pair_values.get(pair_name, [])
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
                    rel_tol=RELATIVE_TOLERANCE,
                    abs_tol=ABSOLUTE_TOLERANCE,
                ):
                    pair_comparison["mismatches"] += 1
    else:
        for name in (
            "sigma_pair_adjacent_min",
            "sigma_pair_all_min",
            "fwhm_delta_over_width_sum_adjacent_min",
            "fwhm_delta_over_width_sum_all_min",
            "fwhm_2delta_over_width_sum_adjacent_min",
            "fwhm_2delta_over_width_sum_all_min",
            "fwhm_1_1774delta_over_width_sum_adjacent_min",
            "fwhm_1_1774delta_over_width_sum_all_min",
        ):
            candidate[f"reported_critical_vs_{name}"] = None

    return {
        "candidates": candidate,
        "reported": {
            "num_peaks": observation.get("num_peaks"),
            "num_resolved_peaks": observation.get("num_resolved_peaks"),
            "critical_resolution": observation.get("critical_resolution"),
        },
        "arrays": value_status_counts,
        "shape_status": shape_status,
        "pair_counts": dict(pair_counts),
        "pair_array_comparison": pair_comparison,
        "targeted": config.get("objective") == "SinglePeak-Resolution",
    }


def _compare_observation(
    analysis: dict[str, Any],
    *,
    origin: str,
    overall: dict[str, dict[str, Any]],
    by_origin: dict[str, dict[str, dict[str, Any]]],
) -> None:
    comparisons = {
        "reported_num_peaks_vs_finite_resolution_count": "num_peaks",
        "reported_num_peaks_vs_finite_retention_time_count": "num_peaks",
        "reported_num_peaks_vs_finite_rt_within_runtime_inclusive": "num_peaks",
        "reported_num_peaks_vs_finite_rt_within_runtime_exclusive": "num_peaks",
        "reported_num_peaks_vs_finite_rt_and_resolution_within_runtime": "num_peaks",
        "reported_critical_vs_min_finite_resolution": "critical_resolution",
        "reported_critical_vs_min_resolution_with_finite_rt": "critical_resolution",
        "reported_critical_vs_min_resolution_within_runtime": "critical_resolution",
        "reported_num_resolved_vs_finite_resolution_threshold_count": "num_resolved_peaks",
        "reported_num_resolved_vs_finite_resolution_default_1_5": "num_resolved_peaks",
    }
    for name in (
        "sigma_pair_adjacent_min",
        "sigma_pair_all_min",
        "fwhm_delta_over_width_sum_adjacent_min",
        "fwhm_delta_over_width_sum_all_min",
        "fwhm_2delta_over_width_sum_adjacent_min",
        "fwhm_2delta_over_width_sum_all_min",
        "fwhm_1_1774delta_over_width_sum_adjacent_min",
        "fwhm_1_1774delta_over_width_sum_all_min",
    ):
        comparisons[f"reported_critical_vs_{name}"] = "critical_resolution"
    for metric, summary_name in comparisons.items():
        _compare(
            metric,
            analysis["candidates"].get(metric),
            analysis["reported"].get(summary_name),
            targeted=analysis["targeted"],
            overall=overall,
            by_origin=by_origin,
            origin=origin,
        )


def _validate_output_target(output_dir: Path, replay_dir: Path) -> None:
    output = output_dir.resolve()
    replay = replay_dir.resolve()
    if (
        output in (replay, replay.parent)
        or output.is_relative_to(replay)
        or replay.is_relative_to(output)
    ):
        raise ValueError("audit output must be separate from the replay evidence directory")
    if output_dir.exists() or output_dir.is_symlink():
        metadata = output_dir.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("output path must be a directory")
        if any(output_dir.iterdir()):
            raise ValueError("output directory must be empty; existing files are preserved")


def _parse_csv_integer(value: str, name: str, *, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"replay {name} is not an integer") from error
    if number < minimum or str(number) != value:
        raise ValueError(f"replay {name} is not an integer of at least {minimum}")
    return number


def _parse_json_cell(value: str, name: str) -> Any:
    try:
        return json.loads(value, object_pairs_hook=score_qualification._unique_pairs)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"replay {name} is invalid JSON") from error


def _validate_campaign_rows(
    replay_dir: Path,
    replay_report: dict[str, Any],
    expected: dict[str, dict[str, Any]],
    config_observations: dict[str, dict[str, Any]],
) -> None:
    configurations = replay_report.get("configurations")
    if not isinstance(configurations, list):
        raise ValueError("replay report is missing configurations")
    configs_by_id = {}
    for config in configurations:
        if not isinstance(config, dict):
            raise ValueError("replay configuration rows must be objects")
        config_id = config.get("configuration_id")
        if not isinstance(config_id, str) or config_id in configs_by_id:
            raise ValueError("replay configuration identities must be unique")
        configs_by_id[config_id] = config
    if set(configs_by_id) != set(config_observations):
        raise ValueError("replay configuration identities differ from archived campaigns")
    if replay_report.get("configuration_count") != len(configs_by_id):
        raise ValueError("replay configuration count differs from its configuration rows")
    for config_id, observed in config_observations.items():
        config_row = configs_by_id[config_id]
        config = config_row.get("config")
        if not isinstance(config, dict):
            raise ValueError("replay configuration omits its scientific settings")
        for report_name, source_name in (
            ("objective", "objective"),
            ("num_compounds", "num_compounds"),
            ("compound_idx", "compound_idx"),
            ("max_run_time", "max_run_time_min"),
            ("count_threshold", "count_threshold"),
            ("resolution_threshold", "resolution_threshold"),
            ("run_time_threshold", "run_time_threshold_min"),
            ("segment_based", "segment_based"),
        ):
            if config.get(report_name) != observed["scoring_config"].get(source_name):
                raise ValueError("replay configuration differs from its archived source config")
        if config_row.get("campaign_count") != observed["campaign_count"]:
            raise ValueError("replay recorded campaign count differs from archived source rows")
        planned_count = config_row.get("planned_campaign_count")
        missing_count = config_row.get("missing_campaign_count")
        if (
            isinstance(planned_count, bool)
            or not isinstance(planned_count, int)
            or planned_count < observed["campaign_count"]
            or isinstance(missing_count, bool)
            or not isinstance(missing_count, int)
            or missing_count < 0
            or planned_count != observed["campaign_count"] + missing_count
        ):
            raise ValueError("replay configuration counts do not preserve its planned cohort")
        if config_row.get("record_kind") != observed["record_kind"]:
            raise ValueError("replay record kind differs from archived source identity")

    with (replay_dir / "campaigns.csv").open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source, strict=True)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("replay campaigns.csv has a missing or duplicate header")
        rows = list(reader)
    if len(rows) != len(expected):
        raise ValueError("replay campaign row count differs from archived campaign identities")
    seen: set[str] = set()
    for row in rows:
        campaign_id = row.get("campaign_id")
        if campaign_id not in expected or campaign_id in seen:
            raise ValueError("replay campaign identities are missing, duplicated or unexpected")
        seen.add(campaign_id)
        raw = expected[campaign_id]
        for field in ("campaign_id", "configuration_id", "source_member", "source_sha256"):
            if row.get(field) != raw[field]:
                raise ValueError(f"replay campaign {field} does not match archived identity")
        for field in ("num_observations", "high_fidelity_count", "low_fidelity_count"):
            if _parse_csv_integer(row.get(field, ""), field, minimum=0) != raw[field]:
                raise ValueError(f"replay campaign {field} differs from archived observations")
        for field, value in (
            ("analyte_set_index", raw["analyte_set_index"]),
            ("repeat_index", raw["repeat_index"]),
        ):
            cell = row.get(field, "")
            parsed = None if cell == "" else _parse_csv_integer(cell, field)
            if parsed != value:
                raise ValueError(f"replay campaign {field} differs from archived identity")
        if row.get("campaign_context") != raw["campaign_context"]:
            raise ValueError("replay campaign context differs from archived record kind")
        config = _parse_json_cell(row.get("config", ""), "campaign config")
        if config != configs_by_id[raw["configuration_id"]].get("config"):
            raise ValueError("replay campaign config differs from its report configuration")
        if row.get("snapshot_status") not in {
            "experimental_summary",
            "individual_only",
            "individual_continuation",
            "grouped_continuation",
            "matching_labelled_history",
            "serialized_complete_cohort_order",
        }:
            raise ValueError("replay campaign snapshot status is unsupported")


def _validate_replay_rows(
    archive_path: Path,
    replay_dir: Path,
    current_manifest: dict[str, Any],
    replay_report: dict[str, Any],
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    audit = {"record_sources": [], "analyte_sets": []}
    source_rows = iter(
        score_qualification._iter_source_observations(archive_path, current_manifest, audit)
    )
    first = next(source_rows, None)
    if first is None:
        raise ValueError("source ZIP has no supported observations")
    expected_header = list(first.expected_replay_row)
    with (replay_dir / "observations.csv").open(newline="", encoding="utf-8") as source_csv:
        reader = csv.DictReader(source_csv, strict=True)
        if reader.fieldnames != expected_header:
            raise ValueError("replay observations.csv columns differ from regenerated source rows")
        expected_rows = chain((first,), source_rows)
        seen: set[tuple[str, int]] = set()
        total = 0
        high_total = low_total = 0
        by_campaign: dict[str, dict[str, Any]] = {}
        config_observations: dict[str, dict[str, Any]] = {}
        overall = defaultdict(_new_metric)
        by_origin = defaultdict(lambda: defaultdict(_new_metric))
        value_status_counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        shape_counts: Counter[tuple[str, str]] = Counter()
        pair_counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        pair_array_counts: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"compared": 0, "mismatches": 0, "max_abs_error": 0.0}
        )

        for source_observation in expected_rows:
            actual = next(reader, None)
            if actual is None or None in actual or any(value is None for value in actual.values()):
                raise ValueError("replay observations.csv is missing or malformed")
            sequence = source_observation.sequence
            identity = (source_observation.campaign_id, sequence)
            if identity in seen:
                raise ValueError("replay observations.csv has a duplicate source identity")
            seen.add(identity)
            expected = source_observation.expected_replay_row
            for name in expected_header:
                if actual.get(name) != score_qualification._csv_cell(expected[name]):
                    raise ValueError(
                        f"replay observation field {name!r} differs from regenerated source at {identity}"
                    )
            total += 1
            high = bool(expected["high_fidelity"])
            high_total += int(high)
            low_total += int(not high)
            campaign = by_campaign.setdefault(
                source_observation.campaign_id,
                {
                    "campaign_id": source_observation.campaign_id,
                    "configuration_id": source_observation.configuration_id,
                    "source_member": source_observation.source_member,
                    "source_sha256": source_observation.source_sha256,
                    "analyte_set_index": source_observation.analyte_set_index,
                    "repeat_index": source_observation.repeat_index,
                    "campaign_context": (
                        "simulator-latent-reference"
                        if source_observation.analyte_set_index is not None
                        else "archived-instrument-analysis"
                    ),
                    "num_observations": 0,
                    "high_fidelity_count": 0,
                    "low_fidelity_count": 0,
                    "record_kind": (
                        "simulated-benchmark"
                        if source_observation.analyte_set_index is not None
                        else "experimental-campaign"
                    ),
                },
            )
            campaign["num_observations"] += 1
            campaign["high_fidelity_count"] += int(high)
            campaign["low_fidelity_count"] += int(not high)
            config_id = source_observation.configuration_id
            config_facts = config_observations.setdefault(
                config_id,
                {
                    "scoring_config": source_observation.config,
                    "campaign_count": 0,
                    "record_kind": campaign["record_kind"],
                },
            )
            if config_facts["record_kind"] != campaign["record_kind"]:
                raise ValueError("one replay configuration mixes simulated and experimental rows")
            if campaign["num_observations"] == 1:
                config_facts["campaign_count"] += 1
            analysis = _candidate_values(source_observation.observation, source_observation.config)
            origin = expected["observation_origin"]
            if not isinstance(origin, str):
                raise ValueError("regenerated replay origin is missing")
            _compare_observation(analysis, origin=origin, overall=overall, by_origin=by_origin)
            for field, counts in analysis["arrays"].items():
                value_status_counts[(origin, field)].update(counts)
            shape_counts[(origin, analysis["shape_status"])] += 1
            pair_counts[(origin, "")].update(analysis["pair_counts"])
            pair_aggregate = pair_array_counts[origin]
            pair_aggregate["compared"] += analysis["pair_array_comparison"]["compared"]
            pair_aggregate["mismatches"] += analysis["pair_array_comparison"]["mismatches"]
            pair_counts[(origin, "")]["source_style_reconstructed_analyte_R_compared_count"] += (
                analysis["pair_array_comparison"]["compared"]
            )
            pair_counts[(origin, "")]["source_style_reconstructed_analyte_R_mismatch_count"] += (
                analysis["pair_array_comparison"]["mismatches"]
            )
            max_error = analysis["pair_array_comparison"]["max_abs_error"]
            if max_error is None:
                pair_aggregate["max_abs_error"] = None
            elif pair_aggregate["max_abs_error"] is not None:
                pair_aggregate["max_abs_error"] = max(pair_aggregate["max_abs_error"], max_error)

        if next(reader, None) is not None:
            raise ValueError("replay observations.csv contains rows without source observations")

    if audit["record_sources"] != replay_report.get("record_sources"):
        raise ValueError("replay record member digests differ from current source ZIP members")
    if audit["analyte_sets"] != replay_report.get("analyte_sets"):
        raise ValueError("replay analyte-set identities differ from current source ZIP members")
    if replay_report.get("observation_count") != total:
        raise ValueError("replay observation count differs from regenerated source rows")
    if replay_report.get("campaign_count") != len(by_campaign):
        raise ValueError("replay campaign count differs from regenerated source identities")
    if replay_report.get("configuration_count") != len(config_observations):
        raise ValueError("replay configuration count differs from regenerated source identities")

    _validate_campaign_rows(replay_dir, replay_report, by_campaign, config_observations)
    aggregate = {
        "observation_count": total,
        "high_fidelity_observation_count": high_total,
        "low_fidelity_observation_count": low_total,
        "campaign_count": len(by_campaign),
        "configuration_count": len(config_observations),
    }
    metrics = {
        "comparisons": {name: item for name, item in sorted(overall.items())},
        "by_origin": {
            origin: {name: item for name, item in sorted(metrics.items())}
            for origin, metrics in sorted(by_origin.items())
        },
        "array_value_status_counts": {
            f"{origin}|{field}": dict(counts)
            for (origin, field), counts in sorted(value_status_counts.items())
        },
        "array_shape_counts": {
            f"{origin}|{shape}": count for (origin, shape), count in sorted(shape_counts.items())
        },
        "pair_and_boundary_counts": {
            f"{origin}|{name}": count
            for (origin, _), counts in sorted(pair_counts.items())
            for name, count in sorted(counts.items())
        },
        "pair_array_reconstruction": {
            origin: counts for origin, counts in sorted(pair_array_counts.items())
        },
    }
    return aggregate, metrics


def _source_manifest(archive_path: Path) -> dict[str, Any]:
    metadata = archive_path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("archive path must be a regular file")
    manifest = moses_archive.inspect_archive(archive_path, verify=True)
    observed = manifest["source"]["observed"]
    if observed.get("sha256") != EXPECTED_ARCHIVE_SHA256:
        raise ValueError("archive full SHA256 differs from the reviewed published archive")
    if manifest["source"].get("matches_expected") is not True:
        raise ValueError("archive size or published MD5 does not match")
    return manifest


def _markdown_report(report: dict[str, Any]) -> str:
    source = report["source"]
    totals = report["replay_binding"]
    lines = [
        "# MoSeS metric candidate audit",
        "",
        "Profile: `moses-metric-source-audit/1`.",
        "",
        "This audit compares archived global summary fields with explicit array-derived candidates. It preserves the summaries and does not infer which code generated them.",
        "",
        f"Verified ZIP: {source['size_bytes']:,} bytes, MD5 `{source['md5']}`, SHA256 `{source['sha256']}`.",
        f"Bound replay rows: {totals['observation_count']:,} across {totals['campaign_count']:,} campaigns and {totals['configuration_count']:,} configurations.",
        "",
        "## Candidate comparisons",
        "",
        "| Comparison | Compared | Exact | Within tolerance | Mismatch | Candidate unavailable | Report unavailable | Targeted excluded |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, item in sorted(report["comparisons"].items()):
        lines.append(
            "| {name} | {compared:,} | {exact:,} | {within:,} | {mismatch:,} | {candidate:,} | {reported:,} | {excluded:,} |".format(
                name=name,
                compared=item["compared"],
                exact=item["exact_matches"],
                within=item["matches_abs_1e-12_rel_1e-12"],
                mismatch=item["mismatches"],
                candidate=item["candidate_unavailable"],
                reported=item["reported_unavailable"],
                excluded=item["excluded_targeted"],
            )
        )
    lines.extend(
        [
            "",
            "Global summary comparisons exclude `SinglePeak-Resolution` observations because their selected-target summaries are not global minima. Those exclusions are counted in the JSON report.",
            "",
            "The Gaussian pair candidate uses `sigma = FWHM / 2.3548` and `Rs = delta_RT / (2 * (sigma_left + sigma_right))` for adjacent finite retention times with positive finite widths. It describes the public simulation formula; it does not establish the external fitted-resolution procedure used for instrument analysis.",
            "",
            "The report contains aggregate counts and checksums. It does not contain per-analyte arrays, archive extraction, or source machine paths.",
            "",
        ]
    )
    return "\n".join(lines)


def audit_metrics(
    archive_path: str | Path, replay_dir: str | Path, output_dir: str | Path
) -> dict[str, Any]:
    """Verify the source ZIP and replay rows, compare metric candidates, and publish a bundle."""
    archive = Path(archive_path)
    replay_root = Path(replay_dir)
    target = Path(output_dir)
    _validate_output_target(target, replay_root)
    implementation = _implementation_hashes()
    complete_before, replay_report, archived_manifest = (
        score_qualification._verify_replay_artifacts(replay_root)
    )
    complete_digest_before = _sha256_file(replay_root / "complete.json")
    source_manifest = _source_manifest(archive)
    score_qualification._validate_report_source(replay_report, archived_manifest, source_manifest)
    if replay_report.get("profile") != moses_replay.PROFILE:
        raise ValueError("replay uses an unsupported observation profile")
    aggregate, metrics = _validate_replay_rows(archive, replay_root, source_manifest, replay_report)
    report = {
        "schema_version": 1,
        "profile": AUDIT_PROFILE,
        "scope": "Compare serialized global summary fields with candidate calculations from bounded source observations. The result identifies candidate agreement only; it does not infer the historical summary implementation or replace archived inputs.",
        "source": {
            "doi": moses_archive.MOSES_DOI,
            "size_bytes": source_manifest["source"]["observed"]["size_bytes"],
            "md5": source_manifest["source"]["observed"]["md5"],
            "sha256": source_manifest["source"]["observed"]["sha256"],
            "matches_published_size_and_md5": source_manifest["source"]["matches_expected"],
            "matches_reviewed_sha256": True,
            "member_count": source_manifest["archive"]["member_count"],
        },
        "replay_binding": {
            **aggregate,
            "profile": replay_report["profile"],
            "complete_json_sha256": complete_digest_before,
            "report_json_sha256": _sha256_file(replay_root / "report.json"),
            "replay_files_verified": len(complete_before["files"]),
            "source_record_digests_verified": True,
            "all_replay_observation_columns_matched_to_regenerated_rows": True,
        },
        "reader": {
            "archive_reader": "measure_service.benchmarks.moses_archive",
            "pickle_decoder": "measure_service.benchmarks.pickle_records.decode_archive_record",
            "pickle_load_used": False,
            "archive_member_extraction_used": False,
        },
        "candidate_tolerance": {
            "absolute": ABSOLUTE_TOLERANCE,
            "relative": RELATIVE_TOLERANCE,
        },
        "summary_comparison_policy": "Global summary candidates are compared only for non-targeted objectives. SinglePeak-Resolution rows are counted as excluded because their target-specific summaries cannot be interpreted as global minima.",
        "comparisons": metrics["comparisons"],
        "by_origin": metrics["by_origin"],
        "array_value_status_counts": metrics["array_value_status_counts"],
        "array_shape_counts": metrics["array_shape_counts"],
        "pair_and_boundary_counts": metrics["pair_and_boundary_counts"],
        "pair_array_reconstruction": metrics["pair_array_reconstruction"],
        "implementation_sha256": implementation,
        "interpretation": [
            "Finite retention-time counts and finite-resolution counts measure different stored-array properties.",
            "For non-targeted objectives, summary comparisons evaluate the archived fields without repairing or substituting them.",
            "Adjacent-pair reconstruction applies only where the input arrays align and positive finite FWHM values are available.",
            "Candidate agreement does not identify the historical serializer or the external fitted-resolution implementation.",
        ],
    }
    json.dumps(report, allow_nan=False)

    with staged_bundle(target, input_dirs=[replay_root]) as stage:
        (stage / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        (stage / "report.md").write_text(_markdown_report(report), encoding="utf-8")
        complete_after, report_after, manifest_after = score_qualification._verify_replay_artifacts(
            replay_root
        )
        if (
            complete_after != complete_before
            or _sha256_file(replay_root / "complete.json") != complete_digest_before
            or report_after != replay_report
            or manifest_after != archived_manifest
        ):
            raise ValueError("replay evidence changed during metric audit")
        if _source_manifest(archive) != source_manifest:
            raise ValueError("source ZIP changed during metric audit")
        if _implementation_hashes() != implementation:
            raise ValueError("metric audit implementation changed during analysis")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="verified source results.zip")
    parser.add_argument("--replay-dir", type=Path, required=True, help="completed replay bundle")
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="new or empty audit bundle directory"
    )
    args = parser.parse_args()
    result = audit_metrics(args.archive, args.replay_dir, args.output_dir)
    print(
        json.dumps(
            {
                "profile": result["profile"],
                "observation_count": result["replay_binding"]["observation_count"],
                "campaign_count": result["replay_binding"]["campaign_count"],
                "output": str(args.output_dir / "report.md"),
                "source_sha256": result["source"]["sha256"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
