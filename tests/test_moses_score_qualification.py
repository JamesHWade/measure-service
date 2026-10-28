import csv
import hashlib
import json
import zipfile

import pytest

from measure_methods.moses_objective import MOSES_SCORE_PROFILES, moses_observation_score
from measure_service.benchmarks import moses_archive, moses_replay
from measure_service.moses_score_qualification import qualify_moses_scores


def _config(**overrides):
    result = {
        "num_compounds": 2,
        "objective": "NumPeaks-CritRes",
        "compound_idx": None,
        "max_run_time": 20.0,
        "count_threshold": 0.5,
        "resolution_threshold": 1.5,
        "run_time_threshold": 5.0,
        "segment_based": False,
        "budget": 3,
        "seed_budget": 1,
        "low_fidelity_cost": 0.25,
    }
    result.update(overrides)
    return result


def _observation(
    *,
    score=1.0,
    peak_resolutions=None,
    critical_resolution=1.0491945,
    num_peaks=2,
    num_resolved_peaks=1,
    runtime=4.0,
    times=(0.0, 4.0),
):
    result = {
        "num_compounds": 2,
        "gradient": {
            "run_time": runtime,
            "header_gradient_table": ["time", "flow_rate"],
            "gradient_table": [[time, 0.4] for time in times],
        },
        "retention_times": [1.0, 3.0],
        "peak_resolutions": [1.03, 2.0] if peak_resolutions is None else peak_resolutions,
        "fwhms": [0.1, 0.1],
        "peak_areas": [10.0, 20.0],
        "score": score,
        "fidelity_level": 1.0,
        "num_peaks": num_peaks,
        "num_resolved_peaks": num_resolved_peaks,
        "critical_resolution": critical_resolution,
    }
    return result


def _fixture(tmp_path, monkeypatch, *, observations=None, config_overrides=None):
    if observations is None:
        observations = [_observation()]
    source_record = {"config": _config(**(config_overrides or {})), "experiments": observations}
    archive_path = tmp_path / "source.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "results/known_mixture_optimization/synthetic/campaign_summary.json",
            json.dumps(source_record, allow_nan=True),
        )
    source_bytes = archive_path.read_bytes()
    monkeypatch.setattr(moses_archive, "MOSES_ARCHIVE_SIZE_BYTES", len(source_bytes))
    monkeypatch.setattr(
        moses_archive,
        "MOSES_ARCHIVE_MD5",
        hashlib.md5(source_bytes, usedforsecurity=False).hexdigest(),
    )
    monkeypatch.setattr(moses_replay, "_implementation_metadata", lambda: {"synthetic": True})
    monkeypatch.setattr(
        "measure_service.moses_score_qualification.metadata.version",
        lambda _name: "0.1.0.dev0",
    )

    replay_dir = tmp_path / "replay"
    moses_replay.analyze_archive(archive_path, replay_dir)
    return archive_path, replay_dir


def _read_csv(path):
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def _reseal_replay(replay_dir):
    complete_path = replay_dir / "complete.json"
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    for name, item in complete["files"].items():
        payload = (replay_dir / name).read_bytes()
        item["size_bytes"] = len(payload)
        item["sha256"] = hashlib.sha256(payload).hexdigest()
    complete_path.write_text(json.dumps(complete, indent=2, sort_keys=True) + "\n")


