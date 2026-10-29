"""Observable archive-replay contracts using independently specified data."""

import copy
import json
import math
import pickle

import pytest

from measure_service.benchmarks.moses_replay import (
    _benchmark_campaigns,
    _cohort_trajectory,
    _publish_results,
    _reconcile_campaign_records,
    replay_campaign,
)


def observation(score=1, fidelity=1, **kwargs):
    result = {
        "num_compounds": 2,
        "gradient": {
            "run_time": 4,
            "header_gradient_table": ["time", "flow_rate"],
            "gradient_table": [[0, 0.4], [4, 0.4]],
        },
        "retention_times": [1, 3],
        "peak_resolutions": [1.5, 1.5],
        "fwhms": [0.1, 0.1],
        "peak_areas": [10, 20],
        "score": score,
        "fidelity_level": fidelity,
    }
    result.update(kwargs)
    return result


def campaign(*observations, budget=3):
    return {
        "config": {
            "num_compounds": 2,
            "resolution_threshold": 1.5,
            "budget": budget,
            "seed_budget": 1,
            "low_fidelity_cost": 0.25,
            "working_dir": "/private/author/machine",
        },
        "experiments": list(observations),
    }


def replay(data):
    return replay_campaign(
        data,
        campaign_id="test",
        source_member="test.json",
        source_sha256="a" * 64,
        observation_origin="synthetic-test",
    )


def test_seeds_count_and_low_fidelity_success_cannot_advance_physical_role():
    result = replay(campaign(observation(1), observation(3, 0.8), observation(2), observation(2.1)))
    assert result["first_success_high_fidelity_count"] == 2
    assert result["high_fidelity_count"] == 3
    assert result["low_fidelity_count"] == 1
    assert result["declared_evaluation_cost_proxy"] == 3.25
    assert result["observed_program_volume_ml"] == pytest.approx(4.8)
    assert result["budget_status"] == "complete"
    assert [r["best_high_fidelity_score"] for r in result["observations"]] == [1, 1, 2, 2.1]
    assert result["best_high_fidelity_observation"]["sequence"] == 4
    assert "working_dir" not in result["config"]


def test_unavailable_and_non_eluting_values_survive_replay():
    result = replay(
        campaign(
            observation(
                math.nan,
                retention_times=[1, math.inf],
                peak_resolutions=[math.nan, math.nan],
                fwhms=[0.1, math.nan],
            ),
            observation(1.5),
            budget=3,
        )
    )
    first = result["observations"][0]
    assert first["archived_score"] is None
    assert first["num_peaks"] == 0
    assert first["time_window_eluted_count"] == 1
    assert first["measurement_status_counts"]["retention_times"] == {"finite": 1, "non_eluting": 1}
    assert result["first_success_high_fidelity_count"] is None
    assert result["success_within_declared_budget"] is False
    assert result["budget_status"] == "incomplete"
    assert result["missing_score_count"] == 1


def test_threshold_is_inclusive_and_budget_excess_remains_visible():
    result = replay(campaign(observation(1.9), observation(2), budget=1))
    assert result["first_success_high_fidelity_count"] == 2
    assert result["success_within_declared_budget"] is False
    assert result["budget_status"] == "over_budget"


def test_missing_resolution_array_is_not_a_successful_zero_measurement():
    result = replay(campaign(observation(peak_resolutions=[])))
    first = result["observations"][0]
    assert first["num_peaks"] is None
    assert first["critical_resolution"] is None
    assert "peak_resolutions_array_unavailable" in first["diagnostic_codes"]


@pytest.mark.parametrize(
    "field,value",
    [("num_compounds", 3), ("fidelity_level", True), ("fidelity_level", 1.1), ("score", "2")],
)
def test_malformed_observation_cannot_silently_enter_the_cohort(field, value):
    with pytest.raises(ValueError):
        replay(campaign(observation(**{field: value})))


def test_declared_cohort_and_repeat_counts_are_verified():
    data = {
        "config": {"num_compound_sets": 2, "iterations_per_compound_set": 1},
        "campaigns": [[campaign(observation())], [campaign(observation())]],
    }
    assert [item[:2] for item in _benchmark_campaigns(data, "results.pkl")] == [(0, 0), (1, 0)]
    missing = copy.deepcopy(data)
    missing["campaigns"].pop()
    with pytest.raises(ValueError, match="cohort"):
        list(_benchmark_campaigns(missing, "results.pkl"))

    missing = copy.deepcopy(data)
    missing["campaigns"][0].append(campaign(observation()))
    with pytest.raises(ValueError, match="repeats"):
        list(_benchmark_campaigns(missing, "results.pkl"))


