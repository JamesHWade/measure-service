"""Installed-runtime test for the synthetic suggestion recovery example."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

EXAMPLE = Path(__file__).parents[1] / "examples" / "suggestion_recovery.py"
REPOSITORY = Path(__file__).parents[1].resolve()
EXAMPLE_PYTHON = os.environ.get("MEASURE_EXAMPLE_PYTHON", sys.executable)
EXAMPLE_PYTHONPATH = os.environ.get("MEASURE_EXAMPLE_PYTHONPATH")


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("MEASURE_BO_PYTHON", None)
    environment.pop("MEASURE_EXAMPLE_PYTHONPATH", None)
    pythonpath = (
        EXAMPLE_PYTHONPATH if EXAMPLE_PYTHONPATH is not None else environment.get("PYTHONPATH", "")
    )
    retained = [
        entry
        for entry in pythonpath.split(os.pathsep)
        if entry and Path(entry).resolve() not in {REPOSITORY, REPOSITORY / "src"}
    ]
    if retained:
        environment["PYTHONPATH"] = os.pathsep.join(retained)
    else:
        environment.pop("PYTHONPATH", None)
    return environment


def test_example_recovers_and_replays_missing_runtime_suggestion_failure(tmp_path: Path) -> None:
    assert Path(EXAMPLE_PYTHON).is_absolute() and Path(EXAMPLE_PYTHON).is_file()
    completed = subprocess.run(
        [EXAMPLE_PYTHON, str(EXAMPLE)],
        cwd=tmp_path,
        env=_child_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["profile"] == "synthetic-suggestion-recovery-example/1"
    assert "readiness demonstration only" in result["scope"]
    assert result["protocol"] == {
        "profile": "synthetic-isocratic-ablation/1",
        "optimizer_target": "confirmed_acceptance_reward",
        "surrogate_noise_prior": "default",
        "seed": 17,
        "initial_design_size": 3,
        "evaluation_budget": 4,
        "evaluation_cost_unit": "synthetic_evaluation",
    }
    assert result["campaign"]["consumed_evaluations"] == 3
    assert result["campaign"]["budget_remaining"] == 1
    assert result["campaign"]["state"] == "ready_to_suggest"
    assert len(result["campaign"]["initial_consumption_ids"]) == 3

    failure = result["suggestion_failure"]
    assert failure["ordinal"] == 3
    assert failure["state"] == "failed"
    assert failure["failure_type"] == "OptimizerBackendError"
    assert failure["failure_phase"] == "runtime_resolution"
    assert failure["failure_category"] == "runtime_missing"
    assert isinstance(failure["outer_candidate_generation_elapsed_seconds"], (int, float))
    assert failure["outer_candidate_generation_elapsed_seconds"] >= 0
    assert failure["child_subprocess_elapsed_seconds"] is None
    assert failure["requested_training_count"] == 3
    assert re.fullmatch(r"[0-9a-f]{64}", failure["requested_training_sha256"])
    assert [item["ordinal"] for item in failure["requested_training_references"]] == [0, 1, 2]
    assert len({item["snapshot_id"] for item in failure["requested_training_references"]}) == 3
    assert len({item["consumption_id"] for item in failure["requested_training_references"]}) == 3
    assert all(
        item["optimizer_training_included"] for item in failure["requested_training_references"]
    )

    assert (
        failure["attempt_record_sha256_before_retry"]
        == failure["attempt_record_sha256_after_retry"]
    )
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in failure["ledger_hashes"].values())
    assert result["retry"]["same_failure_replayed"] is True
    assert result["retry"]["attempt_records_before"] == 4
    assert result["retry"]["attempt_records_after"] == 4
    assert result["retry"]["no_new_proposal_or_consumption"] is True
    assert result["retry"]["optional_bo_runtime_configured_in_server"] is False
