"""Actual optional BO-MCP execution through the installed public MCP workflow."""

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.bo_backend,
    pytest.mark.skipif(
        not os.environ.get("MEASURE_BO_PYTHON"), reason="optional BO-MCP runtime is not configured"
    ),
]


def test_actual_bo_campaign_replays_across_fresh_stores_and_server_restart(tmp_path):
    example = Path(__file__).resolve().parents[1] / "examples" / "synthetic_campaign.py"
    outputs = []
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, str(example), "--policy", "bo_mcp_gp_logei"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result["campaign"]["state"] == "budget_complete"
        assert result["campaign"]["consumed_evaluations"] == 4
        assert result["campaign"]["evaluation_cost_total"] == 4
        assert result["records"]["proposal_sources"] == ["common_initial_design"] * 3 + [
            "bo_mcp_gp_logei"
        ]
        assert result["assertions"]["bo_backend_used"] is True
        assert (
            result["assertions"]["first_proposal_retry_stable_after_progress_and_restart"] is True
        )
        assert (
            result["assertions"]["first_consumption_retry_stable_after_progress_and_restart"]
            is True
        )
        backend = result["records"]["backend_provenance"][-1]
        assert backend["backend_id"] == "bo-engine/BoTorchBackend"
        assert backend["generation_method"] == "bo"
        assert backend["model_type"] == "SingleTaskGP"
        assert backend["acquisition_implementation"] == "qLogExpectedImprovement"
        assert backend["training_count"] == 3
        assert all(
            backend["distribution_versions"][name]
            for name in ("bo-engine", "botorch", "gpytorch", "torch")
        )
        for key in (
            "source_tree_sha256",
            "adapter_source_sha256",
            "campaign_protocol_source_sha256",
            "design_kernel_source_sha256",
            "training_sha256",
        ):
            assert len(backend[key]) == 64
        assert math.isfinite(backend["runtime"]["elapsed_seconds"])
        assert backend["runtime"]["elapsed_seconds"] >= 0
        outputs.append(result)
    assert outputs[0]["records"]["candidates"] == outputs[1]["records"]["candidates"]
    assert outputs[0]["records"]["method_ids"] != outputs[1]["records"]["method_ids"]
    left, right = [result["records"]["backend_provenance"][-1] for result in outputs]
    assert left["training_sha256"] == right["training_sha256"]
    assert left["source_tree_sha256"] == right["source_tree_sha256"]
