"""Replay the authors' archived observations without rerunning their optimizer.

The score is an archived BoTier output. Raw detection/resolution metrics are
recomputed independently. No simulator, acquisition policy, or instrument is
executed by this module.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import re
import shutil
import tempfile
from collections import Counter
from importlib import metadata
from pathlib import Path

from measure_methods import chromatography, program_solvent_use, volume
from measure_methods.chromatography import summarize_chromatography_observation

from . import moses_archive, pickle_records
from .moses_archive import inspect_archive, read_member
from .pickle_records import decode_archive_record

PROFILE = "moses-zenodo-v1-archived-observations/1"
SCORE_THRESHOLD = 2.0
HIGH_FIDELITY_THRESHOLD = 0.99
_CONFIG_FIELDS = (
    "num_compounds",
    "compound_idx",
    "num_segments",
    "min_segment_length",
    "segment_based",
    "max_run_time",
    "flow_rate",
    "advanced_peak_detection",
    "surrogate_model",
    "acquisition_function",
    "budget",
    "seed_budget",
    "seed_strategy",
    "multi_fidelity",
    "only_current_fidelity",
    "max_simulation_fidelity",
    "simulation_fidelity_decay",
    "low_fidelity_cost",
    "max_consecutive_low_fidelity",
    "mixed_fidelity",
    "objective",
    "objective_type",
    "count_threshold",
    "resolution_threshold",
    "run_time_threshold",
)


def _number(value: object, name: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return result


def _integer(value: object, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _json_object(payload: bytes) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    # The authors explicitly store NaN and Infinity. Metrics preserve their
    # meanings and output JSON null plus statuses; this does not repair them.
    return _object(json.loads(payload, object_pairs_hook=unique_pairs), "record")


def _safe_config(config: dict) -> dict:
    # Source configs also include author-machine paths. Retain only scientific
    # settings; the member digest identifies the complete original record.
    result = {name: config[name] for name in _CONFIG_FIELDS if name in config}
    json.dumps(result, allow_nan=False)
    return result


def _program_volume(gradient: dict) -> float | None:
    table = gradient.get("gradient_table")
    header = gradient.get("header_gradient_table")
    if not isinstance(table, list) or not isinstance(header, list):
        return None
    if "time" not in header or "flow_rate" not in header:
        return None
    time_index, flow_index = header.index("time"), header.index("flow_rate")
    if len(table) < 2:
        return None
    if any(not isinstance(row, list) or len(row) != len(header) for row in table):
        raise ValueError("gradient table rows must match their declared header")
    times = [row[time_index] for row in table]
    flows = [row[flow_index] for row in table]
    # Integrate total flow only. Rounded source A/B fractions need not sum
    # exactly to one; neither fraction is silently normalized here.
    return program_solvent_use(times, flows, [{"total": 1.0} for _ in table])["total"]


def replay_campaign(
    campaign: dict,
    *,
    campaign_id: str,
    source_member: str,
    source_sha256: str,
    observation_origin: str,
    score_threshold: float = SCORE_THRESHOLD,
) -> dict:
    """Recompute one campaign, retaining every observation and seed expense.

    High fidelity means >= 0.99, matching HPLCExperiment.high_fidelity in the
    reviewed source. That label is an archive fidelity role; it does not mean a
    simulation benchmark was physically measured. First success uses only a
    finite archived score >= the explicitly declared threshold.
    """
    threshold = _number(score_threshold, "score_threshold")
    config = _object(campaign.get("config"), "campaign config")
    declared = _integer(config.get("num_compounds"), "config num_compounds")
    resolution_threshold = _number(config.get("resolution_threshold"), "resolution_threshold")
    observations = campaign.get("experiments")
    if not isinstance(observations, list) or not observations:
        raise ValueError("campaign must contain a nonempty experiment list")
    hf_count = lf_count = 0
    best_score = None
    first_success = None
    best_row = None
    total_program_volume = 0.0
    missing_volume = missing_score = 0
    diagnostic_counts: Counter = Counter()
    rows = []
    for sequence, observation in enumerate(observations, 1):
        observation = _object(observation, "experiment")
        if _integer(observation.get("num_compounds"), "experiment num_compounds") != declared:
            raise ValueError("experiment and campaign num_compounds disagree")
        gradient = _object(observation.get("gradient"), "gradient")
        runtime = _number(gradient.get("run_time"), "gradient run_time")
        fidelity = observation.get("fidelity_level")
        if fidelity is None:
            legacy = observation.get("high_fidelity", True)
            if not isinstance(legacy, bool):
                raise ValueError("legacy high_fidelity must be a boolean")
            fidelity = 1.0 if legacy else 0.5
        fidelity = _number(fidelity, "fidelity_level")
        if fidelity > 1:
            raise ValueError("fidelity_level must not exceed one")
        high_fidelity = fidelity >= HIGH_FIDELITY_THRESHOLD
        hf_count += int(high_fidelity)
        lf_count += int(not high_fidelity)
        metrics = summarize_chromatography_observation(
            num_compounds=declared,
            retention_times=observation.get("retention_times"),
            peak_resolutions=observation.get("peak_resolutions"),
            fwhms=observation.get("fwhms"),
            areas=observation.get("peak_areas"),
            run_time=runtime,
            retention_time_unit="min",
            fwhm_unit="min",
            run_time_unit="min",
            area_unit="archive-reported",
            resolution_threshold=resolution_threshold,
        )
        reference = metrics["reference_resolution_metrics"]
        diagnostics = metrics["diagnostics"]
        reported_checks = {}
        for field in ("num_peaks", "num_resolved_peaks", "critical_resolution"):
            if field not in observation:
                continue
            reported = observation[field]
            calculated = reference[field]
            valid = (
                not isinstance(reported, bool)
                and isinstance(reported, (int, float))
                and math.isfinite(reported)
            )
            agrees = (
                valid
                and calculated is not None
                and math.isclose(reported, calculated, rel_tol=1e-12, abs_tol=1e-12)
            )
            reported_checks[field] = {
                "reported": reported if valid else None,
                "recomputed": calculated,
                "status": "agrees" if agrees else "disagrees",
            }
            if not agrees:
                diagnostics.append({"code": "reported_" + field + "_disagrees"})
        diagnostic_counts.update(d["code"] for d in diagnostics)
        score = observation.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            if score is not None:
                raise ValueError("archived score must be numeric or null")
            score = None
        elif not math.isfinite(score):
            score = None
        if score is None:
            missing_score += 1
        volume = _program_volume(gradient)
        if high_fidelity:
            if volume is None:
                missing_volume += 1
            else:
                total_program_volume += volume
        row = {
            "campaign_id": campaign_id,
            "source_member": source_member,
            "source_sha256": source_sha256,
            "sequence": sequence,
            "observation_origin": (
                "simulator-latent-reference"
                if observation_origin == "archived-instrument-analysis" and not high_fidelity
                else observation_origin
            ),
            "fidelity_level": fidelity,
            "high_fidelity": high_fidelity,
            "high_fidelity_count": hf_count,
            "low_fidelity_count": lf_count,
            "archived_score": score,
            "archived_score_status": "finite" if score is not None else "unavailable",
            "num_compounds": declared,
            "run_time_min": runtime,
            "num_peaks": reference["num_peaks"],
            "num_resolved_peaks": reference["num_resolved_peaks"],
            "critical_resolution": reference["critical_resolution"],
            "time_window_eluted_count": metrics["time_window_elution_metrics"][
                "num_eluted_within_run_time"
            ],
            "program_volume_ml": volume,
            "diagnostic_codes": sorted({d["code"] for d in diagnostics}),
            "reported_summary_checks": reported_checks,
            "measurement_status_counts": {
                name: dict(Counter(item["status"] for item in vector["values"]))
                if vector["values"] is not None
                else {"array_unavailable": declared}
                for name, vector in metrics["measurements"].items()
            },
        }
        if high_fidelity and score is not None:
            if best_score is None or score >= best_score:
                best_score, best_row = score, row.copy()
            if first_success is None and score >= threshold:
                first_success = hf_count
        row["best_high_fidelity_score"] = best_score
        rows.append(row)
    budget = _integer(config.get("budget"), "budget")
    low_cost = config.get("low_fidelity_cost")
    declared_cost = (
        None if low_cost is None else hf_count + lf_count * _number(low_cost, "low_fidelity_cost")
    )
    return {
        "campaign_id": campaign_id,
        "source_member": source_member,
        "source_sha256": source_sha256,
        "campaign_context": observation_origin,
        "observation_origin": rows[0]["observation_origin"]
        if len({r["observation_origin"] for r in rows}) == 1
        else "mixed-instrument-and-simulated",
        "config": _safe_config(config),
        "num_observations": len(rows),
        "high_fidelity_count": hf_count,
        "low_fidelity_count": lf_count,
        "declared_budget": budget,
        "declared_seed_budget": config.get("seed_budget"),
        "budget_status": "over_budget"
        if hf_count > budget
        else "complete"
        if hf_count == budget
        else "incomplete",
        "first_success_high_fidelity_count": first_success,
        "success_within_declared_budget": first_success is not None and first_success <= budget,
        "score_threshold": threshold,
        "missing_score_count": missing_score,
        "unavailable_high_fidelity_score_indices": [
            r["high_fidelity_count"]
            for r in rows
            if r["high_fidelity"] and r["archived_score"] is None
        ],
        "best_high_fidelity_score": best_score,
        "best_high_fidelity_observation": best_row,
        "observed_program_volume_ml": total_program_volume,
        "missing_high_fidelity_volume_count": missing_volume,
        "declared_evaluation_cost_proxy": declared_cost,
        "diagnostic_counts": dict(sorted(diagnostic_counts.items())),
        "observations": rows,
    }


def _benchmark_campaigns(record: dict, member: str):
    config = _object(record.get("config"), "benchmark config")
    count = _integer(config.get("num_compound_sets"), "num_compound_sets")
    repeats = _integer(config.get("iterations_per_compound_set"), "iterations_per_compound_set")
    groups = record.get("campaigns")
    if not isinstance(groups, list) or len(groups) != count:
        raise ValueError(f"benchmark campaign groups disagree with declared cohort: {member}")
    for set_index, group in enumerate(groups):
        if not isinstance(group, list) or len(group) != repeats:
            raise ValueError(f"benchmark repeats disagree with declared cohort: {member}")
        for repeat, campaign in enumerate(group):
            yield set_index, repeat, _object(campaign, "campaign")


def _csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, allow_nan=False, sort_keys=True)
                    if isinstance(value, (list, dict))
                    else value
                    for key, value in row.items()
                }
            )


def _history_fingerprint(observations):
    # Convenience summaries are checked independently. They are not raw
    # observations and some older grouped snapshots omit them.
    raw = [
        {
            key: value
            for key, value in observation.items()
            if key not in {"num_peaks", "num_resolved_peaks", "critical_resolution"}
        }
        for observation in observations
    ]
    for observation in raw:
        if isinstance(observation.get("gradient"), dict):
            observation["gradient"] = {
                key: value for key, value in observation["gradient"].items() if key != "name"
            }
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=True).encode()
    ).hexdigest()


def _matching_config(campaign):
    config = _safe_config(_object(campaign.get("config"), "campaign config"))
    # This field was added after some snapshots; the source default is False.
    config.setdefault("mixed_fidelity", False)
    config.setdefault("compound_idx", None)
    return config


def _reconcile_campaign_records(record, group_id, member_names, read_payload):
    """Match compacted groups by content, never by their displayed index."""
    grouped = list(_benchmark_campaigns(record, group_id))
    labelled, records = {}, []
    pattern = re.compile(re.escape(group_id) + r"/CmpdSet-(\d+)(?:_Iter-(\d+))?/campaign\.pkl")
    for member in sorted(member_names):
        match = pattern.fullmatch(member)
        if match is None:
            continue
        key = (int(match[1]), int(match[2] or 0))
        if key in labelled:
            raise ValueError("multiple labelled campaign records have the same identity")
        payload = read_payload(member)
        candidate = _object(decode_archive_record(payload), "individual campaign")
        if not isinstance(candidate.get("experiments"), list) or not candidate["experiments"]:
            raise ValueError("individual campaign requires nonempty observations")
        labelled[key] = candidate, member, "individual_only"
        records.append(
            {
                "member": member,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "format": "data-only-pickle",
            }
        )
    if not labelled:
        # The random-design baseline only has its complete 50-group snapshot.
        # Mark the original driver-order interpretation rather than pretending
        # labelled sidecars verified the identities.
        return {
            (index, repeat): (
                campaign,
                group_id + "/results.pkl",
                "serialized_complete_cohort_order",
            )
            for index, repeat, campaign in grouped
        }, records
    campaigns = dict(labelled)
    matched = set()
    fingerprints = {}

    def fingerprint(observations, length):
        key = id(observations), length
        if key not in fingerprints:
            fingerprints[key] = _history_fingerprint(observations[:length])
        return fingerprints[key]

    for group_index, repeat, previous in grouped:
        before = previous.get("experiments")
        if not isinstance(before, list) or not before:
            raise ValueError("grouped campaign requires nonempty observations")
        candidates = []
        for key, (candidate, member, _) in labelled.items():
            if key[1] != repeat or _matching_config(previous) != _matching_config(candidate):
                continue
            after = candidate["experiments"]
            common = min(len(before), len(after))
            if fingerprint(before, 1) != fingerprint(after, 1):
                continue
            if fingerprint(before, common) == fingerprint(after, common):
                candidates.append((key, candidate, member))
        if len(candidates) != 1:
            raise ValueError(
                f"grouped campaign {group_index}, repeat {repeat} in {group_id} has {len(candidates)} matching labelled histories; identity is ambiguous or divergent"
            )
        key, candidate, member = candidates[0]
        if key in matched:
            raise ValueError("multiple grouped campaigns match one labelled history")
        matched.add(key)
        if len(candidate["experiments"]) > len(before):
            campaigns[key] = candidate, member, "individual_continuation"
        elif len(before) > len(candidate["experiments"]):
            campaigns[key] = previous, group_id + "/results.pkl", "grouped_continuation"
        else:
            campaigns[key] = candidate, member, "matching_labelled_history"
    return campaigns, records


def _cohort_trajectory(group, planned_count, budget):
    for spent in range(1, budget + 1):
        reached = sum(
            c["first_success_high_fidelity_count"] is not None
            and c["first_success_high_fidelity_count"] <= spent
            for c in group
        )
        observed = sum(c["high_fidelity_count"] >= spent for c in group)
        unknown = (
            planned_count
            - len(group)
            + sum(
                not (
                    c["first_success_high_fidelity_count"] is not None
                    and c["first_success_high_fidelity_count"] <= spent
                )
                and (
                    c["high_fidelity_count"] < spent
                    or any(i <= spent for i in c.get("unavailable_high_fidelity_score_indices", []))
                )
                for c in group
            )
        )
        yield {
            "high_fidelity_budget": spent,
            "cohort_size": planned_count,
            "recorded_campaign_count": len(group),
            "observed_at_budget_count": observed,
            "unknown_success_count": unknown,
            "success_count": reached,
            "success_fraction": reached / planned_count,
            "success_fraction_upper_bound": (reached + unknown) / planned_count,
        }


def _implementation_metadata():
    return {
        "distribution": "measure-service",
        "version": metadata.version("measure-service"),
        "python": platform.python_version(),
        "implementation_sha256": {
            module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (moses_archive, pickle_records, chromatography, volume)
        },
        "replay_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def _configuration_label(config):
    return (
        " / ".join(
            str(config.get(key, "unspecified"))
            for key in (
                "num_segments",
                "surrogate_model",
                "acquisition_function",
                "objective_type",
                "seed_strategy",
                "seed_budget",
            )
        )
        + (" / multi" if config.get("multi_fidelity") else " / single")
        + (" / segment-based" if config.get("segment_based") else " / time-based")
        + (" / advanced flag" if config.get("advanced_peak_detection") else " / basic flag")
    )


def _markdown_report(report, campaigns):
    lines = [
        "# MoSeS archived-observation reproduction",
        "",
        f"Profile: `{PROFILE}`. Source: [Zenodo 10.5281/zenodo.22109729](https://doi.org/10.5281/zenodo.22109729).",
        "",
        "This report replays stored BoTier scores and independently recomputes raw detection, resolution and runtime metrics. It does not rerun optimization or chromatogram interpretation.",
        "",
        f"Verified archive SHA256: `{report['archive_source']['observed']['sha256']}`.",
        "",
        "## Simulated benchmark",
        "",
        "High fidelity is a benchmark role, not a physical measurement. Every simulated observation uses latent per-analyte reference outputs. A recorded score >= 2 is satisfactory under this declared replay profile. Initial-design evaluations count toward the budget.",
        "",
        "Configuration columns: segments / surrogate / acquisition / prediction task / initial design / initial runs / fidelity / time parameterization / author processing flag.",
        "",
        "| Configuration | Recorded / planned campaigns | Complete | Score >= 2 by budget | Confirmed fraction |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for item in report["configurations"]:
        if item["record_kind"] != "simulated-benchmark":
            continue
        lines.append(
            f"| {_configuration_label(item['config'])} | {item['campaign_count']} / {item['planned_campaign_count']} | {item['complete_count']} | {item['success_count']} | {100 * item['success_fraction']:.1f}% |"
        )
    lines.extend(
        [
            "",
            "Fractions use all 50 archived input mixtures. Missing or unfinished histories remain unknown, with identified upper bounds in `success-trajectories.csv`; they are not counted as established failures. Shaded plot bounds are not confidence intervals.",
            "",
            "The compoundwise multi-fidelity grouped snapshot compacts completed campaigns. Labelled individual histories establish task identities and retain incomplete campaigns. `snapshot_status` and both member digests record reconciliation. The random-design baseline has only a complete grouped snapshot; its task order follows the source driver and is explicitly marked.",
            "",
            "## Experimental campaign replay",
            "",
            "These are the authors' archived instrument-analysis results, not newly validated measurements. Low-fidelity rows within these campaigns are separately labelled as simulated.",
            "",
            "| Campaign | Instrument-role evaluations | Simulated evaluations | First instrument-role score >= 2 | Best archived score |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in campaigns:
        if item["campaign_context"] != "archived-instrument-analysis":
            continue
        label = (
            "Known mixture"
            if "known_mixture" in item["source_member"]
            else "Target-focused mixture"
        )
        lines.append(
            f"| {label} | {item['high_fidelity_count']} | {item['low_fidelity_count']} | {item['first_success_high_fidelity_count']} | {item['best_high_fidelity_score']:.8f} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation and evidence",
            "",
            "- `report.json` records the named source profile, scientific configurations, all source member digests, input-mixture identities and actual analysis implementation hashes.",
            "- `campaigns.csv` includes incomplete/over-budget histories, seed expenditure, declared evaluation-cost proxies and program-only volume. Flushes and equilibration are excluded from volume, so it is not total instrument consumption.",
            "- `observations.csv` keeps finite-resolution detection separate from time-window elution, distinguishes missing and non-eluting values, and records disagreement diagnostics without repairing source data.",
            "- `success-trajectories.csv` retains the full cohort denominator and unknown-outcome bounds at every evaluation budget.",
            "- `complete.json` verifies every emitted evidence file. Original array data and source machine paths are not copied into this report.",
            "",
            "The author-labelled advanced detection flag is unused by the reviewed simulation wrapper. Differences between these curves cannot qualify DAD/MS processing or analyte tracking. Stored scalar scores are not independently recalculated by this milestone; native retention models, fresh optimizer comparisons and raw-signal reproduction remain separate work.",
            "",
        ]
    )
    return "\n".join(lines)


def _publish_results(target, report, manifest, campaign_rows, observation_rows, trajectory_rows):
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".moses-replay-", dir=target.parent))
    try:
        for name, value in (("report.json", report), ("archive-manifest.json", manifest)):
            (stage / name).write_text(
                json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
                encoding="utf-8",
            )
        (stage / "report.md").write_text(_markdown_report(report, campaign_rows), encoding="utf-8")
        _csv(stage / "campaigns.csv", campaign_rows)
        _csv(stage / "observations.csv", observation_rows)
        _csv(stage / "success-trajectories.csv", trajectory_rows)
        files = {
            item.name: {
                "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                "size_bytes": item.stat().st_size,
            }
            for item in sorted(stage.iterdir())
        }
        (stage / "complete.json").write_text(
            json.dumps({"schema_version": 1, "files": files}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if target.exists() and any(target.iterdir()):
            raise ValueError(
                "output directory changed during replay; existing files are not overwritten"
            )
        os.replace(stage, target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def analyze_archive(archive_path: str | Path, output_dir: str | Path) -> dict:
    """Verify the official archive and emit complete, deterministic replay tables."""
    target = Path(output_dir)
    if target.exists() and any(target.iterdir()):
        raise ValueError("output directory must be empty; existing results are not overwritten")
    implementation = _implementation_metadata()
    manifest = inspect_archive(archive_path)
    members = manifest["archive"]["members"]
    analyte_sets = []
    for item in members:
        name = item["name"]
        match = re.fullmatch(
            r"results/compound_sets/(\d+)x(\d+)C_20min_(\d+)/compound_set_(\d+)\.json", name
        )
        if match is None:
            continue
        payload = read_member(archive_path, name)
        compounds = json.loads(payload)
        if not isinstance(compounds, list) or len(compounds) != int(match[2]):
            raise ValueError("analyte set length disagrees with declared cohort")
        analyte_sets.append(
            {
                "source_member": name,
                "source_sha256": hashlib.sha256(payload).hexdigest(),
                "cohort_size": int(match[1]),
                "num_compounds": int(match[2]),
                "compound_set_hash": match[3],
                "analyte_set_index": int(match[4]),
            }
        )
    selected = [
        m["name"]
        for m in members
        if ("/in_silico_bo_benchmarks/" in m["name"] and m["name"].endswith("/results.pkl"))
        or (
            m["name"].endswith("/campaign_summary.json")
            and any(
                part in m["name"]
                for part in ("/known_mixture_optimization/", "/laser_optimization/")
            )
        )
    ]
    if not selected:
        raise ValueError(
            "archive has no supported benchmark results or experimental campaign summaries"
        )
    campaign_rows, observation_rows, trajectory_rows, sources, configurations = [], [], [], [], []
    for member in selected:
        payload = read_member(archive_path, member)
        digest = hashlib.sha256(payload).hexdigest()
        record = (
            decode_archive_record(payload) if member.endswith(".pkl") else _json_object(payload)
        )
        record = _object(record, "record")
        sources.append(
            {
                "member": member,
                "sha256": digest,
                "size_bytes": len(payload),
                "format": "data-only-pickle" if member.endswith(".pkl") else "json",
            }
        )
        group_id = member.rsplit("/", 1)[0]
        simulated = member.endswith(".pkl")
        if simulated:
            cohort = _object(record.get("config"), "benchmark config")
            matching = [
                s
                for s in analyte_sets
                if s["compound_set_hash"] == str(cohort.get("compound_set_hash"))
                and s["num_compounds"] == cohort.get("num_compounds")
            ]
            input_sizes = {s["cohort_size"] for s in matching}
            if len(input_sizes) != 1:
                raise ValueError("benchmark input cohort is missing or ambiguous")
            input_size = input_sizes.pop()
            if {s["analyte_set_index"] for s in matching} != set(range(input_size)):
                raise ValueError("benchmark lacks the complete declared archived analyte set")
            campaigns_by_id, extra_sources = _reconcile_campaign_records(
                record,
                group_id,
                {m["name"] for m in members},
                lambda name: read_member(archive_path, name),
            )
            sources.extend(extra_sources)
            source_digests = {s["member"]: s["sha256"] for s in sources}
            repeats = _integer(
                cohort.get("iterations_per_compound_set"), "iterations_per_compound_set"
            )
            if any(index >= input_size or repeat >= repeats for index, repeat in campaigns_by_id):
                raise ValueError(
                    "individual campaign identity is outside the archived input cohort"
                )
            if (
                all(
                    value[2] == "serialized_complete_cohort_order"
                    for value in campaigns_by_id.values()
                )
                and len(campaigns_by_id) != input_size * repeats
            ):
                raise ValueError("unlabelled grouped identities require a complete input cohort")
            campaigns = [
                (index, repeat, *value)
                for (index, repeat), value in sorted(campaigns_by_id.items())
            ]
            planned_count = input_size * repeats
            grouped_count = sum(len(g) for g in record["campaigns"])
        else:
            campaigns = [(0, 0, record, member, "experimental_summary")]
            source_digests = {member: digest}
            planned_count = grouped_count = 1
        group = []
        for set_index, repeat, campaign, chosen_source, snapshot_status in campaigns:
            campaign_id = f"{group_id}/set-{set_index}/repeat-{repeat}"
            result = replay_campaign(
                campaign,
                campaign_id=campaign_id,
                source_member=chosen_source,
                source_sha256=source_digests[chosen_source],
                observation_origin="simulator-latent-reference"
                if simulated
                else "archived-instrument-analysis",
            )
            rows = result.pop("observations")
            result.update(
                configuration_id=group_id,
                analyte_set_index=set_index if simulated else None,
                repeat_index=repeat,
                snapshot_status=snapshot_status,
            )
            observation_rows.extend(rows)
            campaign_rows.append(result)
            group.append(result)
        config = _safe_config(campaigns[0][2]["config"])
        budget = _integer(config.get("budget"), "budget")
        successes = sum(c["success_within_declared_budget"] for c in group)
        configurations.append(
            {
                "configuration_id": group_id,
                "observation_origin": group[0]["observation_origin"],
                "campaign_count": len(group),
                "planned_campaign_count": planned_count,
                "grouped_snapshot_campaign_count": grouped_count,
                "missing_campaign_count": planned_count - len(group),
                "snapshot_status_counts": dict(Counter(c["snapshot_status"] for c in group)),
                "record_kind": "simulated-benchmark" if simulated else "experimental-campaign",
                "success_count": successes,
                "success_fraction": successes / planned_count,
                "observed_campaign_success_fraction": successes / len(group),
                "complete_count": sum(c["budget_status"] == "complete" for c in group),
                "config": config,
            }
        )
        for point in _cohort_trajectory(group, planned_count, budget):
            trajectory_rows.append({"configuration_id": group_id, **point})

    report = {
        "schema_version": 1,
        "profile": PROFILE,
        "scope": "Replay archived observations; scalar scores are retained, raw metrics recomputed. No optimizer or signal-processing rerun.",
        "record_policy": "Match compacted grouped histories to labelled individual campaigns by scientific configuration and normalized raw-observation prefix equality; retain the longest consistent history and both source digests. Compare convenience summaries independently; gradient names do not establish chemical identity. Only documented legacy defaults compound_idx=None and mixed_fidelity=False are normalized. Reject divergent or ambiguous histories.",
        "cohort_policy": "Use the complete archived analyte-set cohort as denominator. Unobserved/missing campaigns remain unknown; upper bounds permit success only among unobserved records.",
        "score_threshold": SCORE_THRESHOLD,
        "score_origin": "archived BoTier output; not independently recalculated",
        "high_fidelity_threshold": HIGH_FIDELITY_THRESHOLD,
        "time_unit": "min",
        "program_volume_unit": "mL",
        "cost_scope": "Program-only volume excludes equilibration/flushes. Evaluation cost is a declared proxy, not measured cost.",
        "software": implementation,
        "archive_source": manifest["source"],
        "archive_summary": {k: v for k, v in manifest["archive"].items() if k != "members"},
        "record_sources": sources,
        "analyte_sets": analyte_sets,
        "configuration_count": len(configurations),
        "campaign_count": len(campaign_rows),
        "observation_count": len(observation_rows),
        "configurations": configurations,
    }
    json.dumps(report, allow_nan=False)
    if _implementation_metadata() != implementation:
        raise ValueError(
            "analysis implementation changed during replay; restart with a stable installation"
        )
    _publish_results(target, report, manifest, campaign_rows, observation_rows, trajectory_rows)
    return report
