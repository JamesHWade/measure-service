"""Compare MoSeS success trajectories under qualified score policies.

This adapter consumes an integrity-checked replay and its separately
integrity-checked score qualification. It does not open the source archive,
recompute scalar scores, or modify either input bundle.
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import json
import math
from collections.abc import Iterable, Iterator, Mapping
from importlib import metadata
from itertools import groupby
from pathlib import Path
from typing import Any

from measure_methods.moses_objective import MOSES_SCORE_PROFILES
from measure_methods.trajectories import (
    SuccessObservation,
    aggregate_success_trajectories,
    summarize_campaign_success,
)

from .evidence import VerifiedBundle, staged_bundle
from .sensitivity_summary import summarize_outputs

REPLAY_PROFILE = "moses-zenodo-v1-archived-observations/1"
QUALIFICATION_PROFILE = "moses-score-qualification/1"
REPLAY_FILES = {
    "archive-manifest.json",
    "campaigns.csv",
    "observations.csv",
    "report.json",
    "report.md",
    "success-trajectories.csv",
}
QUALIFICATION_FILES = {"observations.csv", "report.json", "report.md"}
INPUT_POLICIES = ("raw_resolution_array", "serialized_summary_hypothesis")
SCORE_THRESHOLD = 2.0
_SERIES = (
    {
        "name": "archived_score_at_least_two",
        "kind": "archived_score",
        "description": "Finite archived scalar score at least 2.0; high-fidelity rows only.",
        "profile": None,
        "policy": None,
    },
    {
        "name": "published_1.0_raw_resolution_array",
        "kind": "candidate_score",
        "description": "BoTier 1.0.0 profile from the per-analyte raw resolution array.",
        "profile": "moses-botier-1.0.0-binary64/1",
        "policy": "raw_resolution_array",
    },
    {
        "name": "excess_1.1_raw_resolution_array",
        "kind": "candidate_score",
        "description": "BoTier 1.1.0 excess-only profile from the per-analyte raw resolution array.",
        "profile": "moses-botier-1.1.0-binary64/1",
        "policy": "raw_resolution_array",
    },
    {
        "name": "published_1.0_summary_or_selected_target_array",
        "kind": "candidate_score",
        "description": (
            "BoTier 1.0.0 serialized-summary hypothesis for non-targeted objectives; "
            "the selected raw resolution entry for SinglePeak-Resolution."
        ),
        "profile": "moses-botier-1.0.0-binary64/1",
        "policy": "serialized_summary_hypothesis",
    },
    {
        "name": "excess_1.1_summary_or_selected_target_array",
        "kind": "candidate_score",
        "description": (
            "BoTier 1.1.0 serialized-summary hypothesis for non-targeted objectives; "
            "the selected raw resolution entry for SinglePeak-Resolution."
        ),
        "profile": "moses-botier-1.1.0-binary64/1",
        "policy": "serialized_summary_hypothesis",
    },
    {
        "name": "raw_analytical_thresholds",
        "kind": "raw_analytical_thresholds_satisfied",
        "description": "Raw-array count and resolution thresholds; excludes runtime.",
        "profile": None,
        "policy": None,
    },
    {
        "name": "raw_all_objective_thresholds",
        "kind": "raw_all_objective_thresholds_satisfied",
        "description": "Raw-array count, resolution and runtime thresholds.",
        "profile": None,
        "policy": None,
    },
)
_CANDIDATE_SERIES = tuple(item for item in _SERIES if item["name"] != _SERIES[0]["name"])
_PAIR_STATES = ("confirmed_success", "observed_no_success", "unknown")
_IMPLEMENTATION_MODULES = (
    "measure_service.benchmarks.moses_sensitivity",
    "measure_methods.trajectories",
    "measure_service.benchmarks.evidence",
    "measure_service.benchmarks.sensitivity_summary",
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_metadata() -> dict[str, Any]:
    return {
        "distribution": "measure-service",
        "version": metadata.version("measure-service"),
        "implementation_sha256": {
            name: _file_sha256(Path(importlib.import_module(name).__file__))
            for name in _IMPLEMENTATION_MODULES
        },
    }


def _json_object(value: str | bytes, name: str) -> Any:
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in {name}: {key}")
            result[key] = item
        return result

    def reject_constant(value):
        raise ValueError(f"non-finite JSON number in {name}: {value}")

    try:
        return json.loads(value, object_pairs_hook=unique, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON in {name}") from error


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value and value.isdecimal():
        result = int(value)
    else:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    if result < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return result


def _optional_integer(value: object, name: str, *, minimum: int = 0) -> int | None:
    return None if value in (None, "") else _integer(value, name, minimum=minimum)


def _number(value: object, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite numeric data")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite numeric data") from error
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"{name} must be finite numeric data")
    return result


def _optional_number(value: object, name: str) -> float | None:
    return None if value in (None, "") else _number(value, name)


def _optional_bool(value: object, name: str) -> bool | None:
    if value in (None, ""):
        return None
    if value in (True, "True", "true", "1"):
        return True
    if value in (False, "False", "false", "0"):
        return False
    raise ValueError(f"{name} must be true, false or unavailable")


def _required_text(row: Mapping[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"evidence row is missing {name}")
    return value


def _same_number(left: float | None, right: float | None, name: str) -> None:
    if left is None or right is None:
        if left is not right:
            raise ValueError(f"{name} differs between evidence rows")
        return
    if left != right:
        raise ValueError(f"{name} differs between evidence rows")


def _validate_source(replay: VerifiedBundle, qualification: VerifiedBundle) -> dict[str, Any]:
    replay_report = replay.report
    score_report = qualification.report
    if replay_report.get("schema_version") != 1:
        raise ValueError("unsupported replay report schema")
    if score_report.get("schema_version") != 1:
        raise ValueError("unsupported score qualification report schema")

    manifest = _json_object(
        (replay.root / "archive-manifest.json").read_bytes(), "archive manifest"
    )
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported replay archive-manifest schema")
    source = manifest.get("source")
    archive = manifest.get("archive")
    if not isinstance(source, dict) or not isinstance(archive, dict):
        raise ValueError("replay archive manifest lacks source or archive metadata")
    if replay_report.get("archive_source") != source:
        raise ValueError("replay report source does not match its archive manifest")
    archive_summary = {key: value for key, value in archive.items() if key != "members"}
    if replay_report.get("archive_summary") != archive_summary:
        raise ValueError("replay archive summary does not match its archive manifest")
    archive_manifest_sha256 = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()

    expected = source.get("expected")
    observed = source.get("observed")
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        raise ValueError("source ZIP expected and observed digests are required")
    if (
        source.get("matches_expected") is not True
        or expected.get("md5") != observed.get("md5")
        or expected.get("size_bytes") != observed.get("size_bytes")
    ):
        raise ValueError("replay source ZIP failed its declared checksum or size")
    if (
        score_report.get("source_zip_sha256") != observed.get("sha256")
        or score_report.get("source_zip_md5") != observed.get("md5")
        or score_report.get("source_zip_size_bytes") != observed.get("size_bytes")
        or score_report.get("source_zip_member_count") != archive.get("member_count")
        or score_report.get("source_zip_manifest_sha256") != archive_manifest_sha256
    ):
        raise ValueError("score qualification source ZIP identity differs from the replay")
    integrity = score_report.get("integrity")
    if (
        not isinstance(integrity, dict)
        or integrity.get("source_manifest_verified") is not True
        or integrity.get("source_records_verified") is not True
    ):
        raise ValueError("score qualification did not verify its source evidence")
    if score_report.get("replay_profile") != replay.profile:
        raise ValueError("score qualification names a different replay profile")
    if score_report.get("hard_score_threshold") != SCORE_THRESHOLD:
        raise ValueError("score qualification uses a different hard threshold")
    if replay_report.get("score_threshold") != SCORE_THRESHOLD:
        raise ValueError("replay uses a different archived-score threshold")
    if replay_report.get("high_fidelity_threshold") != 0.99:
        raise ValueError("replay uses a different high-fidelity threshold")

    replay_software = replay_report.get("software")
    qualification_software = score_report.get("software")
    if (
        not isinstance(replay_software, dict)
        or not isinstance(qualification_software, dict)
        or qualification_software.get("original_replay") != replay_software
    ):
        raise ValueError("score qualification was not produced from this replay implementation")
    qualification_run = qualification_software.get("qualification")
    if not isinstance(qualification_run, dict) or qualification_run.get(
        "replay_module_sha256"
    ) != replay_software.get("replay_sha256"):
        raise ValueError("score qualification replay implementation identity is inconsistent")

    if score_report.get("profiles") != list(MOSES_SCORE_PROFILES):
        raise ValueError("score qualification profiles are missing, reordered or unsupported")
    if score_report.get("input_policies") != list(INPUT_POLICIES):
        raise ValueError("score qualification input policies are missing, reordered or unsupported")
    source_records = _source_record_index(replay_report)
    return {
        "source": source,
        "archive_summary": archive_summary,
        "archive_manifest_sha256": archive_manifest_sha256,
        "source_records": source_records,
    }


def _source_record_index(report: Mapping[str, Any]) -> dict[str, str]:
    records = report.get("record_sources")
    if not isinstance(records, list):
        raise ValueError("replay report is missing verified source-record identities")
    by_member = {}
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("replay source-record identities must be objects")
        member = item.get("member")
        digest = item.get("sha256")
        if not isinstance(member, str) or not member:
            raise ValueError("replay source-record identity is missing its member path")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("replay source-record SHA256 is invalid")
        previous = by_member.get(member)
        if previous is not None and previous != digest:
            raise ValueError("replay source member has conflicting SHA256 identities")
        by_member[member] = digest
    return by_member


def _configuration_index(
    report: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    raw = report.get("configurations")
    if not isinstance(raw, list) or not raw:
        raise ValueError("replay report must contain its configured campaign cohorts")
    if report.get("configuration_count") != len(raw):
        raise ValueError("replay configuration count differs from its report rows")
    configurations = []
    by_id = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("replay configuration report rows must be objects")
        config_id = item.get("configuration_id")
        config = item.get("config")
        if not isinstance(config_id, str) or not config_id or config_id in by_id:
            raise ValueError("replay configuration identifiers must be unique strings")
        if not isinstance(config, dict):
            raise ValueError("replay configuration is missing declared settings")
        budget = _integer(config.get("budget"), "configuration budget", minimum=1)
        planned = _integer(item.get("planned_campaign_count"), "planned campaign count", minimum=1)
        recorded = _integer(item.get("campaign_count"), "reported campaign count")
        missing = _integer(item.get("missing_campaign_count"), "reported missing count")
        if recorded + missing != planned:
            raise ValueError("replay configuration counts do not preserve the planned cohort")
        low_cost = config.get("low_fidelity_cost")
        low_cost = None if low_cost is None else _number(low_cost, "low_fidelity_cost", minimum=0)
        normalized = {
            **item,
            "configuration_id": config_id,
            "config": config,
            "budget": budget,
            "planned_campaign_count": planned,
            "campaign_count": recorded,
            "missing_campaign_count": missing,
            "low_fidelity_cost": low_cost,
        }
        configurations.append(normalized)
        by_id[config_id] = normalized
    return configurations, by_id


def _campaign_metadata(
    row: Mapping[str, Any],
    configurations: Mapping[str, dict[str, Any]],
    source_records: Mapping[str, str],
) -> dict[str, Any]:
    campaign_id = _required_text(row, "campaign_id")
    configuration_id = _required_text(row, "configuration_id")
    if configuration_id not in configurations:
        raise ValueError(f"campaign refers to an unreported configuration: {configuration_id}")
    config = configurations[configuration_id]
    if campaign_id in ("", configuration_id):
        raise ValueError("campaign identifier is missing its resolved campaign identity")
    if row.get("campaign_context") not in (
        "simulator-latent-reference",
        "archived-instrument-analysis",
    ):
        raise ValueError("campaign context is unsupported or missing")
    campaign_config = _json_object(_required_text(row, "config"), "campaign config")
    if campaign_config != config["config"]:
        raise ValueError("campaign config differs from its configuration report")

    analyte_set_index = _optional_integer(row.get("analyte_set_index"), "analyte_set_index")
    repeat_index = _optional_integer(row.get("repeat_index"), "repeat_index")
    if config.get("record_kind") == "simulated-benchmark":
        if analyte_set_index is None or repeat_index is None:
            raise ValueError("simulated campaign requires its resolved set and repeat identity")
        expected_id = f"{configuration_id}/set-{analyte_set_index}/repeat-{repeat_index}"
        if campaign_id != expected_id:
            raise ValueError("simulated campaign ID differs from its resolved set/repeat identity")
    elif analyte_set_index is not None:
        raise ValueError("non-simulated campaign must not claim an analyte-set index")

    source_member = _required_text(row, "source_member")
    source_sha256 = _required_text(row, "source_sha256")
    if len(source_sha256) != 64 or any(char not in "0123456789abcdef" for char in source_sha256):
        raise ValueError("campaign source SHA256 is invalid")
    if source_records.get(source_member) != source_sha256:
        raise ValueError("campaign source member or digest differs from the replay report")
    budget = _integer(row.get("declared_budget"), "campaign declared budget", minimum=1)
    if budget != config["budget"]:
        raise ValueError("campaign budget differs from its configuration")
    config_low_cost = config["low_fidelity_cost"]
    objective = config["config"].get("objective")
    if not isinstance(objective, str) or not objective:
        raise ValueError("campaign objective is missing from its configuration")
    return {
        "campaign_id": campaign_id,
        "configuration_id": configuration_id,
        "source_member": source_member,
        "source_sha256": source_sha256,
        "analyte_set_index": analyte_set_index,
        "repeat_index": repeat_index,
        "budget": budget,
        "config": config,
        "objective": objective,
        "low_fidelity_cost": config_low_cost,
        "row": row,
    }


def _match_campaign(campaign: Mapping[str, Any], row: Mapping[str, Any], sequence: int) -> None:
    if row.get("campaign_id") != campaign["campaign_id"]:
        raise ValueError("replay observation campaign identity is out of order")
    if row.get("source_member") != campaign["source_member"]:
        raise ValueError("replay observation source member differs from its campaign")
    if row.get("source_sha256") != campaign["source_sha256"]:
        raise ValueError("replay observation source digest differs from its campaign")
    if _integer(row.get("sequence"), "replay observation sequence", minimum=1) != sequence:
        raise ValueError("replay observation sequence is duplicated, missing or reordered")


def _qualification_facts(
    iterator: Iterator[dict[str, str]],
    replay_row: Mapping[str, Any],
    campaign: Mapping[str, Any],
) -> tuple[dict[str, bool | None], bool | None, bool | None, int]:
    facts: dict[tuple[str, str], bool | None] = {}
    raw_analytical = None
    raw_all_objectives = None
    raw_hard_initialized = False
    targeted_summary_rows_unavailable = 0
    score = _optional_number(replay_row.get("archived_score"), "replay archived score")
    archive_class = None if score is None else score >= SCORE_THRESHOLD
    for profile in MOSES_SCORE_PROFILES:
        for policy in INPUT_POLICIES:
            try:
                row = next(iterator)
            except StopIteration as error:
                raise ValueError(
                    "qualification rows end before matching all replay observations"
                ) from error
            expected_identity = {
                "campaign_id": campaign["campaign_id"],
                "configuration_id": campaign["configuration_id"],
                "sequence": str(replay_row["sequence"]),
                "source_member": campaign["source_member"],
                "source_sha256": campaign["source_sha256"],
                "analyte_set_index": (
                    ""
                    if campaign["analyte_set_index"] is None
                    else str(campaign["analyte_set_index"])
                ),
                "repeat_index": ""
                if campaign["repeat_index"] is None
                else str(campaign["repeat_index"]),
                "objective": campaign["objective"],
                "profile": profile,
                "input_policy": policy,
            }
            for name, expected in expected_identity.items():
                if row.get(name) != expected:
                    raise ValueError(f"qualification {name} differs from its replay observation")
            qual_score = _optional_number(row.get("archived_score"), "qualified archived score")
            _same_number(score, qual_score, "archived score")
            archived_boolean = _optional_bool(
                row.get("archived_score_at_least_two"), "archived_score_at_least_two"
            )
            if archived_boolean != archive_class:
                raise ValueError("qualification archived score class differs from the replay score")

            candidate_score = _optional_number(row.get("candidate_score"), "candidate score")
            candidate_boolean = _optional_bool(
                row.get("candidate_score_at_least_two"), "candidate_score_at_least_two"
            )
            candidate_status = row.get("status")
            if candidate_status == "scored":
                if candidate_score is None or candidate_boolean != (
                    candidate_score >= SCORE_THRESHOLD
                ):
                    raise ValueError("qualified candidate score and hard classification disagree")
            elif candidate_status == "unavailable":
                if candidate_score is not None or candidate_boolean is not None:
                    raise ValueError("unavailable qualified candidate has a score classification")
            else:
                raise ValueError("qualification candidate status is unsupported")
            if (
                campaign["objective"] == "SinglePeak-Resolution"
                and policy == "serialized_summary_hypothesis"
            ):
                if candidate_status != "unavailable":
                    raise ValueError(
                        "targeted-objective serialized-summary candidate must be unavailable"
                    )
                targeted_summary_rows_unavailable += 1
            facts[(profile, policy)] = candidate_boolean

            analytical = _optional_bool(
                row.get("raw_analytical_thresholds_satisfied"),
                "raw_analytical_thresholds_satisfied",
            )
            all_objectives = _optional_bool(
                row.get("raw_all_objective_thresholds_satisfied"),
                "raw_all_objective_thresholds_satisfied",
            )
            if not raw_hard_initialized:
                raw_analytical, raw_all_objectives = analytical, all_objectives
                raw_hard_initialized = True
            elif analytical != raw_analytical or all_objectives != raw_all_objectives:
                raise ValueError(
                    "raw hard-threshold results differ across qualification profiles or policies"
                )

    classifications = {}
    for series in _SERIES:
        name, kind = series["name"], series["kind"]
        if kind == "archived_score":
            classifications[name] = archive_class
        elif kind == "candidate_score":
            policy = series["policy"]
            if campaign["objective"] == "SinglePeak-Resolution":
                policy = "raw_resolution_array"
            classifications[name] = facts[(series["profile"], policy)]
        elif kind == "raw_analytical_thresholds_satisfied":
            classifications[name] = raw_analytical
        else:
            classifications[name] = raw_all_objectives
    return (
        classifications,
        raw_analytical,
        raw_all_objectives,
        targeted_summary_rows_unavailable,
    )


def _campaign_replay_observations(
    replay_rows: Iterable[dict[str, str]],
    qualifications: Iterator[dict[str, str]],
    campaign: Mapping[str, Any],
) -> tuple[dict[str, list[SuccessObservation]], dict[str, Any]]:
    grouped = {item["name"]: [] for item in _SERIES}
    high_count = low_count = 0
    observation_count = 0
    targeted_summary_rows_unavailable = 0
    for expected_sequence, row in enumerate(replay_rows, 1):
        observation_count += 1
        _match_campaign(campaign, row, expected_sequence)
        fidelity = _number(row.get("fidelity_level"), "replay fidelity level", minimum=0)
        if fidelity > 1:
            raise ValueError("replay fidelity level cannot exceed one")
        high_fidelity = fidelity >= 0.99
        if _optional_bool(row.get("high_fidelity"), "high_fidelity") is not high_fidelity:
            raise ValueError("replay high-fidelity role disagrees with its fidelity level")
        high_count += int(high_fidelity)
        low_count += int(not high_fidelity)
        if _integer(row.get("high_fidelity_count"), "high-fidelity count") != high_count:
            raise ValueError("replay cumulative high-fidelity count is inconsistent")
        if _integer(row.get("low_fidelity_count"), "low-fidelity count") != low_count:
            raise ValueError("replay cumulative low-fidelity count is inconsistent")
        classifications, _, _, unavailable_target_summaries = _qualification_facts(
            qualifications, row, campaign
        )
        targeted_summary_rows_unavailable += unavailable_target_summaries
        volume = _optional_number(row.get("program_volume_ml"), "program volume")
        observation = {
            name: SuccessObservation(
                sequence=expected_sequence,
                fidelity_role="high" if high_fidelity else "low",
                satisfied=classifications[name],
                program_volume_ml=volume,
            )
            for name in grouped
        }
        for name, item in observation.items():
            grouped[name].append(item)
    if not observation_count:
        raise ValueError("replay campaign has no observations")
    return grouped, {
        "observation_count": observation_count,
        "high_fidelity_count": high_count,
        "low_fidelity_count": low_count,
        "targeted_objective_observation_count": (
            observation_count if campaign["objective"] == "SinglePeak-Resolution" else 0
        ),
        "targeted_summary_candidate_rows_unavailable": targeted_summary_rows_unavailable,
    }


def _first_success_index(summary: Mapping[str, Any]) -> int | None:
    result = summary["first_confirmed_success"]
    return None if result is None else result["high_fidelity_index"]


def _budget_status(high_count: int, budget: int) -> str:
    if high_count > budget:
        return "over_budget"
    if high_count == budget:
        return "complete"
    return "incomplete"


def _check_campaign_summary(
    row: Mapping[str, Any], summary: Mapping[str, Any], counts: Mapping[str, int], budget: int
) -> None:
    if (
        _integer(row.get("num_observations"), "campaign observation count")
        != counts["observation_count"]
    ):
        raise ValueError("campaign observation count differs from its replay rows")
    for field in ("high_fidelity_count", "low_fidelity_count"):
        if _integer(row.get(field), f"campaign {field}") != counts[field]:
            raise ValueError(f"campaign {field} differs from its replay rows")
    if row.get("budget_status") != _budget_status(counts["high_fidelity_count"], budget):
        raise ValueError("campaign budget status differs from its replay rows")
    first_success = _optional_integer(
        row.get("first_success_high_fidelity_count"), "campaign first success", minimum=1
    )
    if first_success != _first_success_index(summary):
        raise ValueError("campaign archived first success differs from its replay observations")
    success_within_budget = _optional_bool(
        row.get("success_within_declared_budget"), "success_within_declared_budget"
    )
    if success_within_budget is not summary["success_within_budget"]:
        raise ValueError("campaign archived budget outcome differs from its replay observations")


def _state(summary: Mapping[str, Any], budget: int) -> str:
    return summary["trajectory"][budget - 1]["success_state"]


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return str(value)


def _baseline_trajectory_rows(bundle: VerifiedBundle) -> dict[tuple[str, int], dict[str, Any]]:
    result = {}
    for row in bundle.iter_csv("success-trajectories.csv"):
        config_id = _required_text(row, "configuration_id")
        budget = _integer(row.get("high_fidelity_budget"), "baseline trajectory budget", minimum=1)
        key = (config_id, budget)
        if key in result:
            raise ValueError(
                "archived success trajectories contain duplicate configuration budgets"
            )
        values = {
            "cohort_size": _integer(row.get("cohort_size"), "baseline cohort size", minimum=1),
            "recorded_campaign_count": _integer(
                row.get("recorded_campaign_count"), "baseline recorded campaign count"
            ),
            "observed_at_budget_count": _integer(
                row.get("observed_at_budget_count"), "baseline observed count"
            ),
            "unknown_success_count": _integer(
                row.get("unknown_success_count"), "baseline unknown count"
            ),
            "success_count": _integer(row.get("success_count"), "baseline success count"),
            "success_fraction": _number(row.get("success_fraction"), "baseline success fraction"),
            "success_fraction_upper_bound": _number(
                row.get("success_fraction_upper_bound"), "baseline success upper bound"
            ),
        }
        if values["success_count"] + values["unknown_success_count"] > values["cohort_size"]:
            raise ValueError("baseline success and unknown counts exceed the cohort")
        if not math.isclose(
            values["success_fraction"],
            values["success_count"] / values["cohort_size"],
            rel_tol=0,
            abs_tol=1e-15,
        ) or not math.isclose(
            values["success_fraction_upper_bound"],
            (values["success_count"] + values["unknown_success_count"]) / values["cohort_size"],
            rel_tol=0,
            abs_tol=1e-15,
        ):
            raise ValueError("baseline trajectory fractions disagree with its counts")
        result[key] = values
    return result


def _compare_archived_trajectory(
    config_id: str,
    budget: int,
    calculated: Mapping[str, Any],
    archived: Mapping[tuple[str, int], Mapping[str, Any]],
) -> None:
    expected = archived.get((config_id, budget))
    if expected is None:
        raise ValueError("archived success trajectories omit a configuration budget")
    fields = (
        "cohort_size",
        "recorded_campaign_count",
        "observed_at_budget_count",
        "unknown_success_count",
        "success_count",
    )
    if any(calculated[field] != expected[field] for field in fields):
        raise ValueError("archived-score trajectory differs from the original replay baseline")
    for field in ("success_fraction", "success_fraction_upper_bound"):
        if not math.isclose(calculated[field], expected[field], rel_tol=0, abs_tol=1e-15):
            raise ValueError("archived-score trajectory fraction differs from the replay baseline")


_CAMPAIGN_OUTPUT_FIELDS = [
    "campaign_id",
    "configuration_id",
    "analyte_set_index",
    "repeat_index",
    "source_member",
    "source_sha256",
    "objective",
    "series",
    "selected_input_policy",
    "budget",
    "observation_count",
    "high_fidelity_observation_count",
    "low_fidelity_observation_count",
    "unavailable_high_fidelity_indices",
    "first_confirmed_success_high_fidelity_index",
    "first_confirmed_success_sequence",
    "evaluation_cost_proxy",
    "evaluation_cost_proxy_status",
    "high_fidelity_program_volume_ml",
    "program_volume_status",
    "first_possible_success_high_fidelity_index",
    "lower_bound_high_fidelity_index",
    "upper_bound_high_fidelity_index",
    "success_within_budget",
    "budget_success_state",
]
_TRAJECTORY_OUTPUT_FIELDS = [
    "configuration_id",
    "series",
    "high_fidelity_budget",
    "cohort_size",
    "recorded_campaign_count",
    "observed_at_budget_count",
    "success_count",
    "unknown_success_count",
    "observed_no_success_count",
    "success_fraction",
    "success_fraction_upper_bound",
]
_PAIRED_OUTPUT_FIELDS = [
    "configuration_id",
    "campaign_id",
    "candidate_series",
    "declared_high_fidelity_budget",
    "archived_score_state",
    "candidate_state",
    "paired_outcome",
    "confirmed_success_gain",
    "confirmed_success_loss",
    "archived_first_confirmed_high_fidelity_index",
    "candidate_first_confirmed_high_fidelity_index",
    "candidate_minus_archived_first_index",
]


def _campaign_output_row(
    campaign: Mapping[str, Any], series: Mapping[str, Any], summary: Mapping[str, Any]
) -> dict[str, Any]:
    first = summary["first_confirmed_success"]
    return {
        "campaign_id": campaign["campaign_id"],
        "configuration_id": campaign["configuration_id"],
        "analyte_set_index": campaign["analyte_set_index"],
        "repeat_index": campaign["repeat_index"],
        "source_member": campaign["source_member"],
        "source_sha256": campaign["source_sha256"],
        "objective": campaign["objective"],
        "series": series["name"],
        "selected_input_policy": (
            "raw_resolution_array"
            if series["kind"] == "candidate_score"
            and campaign["objective"] == "SinglePeak-Resolution"
            else series["policy"]
        ),
        "budget": campaign["budget"],
        "observation_count": summary["observation_count"],
        "high_fidelity_observation_count": summary["high_fidelity_observation_count"],
        "low_fidelity_observation_count": summary["low_fidelity_observation_count"],
        "unavailable_high_fidelity_indices": summary["unavailable_high_fidelity_indices"],
        "first_confirmed_success_high_fidelity_index": (
            None if first is None else first["high_fidelity_index"]
        ),
        "first_confirmed_success_sequence": None if first is None else first["sequence"],
        "evaluation_cost_proxy": None if first is None else first["evaluation_cost_proxy"],
        "evaluation_cost_proxy_status": (
            None if first is None else first["evaluation_cost_proxy_status"]
        ),
        "high_fidelity_program_volume_ml": (
            None if first is None else first["high_fidelity_program_volume_ml"]
        ),
        "program_volume_status": None if first is None else first["program_volume_status"],
        "first_possible_success_high_fidelity_index": summary[
            "first_possible_success_high_fidelity_index"
        ],
        "lower_bound_high_fidelity_index": (
            None if first is None else first.get("lower_bound_high_fidelity_index")
        ),
        "upper_bound_high_fidelity_index": (
            None if first is None else first.get("upper_bound_high_fidelity_index")
        ),
        "success_within_budget": summary["success_within_budget"],
        "budget_success_state": _state(summary, campaign["budget"]),
    }


def _pair_row(
    campaign: Mapping[str, Any],
    series: Mapping[str, Any],
    archived: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    budget = campaign["budget"]
    archived_state = _state(archived, budget)
    candidate_state = _state(candidate, budget)
    both_confirmed = (
        archived["first_confirmed_success"] is not None
        and candidate["first_confirmed_success"] is not None
    )
    archived_first = _first_success_index(archived)
    candidate_first = _first_success_index(candidate)
    gain = candidate_state == "confirmed_success" and archived_state == "observed_no_success"
    loss = archived_state == "confirmed_success" and candidate_state == "observed_no_success"
    if archived_state == candidate_state:
        paired = "same_state"
    elif gain:
        paired = "candidate_confirmed_only"
    elif loss:
        paired = "archived_confirmed_only"
    else:
        paired = "mixed_with_unknown"
    return {
        "configuration_id": campaign["configuration_id"],
        "campaign_id": campaign["campaign_id"],
        "candidate_series": series["name"],
        "declared_high_fidelity_budget": budget,
        "archived_score_state": archived_state,
        "candidate_state": candidate_state,
        "paired_outcome": paired,
        "confirmed_success_gain": gain,
        "confirmed_success_loss": loss,
        "archived_first_confirmed_high_fidelity_index": archived_first,
        "candidate_first_confirmed_high_fidelity_index": candidate_first,
        "candidate_minus_archived_first_index": (
            candidate_first - archived_first if both_confirmed else None
        ),
    }


def _new_matrix() -> dict[str, dict[str, int]]:
    return {left: {right: 0 for right in _PAIR_STATES} for left in _PAIR_STATES}


def _check_configuration_summary(
    config: Mapping[str, Any],
    archived: list[dict[str, Any]],
    aggregate: list[dict[str, Any]],
    budget: int,
) -> None:
    success_count = aggregate[budget - 1]["success_count"]
    planned = config["planned_campaign_count"]
    if config.get("success_count") != success_count:
        raise ValueError("replay report success count differs from campaign score summaries")
    reported_fraction = _number(config.get("success_fraction"), "reported success fraction")
    if not math.isclose(reported_fraction, success_count / planned, rel_tol=0, abs_tol=1e-15):
        raise ValueError("replay report success fraction differs from the full cohort denominator")
    if _integer(config.get("complete_count"), "complete campaign count") != sum(
        item["high_fidelity_observation_count"] == budget for item in archived
    ):
        raise ValueError("replay report complete campaign count differs from campaign histories")
    if archived:
        observed_fraction = _number(
            config.get("observed_campaign_success_fraction"),
            "observed campaign success fraction",
        )
        expected = success_count / len(archived)
        if not math.isclose(observed_fraction, expected, rel_tol=0, abs_tol=1e-15):
            raise ValueError("replay observed success fraction differs from recorded campaigns")


def _write_output_row(writer, row: Mapping[str, Any], fields: list[str]) -> None:
    writer.writerow({key: _csv_cell(row.get(key)) for key in fields})


def _markdown_report(report: Mapping[str, Any]) -> str:
    lines = [
        "# MoSeS trajectory sensitivity",
        "",
        "This analysis compares high-fidelity success trajectories from verified replay and score-qualification bundles. It reuses previously qualified score evidence; it does not read the source ZIP, rerun the score scalarizer, or modify either input.",
        "",
        "## Series",
        "",
        "| Series | Definition |",
        "| --- | --- |",
    ]
    results = [
        "## Final states at each configuration's declared budget",
        "",
        report["overall_count_scope"],
        "",
        "| Rule | Confirmed | Observed no-success | Unknown | Planned |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in report["overall_final_budget_states"]:
        results.append(
            f"| `{row['series']}` | {row['confirmed_success_count']} | {row['observed_no_success_count']} | {row['unknown_count']} | {row['planned_campaign_count']} |"
        )
    results.extend(
        [
            "",
            "## Paired changes from archived scores",
            "",
            report["timing_scope"],
            "",
            "| Rule | Known gains | Known losses | Changes involving unknown | Earlier / later first confirmation |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in report["overall_paired_changes"]:
        results.append(
            f"| `{row['candidate_series']}` | {row.get('known_success_gain_count', 0)} | {row.get('known_success_loss_count', 0)} | {row.get('changed_state_involving_unknown_count', 0)} | {row.get('earlier_confirmed_event_count', 0)} / {row.get('later_confirmed_event_count', 0)} |"
        )
    # Put numerical results before the detailed series definitions.
    lines[4:4] = results + [""]
    lines.extend(f"| `{item['name']}` | {item['description']} |" for item in report["series"])
    lines.extend(
        [
            "",
            "All success decisions use high-fidelity observations only. Initial-design high-fidelity evaluations count toward budget; low-fidelity positives never establish success. Missing qualified values and unobserved histories remain unknown. Cohort fractions use each configuration's full planned campaign count, including campaigns absent from the replay.",
            "",
            "The two score-policy series named `summary_or_selected_target_array` use the serialized-summary hypothesis for non-targeted objectives. For `SinglePeak-Resolution`, they use only the selected raw per-analyte resolution entry because the summary hypothesis is unavailable for targeted objectives. Missing summary values elsewhere remain unknown and are not replaced by raw-array scores.",
            "",
            "Evaluation cost is a caller-declared proxy: high-fidelity count plus low-fidelity count multiplied by the declared low-fidelity cost. Program volume includes high-fidelity volume through first confirmed success. This does not establish physical measurement cost or optimizer performance.",
            "",
            "## Integrity and scope",
            "",
            f"- Replay observations: {report['integrity']['replay_observation_count']}",
            f"- Qualified score rows: {report['integrity']['qualification_row_count']}",
            f"- Configurations: {report['configuration_count']}",
            f"- Recorded campaigns: {report['campaign_count']}",
            f"- Planned campaign denominator: {report['planned_campaign_count']}",
            (
                "- Targeted-objective observations: "
                f"{report['targeted_objective_summary']['observation_count']}; "
                "profile-specific serialized-summary candidate rows unavailable and not imputed: "
                f"{report['targeted_objective_summary']['unavailable_candidate_row_count']}"
            ),
            f"- Verified source ZIP SHA256: `{report['source']['sha256']}`",
            "- The archived-score series must reproduce every cell in the original replay success-trajectories.csv; publication stops if it does not.",
            "",
            "## Paired comparisons",
            "",
            "Paired 3×3 state counts are recorded per configuration at its declared high-fidelity budget. The matrix includes `unknown`/`unknown` pairs for planned campaigns absent from the replay. Candidate-confirmed gains and losses in paired-comparisons.csv require the other method to have an observed no-success state; comparisons against unknown remain indeterminate.",
            "",
            "The source ZIP is not re-opened. Checksum validation proves the supplied evidence files match their completion manifests; it is not a cryptographic signature or a fresh source-record audit.",
            "",
        ]
    )
    return "\n".join(lines)


def compare_moses_trajectories(
    replay_dir: str | Path,
    qualification_dir: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Compare archived and qualified score success trajectories.

    CSV evidence is consumed in input order with one replay campaign resident at
    a time. Campaign identity and source digest must match across the replay and
    all four qualified score rows per observation. All source bundles are
    reverified before the staged output directory is atomically published.
    """
    replay = VerifiedBundle(
        replay_dir,
        required_files=set(REPLAY_FILES),
        profile=REPLAY_PROFILE,
    )
    qualification = VerifiedBundle(
        qualification_dir,
        required_files=set(QUALIFICATION_FILES),
        profile=QUALIFICATION_PROFILE,
    )
    source = _validate_source(replay, qualification)
    replay_report, qualification_report = replay.report, qualification.report
    configurations, configurations_by_id = _configuration_index(replay_report)
    baseline = _baseline_trajectory_rows(replay)

    replay_observation_count = _integer(
        replay_report.get("observation_count"), "replay report observation count"
    )
    if qualification_report.get("replay_observation_count") != replay_observation_count:
        raise ValueError("score qualification observation count differs from the replay report")
    if qualification_report.get("observation_count") != replay_observation_count:
        raise ValueError("score qualification row-group count differs from the replay report")
    expected_qualification_rows = (
        replay_observation_count * len(MOSES_SCORE_PROFILES) * len(INPUT_POLICIES)
    )
    if qualification_report.get("candidate_count") != expected_qualification_rows:
        raise ValueError(
            "score qualification candidate count is not four rows per replay observation"
        )
    if (
        qualification_report.get("integrity", {}).get("rows_matched_to_source")
        != replay_observation_count
    ):
        raise ValueError("score qualification source-match count differs from replay observations")

    implementation = _implementation_metadata()
    output_campaign_count = 0
    output_trajectory_count = 0
    output_pair_count = 0
    targeted_observation_count = 0
    targeted_summary_row_count = 0
    configuration_reports = []
    paired_cross_tabs = []

    with staged_bundle(output_dir, input_dirs=[replay.root, qualification.root]) as stage:
        with (
            (stage / "campaigns.csv").open("w", newline="", encoding="utf-8") as campaign_file,
            (stage / "paired-comparisons.csv").open(
                "w", newline="", encoding="utf-8"
            ) as paired_file,
            (stage / "success-trajectories.csv").open(
                "w", newline="", encoding="utf-8"
            ) as trajectory_file,
        ):
            campaign_writer = csv.DictWriter(campaign_file, fieldnames=_CAMPAIGN_OUTPUT_FIELDS)
            paired_writer = csv.DictWriter(paired_file, fieldnames=_PAIRED_OUTPUT_FIELDS)
            trajectory_writer = csv.DictWriter(
                trajectory_file, fieldnames=_TRAJECTORY_OUTPUT_FIELDS
            )
            campaign_writer.writeheader()
            paired_writer.writeheader()
            trajectory_writer.writeheader()

            replay_groups = iter(
                groupby(replay.iter_csv("observations.csv"), key=lambda row: row["campaign_id"])
            )
            qualification_rows = _counting_iterator(
                iter(qualification.iter_csv("observations.csv"))
            )
            campaign_rows = iter(replay.iter_csv("campaigns.csv"))
            seen_campaigns: set[str] = set()
            seen_observations = 0
            seen_configurations = set()
            pending_campaign = next(campaign_rows, None)

            for config in configurations:
                config_id = config["configuration_id"]
                per_configuration = {item["name"]: [] for item in _SERIES}
                matrices = {item["name"]: _new_matrix() for item in _CANDIDATE_SERIES}
                config_campaign_count = 0
                archived_campaign_summaries = []

                while (
                    pending_campaign is not None
                    and pending_campaign.get("configuration_id") == config_id
                ):
                    campaign_row = pending_campaign
                    pending_campaign = next(campaign_rows, None)
                    campaign = _campaign_metadata(
                        campaign_row, configurations_by_id, source["source_records"]
                    )
                    if campaign["campaign_id"] in seen_campaigns:
                        raise ValueError(
                            "campaign CSV contains a duplicate resolved campaign identity"
                        )
                    seen_campaigns.add(campaign["campaign_id"])
                    try:
                        group_id, observation_group = next(replay_groups)
                    except StopIteration as error:
                        raise ValueError(
                            "replay observations omit a campaign listed in campaigns.csv"
                        ) from error
                    if group_id != campaign["campaign_id"]:
                        raise ValueError(
                            "replay campaigns and observation groups are missing or reordered"
                        )

                    observations, counts = _campaign_replay_observations(
                        observation_group, qualification_rows, campaign
                    )
                    campaign_count = counts["observation_count"]
                    seen_observations += campaign_count
                    targeted_observation_count += counts["targeted_objective_observation_count"]
                    targeted_summary_row_count += counts[
                        "targeted_summary_candidate_rows_unavailable"
                    ]
                    if qualification_rows.rows_consumed != seen_observations * 4:
                        raise ValueError(
                            "qualification evidence rows do not cover every replay observation"
                        )

                    summaries = {
                        item["name"]: summarize_campaign_success(
                            observations[item["name"]],
                            budget=config["budget"],
                            low_fidelity_cost=config["low_fidelity_cost"],
                        )
                        for item in _SERIES
                    }
                    for series in _SERIES:
                        summary = summaries[series["name"]]
                        per_configuration[series["name"]].append(summary)
                        _write_output_row(
                            campaign_writer,
                            _campaign_output_row(campaign, series, summary),
                            _CAMPAIGN_OUTPUT_FIELDS,
                        )
                        output_campaign_count += 1
                    _check_campaign_summary(
                        campaign_row,
                        summaries["archived_score_at_least_two"],
                        counts,
                        config["budget"],
                    )
                    archived_campaign_summaries.append(summaries["archived_score_at_least_two"])
                    for series in _CANDIDATE_SERIES:
                        pair = _pair_row(
                            campaign,
                            series,
                            summaries["archived_score_at_least_two"],
                            summaries[series["name"]],
                        )
                        _write_output_row(paired_writer, pair, _PAIRED_OUTPUT_FIELDS)
                        output_pair_count += 1
                        matrices[series["name"]][pair["archived_score_state"]][
                            pair["candidate_state"]
                        ] += 1
                    config_campaign_count += 1

                if pending_campaign is not None:
                    pending_id = pending_campaign.get("configuration_id")
                    if pending_id not in configurations_by_id:
                        raise ValueError("campaign CSV references an unreported configuration")
                    config_position = configurations.index(config)
                    pending_position = configurations.index(configurations_by_id[pending_id])
                    if pending_position < config_position:
                        raise ValueError("campaign CSV configuration rows are reordered")
                seen_configurations.add(config_id)
                if config_campaign_count != config["campaign_count"]:
                    raise ValueError(
                        "campaign CSV count differs from its replay configuration report"
                    )

                for series in _SERIES:
                    trajectory = aggregate_success_trajectories(
                        per_configuration[series["name"]],
                        planned_count=config["planned_campaign_count"],
                        budget=config["budget"],
                    )
                    for point in trajectory:
                        row = {"configuration_id": config_id, "series": series["name"], **point}
                        _write_output_row(trajectory_writer, row, _TRAJECTORY_OUTPUT_FIELDS)
                        output_trajectory_count += 1
                    if series["kind"] == "archived_score":
                        for point in trajectory:
                            _compare_archived_trajectory(
                                config_id,
                                point["high_fidelity_budget"],
                                point,
                                baseline,
                            )
                        _check_configuration_summary(
                            config,
                            archived_campaign_summaries,
                            trajectory,
                            config["budget"],
                        )
                for series in _CANDIDATE_SERIES:
                    matrix = matrices[series["name"]]
                    missing_pairs = config["planned_campaign_count"] - config_campaign_count
                    matrix["unknown"]["unknown"] += missing_pairs
                    paired_cross_tabs.append(
                        {
                            "configuration_id": config_id,
                            "candidate_series": series["name"],
                            "declared_high_fidelity_budget": config["budget"],
                            "planned_campaign_count": config["planned_campaign_count"],
                            "recorded_campaign_pair_count": config_campaign_count,
                            "unrecorded_campaign_pair_count": missing_pairs,
                            "states": list(_PAIR_STATES),
                            "matrix": matrix,
                        }
                    )
                configuration_reports.append(
                    {
                        "configuration_id": config_id,
                        "objective": config["config"].get("objective"),
                        "record_kind": config.get("record_kind"),
                        "scientific_settings": {
                            key: config["config"][key]
                            for key in (
                                "num_segments",
                                "surrogate_model",
                                "acquisition_function",
                                "objective_type",
                                "seed_strategy",
                                "seed_budget",
                                "multi_fidelity",
                                "segment_based",
                                "advanced_peak_detection",
                            )
                            if key in config["config"]
                        },
                        "declared_high_fidelity_budget": config["budget"],
                        "planned_campaign_count": config["planned_campaign_count"],
                        "recorded_campaign_count": config_campaign_count,
                        "missing_campaign_count": config["planned_campaign_count"]
                        - config_campaign_count,
                    }
                )

            if pending_campaign is not None:
                raise ValueError("campaign CSV has rows outside the reported configuration order")
            if next(replay_groups, None) is not None:
                raise ValueError("replay observations contain a campaign absent from campaigns.csv")
            if next(qualification_rows, None) is not None:
                raise ValueError(
                    "score qualification contains rows beyond the matched replay observations"
                )
            seen_qualification_rows = qualification_rows.rows_consumed
            if seen_observations != replay_observation_count:
                raise ValueError("replay observation rows differ from the replay report count")
            if seen_qualification_rows != expected_qualification_rows:
                raise ValueError("qualification rows differ from the qualified report count")
            if len(seen_campaigns) != _integer(
                replay_report.get("campaign_count"), "replay campaign count"
            ):
                raise ValueError("replay campaign rows differ from the replay report count")
            if len(seen_configurations) != len(configurations):
                raise ValueError(
                    "replay report includes a configuration not covered by its campaign CSV"
                )
            if len(baseline) != sum(item["budget"] for item in configurations):
                raise ValueError(
                    "archived success trajectories have missing or extra configuration budgets"
                )

        descriptive_summary = summarize_outputs(stage)
        total_planned = sum(item["planned_campaign_count"] for item in configurations)
        report = {
            "schema_version": 1,
            "profile": "moses-trajectory-sensitivity/1",
            **descriptive_summary,
            "scope": (
                "Compare high-fidelity success trajectories over previously qualified score evidence. "
                "No raw source ZIP, optimizer, instrument, scalarizer rerun or chromatography calculation is used."
            ),
            "threshold": SCORE_THRESHOLD,
            "high_fidelity_threshold": 0.99,
            "budget_policy": (
                "Count every high-fidelity observation, including initial-design evaluations. "
                "Retain observations beyond the declared budget in campaign histories; trajectory points end at the declared budget."
            ),
            "cohort_policy": (
                "Use each configuration's full planned campaign count. Missing or unobserved campaign outcomes remain unknown."
            ),
            "low_fidelity_policy": (
                "Low-fidelity observations never establish success. Their declared cost is used only in the evaluation-cost proxy."
            ),
            "targeted_objective_policy": (
                "For SinglePeak-Resolution only, summary-or-target series select the qualified raw_resolution_array row, "
                "which contains the selected analyte entry. Other objectives use serialized_summary_hypothesis only; "
                "unavailable summaries remain unknown."
            ),
            "paired_comparison_policy": (
                "Compare candidate and archived-score states at the configuration's declared high-fidelity budget. "
                "Only candidate-confirmed versus archived-observed-no-success is a gain; the reverse is a loss. "
                "Missing campaigns contribute unknown/unknown in each paired state matrix."
            ),
            "series": [dict(item) for item in _SERIES],
            "configuration_count": len(configurations),
            "campaign_count": len(seen_campaigns),
            "planned_campaign_count": total_planned,
            "replay_observation_count": seen_observations,
            "qualification_row_count": seen_qualification_rows,
            "targeted_objective_summary": {
                "observation_count": targeted_observation_count,
                "unavailable_candidate_row_count": targeted_summary_row_count,
                "policy": (
                    "Serialized-summary candidate rows for SinglePeak-Resolution are unavailable; "
                    "the summary-or-target series use only the qualified selected raw resolution entry."
                ),
            },
            "output_row_counts": {
                "campaigns": output_campaign_count,
                "success_trajectories": output_trajectory_count,
                "paired_comparisons": output_pair_count,
            },
            "configurations": configuration_reports,
            "paired_state_cross_tabs": paired_cross_tabs,
            "source": {
                "sha256": source["source"]["observed"]["sha256"],
                "md5": source["source"]["observed"]["md5"],
                "size_bytes": source["source"]["observed"]["size_bytes"],
                "member_count": source["archive_summary"]["member_count"],
                "archive_manifest_sha256": source["archive_manifest_sha256"],
                "replay_archive_manifest_file_sha256": replay.file_digest("archive-manifest.json"),
            },
            "integrity": {
                "replay_profile": replay.profile,
                "qualification_profile": qualification.profile,
                "replay_observation_count": seen_observations,
                "qualification_row_count": seen_qualification_rows,
                "replay_complete_sha256": _file_sha256(replay.root / "complete.json"),
                "qualification_complete_sha256": _file_sha256(qualification.root / "complete.json"),
                "replay_observation_sha256": replay.file_digest("observations.csv"),
                "qualification_observation_sha256": qualification.file_digest("observations.csv"),
                "replay_campaigns_sha256": replay.file_digest("campaigns.csv"),
                "replay_trajectory_sha256": replay.file_digest("success-trajectories.csv"),
                "source_manifest_sha256": replay.file_digest("archive-manifest.json"),
                "archived_trajectory_reproduced": True,
            },
            "software": implementation,
        }
        (stage / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        (stage / "report.md").write_text(_markdown_report(report), encoding="utf-8")

        replay.verify_unchanged()
        qualification.verify_unchanged()
        if _implementation_metadata() != implementation:
            raise ValueError("trajectory sensitivity implementation changed during analysis")

    return report


class _CountingIterator:
    """Track rows consumed while keeping qualification CSV streaming."""

    def __init__(self, source: Iterator[dict[str, str]]):
        self.source = source
        self.rows_consumed = 0

    def __iter__(self):
        return self

    def __next__(self):
        row = next(self.source)
        self.rows_consumed += 1
        return row


def _counting_iterator(source: Iterator[dict[str, str]]) -> _CountingIterator:
    return source if isinstance(source, _CountingIterator) else _CountingIterator(source)
