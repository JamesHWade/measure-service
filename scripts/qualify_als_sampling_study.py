#!/usr/bin/env python3
"""Prepare and run the fixed, declared ALS sampling study."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import random
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
STUDY_DIR = REPOSITORY / "docs" / "reference-data" / "als-sampling-study"
DECLARATION = STUDY_DIR / "declaration.json"
FIT_SCRIPT = Path(__file__).with_name("fit_als_sampling_study.R")
PLOT_SCRIPT = Path(__file__).with_name("plot_als_sampling_study.R")
INPUT_POINT_FIELDS = (
    "input_id",
    "seed",
    "sample_count",
    "noise_sd",
    "sample_index",
    "axis",
    "true_baseline",
    "peak_signal",
    "noise",
    "observed_signal",
)
FIT_NUMERIC_FIELDS = (
    "seed",
    "sample_count",
    "noise_sd",
    "lambda",
    "asymmetry_p",
    "max_iter",
    "baseline_rmse_overall",
    "baseline_rmse_peak_free",
    "endpoint_cut",
    "endpoint_left_true_mean",
    "endpoint_left_estimated_mean",
    "endpoint_left_mean_error",
    "endpoint_left_rmse",
    "endpoint_right_true_mean",
    "endpoint_right_estimated_mean",
    "endpoint_right_mean_error",
    "endpoint_right_rmse",
)
SUMMARY_METRICS = tuple(
    field
    for field in FIT_NUMERIC_FIELDS
    if field
    not in {
        "seed",
        "sample_count",
        "noise_sd",
        "lambda",
        "asymmetry_p",
        "max_iter",
        "endpoint_cut",
    }
)
SUMMARY_FIELDS = (
    "sample_count",
    "noise_sd",
    "lambda",
    "seed_count",
    *(
        f"{metric}_{statistic}"
        for metric in SUMMARY_METRICS
        for statistic in ("median", "minimum", "maximum", "mean", "sample_sd")
    ),
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_json(record: dict[str, Any]) -> bytes:
    return json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _read_declaration() -> dict[str, Any]:
    declaration = json.loads(DECLARATION.read_text(encoding="utf-8"))
    if declaration.get("status") != "declared-before-fit":
        raise SystemExit("Study declaration must remain declared-before-fit.")
    return declaration


def _normal_values(seed: int, count: int) -> list[float]:
    generator = random.Random(seed)
    values: list[float] = []
    for _ in range((count + 1) // 2):
        u1 = 1.0 - generator.random()
        u2 = generator.random()
        radius = math.sqrt(-2.0 * math.log(u1))
        values.append(radius * math.cos(2.0 * math.pi * u2))
        values.append(radius * math.sin(2.0 * math.pi * u2))
    return values[:count]


def _build_inputs(
    declaration: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    signal = declaration["signal"]
    noise_settings = signal["noise"]
    finest_count = max(signal["sample_counts"])
    finest_axis = [
        signal["axis_range"][0]
        + (signal["axis_range"][1] - signal["axis_range"][0]) * index / (finest_count - 1)
        for index in range(finest_count)
    ]
    strides = {count: finest_count // (count - 1) for count in signal["sample_counts"]}
    random_by_seed = {
        seed: _normal_values(seed, finest_count) for seed in noise_settings["seed_values"]
    }

    records: list[dict[str, Any]] = []
    point_rows: list[dict[str, Any]] = []
    for sample_count in signal["sample_counts"]:
        stride = strides[sample_count]
        for noise_sd in noise_settings["noise_sd_values"]:
            for seed in noise_settings["seed_values"]:
                indices = range(0, finest_count, stride)
                axis = [finest_axis[index] for index in indices]
                standard_noise = random_by_seed[seed]
                noise = [
                    standard_noise[index] * noise_sd for index in range(0, finest_count, stride)
                ]
                true_baseline = [
                    0.2 + 0.02 * x + 0.015 * math.sin(2.0 * math.pi * x / 10.0) for x in axis
                ]
                peak_signal = [
                    sum(
                        peak["amplitude"]
                        * math.exp(-0.5 * ((x - peak["center"]) / peak["sigma"]) ** 2)
                        for peak in signal["gaussian_peaks"]
                    )
                    for x in axis
                ]
                observed_signal = [
                    baseline + peak + random_noise
                    for baseline, peak, random_noise in zip(
                        true_baseline,
                        peak_signal,
                        noise,
                        strict=True,
                    )
                ]
                input_id = f"n{sample_count}-noise{noise_sd:.2f}-seed{seed}"
                record = {
                    "input_id": input_id,
                    "seed": seed,
                    "sample_count": sample_count,
                    "noise_sd": noise_sd,
                    "axis": axis,
                    "true_baseline": true_baseline,
                    "peak_signal": peak_signal,
                    "noise": noise,
                    "observed_signal": observed_signal,
                }
                record["input_sha256"] = _sha256(_canonical_json(record))
                records.append(record)
                for index, (x, baseline, peak, random_noise, observed) in enumerate(
                    zip(
                        axis,
                        true_baseline,
                        peak_signal,
                        noise,
                        observed_signal,
                        strict=True,
                    )
                ):
                    point_rows.append(
                        {
                            "input_id": input_id,
                            "seed": seed,
                            "sample_count": sample_count,
                            "noise_sd": noise_sd,
                            "sample_index": index,
                            "axis": x,
                            "true_baseline": baseline,
                            "peak_signal": peak,
                            "noise": random_noise,
                            "observed_signal": observed,
                        }
                    )
    return records, point_rows


def _csv_bytes(fieldnames: tuple[str, ...], rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _expected_inputs(
    declaration: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bytes, bytes]:
    records, points = _build_inputs(declaration)
    manifest = [
        {
            "input_id": record["input_id"],
            "seed": record["seed"],
            "sample_count": record["sample_count"],
            "noise_sd": record["noise_sd"],
            "input_sha256": record["input_sha256"],
        }
        for record in records
    ]
    point_rows = [{key: row[key] for key in INPUT_POINT_FIELDS} for row in points]
    manifest_content = _csv_bytes(
        ("input_id", "seed", "sample_count", "noise_sd", "input_sha256"),
        manifest,
    )
    points_content = _csv_bytes(INPUT_POINT_FIELDS, point_rows)
    return records, points, manifest_content, points_content


def _prepare(output_dir: Path, declaration: dict[str, Any]) -> list[dict[str, Any]]:
    records, _points, manifest_content, points_content = _expected_inputs(declaration)
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in (
        ("declaration.json", DECLARATION.read_bytes()),
        ("input_manifest.csv", manifest_content),
        ("inputs.csv", points_content),
    ):
        path = output_dir / filename
        if path.exists() and path.read_bytes() != content:
            raise SystemExit(f"Refusing to overwrite differing {path}.")
        if not path.exists():
            path.write_bytes(content)
    print(
        f"Prepared {len(records)} hashed signals and "
        f"{sum(record['sample_count'] for record in records)} samples; no fits run."
    )
    return records


def _verify_prepared(
    output_dir: Path,
    declaration: dict[str, Any],
) -> list[dict[str, Any]]:
    records, _points, manifest_content, points_content = _expected_inputs(declaration)
    for filename, expected in (
        ("declaration.json", DECLARATION.read_bytes()),
        ("input_manifest.csv", manifest_content),
        ("inputs.csv", points_content),
    ):
        path = output_dir / filename
        if not path.is_file():
            raise SystemExit(f"Missing {path}; run --prepare first.")
        if path.read_bytes() != expected:
            raise SystemExit(f"{path} differs from the declared deterministic inputs.")
    return records


def _source_provenance(
    source_dir: Path | None,
    source_head_file: Path | None,
) -> dict[str, Any]:
    if source_dir is None:
        return {"status": "not-supplied"}
    if not source_dir.is_dir():
        raise SystemExit(f"Measure source directory does not exist: {source_dir}")
    provenance: dict[str, Any] = {"status": "supplied"}
    for filename in ("peak-operations.R", "baseline-als.R"):
        path = source_dir / "R" / filename
        if path.is_file():
            provenance[f"{filename.replace('-', '_')}_sha256"] = _sha256(path.read_bytes())
    if source_head_file is not None:
        raw_head = json.loads(source_head_file.read_text(encoding="utf-8"))
        provenance["git_head"] = raw_head.get("head") if isinstance(raw_head, dict) else raw_head
        provenance["head_source"] = "supplied-file"
    else:
        result = subprocess.run(
            ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        provenance["git_head"] = result.stdout.strip() if result.returncode == 0 else None
        provenance["head_source"] = "git" if result.returncode == 0 else "unavailable"
    return provenance


def _fit_request(
    declaration: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    metrics = declaration["metrics"]
    settings = declaration["settings"]
    return {
        "inputs": [
            {
                "input_id": record["input_id"],
                "input_sha256": record["input_sha256"],
                "seed": record["seed"],
                "sample_count": record["sample_count"],
                "noise_sd": record["noise_sd"],
                "axis": record["axis"],
                "true_baseline": record["true_baseline"],
                "observed_signal": record["observed_signal"],
            }
            for record in records
        ],
        "lambda_values": settings["lambda_values"],
        "asymmetry_p": settings["p"],
        "max_iter": settings["max_iter"],
        "low_peak_windows": metrics["low_peak_windows"],
        "endpoint_cut": metrics["endpoint_cut"],
    }


def _run_r_script(
    r_binary: Path,
    script: Path,
    arguments: list[str],
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(r_binary), "--vanilla", "--slave", "-f", str(script)]
    if arguments:
        command.extend(["--args", *arguments])
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"R script failed ({result.returncode}):\n{result.stderr}\n{result.stdout}"
        )
    return result


def _summarize(fit_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[int, float, float], list[dict[str, Any]]] = {}
    for row in fit_rows:
        key = (int(row["sample_count"]), float(row["noise_sd"]), float(row["lambda"]))
        groups.setdefault(key, []).append(row)

    summaries: list[dict[str, Any]] = []
    for (sample_count, noise_sd, lam), rows in sorted(groups.items()):
        if len(rows) != 5 or len({int(row["seed"]) for row in rows}) != 5:
            raise SystemExit("Each summary group must contain the five declared seeds.")
        summary: dict[str, Any] = {
            "sample_count": sample_count,
            "noise_sd": noise_sd,
            "lambda": lam,
            "seed_count": len(rows),
        }
        for metric in SUMMARY_METRICS:
            values = [float(row[metric]) for row in rows]
            if not all(math.isfinite(value) for value in values):
                raise SystemExit(f"Metric {metric} contains a non-finite value.")
            summary.update(
                {
                    f"{metric}_median": statistics.median(values),
                    f"{metric}_minimum": min(values),
                    f"{metric}_maximum": max(values),
                    f"{metric}_mean": statistics.mean(values),
                    f"{metric}_sample_sd": statistics.stdev(values),
                }
            )
        summaries.append(summary)
    return summaries


def _fit(
    output_dir: Path,
    declaration: dict[str, Any],
    source_dir: Path | None,
    source_head_file: Path | None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = (
        "fits.csv",
        "summary.csv",
        "lambda-sampling-sensitivity.svg",
        "run.json",
    )
    existing = [name for name in outputs if (output_dir / name).exists()]
    if existing:
        raise SystemExit(
            f"Refusing to overwrite existing outputs in {output_dir}: {', '.join(existing)}. "
            "Choose a new --output-dir."
        )
    records = _verify_prepared(output_dir, declaration)
    source_info = _source_provenance(source_dir, source_head_file)
    r_text = os.environ.get("MEASURE_MCP_R") or os.environ.get("MEASURE_MCP_TEST_R")
    if not r_text:
        raise SystemExit("Set MEASURE_MCP_R to the explicit R binary used for this run.")
    r_binary = Path(r_text).expanduser().resolve()
    if not r_binary.is_file():
        raise SystemExit(f"R binary does not exist: {r_binary}")

    request = _fit_request(declaration, records)
    result = _run_r_script(
        r_binary,
        FIT_SCRIPT,
        [],
        input_text=json.dumps(request, separators=(",", ":"), allow_nan=False),
    )
    try:
        fit_output = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit(f"R fitting script returned invalid JSON: {error}\n{result.stdout}")

    runtime = fit_output["runtime"]
    for field in (
        "r_version",
        "measure_version",
        "measure_r_code_database_md5",
        "recipes_version",
    ):
        if not runtime.get(field):
            raise SystemExit(f"The R runner did not report installed runtime field {field}.")

    fit_rows = fit_output["fits"]
    if len(fit_rows) != declaration["implementation"]["fit_count"]:
        raise SystemExit("The R runner did not return the declared 315 fits.")
    expected_keys = {
        (record["input_id"], lam)
        for record in records
        for lam in declaration["settings"]["lambda_values"]
    }
    observed_keys = {(row["input_id"], row["lambda"]) for row in fit_rows}
    if observed_keys != expected_keys:
        raise SystemExit("The R runner fit set differs from the declared input/lambda grid.")
    hashes_by_input = {record["input_id"]: record["input_sha256"] for record in records}
    for row in fit_rows:
        if row.get("input_sha256") != hashes_by_input.get(row["input_id"]):
            raise SystemExit("A fit row has an undeclared input hash.")
        if any(not math.isfinite(float(row[field])) for field in FIT_NUMERIC_FIELDS):
            raise SystemExit("A fit row contains a non-finite numeric metric.")

    fit_fields = (
        "input_id",
        "input_sha256",
        *FIT_NUMERIC_FIELDS,
        "fit_warnings",
    )
    fits_content = _csv_bytes(fit_fields, fit_rows)
    summary_rows = _summarize(fit_rows)
    summary_content = _csv_bytes(SUMMARY_FIELDS, summary_rows)
    fits_path = output_dir / "fits.csv"
    summary_path = output_dir / "summary.csv"
    fits_path.write_bytes(fits_content)
    summary_path.write_bytes(summary_content)

    plot_result = _run_r_script(
        r_binary,
        PLOT_SCRIPT,
        [str(summary_path), str(output_dir / "lambda-sampling-sensitivity.svg")],
    )
    if plot_result.stderr.strip():
        print(plot_result.stderr.strip(), file=sys.stderr)

    script_hashes = {
        "python_driver_sha256": _sha256(Path(__file__).read_bytes()),
        "r_fitting_script_sha256": _sha256(FIT_SCRIPT.read_bytes()),
        "r_plotting_script_sha256": _sha256(PLOT_SCRIPT.read_bytes()),
    }
    artifacts = (
        "declaration.json",
        "input_manifest.csv",
        "inputs.csv",
        "fits.csv",
        "summary.csv",
        "lambda-sampling-sensitivity.svg",
    )
    run_record = {
        "profile": "measure-als-sampling-study-run/1",
        "status": "completed-fixed-grid",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "fit_count": len(fit_rows),
        "unique_input_count": len(records),
        "input_sample_rows": sum(record["sample_count"] for record in records),
        "r_binary": str(r_binary),
        "r_library_paths": runtime.get("library_paths", []),
        "runtime": runtime,
        "measure_source_provenance": source_info,
        "study_declaration_sha256": _sha256(DECLARATION.read_bytes()),
        **script_hashes,
        "fit_warning_rows": sum(bool(row.get("fit_warnings")) for row in fit_rows),
        "no_parameter_selection_performed": True,
        "artifacts_sha256": {name: _sha256((output_dir / name).read_bytes()) for name in artifacts},
    }
    (output_dir / "run.json").write_text(
        json.dumps(run_record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(fit_rows)} fits, {len(summary_rows)} summaries, and SVG to {output_dir}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true", help="write deterministic input CSVs only")
    mode.add_argument("--run", action="store_true", help="run the predeclared 315-fit ALS grid")
    parser.add_argument("--output-dir", type=Path, default=STUDY_DIR)
    parser.add_argument(
        "--measure-source-dir",
        type=Path,
        help="optional measure source tree for provenance only; never used to install or fit",
    )
    parser.add_argument(
        "--source-head-file",
        type=Path,
        help="optional JSON file with a `head` field, used with --measure-source-dir",
    )
    args = parser.parse_args()
    declaration = _read_declaration()
    if args.source_head_file and not args.measure_source_dir:
        parser.error("--source-head-file requires --measure-source-dir")
    if args.prepare:
        _prepare(args.output_dir, declaration)
    else:
        _fit(
            args.output_dir,
            declaration,
            args.measure_source_dir,
            args.source_head_file,
        )


if __name__ == "__main__":
    main()
