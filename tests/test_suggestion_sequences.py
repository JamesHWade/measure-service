import hashlib
import json
import shutil
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from measure_service.benchmarks import suggestion_sequences as sequences

TASK_IDS = [f"task-{index}" for index in range(4)]


def _minimal_declaration():
    component_cases = []
    for target in sequences._COMPONENT_TARGETS:
        for prior in sequences._NOISE_PRIORS:
            for pattern in sequences._PATTERNS:
                case_id = f"{target}:{prior}:{pattern}"
                scores = sequences._expected_component_patterns()[(target, prior, pattern)]
                component_cases.append(
                    {
                        "case_id": case_id,
                        "optimizer_target": target,
                        "surrogate_noise_prior": prior,
                        "duplicate_policy": "replicate",
                        "seed": sequences.SEED,
                        "fabricated_score_sequence_by_training_ordinal": scores,
                    }
                )
    durable_cases = []
    for task_id in TASK_IDS:
        for prior in sequences._NOISE_PRIORS:
            for duplicate_policy in sequences._DUPLICATE_POLICIES:
                durable_cases.append(
                    {
                        "case_id": f"{task_id}:{prior}:{duplicate_policy}",
                        "task_id": task_id,
                        "optimizer_target": "confirmed_acceptance_reward",
                        "surrogate_noise_prior": prior,
                        "duplicate_policy": duplicate_policy,
                        "policy": "bo_mcp_gp_logei",
                        "seed": sequences.SEED,
                        "evaluation_budget": sequences.EVALUATION_BUDGET,
                        "initial_design_size": sequences.INITIAL_DESIGN_SIZE,
                    }
                )
    coordinates = list(sequences._INITIAL_FABRICATED_COORDINATES)
    return {
        "profile": sequences.PROFILE,
        "protocol_template": {
            "profile": sequences.PROTOCOL_PROFILE,
            "duplicate_policy": "replicate",
            "evaluation_budget": sequences.EVALUATION_BUDGET,
            "initial_design_size": sequences.INITIAL_DESIGN_SIZE,
            "seeds": [sequences.SEED],
            "tasks": [{"task_id": task_id} for task_id in TASK_IDS],
        },
        "fixtures": [{"task_id": task_id} for task_id in TASK_IDS],
        "component_sequences": {
            "case_count": sequences.PLANNED_COMPONENT_CASES,
            "planned_backend_calls": sequences.PLANNED_COMPONENT_CALLS,
            "initial_rows": sequences.INITIAL_DESIGN_SIZE,
            "call_ordinals": list(range(3, 10)),
            "initial_fabricated_coordinates": coordinates,
            "cases": component_cases,
        },
        "initial_design": {
            "coordinates": coordinates,
            "campaign_design_sha256": "0" * 64,
        },
        "durable_campaigns": {
            "campaign_count": sequences.PLANNED_DURABLE_CASES,
            "planned_analytical_slots": sequences.PLANNED_ANALYTICAL_SLOTS,
            "planned_gp_suggestion_slots": sequences.PLANNED_GP_SUGGESTIONS,
            "planned_initial_analytical_slots": 48,
            "planned_analyte_slots": 320,
            "cases": durable_cases,
        },
        "planned_totals": {
            "component_backend_call_slots": sequences.PLANNED_COMPONENT_CALLS,
            "campaign_gp_suggestion_slots": sequences.PLANNED_GP_SUGGESTIONS,
            "maximum_backend_calls_without_retries": 196,
            "analytical_evaluation_slots": sequences.PLANNED_ANALYTICAL_SLOTS,
            "analyte_slots": 320,
        },
        "oracle": {
            "required_cohort_size": (
                "len(fixture.analytes), currently2; never reuse prior three-analyte hardcoding"
            ),
        },
    }


def test_fixed_declaration_derives_component_patterns_and_preserves_full_denominators():
    document = sequences._validate_declaration(_minimal_declaration())

    component_rows = sequences._planned_component_rows(document)
    durable_cases = sequences._planned_durable_cases(document)

    assert len(component_rows) == 84
    assert len(durable_cases) == 16
    assert sum(len(case["slots"]) for case in durable_cases) == 160
    assert {row["pattern"] for row in component_rows} == set(sequences._PATTERNS)
    assert all(row["status"] == "not_attempted_unknown" for row in component_rows)
    assert all(
        slot["status"] == "not_attempted_unknown"
        for case in durable_cases
        for slot in case["slots"]
    )
    assert all("pattern" not in case for case in document["component_sequences"]["cases"])


