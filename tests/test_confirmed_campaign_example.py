"""Installed-runtime integration checks for the confirmed-reward example."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).parents[1] / "examples" / "confirmed_campaign.py"
MEASURE_BO_PYTHON = os.environ.get("MEASURE_BO_PYTHON")
EXAMPLE_PYTHON = os.environ.get("MEASURE_EXAMPLE_PYTHON", sys.executable)
EXAMPLE_PYTHONPATH = os.environ.get("MEASURE_EXAMPLE_PYTHONPATH")
REPOSITORY = Path(__file__).parents[1].resolve()


def _child_environment(*, include_bo_runtime: bool) -> dict[str, str]:
    environment = dict(os.environ)
    # Keep installed dependency overlays but never let source imports shadow the
    # package installed in the interpreter running the example and MCP server.
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
    environment.pop("MEASURE_EXAMPLE_PYTHONPATH", None)
    if not include_bo_runtime:
        environment.pop("MEASURE_BO_PYTHON", None)
    return environment


def _run_example(tmp_path: Path) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    runtime = EXAMPLE_PYTHON
    assert Path(runtime).is_absolute() and Path(runtime).is_file()
    completed = subprocess.run(
        [runtime, str(EXAMPLE)],
        cwd=tmp_path,
        env=_child_environment(include_bo_runtime=True),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_example_requires_an_explicit_bo_runtime(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(EXAMPLE)],
        cwd=tmp_path,
        env=_child_environment(include_bo_runtime=False),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "requires an installed BO-MCP runtime" in completed.stderr


@pytest.mark.bo_backend
@pytest.mark.skipif(not MEASURE_BO_PYTHON, reason="requires MEASURE_BO_PYTHON")
def test_confirmed_reward_campaign_survives_restart_and_uses_actual_bo(
    tmp_path: Path,
) -> None:
    first = _run_example(tmp_path / "first-run")
    second = _run_example(tmp_path / "second-run")

    for result in (first, second):
        assert result["profile"] == "confirmed-acceptance-ablation-example/1"
        assert result["owner"] == "local"
        assert result["protocol"] == {
            "protocol_id": result["protocol"]["protocol_id"],
            "profile": "synthetic-isocratic-ablation/1",
            "optimizer_target": "confirmed_acceptance_reward",
            "surrogate_noise_prior": "low_noise",
            "noise_prior_parameters": [1.1, 100.0],
            "seed": 17,
            "initial_design_size": 3,
            "evaluation_budget": 4,
            "evaluation_cost_unit": "synthetic_evaluation",
        }
        assert result["campaign"]["state"] == "budget_complete"
        assert result["campaign"]["consumed_evaluations"] == 4
        assert result["campaign"]["budget_remaining"] == 0
        assert result["campaign"]["evaluation_cost_total"] == 4.0
        assert result["records"]["analytical_acceptance"] == ["unknown"] * 4
        assert result["records"]["proposal_sources"] == [
            "common_initial_design",
            "common_initial_design",
            "common_initial_design",
            "bo_mcp_gp_logei",
        ]
        assert result["records"]["retention_statuses"] == [["not_eluted"] * 3] * 4
        assert result["records"]["resolution_statuses"] == [["censored"] * 3] * 4
        assert [target["value"] for target in result["records"]["optimizer_targets"]] == [0.0] * 4
        assert all(
            target["name"] == "confirmed_acceptance_reward"
            and target["profile"] == "confirmed-acceptance-lower-bound/1"
            and target["status"] == "scored"
            for target in result["records"]["optimizer_targets"]
        )
        assert result["bo_training"]["count"] == 3
        assert re.fullmatch(r"[0-9a-f]{64}", result["bo_training"]["sha256"])
        assert result["bo_training"]["adapter_profile"] == "bo-mcp-gp-logei-ablation/1"
        assert result["bo_training"]["objective"] == {
            "name": "confirmed_acceptance_reward",
            "direction": "maximize",
        }
        assert result["bo_training"]["noise_prior"] == {
            "family": "Gamma",
            "concentration": 1.1,
            "rate": 100.0,
            "space": "standardized_target_variance",
            "constraint_lower_bound": 0.0001,
            "learned": True,
        }
        assert [member["ordinal"] for member in result["bo_training"]["snapshot_members"]] == [
            0,
            1,
            2,
        ]
        assert all(member["included"] for member in result["bo_training"]["snapshot_members"])
        assert [member["snapshot_id"] for member in result["bo_training"]["snapshot_members"]] == (
            result["records"]["snapshot_ids"][:3]
        )
        assert result["assertions"]["all_three_initial_assessments_unknown"] is True
        assert result["assertions"]["all_unknowns_scored_zero_without_reclassifying_acceptance"]
        assert result["assertions"]["three_zero_rewards_used_for_actual_bo_training"]
        assert result["assertions"]["bo_suggested_after_service_restart"]
        assert result["assertions"]["first_suggestion_and_consumption_retry_stable_after_restart"]

    # The scientific design is frozen across fresh stores while durable record IDs are new.
    assert first["records"]["candidates"] == second["records"]["candidates"]
    assert first["campaign"]["campaign_id"] != second["campaign"]["campaign_id"]
    assert first["protocol"]["protocol_id"] != second["protocol"]["protocol_id"]
    for field in ("proposal_ids", "snapshot_ids", "consumption_ids"):
        assert set(first["records"][field]).isdisjoint(second["records"][field])
