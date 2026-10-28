"""Synthetic checks for the metric audit and its replay/source bindings."""

import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import pytest

from measure_service import moses_score_qualification as score_qualification
from measure_service.benchmarks import evidence, moses_archive, moses_replay

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_moses_metrics.py"
_SPEC = importlib.util.spec_from_file_location("audit_moses_metrics", _SCRIPT)
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


def _config(**overrides):
    config = {
        "num_compounds": 2,
        "objective": "NumPeaks-CritRes",
        "compound_idx": None,
        "max_run_time": 20,
        "count_threshold": 0.5,
        "resolution_threshold": 1.5,
        "run_time_threshold": 5,
        "segment_based": False,
        "budget": 2,
        "low_fidelity_cost": 0.25,
        "working_dir": "/private/author/machine",
    }
    config.update(overrides)
    return config


def _observation(
    *,
    retention_times=None,
    peak_resolutions=None,
    fwhms=None,
    num_peaks=1,
    num_resolved_peaks=1,
    critical_resolution=1.5,
    score=2.1,
    fidelity_level=1.0,
):
    return {
        "num_compounds": 2,
        "gradient": {
            "run_time": 4.0,
            "header_gradient_table": ["time", "flow_rate"],
            "gradient_table": [[0.0, 0.2], [4.0, 0.2]],
        },
        "retention_times": [1.0, 3.0] if retention_times is None else retention_times,
        "peak_resolutions": [1.5, 1.5] if peak_resolutions is None else peak_resolutions,
        "fwhms": [2.3548, 2.3548] if fwhms is None else fwhms,
        "peak_areas": [10.0, 20.0],
        "num_peaks": num_peaks,
        "num_resolved_peaks": num_resolved_peaks,
        "critical_resolution": critical_resolution,
        "score": score,
        "fidelity_level": fidelity_level,
    }


def test_candidates_distinguish_finite_retention_time_from_finite_resolution():
    observation = _observation(
        retention_times=[1.0, float("inf")],
        peak_resolutions=[float("inf"), float("nan")],
        fwhms=[float("nan"), float("inf")],
        num_peaks=1,
        num_resolved_peaks=0,
        critical_resolution=7.0,
    )

    result = audit._candidate_values(
        observation, score_qualification._validate_scoring_config(_config())
    )

    assert result["candidates"]["reported_num_peaks_vs_finite_retention_time_count"] == 1
    assert result["candidates"]["reported_num_peaks_vs_finite_resolution_count"] == 0
    assert result["candidates"]["reported_critical_vs_min_finite_resolution"] == 0.0
    assert result["arrays"]["retention_times"] == {"finite": 1, "positive_infinity": 1}
    assert result["arrays"]["peak_resolutions"] == {"positive_infinity": 1, "nan": 1}
    assert result["pair_counts"]["finite_rt_without_finite_resolution"] == 1


def test_tied_retention_times_produce_zero_gaussian_pair_resolution():
    observation = _observation(
        retention_times=[2.0, 2.0],
        peak_resolutions=[0.0, 0.0],
        fwhms=[2.3548, 2.3548],
        num_peaks=2,
        num_resolved_peaks=0,
        critical_resolution=0.0,
    )

    result = audit._candidate_values(
        observation, score_qualification._validate_scoring_config(_config())
    )

    assert result["candidates"]["reported_critical_vs_sigma_pair_adjacent_min"] == 0.0
    assert result["pair_counts"]["duplicate_retention_time_pairs"] == 1
    assert result["pair_counts"]["zero_resolution_adjacent_pairs"] == 1
    assert result["pair_array_comparison"]["compared"] == 2
    assert result["pair_array_comparison"]["mismatches"] == 0


