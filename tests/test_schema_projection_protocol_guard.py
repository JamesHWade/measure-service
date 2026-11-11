"""Schema-boundary regressions that preserve the frozen protocol implementation."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from measure_service import campaign_protocol
from measure_service.benchmarks import suggestion_sequences
from measure_service.campaign_protocol import CampaignProtocolSpec, CandidateCoordinates

_REVIEWED_CAMPAIGN_PROTOCOL_SHA256 = (
    "4420bc4e469cbe73178b4b44147a863976487df3a2e8256fde76d4abf9d7479e"
)


def _protocol_mapping(**changes):
    value = {
        "domain": {
            "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
            "acquisition_duration_min": {"lower": 1.0, "upper": 10.0},
        },
        "flow_ml_min": 0.5,
        "constraints": {},
        "tasks": [
            {
                "task_id": "task-a",
                "producer_id": "producer-a",
                "observation_contract_id": "contract-a",
            },
            {
                "task_id": "task-b",
                "producer_id": "producer-b",
                "observation_contract_id": "contract-b",
            },
        ],
        "evaluation_cost": 1.0,
        "evaluation_cost_unit": "candidate-evaluation",
    }
    value.update(changes)
    return value


def test_campaign_protocol_source_pin_and_software_manifest_guard_without_live_bo(
    monkeypatch,
):
    protocol_bytes = Path(campaign_protocol.__file__).read_bytes()
    observed_sha256 = hashlib.sha256(protocol_bytes).hexdigest()
    assert observed_sha256 == _REVIEWED_CAMPAIGN_PROTOCOL_SHA256
    inputs = suggestion_sequences._load_fixed_inputs(suggestion_sequences.DEFAULT_DECLARATION)
    assert inputs["initial_design"]["installed_protocol_sha256"] == observed_sha256
    sidecar_probe = {"status": "unavailable", "reason": "isolated schema test"}
    monkeypatch.setattr(suggestion_sequences, "_probe_sidecar_identity", lambda: sidecar_probe)

    design_streams = suggestion_sequences._design_streams(
        inputs["document"], suggestion_sequences._placeholder_bindings(inputs["document"])
    )
    manifest = suggestion_sequences._software_manifest(inputs, design_streams)

    assert manifest["host"]["module_sha256"]["measure_service.campaign_protocol"] == (
        _REVIEWED_CAMPAIGN_PROTOCOL_SHA256
    )
    assert manifest["bo_sidecar"] == sidecar_probe

    mismatched = copy.deepcopy(inputs)
    mismatched["initial_design"]["installed_protocol_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="source differs from the reviewed initial design"):
        suggestion_sequences._software_manifest(mismatched, design_streams)


@pytest.mark.parametrize("field", ["flow_ml_min", "evaluation_cost"])
@pytest.mark.parametrize("value", [True, "0.5", float("nan"), float("inf"), -float("inf")])
def test_campaign_protocol_rejects_non_json_or_nonfinite_numeric_values(field, value):
    with pytest.raises(ValidationError):
        CampaignProtocolSpec.model_validate(_protocol_mapping(**{field: value}))


@pytest.mark.parametrize("field", ["strong_solvent_fraction", "acquisition_duration_min"])
@pytest.mark.parametrize("value", [True, "0.5", float("nan"), float("inf"), -float("inf")])
def test_candidate_coordinate_rejects_non_json_or_nonfinite_numeric_values(field, value):
    candidate = {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 5.0}
    candidate[field] = value
    with pytest.raises(ValidationError):
        CandidateCoordinates.model_validate(candidate)
