"""Matched declarations bind owned immutable scientific inputs before suggestions."""

import json

import pytest

from measure_methods.gaussian_observations import RESOLUTION_DEFINITION
from measure_service import campaign_protocol_registry as protocols
from measure_service import gaussian_producer, observations, retention
from measure_service.campaign_protocol import (
    ABLATION_CAMPAIGN_PROFILE,
    optimizer_configuration,
)
from measure_service.storage import canonical, registry_connection, sha256


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))


def _inputs(owner="chemist", *, required=None, maximum=10, strong_solvent="B"):
    model = retention.register_retention_model(
        owner,
        "Synthetic task",
        {
            "gradient_coordinate": "column_inlet_at_analyte_entry",
            "analytes": [
                {"analyte_id": "A", "model": "linear", "ln_k0": 3.0, "S1": 4.0},
                {"analyte_id": "B", "model": "linear", "ln_k0": 4.5, "S1": 7.0},
            ],
            "column_length_m": 0.1,
            "effective_area_m2": 2.5e-6,
            "strong_solvent": strong_solvent,
            "parameter_origin": "synthetic",
            "parameter_reference": "Constructed test task; no physical fit",
        },
        "model",
    )
    producer = gaussian_producer.register_gaussian_producer(
        owner,
        "Synthetic widths",
        model["model_id"],
        {
            "widths": [{"analyte_id": name, "fwhm_min": 0.15} for name in ("A", "B")],
            "width_origin": "synthetic_assumption",
            "width_reference": "Fixed test width",
        },
        "producer",
    )
    contract = observations.register_observation_contract(
        owner,
        "Complete cohort",
        observations.ObservationContractSpec(
            required_analytes=required or ["A", "B"],
            resolution_definition=RESOLUTION_DEFINITION,
            max_acquisition_duration_min=maximum,
        ),
        "contract",
    )
    return {
        "domain": {
            "strong_solvent_fraction": {"lower": 0.1, "upper": 0.9},
            "acquisition_duration_min": {"lower": 2.0, "upper": 10.0},
        },
        "flow_ml_min": 0.5,
        "constraints": {},
        "tasks": [
            {
                "task_id": "constructed",
                "producer_id": producer["producer_id"],
                "observation_contract_id": contract["contract_id"],
            }
        ],
        "seeds": [17, 29, 43],
        "evaluation_budget": 10,
        "initial_design_size": 3,
        "evaluation_cost": 1.0,
        "evaluation_cost_unit": "synthetic_evaluation",
    }


def test_protocol_binds_scientific_identity_and_original_retry():
    spec = _inputs()
    response = protocols.register_campaign_protocol("chemist", "Matched task", spec, "protocol")
    assert protocols.get_campaign_protocol("chemist", response["protocol_id"]) == response
    assert (
        protocols.register_campaign_protocol("chemist", "Matched task", spec, "protocol")
        == response
    )
    assert response["tasks"][0]["analyte_ids"] == ["A", "B"]
    assert (
        response["tasks"][0]["contract"]["spec"]["utility_input_definition"]
        == "finite-resolution-reference/1"
    )
    assert response["tasks"][0]["producer"]["retention_model_input_sha256"]
    with pytest.raises(ValueError, match="not found or is not accessible"):
        protocols.get_campaign_protocol("stranger", response["protocol_id"])
    with pytest.raises(ValueError, match="not found or is not accessible"):
        protocols.register_campaign_protocol("stranger", "Stolen task", spec, "protocol")
    with pytest.raises(ValueError, match="different registry request"):
        protocols.register_campaign_protocol("chemist", "Changed title", spec, "protocol")


