"""Installed-worker coverage for the first durable synthetic campaign chunk."""

from __future__ import annotations

import math
import os

import pytest

from measure_service.benchmarks import suggestion_sequences as sequences

pytestmark = pytest.mark.bo_backend


@pytest.mark.skipif(
    not os.environ.get("MEASURE_BO_PYTHON"),
    reason="requires the configured public BO-MCP sidecar metadata runtime",
)
def test_installed_worker_consumes_initial_durable_chunk_without_bo_calls(tmp_path):
    """Exercise installed package imports, source checks, and real worker receipts."""
    inputs = sequences._load_fixed_inputs(sequences.DEFAULT_DECLARATION)
    document = inputs["document"]
    sequences._require_replication_profile()

    placeholders = sequences._placeholder_bindings(document)
    design_streams = sequences._design_streams(document, placeholders)
    manifest = sequences._software_manifest(inputs, design_streams)
    assert manifest["bo_sidecar"]["status"] == "available"
    assert manifest["declaration_sha256"] == sequences.DECLARATION_SHA256
    assert manifest["fixture_oracle_sha256"] == sequences.FIXTURE_ORACLE_SHA256
    assert manifest["initial_design_sha256"] == sequences.INITIAL_DESIGN_SHA256
    assert manifest["oracle_source_sha256"] == sequences.ORACLE_SOURCE_SHA256
    assert set(manifest["host"]["module_sha256"]) >= {
        "measure_service.benchmarks.suggestion_sequences",
        "measure_service.campaigns",
        "measure_service.campaign_protocol",
        "measure_methods.gaussian_observations",
    }

    output_path = sequences._store_helpers._preflight_output(tmp_path / "output")
    data_path = sequences._ensure_new_data_dir(tmp_path / "data", output_path)
    run_dir, _ = sequences._reserve_qualification_run(
        data_path, inputs["declaration_sha256"], output_path
    )
    with sequences._persistent_run_evidence_directory(run_dir) as stage:
        copied = sequences._copy_verified_inputs(stage, inputs)
        assert copied["declaration.json"]["sha256"] == inputs["declaration_sha256"]
        assert copied["fixture-oracle.json"]["sha256"] == inputs["fixture_oracle_sha256"]
        assert copied["initial-design.json"]["sha256"] == inputs["initial_design_sha256"]
        assert copied[inputs["oracle_source_path"].name]["sha256"] == inputs["oracle_source_sha256"]

        with sequences._store_helpers._scoped_data_dir(data_path):
            sequences._store_helpers._claim_or_verify_owned_store(
                data_path,
                inputs["declaration_sha256"],
                profile=sequences.PROFILE,
                owner=sequences.OWNER,
                receipt_prefixes=(f"seq-{inputs['declaration_sha256'][:16]}-",),
            )
            bindings = sequences._register_fixture_inputs(document, inputs["declaration_sha256"])
            fixture_task_ids = {fixture["task_id"] for fixture in document["fixtures"]}
            binding_by_task = {binding["task_id"]: binding for binding in bindings}
            assert len(bindings) == 4
            assert set(binding_by_task) == fixture_task_ids

            protocols = sequences._register_durable_protocols(
                document, bindings, inputs["declaration_sha256"]
            )
            assert set(protocols) == {
                (prior, duplicate_policy)
                for prior in sequences._NOISE_PRIORS
                for duplicate_policy in sequences._DUPLICATE_POLICIES
            }
            assert sequences._design_streams(document, bindings) == design_streams

            for entry in protocols.values():
                protocol = entry["stored"]
                assert {task["task_id"] for task in protocol["tasks"]} == fixture_task_ids
                for task in protocol["tasks"]:
                    binding = binding_by_task[task["task_id"]]
                    assert task["producer"]["producer_id"] == binding["producer"]["producer_id"]
                    assert task["contract"]["contract_id"] == binding["contract"]["contract_id"]

            reject = protocols[("default", "reject")]
            replicate = protocols[("default", "replicate")]
            assert reject["registered"]["input_sha256"] != replicate["registered"]["input_sha256"]
            reject_spec = reject["stored"]["spec"]
            replicate_spec = replicate["stored"]["spec"]
            assert reject_spec["duplicate_policy"] == "reject"
            assert replicate_spec["duplicate_policy"] == "replicate"
            assert reject_spec["tasks"] == replicate_spec["tasks"]

            default_streams = {
                item["duplicate_policy"]: item
                for item in design_streams
                if item["optimizer_target"] == "confirmed_acceptance_reward"
                and item["surrogate_noise_prior"] == "default"
            }
            assert (
                default_streams["reject"]["coordinates"]
                == default_streams["replicate"]["coordinates"]
            )
            assert (
                default_streams["reject"]["coordinates"]
                == document["initial_design"]["coordinates"]
            )

            manifest["initial_design_streams"] = design_streams
            manifest["registry_bindings"] = [
                {
                    "task_id": item["task_id"],
                    "retention_model_id": item["retention_model"]["model_id"],
                    "producer_id": item["producer"]["producer_id"],
                    "contract_id": item["contract"]["contract_id"],
                    "model_input_sha256": item["retention_model"]["input_sha256"],
                    "producer_input_sha256": item["producer"]["input_sha256"],
                    "contract_input_sha256": item["contract"]["input_sha256"],
                }
                for item in bindings
            ]
            manifest["registered_protocols"] = [
                {
                    "optimizer_target": "confirmed_acceptance_reward",
                    "surrogate_noise_prior": prior,
                    "duplicate_policy": duplicate,
                    "protocol_id": entry["registered"]["protocol_id"],
                    "protocol_input_sha256": entry["registered"]["input_sha256"],
                    "protocol_output_sha256": entry["registered"]["output_sha256"],
                }
                for (prior, duplicate), entry in sorted(protocols.items())
            ]
            precall_path = stage / sequences._PRECALL_MARKER
            sequences._write_json(precall_path, manifest)
            sequences._fsync_file(precall_path)
            sequences._fsync_directory(stage)

            case = next(
                item
                for item in sequences._planned_durable_cases(document)
                if item["optimizer_target"] == "confirmed_acceptance_reward"
                and item["surrogate_noise_prior"] == "default"
                and item["duplicate_policy"] == "replicate"
            )
            protocol_entry = protocols[(case["surrogate_noise_prior"], case["duplicate_policy"])]
            case["protocol_id"] = protocol_entry["registered"]["protocol_id"]
            created = sequences.campaigns.create_campaign(
                sequences.OWNER,
                case["protocol_id"],
                case["task_id"],
                case["seed"],
                case["policy"],
                sequences._registry_key(
                    inputs["declaration_sha256"], "campaign", case["case_id"], "create"
                ),
            )
            case["campaign_id"] = created["campaign_id"]
            assert created["revision"] == 1
            assert created["state"] == "ready_to_suggest"
            assert created["policy"] == "bo_mcp_gp_logei"

            worker = sequences._invoke_worker(
                stage=stage,
                data_path=data_path,
                declaration_path=inputs["declaration_path"],
                declaration_sha256=inputs["declaration_sha256"],
                case=case,
                campaign_id=case["campaign_id"],
                protocol_id=case["protocol_id"],
                ordinals=(0, 1, 2),
                prior_slots=[],
                manifest=manifest,
            )

            response = worker["response"]
            assert worker["status"] == "returned", worker
            assert worker["return_code"] == 0
            assert isinstance(worker["pid"], int) and worker["pid"] != os.getpid()
            assert response["worker_pid"] == worker["pid"]
            assert response["status"] == "returned"
            assert response["case_id"] == case["case_id"]
            assert response["campaign_id"] == case["campaign_id"]
            assert response["ordinals_requested"] == [0, 1, 2]
            assert response["restart_receipt_replay"] is None
            assert len(response["slots"]) == 3
            assert not (stage / sequences._FIRST_CALL_MARKER).exists()

            task = response["campaign"]["task"]
            selected_binding = binding_by_task[case["task_id"]]
            assert task["task_id"] == case["task_id"]
            assert task["producer"]["producer_id"] == selected_binding["producer"]["producer_id"]
            assert task["contract"]["contract_id"] == selected_binding["contract"]["contract_id"]

            for ordinal, slot in enumerate(response["slots"]):
                assert slot["ordinal"] == ordinal
                assert slot["status"] == "consumed"
                assert slot["candidate"] == document["initial_design"]["coordinates"][ordinal]
                proposal = slot["proposal"]
                assert proposal["proposal_source"] == "common_initial_design"
                assert proposal["candidate"] == slot["candidate"]
                assert proposal["backend"] is None
                assert proposal["campaign_id"] == case["campaign_id"]
                assert proposal["ordinal"] == ordinal
                matches = [
                    {
                        "proposal_id": prior["proposal"]["proposal_id"],
                        "ordinal": prior["ordinal"],
                    }
                    for prior in response["slots"][:ordinal]
                    if prior["candidate"] == slot["candidate"]
                ]
                expected_repetition = {
                    "policy": "replicate",
                    "is_repeat": bool(matches),
                    "first_proposal_id": None if not matches else matches[0]["proposal_id"],
                    "matching_prior_proposals": matches,
                }
                assert proposal["repetition"] == expected_repetition
                assert slot["repetition"] == proposal["repetition"]
                if ordinal == 0:
                    assert proposal["repetition"] == {
                        "policy": "replicate",
                        "is_repeat": False,
                        "first_proposal_id": None,
                        "matching_prior_proposals": [],
                    }

                attempt = slot["suggestion_attempt"]
                assert attempt["state"] == "accepted"
                assert attempt["context"]["generation"]["kind"] == "common_initial_design"
                assert attempt["outcome"]["proposal_id"] == proposal["proposal_id"]
                assert attempt["outcome"]["candidate"] == proposal["candidate"]
                assert attempt["outcome"]["backend"] is None
                assert slot["oracle_comparison"]["passed"] is True
                assert slot["oracle"] == slot["oracle_comparison"]["oracle"]

                suggestion_replays = [
                    event
                    for event in slot["suggestion_events"]
                    if event.get("kind") == "suggestion_receipt_replay"
                ]
                assert len(suggestion_replays) == 1
                assert suggestion_replays[0]["same_response"] is True
                assert suggestion_replays[0]["resource_counts_unchanged"] is True
                assert slot["consumption_replay"]["same_response"] is True
                assert slot["consumption_replay"]["resource_counts_unchanged"] is True

            campaign = response["campaign"]
            assert campaign["policy"] == "bo_mcp_gp_logei"
            assert campaign["revision"] == 7
            assert campaign["consumed_evaluations"] == 3
            assert campaign["budget_remaining"] == 7
            assert [item["ordinal"] for item in campaign["history"]] == [0, 1, 2]
            assert math.isclose(
                sum(item["evaluation_cost"] for item in campaign["history"]),
                3 * document["protocol_template"]["evaluation_cost"],
                rel_tol=0,
                abs_tol=0,
            )

            journal = sequences._journal_records(
                stage / "durable_worker_journal.jsonl", case["case_id"]
            )
            assert journal.missing is False
            assert journal.tail_error is None
            assert len(journal) == 3
            assert [record["ordinal"] for record in journal] == [0, 1, 2]
            assert all(record["worker_pid"] == worker["pid"] for record in journal)
            assert all(
                sequences.canonical(record["slot"])
                == sequences.canonical(response["slots"][record["ordinal"]])
                for record in journal
            )

            attempts = sequences.campaigns.list_campaign_suggestion_attempts(
                sequences.OWNER, case["campaign_id"], limit=10
            )
            assert sorted(item["ordinal"] for item in attempts["records"]) == [0, 1, 2]
            for ordinal in range(3):
                record = sequences._attempt_record_for_ordinal(
                    sequences.OWNER, case["campaign_id"], ordinal
                )
                assert record["state"] == "accepted"
                assert record["context"]["generation"]["kind"] == "common_initial_design"
                assert record["outcome"]["backend"] is None

            counts = sequences._resource_counts(sequences.OWNER, case["campaign_id"])
            assert counts["proposals"] == 3
            assert counts["suggestion_attempts"] == 3
            assert counts["experiments"] == 3
            assert counts["attempts"] == 3
            assert counts["predictions"] == 3
            assert counts["gaussian_observations"] == 3
            assert counts["analytical_observations"] == 3
            assert counts["snapshots"] == 3
            assert counts["consumptions"] == 3
