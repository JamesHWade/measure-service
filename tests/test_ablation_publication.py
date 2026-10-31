"""Integrity and coverage checks for the packaged synthetic ablation reference."""

from __future__ import annotations

import csv
import hashlib
import json
import stat
from collections import Counter
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PUBLICATION_DIR = DOCS / "reference-data" / "ablations"
PUBLICATION_FILE = PUBLICATION_DIR / "publication.json"

EXPECTED_OUTER_FILES = {
    "figures/ablation-endpoints.png",
    "figures/ablation-endpoints.svg",
    "figures/ablation-success.png",
    "figures/ablation-success.svg",
    "reference-data/ablations/analytes.csv",
    "reference-data/ablations/complete.json",
    "reference-data/ablations/evaluations.csv",
    "reference-data/ablations/metadata.json",
    "reference-data/ablations/independent-review.json",
    "reference-data/ablations/paired-comparisons.csv",
    "reference-data/ablations/paired-contrast-summary.csv",
    "reference-data/ablations/success-trajectories.csv",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular(path: Path) -> bytes:
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode), f"expected a regular non-symlink file: {path}"
    return path.read_bytes()


def _read_json(path: Path) -> dict:
    return json.loads(_read_regular(path))


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames is not None
        return reader.fieldnames, list(reader)


def test_publication_and_core_seals_match_published_files_and_metadata():
    publication = _read_json(PUBLICATION_FILE)
    assert publication["profile"] == "measure-service-synthetic-ablation-publication/1"
    assert set(publication["files"]) == EXPECTED_OUTER_FILES

    for relative_name, entry in publication["files"].items():
        relative = PurePosixPath(relative_name)
        assert not relative.is_absolute()
        assert ".." not in relative.parts
        data = _read_regular(DOCS.joinpath(*relative.parts))
        assert len(data) == entry["size_bytes"]
        assert _digest(data) == entry["sha256"]

    complete_bytes = _read_regular(PUBLICATION_DIR / "complete.json")
    complete = json.loads(complete_bytes)
    assert complete["schema_version"] == 1
    assert complete["profile"] == "measure-service-synthetic-ablation-reference/1"
    assert set(complete["files"]) == {"analytes.csv", "evaluations.csv", "metadata.json"}
    for filename, entry in complete["files"].items():
        data = _read_regular(PUBLICATION_DIR / filename)
        assert len(data) == entry["size_bytes"]
        assert _digest(data) == entry["sha256"]

    metadata = _read_json(PUBLICATION_DIR / "metadata.json")
    review = _read_json(PUBLICATION_DIR / "independent-review.json")
    assert review["status"] == "passed"
    assert review["issues"] == review["near_threshold_reviews"] == []
    assert review["core_sha256"]["complete.json"] == publication["core_complete_sha256"]
    assert metadata["profile"] == complete["profile"]
    assert metadata["source_bundle"]["complete_json_sha256"] == publication["core_complete_sha256"]
    assert metadata["study"]["sha256"] == publication["study_fixture_sha256"]
    assert metadata["study"]["sha256"] == (
        "0c607fa590c3c5682018765f25b37c4c9820a2ef36fc3d57bb205d0f1aa31094"
    )
    assert len(metadata["study"]["arms"]) == 5
    assert len(metadata["study"]["tasks"]) == 4
    assert metadata["study"]["seeds"] == [17, 29, 43]
    assert metadata["study"]["evaluation_budget"] == 10