def test_packaged_frozen_declaration_and_oracle_bundle_is_exact():
    inputs = sequences._load_fixed_inputs(sequences.DEFAULT_DECLARATION)
    document = inputs["document"]
    component_rows = sequences._planned_component_rows(document)
    durable_cases = sequences._planned_durable_cases(document)

    assert inputs["declaration_sha256"] == sequences.DECLARATION_SHA256
    assert inputs["fixture_oracle_sha256"] == sequences.FIXTURE_ORACLE_SHA256
    assert inputs["initial_design_sha256"] == sequences.INITIAL_DESIGN_SHA256
    assert inputs["oracle_source_sha256"] == sequences.ORACLE_SOURCE_SHA256
    assert len(component_rows) == sequences.PLANNED_COMPONENT_CALLS == 84
    assert len(durable_cases) == sequences.PLANNED_DURABLE_CASES == 16
    assert sum(len(case["slots"]) for case in durable_cases) == 160
    assert document["planned_totals"]["analyte_slots"] == 320
    assert (
        sum(
            len(fixture["analytes"])
            * sequences.EVALUATION_BUDGET
            * len(sequences._NOISE_PRIORS)
            * len(sequences._DUPLICATE_POLICIES)
            for fixture in document["fixtures"]
        )
        == 320
    )
    assert {row["pattern"] for row in component_rows} == set(sequences._PATTERNS)


def test_verified_oracle_executes_the_retained_bytes_after_path_mutation(tmp_path):
    inputs = sequences._load_fixed_inputs(sequences.DEFAULT_DECLARATION)
    oracle_path = tmp_path / "independent_oracle.py.txt"
    oracle_bytes = inputs["oracle_source_raw"]
    oracle_path.write_bytes(oracle_bytes)
    verified_module = sequences._load_decimal_oracle(oracle_path, oracle_bytes)
    fixture = inputs["document"]["fixtures"][0]
    candidate = inputs["document"]["initial_design"]["coordinates"][0]
    expected = verified_module.evaluate(fixture, candidate, inputs["document"])

    oracle_path.write_text("raise RuntimeError('changed after verified read')\n", encoding="utf-8")
    observed = verified_module.evaluate(fixture, candidate, inputs["document"])

    assert observed == expected
    assert hashlib.sha256(oracle_bytes).hexdigest() == sequences.ORACLE_SOURCE_SHA256


def test_packaged_oracle_tampering_fails_before_parsing(tmp_path):
    copied_bundle = tmp_path / "bundle"
    shutil.copytree(sequences.DEFAULT_DECLARATION.parent, copied_bundle)
    oracle_path = copied_bundle / "fixture-oracle.json"
    oracle_path.write_bytes(oracle_path.read_bytes() + b" ")

    with pytest.raises(ValueError, match="failed SHA256 verification"):
        sequences._load_fixed_inputs(copied_bundle / sequences.DEFAULT_DECLARATION.name)


def test_run_reservation_is_exclusive_and_blocks_resume(tmp_path):
    data_path = tmp_path / "data"
    output_path = tmp_path / "output"
    run_dir, reservation = sequences._reserve_qualification_run(data_path, "a" * 64, output_path)
    marker = data_path / sequences._RUN_RESERVATION_MARKER

    assert marker.is_file()
    assert run_dir.is_dir()
    assert json.loads(marker.read_text(encoding="utf-8"))["run_id"] == reservation["run_id"]
    assert json.loads((run_dir / "run_reservation.json").read_text(encoding="utf-8")) == reservation
    with pytest.raises(ValueError, match="reserved by another or previous run"):
        sequences._reserve_qualification_run(data_path, "a" * 64, output_path)
    with pytest.raises(ValueError, match="already has a reserved run"):
        sequences._ensure_new_data_dir(data_path, output_path)


def test_interrupted_run_keeps_evidence_and_never_allows_resume(tmp_path):
    run_dir = tmp_path / "persistent"
    run_dir.mkdir()

    with pytest.raises(KeyboardInterrupt):
        with sequences._persistent_run_evidence_directory(run_dir):
            (run_dir / "first_call.json").write_text("sealed", encoding="utf-8")
            raise KeyboardInterrupt

    interruption = json.loads((run_dir / "run_interruption.json").read_text(encoding="utf-8"))
    assert (run_dir / "first_call.json").read_text(encoding="utf-8") == "sealed"
    assert interruption["failure"]["type"] == "KeyboardInterrupt"
    assert interruption["resume_policy"].startswith("never")