def test_qualifies_both_profiles_and_input_policies_with_atomic_checksummed_outputs(
    tmp_path, monkeypatch
):
    observation = _observation()
    source_score = moses_observation_score(
        profile="moses-botier-1.0.0-binary64/1",
        num_compounds=2,
        peak_resolutions=observation["peak_resolutions"],
        run_time_min=4.0,
        max_run_time_min=20.0,
    )["score"]
    observation["score"] = source_score
    archive_path, replay_dir = _fixture(tmp_path, monkeypatch, observations=[observation])
    original_replay_bytes = {
        path.name: path.read_bytes() for path in replay_dir.iterdir() if path.is_file()
    }
    output_dir = tmp_path / "qualification"

    report = qualify_moses_scores(archive_path, replay_dir, output_dir)

    assert report["profiles"] == list(MOSES_SCORE_PROFILES)
    assert report["input_policies"] == [
        "raw_resolution_array",
        "serialized_summary_hypothesis",
    ]
    assert report["candidate_count"] == 4
    rows = _read_csv(output_dir / "observations.csv")
    assert len(rows) == 4
    exact = next(
        row
        for row in rows
        if row["profile"] == "moses-botier-1.0.0-binary64/1"
        and row["input_policy"] == "raw_resolution_array"
    )
    assert exact["within_tolerance"] == "True"
    assert exact["runtime_derivation"] == "last_gradient_time_endpoint"
    assert exact["archived_score"] == exact["candidate_score"]
    assert report["raw_hard_threshold_confusion"]["rows_with_both_values"] == 1

    complete = json.loads((output_dir / "complete.json").read_text(encoding="utf-8"))
    assert set(complete["files"]) == {"report.json", "report.md", "observations.csv"}
    for name, entry in complete["files"].items():
        payload = (output_dir / name).read_bytes()
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
        assert entry["size_bytes"] == len(payload)
    assert {
        path.name: path.read_bytes() for path in replay_dir.iterdir() if path.is_file()
    } == original_replay_bytes
    assert "peak_resolutions" not in (output_dir / "report.json").read_text(encoding="utf-8")


def test_serialized_summary_hypothesis_keeps_disagreement_with_raw_resolution_array(
    tmp_path, monkeypatch
):
    archive_path, replay_dir = _fixture(tmp_path, monkeypatch)
    output_dir = tmp_path / "qualification"

    qualify_moses_scores(archive_path, replay_dir, output_dir)

    rows = _read_csv(output_dir / "observations.csv")
    raw = next(row for row in rows if row["input_policy"] == "raw_resolution_array")
    summary = next(row for row in rows if row["input_policy"] == "serialized_summary_hypothesis")
    assert float(raw["raw_array_critical_resolution"]) == pytest.approx(1.03)
    assert float(summary["serialized_critical_resolution"]) == pytest.approx(1.0491945)
    assert float(summary["raw_vs_serialized_critical_resolution_abs_difference"]) == pytest.approx(
        0.0191945
    )
    report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    assert report["raw_summary_disagreements"]["critical_resolution_disagreement_count"] == 1


def test_optional_replay_plot_artifacts_are_preserved_and_not_treated_as_core_checksums(
    tmp_path, monkeypatch
):
    archive_path, replay_dir = _fixture(tmp_path, monkeypatch)
    optional_files = {
        "plot.png": b"synthetic png placeholder",
        "plot.svg": b"<svg></svg>",
        "bundle-manifest.json": b'{"kind":"synthetic"}\n',
    }
    for name, payload in optional_files.items():
        (replay_dir / name).write_bytes(payload)

    report = qualify_moses_scores(archive_path, replay_dir, tmp_path / "qualification")

    assert report["integrity"]["optional_replay_files_ignored"] == sorted(optional_files)
    assert {name: (replay_dir / name).read_bytes() for name in optional_files} == optional_files
    complete = json.loads((replay_dir / "complete.json").read_text(encoding="utf-8"))
    assert not set(optional_files).intersection(complete["files"])


def test_segment_runtime_sums_four_decimal_rounded_durations_and_reports_difference(
    tmp_path, monkeypatch
):
    observation = _observation(runtime=2.2469, times=(0.0, 1.12345, 2.2469))
    archive_path, replay_dir = _fixture(
        tmp_path,
        monkeypatch,
        observations=[observation],
        config_overrides={"segment_based": True},
    )
    output_dir = tmp_path / "qualification"

    qualify_moses_scores(archive_path, replay_dir, output_dir)

    rows = _read_csv(output_dir / "observations.csv")
    raw = next(row for row in rows if row["input_policy"] == "raw_resolution_array")
    assert raw["runtime_derivation"] == "sum_of_rounded_4_decimal_segment_durations"
    assert float(raw["derived_run_time_min"]) == pytest.approx(2.2468)
    assert raw["runtime_status"] == "disagrees"