def test_published_tables_preserve_planned_grains_keys_and_campaigns():
    publication = _read_json(PUBLICATION_FILE)
    metadata = _read_json(PUBLICATION_DIR / "metadata.json")
    evaluation_fields, evaluations = _read_csv(PUBLICATION_DIR / "evaluations.csv")
    analyte_fields, analytes = _read_csv(PUBLICATION_DIR / "analytes.csv")
    evaluation_table = metadata["tables"]["evaluations.csv"]
    analyte_table = metadata["tables"]["analytes.csv"]

    assert evaluation_fields == evaluation_table["columns"]
    assert analyte_fields == analyte_table["columns"]
    assert evaluation_table["grain"] == "one row per planned (task_id, seed, arm_id, ordinal) slot"
    assert analyte_table["key"] == ["task_id", "seed", "arm_id", "ordinal", "analyte_id"]
    assert evaluation_table["planned_rows"] == evaluation_table["exported_rows"] == 600
    assert analyte_table["planned_rows"] == analyte_table["exported_rows"] == 1800
    assert len(evaluations) == 600
    assert len(analytes) == 1800

    evaluation_keys = {
        (row["task_id"], row["seed"], row["arm_id"], row["ordinal"]) for row in evaluations
    }
    expected_evaluation_keys = {
        (task["task_id"], str(seed), arm["arm_id"], str(ordinal))
        for task in metadata["study"]["tasks"]
        for seed in metadata["study"]["seeds"]
        for arm in metadata["study"]["arms"]
        for ordinal in range(metadata["study"]["evaluation_budget"])
    }
    assert len(evaluation_keys) == 600
    assert evaluation_keys == expected_evaluation_keys

    analyte_keys = {
        (row["task_id"], row["seed"], row["arm_id"], row["ordinal"], row["analyte_id"])
        for row in analytes
    }
    expected_analyte_keys = {
        (task["task_id"], str(seed), arm["arm_id"], str(ordinal), analyte["analyte_id"])
        for task in metadata["study"]["tasks"]
        for seed in metadata["study"]["seeds"]
        for arm in metadata["study"]["arms"]
        for ordinal in range(metadata["study"]["evaluation_budget"])
        for analyte in task["analytes"]
    }
    assert len(analyte_keys) == 1800
    assert analyte_keys == expected_analyte_keys

    by_campaign: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for row in evaluations:
        by_campaign.setdefault((row["task_id"], row["seed"], row["arm_id"]), []).append(row)
    assert len(by_campaign) == publication["planned_campaigns"] == 60
    assert len({rows[0]["campaign_id"] for rows in by_campaign.values()}) == 60
    assert all(len(rows) == 10 for rows in by_campaign.values())
    for rows in by_campaign.values():
        assert len({row["campaign_id"] for row in rows}) == 1
        assert len({row["campaign_status"] for row in rows}) == 1
    complete_campaigns = sum(
        rows[0]["campaign_status"] == "budget_complete" for rows in by_campaign.values()
    )
    partial_campaigns = sum(
        rows[0]["campaign_status"] == "partial_failure" for rows in by_campaign.values()
    )
    assert complete_campaigns == publication["budget_complete_campaigns"] == 41
    assert partial_campaigns == publication["duplicate_rejected_suggestion_attempts"] == 19


def test_partial_execution_keeps_unknown_rewards_blank_and_scored_zeros_present():
    publication = _read_json(PUBLICATION_FILE)
    _, evaluations = _read_csv(PUBLICATION_DIR / "evaluations.csv")
    statuses = Counter(row["evaluation_status"] for row in evaluations)
    assert statuses == {
        "consumed": 543,
        "partial_failure": 19,
        "not_attempted_unknown": 38,
    }

    consumed = [row for row in evaluations if row["consumed_evaluation"] == "True"]
    unresolved = [row for row in evaluations if row["evaluation_status"] != "consumed"]
    rejected_suggestions = [row for row in evaluations if row["failure_step"] == "suggest_campaign"]
    assert len(consumed) == publication["consumed_evaluations"] == 543
    assert len(unresolved) == publication["unresolved_future_slots"] == 57
    assert len(rejected_suggestions) == publication["duplicate_rejected_suggestion_attempts"] == 19
    assert {row["ordinal"] for row in rejected_suggestions} == {"7"}
    assert {row["failure_type"] for row in rejected_suggestions} == {"ValueError"}
    assert {row["failure_message"] for row in rejected_suggestions} == {
        "Campaign candidate duplicates a prior proposal"
    }

    unavailable_rewards = [
        row for row in evaluations if row["optimizer_reward_status"] == "unavailable"
    ]
    scored_rewards = [row for row in evaluations if row["optimizer_reward_status"] == "scored"]
    assert len(unavailable_rewards) == 57
    assert len(scored_rewards) == 543
    assert all(row["optimizer_reward_value"] == "" for row in unavailable_rewards)
    assert all(row["optimizer_reward_value"] != "" for row in scored_rewards)
    assert any(float(row["optimizer_reward_value"]) == 0.0 for row in scored_rewards)