def test_compacted_completed_snapshot_matches_labels_by_raw_content():
    first = campaign(observation(1, retention_times=[1, 2]))
    second = campaign(observation(2, retention_times=[1, 3]))
    second["config"].update(compound_idx=None, mixed_fidelity=False)
    grouped = {
        "config": {"num_compound_sets": 1, "iterations_per_compound_set": 1},
        "campaigns": [[second]],
    }
    sidecar = copy.deepcopy(second)
    sidecar["config"].pop("compound_idx")
    sidecar["config"].pop("mixed_fidelity")
    sidecar["experiments"][0]["num_peaks"] = 2
    files = {
        "group/CmpdSet-0/campaign.pkl": pickle.dumps(first),
        "group/CmpdSet-4/campaign.pkl": pickle.dumps(sidecar),
    }
    resolved, sources = _reconcile_campaign_records(grouped, "group", set(files), files.__getitem__)
    assert set(resolved) == {(0, 0), (4, 0)}
    assert resolved[(4, 0)][2] == "matching_labelled_history"
    assert resolved[(0, 0)][2] == "individual_only"
    assert len(sources) == 2


def test_reconciliation_keeps_continuations_but_rejects_ambiguous_or_divergent_histories():
    original = campaign(observation(1))
    grouped = {
        "config": {"num_compound_sets": 1, "iterations_per_compound_set": 1},
        "campaigns": [[original]],
    }
    longer = copy.deepcopy(original)
    longer["experiments"].append(observation(2))
    files = {"group/CmpdSet-7/campaign.pkl": pickle.dumps(longer)}
    result, _ = _reconcile_campaign_records(grouped, "group", set(files), files.__getitem__)
    assert result[(7, 0)][2] == "individual_continuation"
    files["group/CmpdSet-8/campaign.pkl"] = files["group/CmpdSet-7/campaign.pkl"]
    with pytest.raises(ValueError, match="ambiguous"):
        _reconcile_campaign_records(grouped, "group", set(files), files.__getitem__)
    longer["experiments"][0]["retention_times"][0] = 1.1
    files = {"group/CmpdSet-7/campaign.pkl": pickle.dumps(longer)}
    with pytest.raises(ValueError, match="divergent"):
        _reconcile_campaign_records(grouped, "group", set(files), files.__getitem__)


def test_experimental_campaign_low_fidelity_rows_have_simulator_origin():
    result = replay_campaign(
        campaign(observation(1), observation(2, 0.8)),
        campaign_id="experimental",
        source_member="summary.json",
        source_sha256="b" * 64,
        observation_origin="archived-instrument-analysis",
    )
    assert result["observation_origin"] == "mixed-instrument-and-simulated"
    assert [r["observation_origin"] for r in result["observations"]] == [
        "archived-instrument-analysis",
        "simulator-latent-reference",
    ]


def test_unknown_bounds_do_not_double_count_early_successes_or_drop_missing_campaigns():
    success = replay(campaign(observation(2), budget=4))
    unsuccessful = replay(campaign(*[observation(1)] * 4, budget=4))
    unfinished = replay(campaign(observation(1), budget=4))
    curve = list(_cohort_trajectory([success, unsuccessful, unfinished], 4, 4))
    assert curve[-1]["success_count"] == 1
    assert curve[-1]["unknown_success_count"] == 2
    assert curve[-1]["success_fraction"] == 0.25
    assert curve[-1]["success_fraction_upper_bound"] == 0.75
    assert all(point["success_fraction_upper_bound"] <= 1 for point in curve)


def test_reported_convenience_fields_are_checked_without_repairing_source_values():
    result = replay(campaign(observation(num_peaks=1, critical_resolution=1.5)))
    row = result["observations"][0]
    assert row["num_peaks"] == 2
    assert row["reported_summary_checks"]["num_peaks"] == {
        "reported": 1,
        "recomputed": 2,
        "status": "disagrees",
    }
    assert row["reported_summary_checks"]["critical_resolution"]["status"] == "agrees"


def test_publication_failure_leaves_no_partial_success_report(tmp_path, monkeypatch):
    from measure_service.benchmarks import moses_replay

    monkeypatch.setattr(moses_replay, "_markdown_report", lambda *args: "test report")

    def failure(*args):
        raise OSError("disk full")

    monkeypatch.setattr(moses_replay, "_csv", failure)
    target = tmp_path / "result"
    with pytest.raises(OSError, match="disk full"):
        _publish_results(target, {}, {}, [], [], [])
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_complete_manifest_identifies_every_atomically_published_evidence_file(
    tmp_path, monkeypatch
):
    from measure_service.benchmarks import moses_replay

    monkeypatch.setattr(moses_replay, "_markdown_report", lambda *args: "test report")
    target = tmp_path / "result"
    _publish_results(
        target, {"schema_version": 1}, {}, [{"value": 1}], [{"value": 2}], [{"value": 3}]
    )
    manifest = json.loads((target / "complete.json").read_text())
    assert set(manifest["files"]) == {
        "report.json",
        "report.md",
        "archive-manifest.json",
        "campaigns.csv",
        "observations.csv",
        "success-trajectories.csv",
    }