def test_failed_bundle_publication_keeps_the_only_run_evidence(monkeypatch, tmp_path):
    run_dir = tmp_path / "persistent"
    run_dir.mkdir()
    evidence = run_dir / "component_calls.journal.jsonl"
    evidence.write_text('{"status":"returned"}\n', encoding="utf-8")

    def fail_publication(*args, **kwargs):
        raise OSError("synthetic staging failure")

    monkeypatch.setattr(sequences, "staged_bundle", fail_publication)

    with pytest.raises(OSError, match="synthetic staging failure"):
        sequences._publish_persistent_run(tmp_path / "published", run_dir)

    failure = json.loads((run_dir / "publication_failure.json").read_text(encoding="utf-8"))
    assert evidence.read_text(encoding="utf-8") == '{"status":"returned"}\n'
    assert failure["failure"]["type"] == "OSError"
    assert failure["failure"]["message"] == "synthetic staging failure"


def test_resource_counts_treats_absent_attempt_ledger_as_zero_without_ddl(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE campaign_proposals (owner TEXT, campaign_id TEXT);
        CREATE TABLE executable_methods (owner TEXT);
        CREATE TABLE experiments (owner TEXT);
        CREATE TABLE experiment_attempts (owner TEXT);
        CREATE TABLE retention_predictions (owner TEXT);
        CREATE TABLE gaussian_observations (owner TEXT);
        CREATE TABLE analytical_observations (owner TEXT);
        CREATE TABLE observation_snapshots (owner TEXT);
        CREATE TABLE campaign_consumptions (owner TEXT, campaign_id TEXT);
        """
    )

    @contextmanager
    def registry_connection():
        yield connection

    monkeypatch.setattr(sequences, "registry_connection", registry_connection)

    counts = sequences._resource_counts(sequences.OWNER, "campaign-absent-ledger")
    attempt_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_suggestion_attempts'"
    ).fetchone()

    assert counts["suggestion_attempts"] == 0
    assert attempt_table is None
    connection.close()


def test_four_fixture_initial_campaign_pipeline_uses_sources_and_oracle_without_bo(
    monkeypatch, tmp_path
):
    inputs = sequences._load_fixed_inputs(sequences.DEFAULT_DECLARATION)
    document = inputs["document"]
    data_path = tmp_path / "service-data"
    optimizer_calls = []

    def fail_if_optimizer_called(*args, **kwargs):
        optimizer_calls.append((args, kwargs))
        raise AssertionError("initial analytical slots must not invoke the BO optimizer")

    monkeypatch.setattr(sequences.optimizer_backend, "suggest_candidate", fail_if_optimizer_called)

    data_path.mkdir()
    with sequences._store_helpers._scoped_data_dir(data_path):
        sequences._store_helpers._claim_or_verify_owned_store(
            data_path,
            inputs["declaration_sha256"],
            profile=sequences.PROFILE,
            owner=sequences.OWNER,
            receipt_prefixes=(f"seq-{inputs['declaration_sha256'][:16]}-",),
        )
        bindings = sequences._register_fixture_inputs(document, inputs["declaration_sha256"])
        protocols = sequences._register_durable_protocols(
            document, bindings, inputs["declaration_sha256"]
        )
        oracle_module = sequences._load_decimal_oracle(
            inputs["oracle_source_path"], inputs["oracle_source_raw"]
        )
        durable_cases = sequences._planned_durable_cases(document)
        durable_cases_by_id = {case["case_id"]: case for case in durable_cases}

        for binding in bindings:
            case = durable_cases_by_id[f"{binding['task_id']}:default:replicate"]
            protocol_entry = protocols[("default", "replicate")]
            created = sequences.campaigns.create_campaign(
                sequences.OWNER,
                protocol_entry["registered"]["protocol_id"],
                case["task_id"],
                case["seed"],
                case["policy"],
                sequences._registry_key(
                    inputs["declaration_sha256"], "initial-pipeline-test", case["case_id"]
                ),
            )
            case["campaign_id"] = created["campaign_id"]
            campaign = sequences.campaigns.get_campaign(sequences.OWNER, case["campaign_id"])
            before_first_suggestion = sequences._resource_counts(
                sequences.OWNER, case["campaign_id"]
            )
            assert before_first_suggestion["suggestion_attempts"] == 0
            prior_slots = []

            for ordinal in range(sequences.INITIAL_DESIGN_SIZE):
                slot, campaign, stopped = sequences._process_slot(
                    document=document,
                    case=case,
                    campaign=campaign,
                    protocol=protocol_entry["spec"],
                    fixture=binding["fixture"],
                    oracle_module=oracle_module,
                    declaration_sha256=inputs["declaration_sha256"],
                    ordinal=ordinal,
                    prior_slots=prior_slots,
                )
                slot["iteration"] = ordinal
                case["slots"][ordinal] = slot
                prior_slots.append(slot)

                assert slot["status"] == "consumed", {
                    "failure": slot.get("failure"),
                    "oracle_mismatches": {
                        key: difference
                        for key, difference in slot.get("oracle_comparison", {})
                        .get("differences", {})
                        .items()
                        if difference.get("passed") is False
                    },
                }
                assert slot["oracle_comparison"]["passed"] is True, slot["oracle_comparison"][
                    "differences"
                ]
                assert stopped is False

            for slot in prior_slots:
                proposal_id = slot["proposal"]["proposal_id"]
                experiment_id = slot["experiment"]["experiment_id"]
                attempt_id = slot["attempt"]["attempt_id"]
                prediction_id = slot["retention_prediction"]["prediction_id"]
                gaussian_run_id = slot["gaussian_observation"]["run_id"]
                observation_id = slot["observation"]["observation_id"]
                snapshot_id = slot["snapshot"]["snapshot_id"]
                consumption_id = slot["consumption"]["consumption_id"]

                assert (
                    sequences.campaigns.get_campaign_proposal(sequences.OWNER, proposal_id)[
                        "proposal_id"
                    ]
                    == proposal_id
                )
                assert (
                    sequences.experiments.get_experiment(sequences.OWNER, experiment_id)[
                        "experiment_id"
                    ]
                    == experiment_id
                )
                assert (
                    sequences.experiments.get_attempt(sequences.OWNER, attempt_id)["attempt_id"]
                    == attempt_id
                )
                assert (
                    sequences.retention.get_retention_prediction(sequences.OWNER, prediction_id)[
                        "prediction_id"
                    ]
                    == prediction_id
                )
                assert (
                    sequences.gaussian_producer.get_gaussian_observation(
                        sequences.OWNER, gaussian_run_id
                    )["run_id"]
                    == gaussian_run_id
                )
                assert (
                    sequences.observations.get_observation(sequences.OWNER, observation_id)[
                        "observation_id"
                    ]
                    == observation_id
                )
                assert (
                    sequences.observations.get_observation_snapshot(sequences.OWNER, snapshot_id)[
                        "snapshot_id"
                    ]
                    == snapshot_id
                )
                assert (
                    sequences.campaigns.get_campaign_consumption(sequences.OWNER, consumption_id)[
                        "consumption_id"
                    ]
                    == consumption_id
                )

        assert optimizer_calls == []
        assert len(durable_cases) == 16
        assert sum(len(case["slots"]) for case in durable_cases) == 160
        assert (
            sum(
                len(binding["fixture"]["analytes"])
                * sequences.EVALUATION_BUDGET
                * len(sequences._NOISE_PRIORS)
                * len(sequences._DUPLICATE_POLICIES)
                for binding in bindings
            )
            == 320
        )
        assert (
            sum(slot["status"] == "consumed" for case in durable_cases for slot in case["slots"])
            == 12
        )
        assert (
            sum(
                slot["status"] == "not_attempted_unknown"
                for case in durable_cases
                for slot in case["slots"]
            )
            == 148
        )
        mixed_case = durable_cases_by_id["qualify-mixed-initial:default:replicate"]
        mixed_fixture = next(
            fixture
            for fixture in document["fixtures"]
            if fixture["task_id"] == "qualify-mixed-initial"
        )
        reversed_reference = oracle_module.evaluate(
            mixed_fixture,
            document["initial_design"]["coordinates"][1],
            document,
        )
        assert reversed_reference["pairs"][0]["analytes"] == ["B", "A"]
        assert mixed_case["slots"][1]["oracle_comparison"]["passed"] is True


def test_component_sequences_keep_duplicate_rows_and_stop_only_failed_case(monkeypatch):
    document = _minimal_declaration()
    bounds = SimpleNamespace(
        strong_solvent_fraction=SimpleNamespace(lower=0.1, upper=0.9),
        acquisition_duration_min=SimpleNamespace(lower=2.0, upper=10.0),
    )
    specs = {
        (target, prior): SimpleNamespace(domain=bounds)
        for target in sequences._COMPONENT_TARGETS
        for prior in sequences._NOISE_PRIORS
    }
    candidate = {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 5.0}
    calls = []

    def build_request(spec, history, seed):
        return {"history": json.loads(sequences.canonical(history)), "seed": seed}

    def suggest(spec, history, seed):
        calls.append((spec, json.loads(sequences.canonical(history)), seed))
        if len(history) == 4 and all(item["score"] == 0.0 for item in history):
            if len(calls) == 2:
                raise RuntimeError("synthetic backend failure")
        return {"candidate": dict(candidate), "backend": {}}

    monkeypatch.setattr(
        sequences,
        "_validate_component_response",
        lambda result, **kwargs: (
            dict(result["candidate"]),
            {"runtime": {"elapsed_seconds": 0.01}},
        ),
    )
    rows = sequences._run_component_sequences(
        document,
        [],
        metadata=None,
        first_call_marker=None,
        suggest_call=suggest,
        request_builder=build_request,
        protocol_specs=specs,
    )

    assert len(rows) == 84
    assert len(calls) == 79
    assert sum(row["attempted"] for row in rows) == 79
    failed = [row for row in rows if row["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["case_id"] == "historical_utility:default:all_zero"
    assert failed[0]["ordinal"] == 4
    remaining = [
        row
        for row in rows
        if row["case_id"] == failed[0]["case_id"] and row["ordinal"] > failed[0]["ordinal"]
    ]
    assert len(remaining) == 5
    assert all(row["status"] == "not_attempted_unknown" for row in remaining)
    assert all(
        len(row["history"]) == min(row["ordinal"], 4)
        if row["case_id"] == failed[0]["case_id"]
        else len(row["history"]) == row["ordinal"]
        for row in rows
    )
    assert all(row["history"][-1]["candidate"] == candidate for row in rows if row["ordinal"] > 3)


def test_shared_worker_journal_filters_other_valid_campaigns(tmp_path):
    journal = tmp_path / "worker.jsonl"
    records = [
        {
            "profile": "measure-service-suggestion-sequence-worker-slot/1",
            "case_id": case_id,
            "ordinal": ordinal,
            "slot": {"ordinal": ordinal, "status": "consumed"},
        }
        for case_id, ordinal in (("case-a", 0), ("case-b", 1))
    ]
    journal.write_text("".join(sequences.canonical(record) + "\n" for record in records))

    assert [row["case_id"] for row in sequences._journal_records(journal, "case-b")] == ["case-b"]


def test_restart_replays_exact_suggestion_attempt_and_consumption_receipts(monkeypatch):
    suggestion_request = {
        "expected_revision": 5,
        "idempotency_key": "original-suggestion-key",
    }
    suggestion_response = {"proposal": {"proposal_id": "proposal-5"}, "revision": 6}
    attempt = {"suggestion_attempt_id": "attempt-ledger-5", "state": "accepted"}
    consumption_request = {
        "proposal_id": "proposal-5",
        "snapshot_id": "snapshot-5",
        "expected_revision": 6,
        "idempotency_key": "original-consumption-key",
    }
    consumption_response = {
        "consumption": {"consumption_id": "consumption-5"},
        "revision": 7,
    }
    counts = {"proposals": 5, "consumptions": 2}
    campaign_state = {"revision": 7, "consumed_evaluations": 3, "state": "ready_to_suggest"}
    calls = []
    monkeypatch.setattr(
        sequences.campaigns,
        "get_campaign",
        lambda owner, campaign_id: dict(campaign_state),
    )
    monkeypatch.setattr(sequences, "_resource_counts", lambda owner, campaign_id: dict(counts))
    monkeypatch.setattr(
        sequences.campaigns,
        "suggest_campaign",
        lambda owner, campaign_id, revision, key: calls.append((revision, key))
        or dict(suggestion_response),
    )
    monkeypatch.setattr(
        sequences.campaigns,
        "get_campaign_suggestion_attempt",
        lambda owner, attempt_id: dict(attempt),
    )
    monkeypatch.setattr(
        sequences.campaigns,
        "consume_campaign_snapshot",
        lambda owner, campaign_id, proposal_id, snapshot_id, *, expected_revision, key: dict(
            consumption_response
        ),
    )

    result = sequences._replay_restart_receipts(
        "campaign-5",
        {
            "ordinal": 2,
            "suggestion_events": [
                {
                    "kind": "suggestion_request",
                    "request": suggestion_request,
                    "response": suggestion_response,
                }
            ],
            "suggestion_attempt": attempt,
            "consumption_request": consumption_request,
            "consumption_response": consumption_response,
        },
    )

    assert result["passed"] is True
    assert result["suggestion_same_as_original"] is True
    assert result["attempt_record_same_as_original"] is True
    assert result["consumption_same_as_original"] is True
    assert result["resources_unchanged"] is True
    assert result["campaign_revision_and_budget_unchanged"] is True
    assert calls == [(5, "original-suggestion-key")]


def test_worker_uses_bounded_spools_and_clears_import_overrides(monkeypatch, tmp_path):
    child_response = {
        "profile": sequences._WORKER_PROFILE,
        "status": "returned",
        "slots": [],
    }
    observed = {}

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, *, stdin, stdout, stderr, env, **kwargs):
            observed["command"] = command
            observed["environment"] = dict(env)
            observed["popen_kwargs"] = kwargs
            request = json.loads(stdin.read().decode("utf-8"))
            observed["request"] = request
            stdout.write(sequences.canonical(child_response).encode("utf-8"))
            stderr.write(b"warning " * 200)
            stdout.flush()
            stderr.flush()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(sequences.subprocess, "Popen", FakeProcess)
    monkeypatch.setenv("PYTHONPATH", "/tmp/untrusted-python-path")
    monkeypatch.setenv("PYTHONHOME", "/tmp/untrusted-python-home")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / sequences._PRECALL_MARKER).write_text("precalls-sealed", encoding="utf-8")

    result = sequences._invoke_worker(
        stage=stage,
        data_path=tmp_path / "data",
        declaration_path=tmp_path / "candidate-declaration.json",
        declaration_sha256="a" * 64,
        case={"case_id": "case-a"},
        campaign_id="campaign-a",
        protocol_id="protocol-a",
        ordinals=(0,),
        prior_slots=[],
        manifest={
            "host": {"module_sha256": {"measure_service.campaigns": "b" * 64}},
            "bo_sidecar": {"bo_engine_source_tree_sha256": "c" * 64},
        },
    )

    assert "PYTHONPATH" not in observed["environment"]
    assert "PYTHONHOME" not in observed["environment"]
    assert observed["environment"]["PYTHONNOUSERSITE"] == "1"
    assert observed["request"]["case_id"] == "case-a"
    assert result["status"] == "returned"
    assert result["response"] == child_response
    assert result["stderr_bytes"] == len(b"warning " * 200)
    assert result["stderr_truncated"] is True
    assert len(result["stderr_excerpt"]) == 1000


def test_worker_output_limit_fails_without_parsing_unbounded_spool(monkeypatch, tmp_path):
    class OversizeProcess:
        pid = 12346
        returncode = 0

        def __init__(self, command, *, stdout, **kwargs):
            self.command = command
            stdout.write(b"x" * 4096)
            stdout.flush()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(sequences.subprocess, "Popen", OversizeProcess)
    monkeypatch.setattr(
        sequences,
        "_terminate_worker_process",
        lambda process, *, process_group_id: process.kill(),
    )
    monkeypatch.setattr(sequences, "_MAX_WORKER_OUTPUT_BYTES", 2048)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / sequences._PRECALL_MARKER).write_text("pre", encoding="utf-8")

    result = sequences._invoke_worker(
        stage=stage,
        data_path=tmp_path / "data",
        declaration_path=tmp_path / "declaration.json",
        declaration_sha256="a" * 64,
        case={"case_id": "case-a"},
        campaign_id="campaign-a",
        protocol_id="protocol-a",
        ordinals=(0,),
        prior_slots=[],
        manifest={
            "host": {"module_sha256": {}},
            "bo_sidecar": {"bo_engine_source_tree_sha256": "c" * 64},
        },
    )

    assert result["status"] == "output_limit_exceeded"
    assert result["output_limit_exceeded"] is True
    assert result["response"] is None
    assert result["stdout_bytes"] == 4096


def test_worker_interrupt_kills_term_ignoring_descendant_and_preserves_journal_prefix(
    monkeypatch, tmp_path
):
    original_popen = sequences.subprocess.Popen
    original_sleep = sequences.time.sleep
    ready = tmp_path / "descendant-ready.txt"
    sentinel = tmp_path / "late-child-write.txt"
    descendant_program = (
        "import os,pathlib,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()));"
        "time.sleep(1.0);"
        f"pathlib.Path({str(sentinel)!r}).write_text('late')"
    )
    child_program = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{descendant_program!r}]);"
        "time.sleep(30)"
    )
    children = []
    popen_options = []

    def launch_harmless_child(command, **kwargs):
        popen_options.append(kwargs.get("start_new_session"))
        child = original_popen(
            [sequences.sys.executable, "-c", child_program],
            **kwargs,
        )
        children.append(child)
        return child

    def interrupt_parent(_seconds):
        deadline = sequences.time.monotonic() + 3.0
        while not ready.exists() and sequences.time.monotonic() < deadline:
            original_sleep(0.01)
        if not ready.exists():
            raise AssertionError("the SIGTERM-ignoring descendant did not become ready")
        raise KeyboardInterrupt

    monkeypatch.setattr(sequences.subprocess, "Popen", launch_harmless_child)
    monkeypatch.setattr(sequences.time, "sleep", interrupt_parent)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / sequences._PRECALL_MARKER).write_text("precalls-sealed", encoding="utf-8")
    journal = stage / "durable_worker_journal.jsonl"
    prefix = {
        "profile": "measure-service-suggestion-sequence-worker-slot/1",
        "case_id": "case-a",
        "ordinal": 0,
        "slot": {"ordinal": 0, "status": "consumed"},
    }
    sequences._append_worker_journal(journal, prefix)
    slot = {"ordinal": 1, "status": "not_attempted_unknown"}

    with pytest.raises(KeyboardInterrupt):
        sequences._invoke_worker(
            stage=stage,
            data_path=tmp_path / "data",
            declaration_path=tmp_path / "candidate-declaration.json",
            declaration_sha256="a" * 64,
            case={"case_id": "case-a"},
            campaign_id="campaign-a",
            protocol_id="protocol-a",
            ordinals=(1,),
            prior_slots=[prefix["slot"]],
            manifest={
                "host": {"module_sha256": {"measure_service.campaigns": "b" * 64}},
                "bo_sidecar": {"bo_engine_source_tree_sha256": "c" * 64},
            },
        )

    original_sleep(1.2)
    records = sequences._journal_records(journal, "case-a")

    assert popen_options == [True]
    assert len(children) == 1 and children[0].poll() is not None
    assert sentinel.exists() is False
    assert records == [prefix]
    assert records.tail_error is None
    assert slot["status"] == "not_attempted_unknown"


def test_journal_malformed_tail_retains_validated_fsynced_prefix(tmp_path):
    journal = tmp_path / "worker.jsonl"
    prefix = {
        "profile": "measure-service-suggestion-sequence-worker-slot/1",
        "case_id": "case-a",
        "ordinal": 0,
        "slot": {"ordinal": 0, "status": "consumed"},
    }
    sequences._append_worker_journal(journal, prefix)
    with journal.open("ab") as destination:
        destination.write(b'{"profile":"broken"')
        destination.flush()
        sequences.os.fsync(destination.fileno())

    records = sequences._journal_records(journal, "case-a")

    assert records == [prefix]
    assert records.tail_error["profile"] == (
        "measure-service-suggestion-sequence-journal-tail-error/1"
    )
    assert records.tail_error["line_number"] == 2
    assert records.tail_error["size_bytes"] == len(b'{"profile":"broken"')
    assert records.tail_error["terminated_by_newline"] is False
    assert records.tail_error["sha256"] == hashlib.sha256(b'{"profile":"broken"').hexdigest()
    assert records.tail_error["error"]["type"] == "ValueError"
    case = {}
    worker_record = {}
    sequences._retain_journal_tail_error(case, records, worker_record)
    assert case["worker_journal_tail_error"] == records.tail_error
    assert worker_record["journal_tail_error"] == records.tail_error


def test_missing_journal_returns_explicit_empty_evidence_without_attribute_error(tmp_path):
    records = sequences._journal_records(tmp_path / "missing-worker.jsonl", "case-a")
    case = {"slots": [{"ordinal": 0, "status": "not_attempted_unknown"}]}
    worker_record = {}

    sequences._retain_journal_tail_error(case, records, worker_record)

    assert records == []
    assert records.missing is True
    assert records.tail_error is None
    assert case["slots"][0]["status"] == "not_attempted_unknown"
    assert case["worker_journal_error"]["profile"] == (
        "measure-service-suggestion-sequence-journal-missing/1"
    )
    assert worker_record["journal_error"] == case["worker_journal_error"]


def test_database_reconciliation_counts_lost_ack_spend_without_changing_status(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE campaign_proposals (id TEXT, ordinal INTEGER, payload TEXT, owner TEXT, campaign_id TEXT);
        CREATE TABLE campaign_consumptions (
          id TEXT, owner TEXT, campaign_id TEXT, proposal_id TEXT, attempt_id TEXT,
          attempt_revision INTEGER, observation_id TEXT
        );
        CREATE TABLE experiments (id TEXT, payload TEXT, owner TEXT, created_at TEXT);
        CREATE TABLE experiment_attempts (
          id TEXT, latest_revision INTEGER, owner TEXT, experiment_id TEXT, created_at TEXT
        );
        CREATE TABLE registry_receipts (owner TEXT, request_key TEXT, response TEXT);
        """
    )
    proposal_id = "proposal-ordinal-2"
    experiment_id = "experiment-ordinal-2"
    attempt_id = "attempt-ordinal-2"
    observation_id = "observation-ordinal-2"
    connection.execute(
        "INSERT INTO campaign_proposals VALUES (?, ?, ?, ?, ?)",
        (
            proposal_id,
            2,
            sequences.canonical({"proposal_id": proposal_id}),
            sequences.OWNER,
            "campaign-1",
        ),
    )
    connection.execute(
        "INSERT INTO campaign_consumptions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "consumption-ordinal-2",
            sequences.OWNER,
            "campaign-1",
            proposal_id,
            attempt_id,
            1,
            observation_id,
        ),
    )
    connection.execute(
        "INSERT INTO experiments VALUES (?, ?, ?, ?)",
        (
            experiment_id,
            sequences.canonical({"proposal_reference": proposal_id}),
            sequences.OWNER,
            "2026-11-01T00:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO experiment_attempts VALUES (?, ?, ?, ?, ?)",
        (attempt_id, 1, sequences.OWNER, experiment_id, "2026-11-01T00:00:01+00:00"),
    )

    @contextmanager
    def registry_connection():
        yield connection

    proposal = {
        "proposal_id": proposal_id,
        "candidate": {"strong_solvent_fraction": 0.5, "acquisition_duration_min": 5.0},
        "method": {"method_id": "method-2"},
        "repetition": {"is_repeat": False, "matching_prior_proposals": []},
    }
    attempt = {
        "attempt_id": attempt_id,
        "experiment_id": experiment_id,
        "revision": 1,
        "spec": {"status": "completed", "evaluator_kind": "simulator", "fidelity": 1.0},
    }
    consumption = {
        "consumption_id": "consumption-ordinal-2",
        "proposal_id": proposal_id,
        "attempt_id": attempt_id,
        "snapshot_id": "snapshot-2",
        "observation_id": observation_id,
        "result": {"evaluation_cost": 1.0},
    }
    monkeypatch.setattr(sequences, "registry_connection", registry_connection)
    monkeypatch.setattr(sequences.campaigns, "get_campaign_proposal", lambda owner, item: proposal)
    monkeypatch.setattr(
        sequences.campaigns, "get_campaign_consumption", lambda owner, item: consumption
    )
    monkeypatch.setattr(
        sequences.campaigns,
        "list_campaign_suggestion_attempts",
        lambda owner, campaign_id, limit: {"records": [{"ordinal": 2}]},
    )
    monkeypatch.setattr(
        sequences.experiments, "get_experiment", lambda owner, item: {"experiment_id": item}
    )
    monkeypatch.setattr(sequences.experiments, "get_attempt", lambda owner, item: attempt)
    monkeypatch.setattr(
        sequences.observations,
        "get_observation_snapshot",
        lambda owner, item: {"snapshot_id": item, "members": []},
    )

    case = {
        "case_id": "task-0:default:replicate",
        "duplicate_policy": "replicate",
        "surrogate_noise_prior": "default",
        "evaluation_budget": sequences.EVALUATION_BUDGET,
        "campaign_id": "campaign-1",
        "slots": [
            {
                "ordinal": ordinal,
                "status": "interrupted_unknown",
                "proposal_available": False,
                "step_timings": [],
                "suggestion_events": [],
            }
            for ordinal in range(sequences.EVALUATION_BUDGET)
        ],
        "worker_processes": [],
        "restart_checkpoints": [],
    }

    reconciliation = sequences._reconcile_durable_case_from_store(case, "a" * 64)
    summary = sequences._durable_summary([case])

    assert reconciliation["status"] == "reconciled"
    assert reconciliation["consumed_ordinals"] == [2]
    assert reconciliation["completed_simulator_attempts"][0]["attempt_id"] == attempt_id
    assert case["slots"][2]["status"] == "interrupted_unknown"
    assert case["slots"][2]["consumption"]["consumption_id"] == "consumption-ordinal-2"
    assert summary["consumed_evaluations"] == 1
    assert summary["consumed_evaluations_by_status"] == 0
    assert summary["completed_simulator_attempt_count"] == 1
    assert summary["attempted_suggestion_slots"] == 1
    assert summary["known_consumed_evaluation_cost_total"] == 1.0
    assert summary["unknown_consumed_evaluation_cost_count"] == 0


def test_incomplete_accounting_keeps_unknown_costs_unknown():
    case = {
        "case_id": "task-0:default:replicate",
        "duplicate_policy": "replicate",
        "surrogate_noise_prior": "default",
        "evaluation_budget": sequences.EVALUATION_BUDGET,
        "status": "partial_failure",
        "slots": [
            {
                "ordinal": 0,
                "status": "interrupted_unknown",
                "consumption": {"consumption_id": "known-id", "result": {}},
                "suggestion_events": [],
            },
            *[
                {"ordinal": ordinal, "status": "not_attempted_unknown", "suggestion_events": []}
                for ordinal in range(1, sequences.EVALUATION_BUDGET)
            ],
        ],
        "restart_checkpoints": [],
        "store_reconciliation": {"status": "unavailable"},
    }

    summary = sequences._durable_summary([case])

    assert summary["consumed_evaluations"] == 1
    assert summary["known_consumed_evaluation_cost_total"] is None
    assert summary["known_consumed_evaluation_cost_count"] == 0
    assert summary["unknown_consumed_evaluation_cost_count"] == 1