@pytest.mark.parametrize(("duration", "expected"), [(5.00015, 5.0002), (1.23445, 1.2344)])
def test_segment_runtime_matches_numpy_scaled_four_decimal_rounding(
    tmp_path, monkeypatch, duration, expected
):
    observation = _observation(runtime=duration, times=(0.0, duration))
    archive_path, replay_dir = _fixture(
        tmp_path,
        monkeypatch,
        observations=[observation],
        config_overrides={"segment_based": True},
    )
    output_dir = tmp_path / "qualification"

    qualify_moses_scores(archive_path, replay_dir, output_dir)

    raw = next(
        row
        for row in _read_csv(output_dir / "observations.csv")
        if row["input_policy"] == "raw_resolution_array"
    )
    assert float(raw["derived_run_time_min"]) == expected


@pytest.mark.parametrize("representation", ["missing", "null", "empty"])
def test_missing_raw_resolution_array_is_unscorable_under_raw_policy(
    tmp_path, monkeypatch, representation
):
    observation = _observation()
    if representation == "missing":
        observation.pop("peak_resolutions")
    else:
        observation["peak_resolutions"] = None if representation == "null" else []
    archive_path, replay_dir = _fixture(tmp_path, monkeypatch, observations=[observation])
    output_dir = tmp_path / "qualification"

    qualify_moses_scores(archive_path, replay_dir, output_dir)

    rows = _read_csv(output_dir / "observations.csv")
    raw_rows = [row for row in rows if row["input_policy"] == "raw_resolution_array"]
    summary_rows = [row for row in rows if row["input_policy"] == "serialized_summary_hypothesis"]
    assert len(raw_rows) == 2
    assert all(row["status"] == "unavailable" and row["candidate_score"] == "" for row in raw_rows)
    assert all(row["candidate_score"] != "" for row in summary_rows)


@pytest.mark.parametrize("tamper", ["append", "delete", "source_member"])
def test_replay_rows_are_reconnected_to_source_and_reject_tampering(tmp_path, monkeypatch, tamper):
    archive_path, replay_dir = _fixture(
        tmp_path, monkeypatch, observations=[_observation(), _observation(score=1.1)]
    )
    path = replay_dir / "observations.csv"
    with path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.reader(source))
    if tamper == "append":
        rows.append(rows[1])
    elif tamper == "delete":
        rows.pop()
    else:
        rows[1][rows[0].index("source_member")] = "results/other/campaign_summary.json"
    with path.open("w", newline="", encoding="utf-8") as target:
        csv.writer(target).writerows(rows)
    _reseal_replay(replay_dir)

    with pytest.raises(ValueError, match="source|identity|missing|duplicate"):
        qualify_moses_scores(archive_path, replay_dir, tmp_path / "qualification")
    assert not (tmp_path / "qualification").exists()


def test_replay_checksums_and_exact_source_zip_manifest_are_required(tmp_path, monkeypatch):
    archive_path, replay_dir = _fixture(tmp_path, monkeypatch)
    observations_path = replay_dir / "observations.csv"
    original_observations = observations_path.read_bytes()
    with observations_path.open("a", encoding="utf-8") as output:
        output.write("tampered\n")
    with pytest.raises(ValueError, match="size mismatch|SHA256 mismatch"):
        qualify_moses_scores(archive_path, replay_dir, tmp_path / "qualification")

    observations_path.write_bytes(original_observations)
    archive_path.write_bytes(archive_path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="does not match the expected source"):
        qualify_moses_scores(archive_path, replay_dir, tmp_path / "qualification")


