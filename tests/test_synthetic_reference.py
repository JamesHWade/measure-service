"""The public reference tables preserve their grain, mathematics and interpretation."""

import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from importlib.resources import files
from pathlib import Path

import pytest

from measure_methods.moses_objective import moses_observation_score

ROOT = Path(__file__).resolve().parents[1]


def _reference():
    location = ROOT / "docs" / "reference-data"
    metadata = json.loads((location / "synthetic-reference.json").read_text())
    with (location / "synthetic-evaluations.csv").open(newline="") as source:
        evaluations = list(csv.DictReader(source))
    with (location / "synthetic-analytes.csv").open(newline="") as source:
        analytes = list(csv.DictReader(source))
    return metadata, evaluations, analytes


def _key(row):
    return row["task_id"], int(row["seed"]), row["policy"], int(row["ordinal"])


def test_reference_files_and_complete_logical_grains_are_integrity_bound():
    metadata, evaluations, analytes = _reference()
    assert metadata["counts"] == {
        "campaigns": 12,
        "evaluations": 120,
        "analytes": 360,
        "actual_gp_calls": 42,
    }
    assert len(evaluations) == len({_key(row) for row in evaluations}) == 120
    assert len(analytes) == len({(*_key(row), row["analyte_id"]) for row in analytes}) == 360
    assert Counter(row["policy"] for row in evaluations) == {"random": 60, "bo_mcp_gp_logei": 60}
    assert Counter(_key(row) for row in analytes) == {_key(row): 3 for row in evaluations}
    for name, item in metadata["files"].items():
        raw = (ROOT / "docs" / name).read_bytes()
        assert len(raw) == item["size_bytes"]
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
    assert (
        metadata["plot_script_sha256"]
        == hashlib.sha256(
            (ROOT / "scripts" / "plot_synthetic_campaigns.R").read_bytes()
        ).hexdigest()
    )


def test_every_public_analyte_matches_independent_isocratic_and_gaussian_equations():
    metadata, evaluations, analytes = _reference()
    raw = (
        files("measure_service.benchmarks")
        .joinpath("reference_data/synthetic-isocratic-paired-v1.json")
        .read_bytes()
    )
    assert hashlib.sha256(raw).hexdigest() == metadata["predeclared_input_sha256"]
    declaration = json.loads(raw)
    tasks = {task["task_id"]: task for task in declaration["tasks"]}
    grouped = defaultdict(dict)
    for row in analytes:
        grouped[_key(row)][row["analyte_id"]] = row
    for evaluation in evaluations:
        rows = grouped[_key(evaluation)]
        task = tasks[evaluation["task_id"]]
        phi, duration = (
            float(evaluation["strong_solvent_fraction"]),
            float(evaluation["acquisition_duration_min"]),
        )
        geometry = declaration["geometry"]
        dead_time = (
            geometry["column_length_m"]
            * geometry["effective_area_m2"]
            * 1e6
            / declaration["flow_ml_min"]
        )
        times = {
            item["analyte_id"]: dead_time * (1 + math.exp(item["ln_k0"] - item["S1"] * phi))
            for item in task["analytes"]
        }
        eluted = sorted((time, name) for name, time in times.items() if time <= duration)
        incident = defaultdict(list)
        for (left_time, left), (right_time, right) in zip(eluted, eluted[1:]):
            resolution = (right_time - left_time) / (4 * task["fwhm_min"] / 2.3548)
            incident[left].append(resolution)
            incident[right].append(resolution)
        for name, expected_time in times.items():
            row = rows[name]
            if expected_time > duration:
                assert row["retention_status"] == "not_eluted"
                assert row["retention_time_min"] == row["resolution"] == ""
            else:
                assert row["retention_status"] == "eluted"
                assert float(row["retention_time_min"]) == pytest.approx(
                    expected_time, rel=1e-12, abs=0
                )
                if incident[name]:
                    assert row["resolution_status"] == "observed"
                    assert float(row["resolution"]) == pytest.approx(
                        min(incident[name]), rel=1e-12, abs=0
                    )
                else:
                    assert row["resolution_status"] == "unsupported" and row["resolution"] == ""
        # This is a qualified-kernel consistency check, separate from the
        # independent equation checks above and the retained Decimal-70 audit.
        score = moses_observation_score(
            profile="moses-botier-1.0.0-binary64/1",
            num_compounds=3,
            peak_resolutions=[
                float(rows[name]["resolution"]) if rows[name]["resolution"] else None
                for name in ("A", "B", "C")
            ],
            run_time_min=duration,
            max_run_time_min=10,
        )
        assert float(evaluation["utility_score"]) == pytest.approx(score["score"], rel=1e-12, abs=0)


def test_reference_success_definitions_and_common_initialization_stay_separate():
    metadata, evaluations, _ = _reference()
    indexed = {_key(row): row for row in evaluations}
    strict, utility = Counter(), Counter()
    for task in ("parallel-linear", "crossing-linear"):
        for seed in (17, 29, 43):
            for ordinal in range(3):
                left, right = [
                    indexed[task, seed, policy, ordinal] for policy in ("random", "bo_mcp_gp_logei")
                ]
                assert left["strong_solvent_fraction"] == right["strong_solvent_fraction"]
                assert left["acquisition_duration_min"] == right["acquisition_duration_min"]
            for policy in ("random", "bo_mcp_gp_logei"):
                rows = [indexed[task, seed, policy, ordinal] for ordinal in range(10)]
                strict[policy] += any(row["analytical_acceptance"] == "accepted" for row in rows)
                utility[policy] += any(float(row["utility_score"]) >= 2 for row in rows)
    assert strict == {"random": 5, "bo_mcp_gp_logei": 5}
    assert utility == {"random": 6, "bo_mcp_gp_logei": 3}
    assert sum(row["proposal_source"] == "bo_mcp_gp_logei" for row in evaluations) == 42
    for row in metadata["outcomes"]:
        assert row["cohort_size"] == 6
        assert (
            row["success_count"]
            == (strict if row["series"] == "strict_analytical_accepted" else utility)[row["policy"]]
        )