@pytest.mark.parametrize("change", [{"required": ["A"]}, {"maximum": 9}, {"strong_solvent": "A"}])
def test_protocol_rejects_mismatched_cohort_duration_or_coordinate(change):
    spec = _inputs(**change)
    with pytest.raises(ValueError):
        protocols.register_campaign_protocol("chemist", "Invalid task", spec, "protocol")
    with registry_connection() as con:
        assert con.execute("SELECT COUNT(*) FROM campaign_protocols").fetchone()[0] == 0
        assert (
            con.execute(
                "SELECT COUNT(*) FROM registry_receipts WHERE request_key='protocol'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("section", ["spec", "tasks", "implementation_provenance"])
def test_protocol_retrieval_verifies_every_retained_section(section):
    response = protocols.register_campaign_protocol(
        "chemist", "Matched task", _inputs(), "protocol"
    )
    with registry_connection(write=True) as con:
        row = con.execute(
            "SELECT * FROM campaign_protocols WHERE id=?", (response["protocol_id"],)
        ).fetchone()
        payload = json.loads(row["payload"])
        if section == "spec":
            payload["spec"]["evaluation_budget"] += 1
        elif section == "tasks":
            payload["tasks"][0]["producer"]["input_sha256"] = "0" * 64
        else:
            payload["implementation_provenance"]["registry_module_sha256"] = "0" * 64
        con.execute(
            "UPDATE campaign_protocols SET payload=? WHERE id=?",
            (canonical(payload), response["protocol_id"]),
        )
    with pytest.raises(ValueError, match="digest"):
        protocols.get_campaign_protocol("chemist", response["protocol_id"])


def test_oversized_response_rolls_back_protocol_and_receipt(monkeypatch):
    spec = _inputs()
    original = protocols._protocol_response

    def oversized(row):
        return protocols._bounded({**original(row), "extra": "x" * 64_000})

    monkeypatch.setattr(protocols, "_protocol_response", oversized)
    with pytest.raises(ValueError, match="byte limit"):
        protocols.register_campaign_protocol("chemist", "Matched task", spec, "protocol")
    with registry_connection() as con:
        assert con.execute("SELECT COUNT(*) FROM campaign_protocols").fetchone()[0] == 0
        assert (
            con.execute(
                "SELECT COUNT(*) FROM registry_receipts WHERE request_key='protocol'"
            ).fetchone()[0]
            == 0
        )


def test_producer_source_identity_cannot_be_rebound_after_declaration():
    spec = _inputs()
    producer_id = spec["tasks"][0]["producer_id"]
    with registry_connection(write=True) as con:
        con.execute(
            "UPDATE gaussian_producers SET retention_model_input_digest=? WHERE id=?",
            ("0" * 64, producer_id),
        )
    with pytest.raises(ValueError, match="identity is inconsistent"):
        protocols.register_campaign_protocol("chemist", "Tampered task", spec, "protocol")


def test_protocol_output_hash_covers_bound_tasks_and_provenance():
    response = protocols.register_campaign_protocol(
        "chemist", "Matched task", _inputs(), "protocol"
    )
    assert response["output_sha256"] == sha256(
        {key: response[key] for key in ("spec", "tasks", "implementation_provenance")}
    )


def test_ablation_protocol_roundtrip_binds_optimizer_configuration_and_owner():
    spec = {
        **_inputs(),
        "profile": ABLATION_CAMPAIGN_PROFILE,
        "optimizer_target": "confirmed_acceptance_reward",
        "surrogate_noise_prior": "low_noise",
    }
    response = protocols.register_campaign_protocol(
        "chemist", "Ablation task", spec, "ablation-protocol"
    )

    assert response["spec"]["profile"] == ABLATION_CAMPAIGN_PROFILE
    assert response["spec"]["optimizer_target"] == "confirmed_acceptance_reward"
    assert response["spec"]["surrogate_noise_prior"] == "low_noise"
    assert response["optimizer_configuration"] == optimizer_configuration(response["spec"])
    assert response["output_sha256"] == sha256(
        {
            key: response[key]
            for key in (
                "spec",
                "tasks",
                "implementation_provenance",
                "optimizer_configuration",
            )
        }
    )
    assert protocols.get_campaign_protocol("chemist", response["protocol_id"]) == response
    with pytest.raises(ValueError, match="not found or is not accessible"):
        protocols.get_campaign_protocol("stranger", response["protocol_id"])


def test_ablation_protocol_retrieval_rejects_tampered_optimizer_configuration():
    response = protocols.register_campaign_protocol(
        "chemist",
        "Ablation task",
        {
            **_inputs(),
            "profile": ABLATION_CAMPAIGN_PROFILE,
            "optimizer_target": "historical_utility",
            "surrogate_noise_prior": "default",
        },
        "ablation-protocol",
    )
    with registry_connection(write=True) as con:
        row = con.execute(
            "SELECT * FROM campaign_protocols WHERE id=?", (response["protocol_id"],)
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["optimizer_configuration"]["noise_prior_params"] = [1.1, 100.0]
        con.execute(
            "UPDATE campaign_protocols SET payload=? WHERE id=?",
            (canonical(payload), response["protocol_id"]),
        )
    with pytest.raises(ValueError, match="optimizer configuration"):
        protocols.get_campaign_protocol("chemist", response["protocol_id"])