def test_fwhm_conversion_uses_gaussian_sigma_and_keeps_incorrect_summary_visible():
    observation = _observation(
        retention_times=[1.0, 3.0],
        peak_resolutions=[0.5, 0.5],
        fwhms=[2.3548, 2.3548],
        num_peaks=2,
        num_resolved_peaks=0,
        critical_resolution=4.0,
    )
    config = score_qualification._validate_scoring_config(_config())
    result = audit._candidate_values(observation, config)

    assert result["candidates"]["reported_critical_vs_sigma_pair_adjacent_min"] == pytest.approx(
        0.5
    )
    assert result["pair_array_comparison"]["compared"] == 2
    assert result["reported"]["critical_resolution"] == 4.0

    overall = defaultdict(audit._new_metric)
    by_origin = defaultdict(lambda: defaultdict(audit._new_metric))
    audit._compare_observation(
        result, origin="simulator-latent-reference", overall=overall, by_origin=by_origin
    )
    summary = overall["reported_critical_vs_sigma_pair_adjacent_min"]
    assert summary["mismatches"] == 1
    assert summary["max_abs_error"] == pytest.approx(3.5)


def test_empty_finite_arrays_use_source_zero_fallback_but_unavailable_arrays_stay_unknown():
    config = score_qualification._validate_scoring_config(_config())
    present = audit._candidate_values(
        _observation(
            retention_times=[float("inf"), float("inf")],
            peak_resolutions=[float("nan"), float("inf")],
            fwhms=[float("nan"), float("inf")],
            critical_resolution=9.0,
        ),
        config,
    )
    unavailable = _observation()
    unavailable.pop("retention_times")
    unavailable.pop("peak_resolutions")
    unavailable.pop("fwhms")
    absent = audit._candidate_values(unavailable, config)

    assert present["candidates"]["reported_critical_vs_min_finite_resolution"] == 0.0
    assert present["candidates"]["reported_num_peaks_vs_finite_retention_time_count"] == 0
    overall = defaultdict(audit._new_metric)
    by_origin = defaultdict(lambda: defaultdict(audit._new_metric))
    audit._compare_observation(
        present, origin="simulator-latent-reference", overall=overall, by_origin=by_origin
    )
    wrong_minimum = overall["reported_critical_vs_min_finite_resolution"]
    assert wrong_minimum["mismatches"] == 1
    assert wrong_minimum["max_abs_error"] == 9.0
    assert absent["candidates"]["reported_critical_vs_min_finite_resolution"] is None
    assert absent["arrays"]["retention_times"] == {"array_unavailable": 1}


def test_targeted_summary_is_excluded_from_global_minimum_comparison():
    config = score_qualification._validate_scoring_config(
        _config(objective="SinglePeak-Resolution", compound_idx=0)
    )
    observation = _observation(
        peak_resolutions=[0.1, 3.0], critical_resolution=None, num_peaks=None
    )
    result = audit._candidate_values(observation, config)
    overall = defaultdict(audit._new_metric)
    by_origin = defaultdict(lambda: defaultdict(audit._new_metric))

    audit._compare_observation(
        result, origin="archived-instrument-analysis", overall=overall, by_origin=by_origin
    )

    summary = overall["reported_critical_vs_min_finite_resolution"]
    assert result["candidates"]["reported_critical_vs_min_finite_resolution"] == 0.1
    assert summary["compared"] == 0
    assert summary["reported_unavailable"] == 0
    assert summary["excluded_targeted"] == 1


