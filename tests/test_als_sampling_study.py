"""Integrity and reproducibility checks for the declared ALS sensitivity study."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
STUDY_DIR = REPOSITORY / "docs" / "reference-data" / "als-sampling-study"
DRIVER = REPOSITORY / "scripts" / "qualify_als_sampling_study.py"
FIT_SCRIPT = REPOSITORY / "scripts" / "fit_als_sampling_study.R"
FIT_FIELDS = (
    "baseline_rmse_overall",
    "baseline_rmse_peak_free",
    "endpoint_left_true_mean",
    "endpoint_left_estimated_mean",
    "endpoint_left_mean_error",
    "endpoint_left_rmse",
    "endpoint_right_true_mean",
    "endpoint_right_estimated_mean",
    "endpoint_right_mean_error",
    "endpoint_right_rmse",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_prepared_inputs(output_dir: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(DRIVER),
            "--prepare",
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "no fits run" in result.stdout


def test_declared_input_csv_is_complete_hashed_and_nested(tmp_path: Path) -> None:
    output_dir = tmp_path / "prepared"
    _write_prepared_inputs(output_dir)
    declaration = json.loads((output_dir / "declaration.json").read_text())
    expected_counts = declaration["signal"]["sample_counts"]
    expected_noise = declaration["signal"]["noise"]["noise_sd_values"]
    expected_seeds = declaration["signal"]["noise"]["seed_values"]
    manifest = _read_csv(output_dir / "input_manifest.csv")
    points = _read_csv(output_dir / "inputs.csv")

    assert len(manifest) == 45
    assert len(points) == sum(expected_counts) * len(expected_noise) * len(expected_seeds)
    points_by_id: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in points:
        points_by_id[row["input_id"]].append(row)

    values_by_condition: dict[tuple[int, float, int], list[dict[str, str]]] = {}
    for record in manifest:
        rows = points_by_id[record["input_id"]]
        rows.sort(key=lambda row: int(row["sample_index"]))
        assert len(rows) == int(record["sample_count"])
        assert [int(row["sample_index"]) for row in rows] == list(range(len(rows)))
        for field in ("axis", "true_baseline", "peak_signal", "noise", "observed_signal"):
            values = [float(row[field]) for row in rows]
            assert all(math.isfinite(value) for value in values)
        hash_record = {
            "input_id": record["input_id"],
            "seed": int(record["seed"]),
            "sample_count": int(record["sample_count"]),
            "noise_sd": float(record["noise_sd"]),
            **{
                field: [float(row[field]) for row in rows]
                for field in (
                    "axis",
                    "true_baseline",
                    "peak_signal",
                    "noise",
                    "observed_signal",
                )
            },
        }
        canonical = json.dumps(
            hash_record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        assert hashlib.sha256(canonical).hexdigest() == record["input_sha256"]
        key = (int(record["sample_count"]), float(record["noise_sd"]), int(record["seed"]))
        values_by_condition[key] = rows

    assert len(values_by_condition) == 45
    for noise_sd in expected_noise:
        for seed in expected_seeds:
            fine = values_by_condition[(max(expected_counts), float(noise_sd), int(seed))]
            for sample_count in expected_counts[:-1]:
                sparse = values_by_condition[(int(sample_count), float(noise_sd), int(seed))]
                stride = (len(fine) - 1) // (int(sample_count) - 1)
                assert [row["axis"] for row in sparse] == [
                    fine[index * stride]["axis"] for index in range(int(sample_count))
                ]
                assert [row["noise"] for row in sparse] == [
                    fine[index * stride]["noise"] for index in range(int(sample_count))
                ]


def test_summary_csv_matches_all_315_rows_and_declared_seed_statistics() -> None:
    declaration = json.loads((STUDY_DIR / "declaration.json").read_text())
    fits = _read_csv(STUDY_DIR / "fits.csv")
    summaries = _read_csv(STUDY_DIR / "summary.csv")
    assert len(fits) == declaration["implementation"]["fit_count"] == 315
    assert len(summaries) == 3 * 3 * 7
    assert all("fit_warnings" in row for row in fits)
    assert all(math.isfinite(float(row[field])) for row in fits for field in FIT_FIELDS)

    groups: dict[tuple[int, float, float], list[dict[str, str]]] = defaultdict(list)
    for row in fits:
        groups[(int(row["sample_count"]), float(row["noise_sd"]), float(row["lambda"]))].append(row)
    summaries_by_key = {
        (int(row["sample_count"]), float(row["noise_sd"]), float(row["lambda"])): row
        for row in summaries
    }
    assert set(groups) == set(summaries_by_key)
    for key, rows in groups.items():
        assert len(rows) == 5
        assert len({int(row["seed"]) for row in rows}) == 5
        summary = summaries_by_key[key]
        assert int(summary["seed_count"]) == 5
        for metric in FIT_FIELDS:
            values = [float(row[metric]) for row in rows]
            assert all(math.isfinite(value) for value in values)
            observed = {
                "median": statistics.median(values),
                "minimum": min(values),
                "maximum": max(values),
                "mean": statistics.mean(values),
                "sample_sd": statistics.stdev(values),
            }
            for name, expected in observed.items():
                got = float(summary[f"{metric}_{name}"])
                assert math.isfinite(got)
                assert math.isclose(got, expected, rel_tol=0, abs_tol=1e-14)
            assert (
                float(summary[f"{metric}_minimum"])
                <= float(summary[f"{metric}_median"])
                <= float(summary[f"{metric}_maximum"])
            )

    run = json.loads((STUDY_DIR / "run.json").read_text())
    assert run["fit_count"] == 315
    assert run["unique_input_count"] == 45
    assert run["no_parameter_selection_performed"] is True
    assert run["runtime"]["measure_r_code_database_md5"]
    assert (
        run["artifacts_sha256"]["fits.csv"]
        == hashlib.sha256((STUDY_DIR / "fits.csv").read_bytes()).hexdigest()
    )


@pytest.mark.r_backend
def test_single_r_fit_matches_well_conditioned_reference() -> None:
    r_binary = os.environ.get("MEASURE_MCP_TEST_R") or os.environ.get("MEASURE_MCP_R")
    if not r_binary:
        pytest.skip("Set MEASURE_MCP_TEST_R to run the ALS recipe smoke test.")

    declaration = json.loads((STUDY_DIR / "declaration.json").read_text())
    input_id = "n101-noise0.01-seed101"
    manifest = next(
        row for row in _read_csv(STUDY_DIR / "input_manifest.csv") if row["input_id"] == input_id
    )
    points = sorted(
        (row for row in _read_csv(STUDY_DIR / "inputs.csv") if row["input_id"] == input_id),
        key=lambda row: int(row["sample_index"]),
    )
    assert len(points) == 101
    input_record = {
        "input_id": input_id,
        "input_sha256": manifest["input_sha256"],
        "seed": int(manifest["seed"]),
        "sample_count": int(manifest["sample_count"]),
        "noise_sd": float(manifest["noise_sd"]),
        "axis": [float(row["axis"]) for row in points],
        "true_baseline": [float(row["true_baseline"]) for row in points],
        "observed_signal": [float(row["observed_signal"]) for row in points],
    }
    request = {
        "inputs": [input_record],
        "lambda_values": [declaration["settings"]["lambda_values"][0]],
        "asymmetry_p": declaration["settings"]["p"],
        "max_iter": declaration["settings"]["max_iter"],
        "low_peak_windows": declaration["metrics"]["low_peak_windows"],
        "endpoint_cut": declaration["metrics"]["endpoint_cut"],
    }
    environment = os.environ.copy()
    environment["MEASURE_MCP_R"] = r_binary
    environment["MEASURE_MCP_TEST_R"] = r_binary
    result = subprocess.run(
        [
            r_binary,
            "--vanilla",
            "--slave",
            "-f",
            str(FIT_SCRIPT),
        ],
        cwd=REPOSITORY,
        env=environment,
        input=json.dumps(request, separators=(",", ":"), allow_nan=False),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    fit_output = json.loads(result.stdout)
    runtime = fit_output["runtime"]
    for field in (
        "r_version",
        "measure_version",
        "measure_r_code_database_md5",
        "recipes_version",
    ):
        assert isinstance(runtime[field], str) and runtime[field]
    assert isinstance(runtime["library_paths"], list) and runtime["library_paths"]

    fits = fit_output["fits"]
    assert len(fits) == 1
    observed = fits[0]
    assert observed["input_id"] == input_id
    assert observed["input_sha256"] == manifest["input_sha256"]
    assert observed["seed"] == input_record["seed"]
    assert observed["sample_count"] == input_record["sample_count"]
    assert observed["noise_sd"] == input_record["noise_sd"]
    assert observed["lambda"] == request["lambda_values"][0]
    assert observed["asymmetry_p"] == request["asymmetry_p"]
    assert observed["max_iter"] == request["max_iter"]
    assert isinstance(observed["fit_warnings"], str)
    assert all(math.isfinite(float(observed[field])) for field in FIT_FIELDS)

    expected = next(
        row
        for row in _read_csv(STUDY_DIR / "fits.csv")
        if row["input_id"] == input_id and float(row["lambda"]) == request["lambda_values"][0]
    )
    for field in FIT_FIELDS:
        assert math.isclose(
            float(observed[field]),
            float(expected[field]),
            rel_tol=0,
            abs_tol=1e-8,
        )
