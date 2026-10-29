"""Synthetic contract tests for the MoSeS trajectory comparison adapter."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from measure_methods.moses_objective import MOSES_SCORE_PROFILES
from measure_service.benchmarks.moses_sensitivity import (
    INPUT_POLICIES,
    QUALIFICATION_FILES,
    QUALIFICATION_PROFILE,
    REPLAY_FILES,
    REPLAY_PROFILE,
    compare_moses_trajectories,
)

_SOURCE_SHA = "a" * 64
_OTHER_SOURCE_SHA = "b" * 64
_SOURCE_MD5 = "c" * 32
_SOURCE_SIZE = 100
_ARCHIVE_SUMMARY = {
    "directory_count": 0,
    "file_count": 1,
    "member_count": 1,
    "total_compressed_bytes": 20,
    "total_uncompressed_bytes": 30,
}
_SOURCE = {
    "doi": "10.5281/synthetic.1",
    "url": "https://example.invalid/synthetic.zip",
    "expected": {"md5": _SOURCE_MD5, "size_bytes": _SOURCE_SIZE},
    "observed": {
        "md5": _SOURCE_MD5,
        "sha256": _SOURCE_SHA,
        "size_bytes": _SOURCE_SIZE,
    },
    "matches_expected": True,
}
_SOFTWARE = {"distribution": "measure-service", "version": "test", "replay_sha256": "d" * 64}
_PUBLISHED_PROFILE, _EXCESS_PROFILE = MOSES_SCORE_PROFILES


def _csv_bytes(fields, rows):
    from io import StringIO

    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _seal(root: Path, files: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        (root / name).write_bytes(payload)
    core = {
        name: {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}
        for name, payload in files.items()
    }
    (root / "complete.json").write_text(
        json.dumps({"schema_version": 1, "files": core}, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _reseal(root: Path, names: set[str]) -> None:
    complete_path = root / "complete.json"
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    assert set(complete["files"]) == names
    for name, record in complete["files"].items():
        payload = (root / name).read_bytes()
        record["sha256"] = hashlib.sha256(payload).hexdigest()
        record["size_bytes"] = len(payload)
    complete_path.write_text(
        json.dumps(complete, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _config(config_id, objective, budget):
    return {
        "configuration_id": config_id,
        "record_kind": "simulated-benchmark",
        "observation_origin": "simulator-latent-reference",
        "campaign_count": 2 if config_id == "cfg-a" else 1,
        "planned_campaign_count": 3 if config_id == "cfg-a" else 1,
        "missing_campaign_count": 1 if config_id == "cfg-a" else 0,
        "complete_count": 0 if config_id == "cfg-a" else 1,
        "success_count": 1,
        "success_fraction": 1 / 3 if config_id == "cfg-a" else 1.0,
        "observed_campaign_success_fraction": 0.5 if config_id == "cfg-a" else 1.0,
        "config": {
            "objective": objective,
            "num_compounds": 2,
            "budget": budget,
            "low_fidelity_cost": 0.25,
        },
    }


def _campaign(config, set_index, *, score_success, high_count, low_count):
    config_id = config["configuration_id"]
    campaign_id = f"{config_id}/set-{set_index}/repeat-0"
    source_member = f"{config_id}/set-{set_index}/campaign.pkl"
    source_sha = (str(set_index + 1) if config_id == "cfg-a" else "4") * 64
    config_json = json.dumps(config["config"], sort_keys=True)
    high_observations = high_count
    observation_count = high_count + low_count
    budget = config["config"]["budget"]
    return {
        "campaign_id": campaign_id,
        "source_member": source_member,
        "source_sha256": source_sha,
        "campaign_context": "simulator-latent-reference",
        "observation_origin": "simulator-latent-reference",
        "config": config_json,
        "num_observations": observation_count,
        "high_fidelity_count": high_count,
        "low_fidelity_count": low_count,
        "declared_budget": budget,
        "declared_seed_budget": 1,
        "budget_status": "complete" if high_count == budget else "incomplete",
        "first_success_high_fidelity_count": score_success,
        "success_within_declared_budget": score_success is not None and score_success <= budget,
        "score_threshold": 2.0,
        "missing_score_count": 1 if config_id == "cfg-a" and set_index == 1 else 0,
        "unavailable_high_fidelity_score_indices": "[1]"
        if config_id == "cfg-a" and set_index == 1
        else "[]",
        "best_high_fidelity_score": 2.0 if score_success is not None else 1.0,
        "best_high_fidelity_observation": "{}",
        "observed_program_volume_ml": 1.0 * high_observations,
        "missing_high_fidelity_volume_count": 0,
        "declared_evaluation_cost_proxy": high_count + low_count * 0.25,
        "diagnostic_counts": "{}",
        "configuration_id": config_id,
        "analyte_set_index": set_index,
        "repeat_index": 0,
        "snapshot_status": "individual_continuation",
    }


def _observation(campaign, sequence, *, fidelity, score, volume=1.0):
    high = fidelity >= 0.99
    previous = [
        item
        for item in getattr(_observation, "seen", [])
        if item["campaign_id"] == campaign["campaign_id"]
    ]
    high_count = sum(item["high_fidelity"] for item in previous) + int(high)
    low_count = sum(not item["high_fidelity"] for item in previous) + int(not high)
    row = {
        "campaign_id": campaign["campaign_id"],
        "source_member": campaign["source_member"],
        "source_sha256": campaign["source_sha256"],
        "sequence": sequence,
        "observation_origin": "simulator-latent-reference",
        "fidelity_level": fidelity,
        "high_fidelity": high,
        "high_fidelity_count": high_count,
        "low_fidelity_count": low_count,
        "archived_score": score,
        "archived_score_status": "finite" if score is not None else "unavailable",
        "num_compounds": 2,
        "run_time_min": 3.0,
        "num_peaks": 2,
        "num_resolved_peaks": 1,
        "critical_resolution": 1.4,
        "time_window_eluted_count": 2,
        "program_volume_ml": volume,
        "diagnostic_codes": "[]",
        "reported_summary_checks": "{}",
        "measurement_status_counts": "{}",
        "best_high_fidelity_score": score,
    }
    _observation.seen = [*getattr(_observation, "seen", []), row]
    return row


def _qualified_rows(replay_rows, campaign, candidate_classifications, raw_hard):
    rows = []
    for replay_row in replay_rows:
        sequence = replay_row["sequence"]
        for profile in MOSES_SCORE_PROFILES:
            for policy in INPUT_POLICIES:
                classification = candidate_classifications.get(
                    (replay_row["campaign_id"], sequence, profile, policy), False
                )
                status = "unavailable" if classification is None else "scored"
                score = "" if classification is None else (2.5 if classification else 1.25)
                archived = replay_row["archived_score"]
                archived_class = "" if archived is None else str(archived >= 2.0)
                rows.append(
                    {
                        "campaign_id": replay_row["campaign_id"],
                        "configuration_id": campaign["configuration_id"],
                        "sequence": sequence,
                        "source_member": campaign["source_member"],
                        "source_sha256": campaign["source_sha256"],
                        "analyte_set_index": campaign["analyte_set_index"],
                        "repeat_index": campaign["repeat_index"],
                        "objective": json.loads(campaign["config"])["objective"],
                        "profile": profile,
                        "input_policy": policy,
                        "status": status,
                        "archived_score": "" if archived is None else archived,
                        "archived_score_at_least_two": archived_class,
                        "candidate_score": score,
                        "candidate_score_at_least_two": ""
                        if classification is None
                        else str(classification),
                        "raw_analytical_thresholds_satisfied": ""
                        if raw_hard[0] is None
                        else str(raw_hard[0]),
                        "raw_all_objective_thresholds_satisfied": ""
                        if raw_hard[1] is None
                        else str(raw_hard[1]),
                    }
                )
    return rows


def _fixture(tmp_path, monkeypatch):
    from measure_service.benchmarks import moses_sensitivity

    monkeypatch.setattr(moses_sensitivity.metadata, "version", lambda _: "0.1.0.test")
    _observation.seen = []
    config_a = _config("cfg-a", "NumPeaks-CritRes", 3)
    config_b = _config("cfg-b", "SinglePeak-Resolution", 2)
    campaign_a0 = _campaign(config_a, 0, score_success=2, high_count=2, low_count=1)
    campaign_a1 = _campaign(config_a, 1, score_success=None, high_count=1, low_count=0)
    campaign_b0 = _campaign(config_b, 0, score_success=2, high_count=2, low_count=0)
    campaigns = [campaign_a0, campaign_a1, campaign_b0]
    observations = [
        _observation(campaign_a0, 1, fidelity=0.5, score=3.0, volume=0.4),
        _observation(campaign_a0, 2, fidelity=1.0, score=1.0, volume=0.8),
        _observation(campaign_a0, 3, fidelity=1.0, score=2.0, volume=1.1),
        _observation(campaign_a1, 1, fidelity=1.0, score=None, volume=0.7),
        _observation(campaign_b0, 1, fidelity=1.0, score=1.0, volume=0.6),
        _observation(campaign_b0, 2, fidelity=1.0, score=2.0, volume=0.9),
    ]
    replay_report = {
        "schema_version": 1,
        "profile": REPLAY_PROFILE,
        "archive_source": _SOURCE,
        "archive_summary": _ARCHIVE_SUMMARY,
        "campaign_count": len(campaigns),
        "configuration_count": 2,
        "observation_count": len(observations),
        "score_threshold": 2.0,
        "high_fidelity_threshold": 0.99,
        "software": _SOFTWARE,
        "record_sources": [
            {"member": row["source_member"], "sha256": row["source_sha256"]} for row in campaigns
        ],
        "configurations": [config_a, config_b],
    }
    replay_baseline = [
        {
            "configuration_id": "cfg-a",
            "high_fidelity_budget": 1,
            "cohort_size": 3,
            "recorded_campaign_count": 2,
            "observed_at_budget_count": 2,
            "unknown_success_count": 2,
            "success_count": 0,
            "success_fraction": 0.0,
            "success_fraction_upper_bound": 2 / 3,
        },
        {
            "configuration_id": "cfg-a",
            "high_fidelity_budget": 2,
            "cohort_size": 3,
            "recorded_campaign_count": 2,
            "observed_at_budget_count": 1,
            "unknown_success_count": 2,
            "success_count": 1,
            "success_fraction": 1 / 3,
            "success_fraction_upper_bound": 1.0,
        },
        {
            "configuration_id": "cfg-a",
            "high_fidelity_budget": 3,
            "cohort_size": 3,
            "recorded_campaign_count": 2,
            "observed_at_budget_count": 0,
            "unknown_success_count": 2,
            "success_count": 1,
            "success_fraction": 1 / 3,
            "success_fraction_upper_bound": 1.0,
        },
        {
            "configuration_id": "cfg-b",
            "high_fidelity_budget": 1,
            "cohort_size": 1,
            "recorded_campaign_count": 1,
            "observed_at_budget_count": 1,
            "unknown_success_count": 0,
            "success_count": 0,
            "success_fraction": 0.0,
            "success_fraction_upper_bound": 0.0,
        },
        {
            "configuration_id": "cfg-b",
            "high_fidelity_budget": 2,
            "cohort_size": 1,
            "recorded_campaign_count": 1,
            "observed_at_budget_count": 1,
            "unknown_success_count": 0,
            "success_count": 1,
            "success_fraction": 1.0,
            "success_fraction_upper_bound": 1.0,
        },
    ]
    campaign_fields = list(campaigns[0])
    replay_observation_fields = list(observations[0])
    manifest = {
        "schema_version": 1,
        "source": _SOURCE,
        "archive": {**_ARCHIVE_SUMMARY, "members": [{"name": "synthetic/input.pkl"}]},
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    replay_files = {
        "archive-manifest.json": manifest_bytes,
        "campaigns.csv": _csv_bytes(campaign_fields, campaigns),
        "observations.csv": _csv_bytes(replay_observation_fields, observations),
        "report.json": (json.dumps(replay_report, sort_keys=True) + "\n").encode(),
        "report.md": b"synthetic replay\n",
        "success-trajectories.csv": _csv_bytes(list(replay_baseline[0]), replay_baseline),
    }
    replay_dir = tmp_path / "replay"
    _seal(replay_dir, replay_files)

    candidate_classes = {}
    for obs in observations:
        key = obs["campaign_id"], obs["sequence"]
        # A positive low-fidelity result is deliberately present and must not
        # establish any series' success.
        if key == (campaign_a0["campaign_id"], 1):
            classes = {
                profile: {policy: True for policy in INPUT_POLICIES}
                for profile in MOSES_SCORE_PROFILES
            }
        elif key == (campaign_a0["campaign_id"], 2):
            classes = {
                _PUBLISHED_PROFILE: {
                    "raw_resolution_array": False,
                    "serialized_summary_hypothesis": True,
                },
                _EXCESS_PROFILE: {
                    "raw_resolution_array": False,
                    "serialized_summary_hypothesis": False,
                },
            }
        elif key == (campaign_a0["campaign_id"], 3):
            classes = {
                _PUBLISHED_PROFILE: {
                    "raw_resolution_array": True,
                    "serialized_summary_hypothesis": False,
                },
                _EXCESS_PROFILE: {
                    "raw_resolution_array": False,
                    "serialized_summary_hypothesis": False,
                },
            }
        elif key == (campaign_a1["campaign_id"], 1):
            classes = {
                _PUBLISHED_PROFILE: {
                    "raw_resolution_array": False,
                    "serialized_summary_hypothesis": None,
                },
                _EXCESS_PROFILE: {
                    "raw_resolution_array": False,
                    "serialized_summary_hypothesis": None,
                },
            }
        elif key == (campaign_b0["campaign_id"], 1):
            classes = {
                _PUBLISHED_PROFILE: {
                    "raw_resolution_array": True,
                    "serialized_summary_hypothesis": None,
                },
                _EXCESS_PROFILE: {
                    "raw_resolution_array": False,
                    "serialized_summary_hypothesis": None,
                },
            }
        else:
            classes = {
                _PUBLISHED_PROFILE: {
                    "raw_resolution_array": False,
                    "serialized_summary_hypothesis": None,
                },
                _EXCESS_PROFILE: {
                    "raw_resolution_array": False,
                    "serialized_summary_hypothesis": None,
                },
            }
        for profile in MOSES_SCORE_PROFILES:
            for policy in INPUT_POLICIES:
                candidate_classes[(key[0], key[1], profile, policy)] = classes[profile][policy]
    qualifications = []
    for campaign in campaigns:
        campaign_observations = [
            row for row in observations if row["campaign_id"] == campaign["campaign_id"]
        ]
        raw_flags = {
            "cfg-a/set-0/repeat-0": (False, False),
            "cfg-a/set-1/repeat-0": (None, None),
            "cfg-b/set-0/repeat-0": (True, True),
        }[campaign["campaign_id"]]
        qualifications.extend(
            _qualified_rows(campaign_observations, campaign, candidate_classes, raw_flags)
        )

    qualification_fields = list(qualifications[0])
    source_manifest_sha = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    qualification_report = {
        "schema_version": 1,
        "profile": QUALIFICATION_PROFILE,
        "replay_profile": REPLAY_PROFILE,
        "profiles": list(MOSES_SCORE_PROFILES),
        "input_policies": list(INPUT_POLICIES),
        "hard_score_threshold": 2.0,
        "observation_count": len(observations),
        "replay_observation_count": len(observations),
        "candidate_count": len(observations) * 4,
        "source_zip_sha256": _SOURCE_SHA,
        "source_zip_md5": _SOURCE_MD5,
        "source_zip_size_bytes": _SOURCE_SIZE,
        "source_zip_member_count": 1,
        "source_zip_manifest_sha256": source_manifest_sha,
        "integrity": {
            "rows_matched_to_source": len(observations),
            "source_manifest_verified": True,
            "source_records_verified": True,
        },
        "software": {
            "original_replay": _SOFTWARE,
            "qualification": {"replay_module_sha256": _SOFTWARE["replay_sha256"]},
        },
    }
    qualification_files = {
        "observations.csv": _csv_bytes(qualification_fields, qualifications),
        "report.json": (json.dumps(qualification_report, sort_keys=True) + "\n").encode(),
        "report.md": b"synthetic qualification\n",
    }
    qualification_dir = tmp_path / "qualification"
    _seal(qualification_dir, qualification_files)
    return replay_dir, qualification_dir, campaigns, observations, qualifications


def _read_csv(path):
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def test_compares_policies_with_hf_only_success_and_full_planned_denominator(tmp_path, monkeypatch):
    replay, qualification, campaigns, observations, qualified = _fixture(tmp_path, monkeypatch)
    before = {
        (path, name): (path / name).read_bytes()
        for path, names in (
            (replay, REPLAY_FILES | {"complete.json"}),
            (qualification, QUALIFICATION_FILES | {"complete.json"}),
        )
        for name in names
    }
    output = tmp_path / "sensitivity"

    report = compare_moses_trajectories(replay, qualification, output)

    assert report["campaign_count"] == 3
    assert report["planned_campaign_count"] == 4
    assert report["replay_observation_count"] == len(observations)
    assert report["qualification_row_count"] == len(qualified)
    assert report["targeted_objective_summary"]["observation_count"] == 2
    assert report["targeted_objective_summary"]["unavailable_candidate_row_count"] == 4
    assert report["integrity"]["archived_trajectory_reproduced"] is True
    assert report["configuration_summary_row_count"] == 14
    states = next(
        row
        for row in report["overall_final_budget_states"]
        if row["series"] == "archived_score_at_least_two"
    )
    assert (
        states["confirmed_success_count"],
        states["unknown_count"],
        states["observed_no_success_count"],
        states["planned_campaign_count"],
    ) == (2, 2, 0, 4)
    timing = next(
        row
        for row in report["overall_paired_changes"]
        if row["candidate_series"] == "published_1.0_summary_or_selected_target_array"
    )
    # Both the untargeted summary and selected-target rule confirm at HF1,
    # while their archived histories first confirm at HF2.
    assert timing["earlier_confirmed_event_count"] == 2
    assert timing["known_success_gain_count"] == 0

    assert len(_read_csv(output / "campaigns.csv")) == 3 * 7
    assert len(_read_csv(output / "paired-comparisons.csv")) == 3 * 6
    trajectory = _read_csv(output / "success-trajectories.csv")
    archive_a = [
        row
        for row in trajectory
        if row["configuration_id"] == "cfg-a" and row["series"] == "archived_score_at_least_two"
    ]
    assert [
        (int(row["success_count"]), int(row["unknown_success_count"])) for row in archive_a
    ] == [
        (0, 2),
        (1, 2),
        (1, 2),
    ]
    summary_budget_one = next(
        row
        for row in trajectory
        if row["configuration_id"] == "cfg-a"
        and row["series"] == "published_1.0_summary_or_selected_target_array"
        and row["high_fidelity_budget"] == "1"
    )
    raw_budget_one = next(
        row
        for row in trajectory
        if row["configuration_id"] == "cfg-a"
        and row["series"] == "published_1.0_raw_resolution_array"
        and row["high_fidelity_budget"] == "1"
    )
    assert summary_budget_one["unknown_success_count"] == "2"
    assert raw_budget_one["unknown_success_count"] == "1"

    campaigns_output = _read_csv(output / "campaigns.csv")
    raw_first_campaign = next(
        row
        for row in campaigns_output
        if row["campaign_id"] == campaigns[0]["campaign_id"]
        and row["series"] == "published_1.0_raw_resolution_array"
    )
    summary_first_campaign = next(
        row
        for row in campaigns_output
        if row["campaign_id"] == campaigns[0]["campaign_id"]
        and row["series"] == "published_1.0_summary_or_selected_target_array"
    )
    assert raw_first_campaign["first_confirmed_success_high_fidelity_index"] == "2"
    assert summary_first_campaign["first_confirmed_success_high_fidelity_index"] == "1"
    assert summary_first_campaign["selected_input_policy"] == "serialized_summary_hypothesis"
    missing_summary = next(
        row
        for row in campaigns_output
        if row["campaign_id"] == campaigns[1]["campaign_id"]
        and row["series"] == "published_1.0_summary_or_selected_target_array"
    )
    raw_missing_summary = next(
        row
        for row in campaigns_output
        if row["campaign_id"] == campaigns[1]["campaign_id"]
        and row["series"] == "published_1.0_raw_resolution_array"
    )
    assert missing_summary["budget_success_state"] == "unknown"
    assert raw_missing_summary["budget_success_state"] == "unknown"

    targeted = next(
        row
        for row in campaigns_output
        if row["campaign_id"] == campaigns[2]["campaign_id"]
        and row["series"] == "published_1.0_summary_or_selected_target_array"
    )
    assert targeted["first_confirmed_success_high_fidelity_index"] == "1"
    assert targeted["selected_input_policy"] == "raw_resolution_array"

    cross = next(
        item
        for item in report["paired_state_cross_tabs"]
        if item["configuration_id"] == "cfg-a"
        and item["candidate_series"] == "published_1.0_summary_or_selected_target_array"
    )
    assert cross["unrecorded_campaign_pair_count"] == 1
    assert cross["matrix"]["unknown"]["unknown"] >= 1
    for path, names in (
        (replay, REPLAY_FILES | {"complete.json"}),
        (qualification, QUALIFICATION_FILES | {"complete.json"}),
    ):
        for name in names:
            assert (path / name).read_bytes() == before[(path, name)]
    complete = json.loads((output / "complete.json").read_text(encoding="utf-8"))
    assert set(complete["files"]) == {
        "campaigns.csv",
        "configuration-summary.csv",
        "paired-comparisons.csv",
        "report.json",
        "report.md",
        "success-trajectories.csv",
    }


@pytest.mark.parametrize(
    "mutation", ["reorder", "source", "archived-score", "source-zip", "denominator"]
)
def test_rejects_divergent_or_reordered_evidence_without_publishing(
    tmp_path, monkeypatch, mutation
):
    replay, qualification, _, _, _ = _fixture(tmp_path, monkeypatch)
    if mutation == "reorder":
        rows = _read_csv(qualification / "observations.csv")
        rows[0], rows[1] = rows[1], rows[0]
        _replace_csv(qualification / "observations.csv", rows)
        _reseal(qualification, QUALIFICATION_FILES)
    elif mutation in {"source", "archived-score"}:
        rows = _read_csv(qualification / "observations.csv")
        if mutation == "source":
            rows[0]["source_sha256"] = _OTHER_SOURCE_SHA
        else:
            rows[0]["archived_score"] = "99.0"
            rows[0]["archived_score_at_least_two"] = "True"
        _replace_csv(qualification / "observations.csv", rows)
        _reseal(qualification, QUALIFICATION_FILES)
    elif mutation == "source-zip":
        report_path = qualification / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["source_zip_sha256"] = _OTHER_SOURCE_SHA
        report_path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
        _reseal(qualification, QUALIFICATION_FILES)
    else:
        rows = _read_csv(replay / "success-trajectories.csv")
        for row in rows:
            if row["configuration_id"] == "cfg-a":
                row["cohort_size"] = "4"
                row["success_fraction"] = str(int(row["success_count"]) / 4)
                row["success_fraction_upper_bound"] = str(
                    (int(row["success_count"]) + int(row["unknown_success_count"])) / 4
                )
        _replace_csv(replay / "success-trajectories.csv", rows)
        _reseal(replay, REPLAY_FILES)

    output = tmp_path / "must-not-publish"
    with pytest.raises(ValueError):
        compare_moses_trajectories(replay, qualification, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".measure-evidence-*"))


def _replace_csv(path: Path, rows):
    fields = list(rows[0])
    path.write_bytes(_csv_bytes(fields, rows))


@pytest.mark.parametrize(
    ("states", "gain", "loss"),
    [
        (("confirmed_success", "observed_no_success"), 0, 1),
        (("observed_no_success", "confirmed_success"), 1, 0),
        (("unknown", "unknown"), 0, 0),
        (None, 0, 0),
    ],
)
def test_summary_keeps_zero_timing_counts_without_joint_success(tmp_path, states, gain, loss):
    from measure_service.benchmarks.sensitivity_summary import summarize_outputs

    baseline = "archived_score_at_least_two"
    candidate = "published_1.0_raw_resolution_array"
    curves = [
        {
            "configuration_id": "synthetic",
            "series": series,
            "high_fidelity_budget": 1,
            "cohort_size": 1,
            "success_count": int(state == "confirmed_success"),
            "unknown_success_count": int(state == "unknown"),
            "observed_no_success_count": int(state == "observed_no_success"),
            "success_fraction": float(state == "confirmed_success"),
            "success_fraction_upper_bound": float(state != "observed_no_success"),
        }
        for series, state in zip((baseline, candidate), states or ("unknown", "unknown"))
    ]
    (tmp_path / "success-trajectories.csv").write_bytes(_csv_bytes(list(curves[0]), curves))
    fields = [
        "configuration_id",
        "candidate_series",
        "archived_score_state",
        "candidate_state",
        "confirmed_success_gain",
        "confirmed_success_loss",
        "candidate_minus_archived_first_index",
    ]
    pairs = (
        []
        if states is None
        else [dict(zip(fields, ["synthetic", candidate, *states, bool(gain), bool(loss), ""]))]
    )
    (tmp_path / "paired-comparisons.csv").write_bytes(_csv_bytes(fields, pairs))
    result = summarize_outputs(tmp_path)
    overall = result["overall_paired_changes"][0]
    assert overall["known_success_gain_count"] == gain
    assert overall["known_success_loss_count"] == loss
    for field in (
        "both_confirmed_by_budget_count",
        "earlier_confirmed_event_count",
        "later_confirmed_event_count",
        "unchanged_confirmed_event_count",
    ):
        assert overall[field] == 0
    with (tmp_path / "configuration-summary.csv").open(newline="") as source:
        configuration = next(row for row in csv.DictReader(source) if row["series"] == candidate)
    assert configuration["known_success_gain_count"] == str(gain)
    assert configuration["known_success_loss_count"] == str(loss)
    assert configuration["earlier_confirmed_event_count"] == "0"
    assert configuration["later_confirmed_event_count"] == "0"