def test_invalid_score_config_fails_without_publishing_partial_output(tmp_path, monkeypatch):
    archive_path, replay_dir = _fixture(
        tmp_path, monkeypatch, config_overrides={"objective": "not-a-source-objective"}
    )
    output_dir = tmp_path / "qualification"

    with pytest.raises(ValueError, match="objective"):
        qualify_moses_scores(archive_path, replay_dir, output_dir)
    assert not output_dir.exists()
    assert not list(tmp_path.glob(".moses-score-qualification-*"))


def test_target_score_never_synthesizes_a_global_summary_candidate(tmp_path, monkeypatch):
    observation = _observation(peak_resolutions=[2.2, 0.2], critical_resolution=0.2)
    archive_path, replay_dir = _fixture(
        tmp_path,
        monkeypatch,
        observations=[observation],
        config_overrides={"objective": "SinglePeak-Resolution", "compound_idx": 0},
    )
    output_dir = tmp_path / "qualification"
    report = qualify_moses_scores(archive_path, replay_dir, output_dir)
    rows = _read_csv(output_dir / "observations.csv")
    raw = [row for row in rows if row["input_policy"] == "raw_resolution_array"]
    summaries = [row for row in rows if row["input_policy"] == "serialized_summary_hypothesis"]
    assert all(float(row["raw_array_critical_resolution"]) == 2.2 for row in raw)
    assert all(row["status"] == "unavailable" and not row["candidate_score"] for row in summaries)
    # The global minimum and selected target resolution are different quantities,
    # so their difference must not be labelled a contradictory summary.
    assert report["raw_summary_disagreements"]["critical_resolution_disagreement_count"] == 0
    assert report["raw_summary_disagreements"]["detected_count_disagreement_count"] == 0


def test_a_resealed_replay_change_during_scoring_prevents_publication(tmp_path, monkeypatch):
    from measure_service import moses_score_qualification as qualification

    archive_path, replay_dir = _fixture(tmp_path, monkeypatch)
    original = qualification._qualify_row

    def change_replay_after_initial_validation(source, runtime):
        result = original(source, runtime)
        path = replay_dir / "report.md"
        path.write_text(path.read_text() + "changed during scoring\n")
        _reseal_replay(replay_dir)
        return result

    monkeypatch.setattr(qualification, "_qualify_row", change_replay_after_initial_validation)
    output_dir = tmp_path / "qualification"
    with pytest.raises(ValueError, match="changed during"):
        qualify_moses_scores(archive_path, replay_dir, output_dir)
    assert not output_dir.exists()
    assert not list(tmp_path.glob(".moses-score-qualification-*"))


def test_unevaluated_summary_resolved_count_is_not_an_input_disagreement(tmp_path, monkeypatch):
    archive_path, replay_dir = _fixture(tmp_path, monkeypatch)
    output_dir = tmp_path / "qualification"
    qualify_moses_scores(archive_path, replay_dir, output_dir)
    rows = _read_csv(output_dir / "observations.csv")
    assert all(row["raw_array_resolved_count"] == "1" for row in rows)
    assert all(row["serialized_resolved_count"] == "" for row in rows)
    assert all(row["raw_vs_serialized_resolved_count_disagreement"] == "" for row in rows)


@pytest.mark.parametrize(("reported_resolved", "disagrees"), [(1, "False"), (0, "True")])
def test_evaluated_summary_resolved_counts_are_compared_when_available(
    tmp_path, monkeypatch, reported_resolved, disagrees
):
    archive_path, replay_dir = _fixture(
        tmp_path,
        monkeypatch,
        observations=[_observation(num_resolved_peaks=reported_resolved)],
        config_overrides={"objective": "NumPeaks-NumResPeaks"},
    )
    output_dir = tmp_path / "qualification"
    qualify_moses_scores(archive_path, replay_dir, output_dir)
    rows = _read_csv(output_dir / "observations.csv")
    assert all(row["serialized_resolved_count"] == str(reported_resolved) for row in rows)
    assert all(row["raw_vs_serialized_resolved_count_disagreement"] == disagrees for row in rows)