def _write_csv(path, rows, fieldnames):
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_complete(root):
    files = {}
    for item in sorted(root.iterdir()):
        if item.name == "complete.json":
            continue
        payload = item.read_bytes()
        files[item.name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
    (root / "complete.json").write_text(
        json.dumps({"schema_version": 1, "files": files}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _synthetic_source(tmp_path, monkeypatch):
    archive = tmp_path / "source.zip"
    member = "results/known_mixture_optimization/synthetic/campaign_summary.json"
    record = {
        "config": _config(),
        "experiments": [
            _observation(
                retention_times=[1.0, float("inf")],
                peak_resolutions=[1.5, float("inf")],
                fwhms=[2.3548, 2.3548],
                num_peaks=1,
                num_resolved_peaks=1,
                critical_resolution=1.5,
            )
        ],
    }
    payload = json.dumps(record, allow_nan=True, sort_keys=True).encode("utf-8")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(member, payload)
    archive_bytes = archive.read_bytes()
    monkeypatch.setattr(moses_archive, "MOSES_ARCHIVE_SIZE_BYTES", len(archive_bytes))
    monkeypatch.setattr(moses_archive, "MOSES_ARCHIVE_MD5", hashlib.md5(archive_bytes).hexdigest())
    monkeypatch.setattr(audit, "EXPECTED_ARCHIVE_SHA256", hashlib.sha256(archive_bytes).hexdigest())
    manifest = moses_archive.inspect_archive(archive)
    generated = {"record_sources": [], "analyte_sets": []}
    sources = list(score_qualification._iter_source_observations(archive, manifest, generated))
    assert sources
    replay_root = tmp_path / "replay"
    replay_root.mkdir()
    (replay_root / "archive-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    (replay_root / "report.md").write_text("Synthetic replay evidence.\n", encoding="utf-8")
    (replay_root / "success-trajectories.csv").write_text("configuration_id\n", encoding="utf-8")

    first = sources[0]
    expected_rows = [source.expected_replay_row for source in sources]
    observation_header = list(expected_rows[0])
    _write_csv(
        replay_root / "observations.csv",
        [
            {key: score_qualification._csv_cell(row[key]) for key in observation_header}
            for row in expected_rows
        ],
        observation_header,
    )
    source_cfg = moses_replay._safe_config(record["config"])
    configuration = {
        "configuration_id": first.configuration_id,
        "observation_origin": "archived-instrument-analysis",
        "campaign_count": 1,
        "planned_campaign_count": 1,
        "grouped_snapshot_campaign_count": 1,
        "missing_campaign_count": 0,
        "snapshot_status_counts": {"experimental_summary": 1},
        "record_kind": "experimental-campaign",
        "success_count": 0,
        "success_fraction": 0.0,
        "observed_campaign_success_fraction": 0.0,
        "complete_count": 0,
        "config": source_cfg,
    }
    report = {
        "schema_version": 1,
        "profile": moses_replay.PROFILE,
        "archive_source": manifest["source"],
        "archive_summary": {
            key: value for key, value in manifest["archive"].items() if key != "members"
        },
        "record_sources": generated["record_sources"],
        "analyte_sets": generated["analyte_sets"],
        "observation_count": len(sources),
        "campaign_count": 1,
        "configuration_count": 1,
        "configurations": [configuration],
    }
    (replay_root / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    campaign = {
        "campaign_id": first.campaign_id,
        "configuration_id": first.configuration_id,
        "source_member": first.source_member,
        "source_sha256": first.source_sha256,
        "campaign_context": "archived-instrument-analysis",
        "num_observations": len(sources),
        "high_fidelity_count": sum(bool(row["high_fidelity"]) for row in expected_rows),
        "low_fidelity_count": sum(not bool(row["high_fidelity"]) for row in expected_rows),
        "declared_budget": 2,
        "analyte_set_index": "",
        "repeat_index": 0,
        "snapshot_status": "experimental_summary",
        "config": json.dumps(source_cfg, sort_keys=True, allow_nan=False),
    }
    _write_csv(replay_root / "campaigns.csv", [campaign], list(campaign))
    _write_complete(replay_root)
    return archive, replay_root


def test_audit_publishes_redacted_complete_bundle_from_verified_source(tmp_path, monkeypatch):
    archive, replay = _synthetic_source(tmp_path, monkeypatch)
    output = tmp_path / "audit"

    report = audit.audit_metrics(archive, replay, output)

    assert report["profile"] == audit.AUDIT_PROFILE
    assert report["replay_binding"]["observation_count"] == 1
    assert report["replay_binding"]["all_replay_observation_columns_matched_to_regenerated_rows"]
    verified = evidence.VerifiedBundle(
        output,
        required_files={"report.json", "report.md"},
        profile=audit.AUDIT_PROFILE,
    )
    assert verified.report["source"]["sha256"] == audit.EXPECTED_ARCHIVE_SHA256
    output_text = (output / "report.json").read_text(encoding="utf-8")
    assert "/private/author/machine" not in output_text
    assert '"retention_times": [' not in output_text


def test_changed_replay_source_binding_is_rejected_even_with_rehashed_bundle(tmp_path, monkeypatch):
    archive, replay = _synthetic_source(tmp_path, monkeypatch)
    rows_path = replay / "observations.csv"
    with rows_path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
        fieldnames = reader.fieldnames
    rows[0]["source_sha256"] = "0" * 64
    _write_csv(rows_path, rows, fieldnames)
    _write_complete(replay)
    evidence.VerifiedBundle(
        replay,
        required_files=audit._REPLAY_FILES,
        profile=moses_replay.PROFILE,
    )

    with pytest.raises(ValueError, match="differs from regenerated source"):
        audit.audit_metrics(archive, replay, tmp_path / "audit")


def test_archive_digest_change_is_rejected(tmp_path, monkeypatch):
    archive, replay = _synthetic_source(tmp_path, monkeypatch)
    archive.write_bytes(archive.read_bytes() + b"tamper")

    with pytest.raises(ValueError, match="archive does not match the expected source"):
        audit.audit_metrics(archive, replay, tmp_path / "audit")


def test_cli_exposes_documented_arguments_outside_checkout(tmp_path):
    script = Path(audit.__file__).resolve()
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--archive" in result.stdout
    assert "--replay-dir" in result.stdout
    assert "--output-dir" in result.stdout


def test_empty_source_arrays_are_unavailable_not_observed_zero_metrics():
    config = score_qualification._validate_scoring_config(_config())
    result = audit._candidate_values(
        _observation(
            retention_times=[], peak_resolutions=[], fwhms=[], num_peaks=0, critical_resolution=0
        ),
        config,
    )
    for name in (
        "reported_num_peaks_vs_finite_retention_time_count",
        "reported_num_peaks_vs_finite_resolution_count",
        "reported_critical_vs_min_finite_resolution",
    ):
        assert result["candidates"][name] is None
    overall = defaultdict(audit._new_metric)
    by_origin = defaultdict(lambda: defaultdict(audit._new_metric))
    audit._compare_observation(
        result, origin="simulator-latent-reference", overall=overall, by_origin=by_origin
    )
    assert overall["reported_critical_vs_min_finite_resolution"]["candidate_unavailable"] == 1
    assert overall["reported_critical_vs_min_finite_resolution"]["compared"] == 0


def test_adjacent_width_diagnostic_does_not_deny_an_available_nonadjacent_pair():
    config = score_qualification._validate_scoring_config(_config(num_compounds=3))
    result = audit._candidate_values(
        _observation(retention_times=[1, 2, 3], fwhms=[1, None, 1], peak_resolutions=[1, None, 1]),
        config,
    )
    pairs = result["pair_counts"]
    assert pairs["source_style_all_pairs"] == 1
    assert pairs["source_style_adjacent_pairs"] == 0
    assert (
        pairs["observations_with_finite_rt_pairs_but_no_positive_finite_width_adjacent_pair"] == 1
    )


def test_maximum_error_retains_real_differences_inside_the_tolerance():
    config = score_qualification._validate_scoring_config(_config())
    epsilon = 5e-13
    result = audit._candidate_values(
        _observation(
            retention_times=[1, 3],
            fwhms=[2.3548, 2.3548],
            peak_resolutions=[0.5 + epsilon, 0.5 + epsilon],
            critical_resolution=0.5,
        ),
        config,
    )
    pairs = result["pair_array_comparison"]
    assert pairs["compared"] == 2
    assert pairs["mismatches"] == 0
    assert pairs["max_abs_error"] == pytest.approx(epsilon, rel=0.001, abs=0)
    overall = defaultdict(audit._new_metric)
    by_origin = defaultdict(lambda: defaultdict(audit._new_metric))
    audit._compare_observation(
        result, origin="simulator-latent-reference", overall=overall, by_origin=by_origin
    )
    comparison = overall["reported_critical_vs_min_finite_resolution"]
    assert comparison["mismatches"] == 0
    assert comparison["max_abs_error"] == pytest.approx(epsilon, rel=0.001, abs=0)
