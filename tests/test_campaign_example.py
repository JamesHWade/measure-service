"""The installed synthetic campaign example exercises durable MCP transitions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_synthetic_campaign_example_runs_outside_checkout(tmp_path):
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    source_tree = (root / "src").resolve()
    pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        entry
        for entry in pythonpath.split(os.pathsep)
        if entry and Path(entry).resolve() != source_tree
    )
    completed = subprocess.run(
        [sys.executable, str(root / "examples" / "synthetic_campaign.py")],
        capture_output=True,
        text=True,
        timeout=45,
        cwd=tmp_path,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    result = json.loads(completed.stdout)

    assert result["profile"] == "durable-synthetic-campaign-example/1"
    assert result["campaign"]["policy"] == "random"
    assert result["campaign"]["seed"] == 17
    assert result["campaign"]["state"] == "budget_complete"
    assert result["campaign"]["revision"] == 9
    assert result["campaign"]["evaluation_budget"] == 4
    assert result["campaign"]["consumed_evaluations"] == 4
    assert result["campaign"]["budget_remaining"] == 0
    assert result["campaign"]["evaluation_cost_total"] == 4.0
    assert result["campaign"]["evaluation_cost_unit"] == "synthetic_evaluation"

    assert result["capabilities"]["campaign_policies"] == ["random", "bo_mcp_gp_logei"]
    assert result["capabilities"]["random_policy_supported"] is True
    assert result["capabilities"]["generic_bo_service"] is False
    assert result["records"]["proposal_sources"] == [
        "common_initial_design",
        "common_initial_design",
        "common_initial_design",
        "random_policy",
    ]
    assert len(set(result["records"]["proposal_ids"])) == 4
    assert len(set(result["records"]["method_ids"])) == 4
    for field in (
        "proposal_ids",
        "method_ids",
        "snapshot_ids",
        "snapshot_sha256",
        "consumption_ids",
        "consumption_input_sha256",
        "consumption_output_sha256",
        "observation_ids",
    ):
        assert len(result["records"][field]) == 4
    for field in ("snapshot_sha256", "consumption_input_sha256", "consumption_output_sha256"):
        assert all(len(digest) == 64 for digest in result["records"][field])

    assert result["assertions"] == {
        "all_campaign_steps_used_mcp": True,
        "r_backend_required": False,
        "bo_backend_used": False,
        "initial_design_size": 3,
        "random_post_initialization_proposal": True,
        "all_proposals_used_stored_methods": True,
        "first_proposal_retry_stable_after_progress_and_restart": True,
        "first_consumption_retry_stable_after_progress_and_restart": True,
        "snapshots_unchanged_after_restart": True,
        "all_consumption_digests_unchanged_after_restart": True,
        "all_three_analytes_assessed_per_evaluation": True,
    }
