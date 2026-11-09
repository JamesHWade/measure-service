"""Durable, owner-scoped tests for immutable experiment-design lineages."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from measure_service import design_lineage, experiments
from measure_service.storage import canonical, connect, db_path, sha256

LINEAGE_TABLES = {
    "design_lineage_schema",
    "design_lineage_roots",
    "design_lineage_versions",
}


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))


def _spec(title: str = "Root design", *, condition_value=1, **changes):
    values = {
        "title": title,
        "source": "caller-declared",
        "intended_conditions": [
            experiments.Condition(
                name="flow",
                value=condition_value,
                unit=None if isinstance(condition_value, bool) else "mL/min",
            )
        ],
    }
    values.update(changes)
    return experiments.ExperimentSpec(**values)


def _create(owner: str = "chemist", key: str = "root", **changes):
    return experiments.create_experiment(owner, _spec(**changes), key)


def _supersede(parent, spec, *, owner="chemist", key="replace", reason="Changed design"):
    return design_lineage.supersede_experiment(
        owner,
        parent["experiment_id"],
        parent["input_sha256"],
        spec,
        reason,
        key,
    )


def _attempt(owner: str, experiment_id: str, key: str):
    return experiments.record_attempt(
        owner,
        experiment_id,
        experiments.AttemptSpec(status="completed", evaluator_kind="manual"),
        key,
    )


def _result(owner: str, attempt_id: str, key: str, value: float = 1.25):
    recorded = experiments.record_results(
        owner,
        attempt_id,
        1,
        [
            experiments.ResultSpec(
                metric="response",
                status="observed",
                unit="1",
                value=value,
            )
        ],
        key,
    )
    return recorded["results"][0]


def _lineage(owner: str, experiment_id: str, offset: int = 0, limit: int = 10):
    return design_lineage.get_experiment_design_lineage(owner, experiment_id, offset, limit)


def _objects():
    with connect() as con:
        return {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }


def _counts():
    with connect() as con:
        return {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "experiments",
                "experiment_attempts",
                "experiment_results",
                "registry_receipts",
            )
        }


def _entry_payload(entry):
    return {key: value for key, value in entry.items() if key != "entry_sha256"}


def _requested(spec):
    payload = spec.model_dump(mode="json")
    for field in ("executable_method_id", "observation_contract_id", "observation_producer_id"):
        if payload.get(field) is None:
            payload.pop(field, None)
    return payload


def _assert_entry(entry, *, root, revision, experiment, previous=None, reason=None):
    assert set(entry) == {
        "profile",
        "schema_version",
        "root_experiment_id",
        "revision",
        "experiment_id",
        "experiment_input_sha256",
        "supersedes_experiment_id",
        "supersedes_entry_sha256",
        "reason",
        "created_at",
        "entry_sha256",
    }
    assert entry["profile"] == "measure-experiment-design-version/1"
    assert entry["schema_version"] == "1.0"
    assert entry["root_experiment_id"] == root["experiment_id"]
    assert entry["revision"] == revision
    assert entry["experiment_id"] == experiment["experiment_id"]
    assert entry["experiment_input_sha256"] == experiment["input_sha256"]
    assert entry["created_at"] == experiment["created_at"]
    assert entry["reason"] == ("Initial immutable experiment" if reason is None else reason)
    assert entry["supersedes_experiment_id"] == (
        None if previous is None else previous["experiment_id"]
    )
    assert entry["supersedes_entry_sha256"] == (
        None if previous is None else previous["entry_sha256"]
    )
    assert entry["entry_sha256"] == sha256(_entry_payload(entry))


def test_legacy_root_read_is_read_only_then_versions_are_explicit_and_exact():
    root = _create()
    assert not (LINEAGE_TABLES & _objects())

    virtual = _lineage("chemist", root["experiment_id"])
    assert not (LINEAGE_TABLES & _objects())
    assert virtual["profile"] == "measure-experiment-design-lineage/1"
    assert virtual["schema_version"] == "1.0"
    assert virtual["root_experiment_id"] == root["experiment_id"]
    assert virtual["selected_experiment_id"] == root["experiment_id"]
    assert virtual["selected_revision"] == 1
    assert virtual["current_experiment_id"] == root["experiment_id"]
    assert virtual["current_revision"] == 1
    assert virtual["count"] == 1
    assert virtual["offset"] == 0
    assert virtual["limit"] == 10
    assert virtual["next_offset"] is None
    _assert_entry(virtual["entries"][0], root=root, revision=1, experiment=root)

    child = _spec("Replacement", condition_value=2)
    receipt = _supersede(root, child, key="first-replacement")
    assert set(receipt) == {"profile", "schema_version", "entry"}
    assert receipt["profile"] == "measure-experiment-design-supersession/1"
    assert receipt["schema_version"] == "1.0"
    persisted = experiments.get_experiment("chemist", receipt["entry"]["experiment_id"])
    lineage = _lineage("chemist", persisted["experiment_id"])
    assert lineage["count"] == 2
    assert lineage["root_experiment_id"] == root["experiment_id"]
    assert lineage["selected_revision"] == 2
    assert lineage["current_revision"] == 2
    assert lineage["current_experiment_id"] == persisted["experiment_id"]
    assert lineage["current_entry_sha256"] == receipt["entry"]["entry_sha256"]
    _assert_entry(lineage["entries"][0], root=root, revision=1, experiment=root)
    _assert_entry(
        lineage["entries"][1],
        root=root,
        revision=2,
        experiment=persisted,
        previous=lineage["entries"][0],
        reason="Changed design",
    )
    root_lineage = _lineage("chemist", root["experiment_id"])
    assert root_lineage["entries"] == lineage["entries"]
    assert root_lineage["current_experiment_id"] == lineage["current_experiment_id"]
    assert root_lineage["current_entry_sha256"] == lineage["current_entry_sha256"]
    assert root_lineage["selected_experiment_id"] == root["experiment_id"]
    assert root_lineage["selected_revision"] == 1
    assert LINEAGE_TABLES <= _objects()


def test_attempts_and_results_keep_old_design_identity_and_late_writes_allowed():
    root = _create()
    attempt = _attempt("chemist", root["experiment_id"], "old-attempt")
    result = _result("chemist", attempt["attempt_id"], "old-result")
    attempt_before = experiments.get_attempt("chemist", attempt["attempt_id"])
    result_before = experiments.get_result("chemist", result["result_id"])
    with connect() as con:
        raw_before = (
            con.execute(
                "SELECT * FROM experiment_attempts WHERE id=?", (attempt["attempt_id"],)
            ).fetchone(),
            con.execute(
                "SELECT * FROM attempt_revisions WHERE attempt_id=?", (attempt["attempt_id"],)
            ).fetchall(),
            con.execute(
                "SELECT * FROM experiment_results WHERE id=?", (result["result_id"],)
            ).fetchone(),
        )

    child_receipt = _supersede(root, _spec("Replacement", condition_value=2), key="replace")
    child_id = child_receipt["entry"]["experiment_id"]
    assert experiments.get_attempt("chemist", attempt["attempt_id"]) == attempt_before
    assert experiments.get_result("chemist", result["result_id"]) == result_before
    with connect() as con:
        raw_after = (
            con.execute(
                "SELECT * FROM experiment_attempts WHERE id=?", (attempt["attempt_id"],)
            ).fetchone(),
            con.execute(
                "SELECT * FROM attempt_revisions WHERE attempt_id=?", (attempt["attempt_id"],)
            ).fetchall(),
            con.execute(
                "SELECT * FROM experiment_results WHERE id=?", (result["result_id"],)
            ).fetchone(),
        )
    assert raw_after == raw_before

    late_attempt = _attempt("chemist", root["experiment_id"], "late-attempt")
    late_result = _result("chemist", late_attempt["attempt_id"], "late-result", value=2.5)
    assert late_attempt["experiment_id"] == root["experiment_id"]
    assert late_result["attempt_id"] == late_attempt["attempt_id"]
    assert child_id != root["experiment_id"]
    assert _lineage("chemist", root["experiment_id"])["current_experiment_id"] == child_id


def test_retry_changed_key_cas_cross_owner_and_noop_semantics():
    root = _create()
    replacement = _spec("Replacement", condition_value=2)
    first = _supersede(root, replacement, key="same-request")
    assert _supersede(root, replacement, key="same-request") == first
    counts = _counts()

    with pytest.raises(ValueError, match="different registry request"):
        _supersede(root, _spec("Different", condition_value=3), key="same-request")
    assert _counts() == counts

    with pytest.raises(ValueError, match="current design"):
        _supersede(root, _spec("Stale", condition_value=3), key="stale-parent")
    with pytest.raises(ValueError, match="digest conflict"):
        design_lineage.supersede_experiment(
            "chemist",
            first["entry"]["experiment_id"],
            "0" * 64,
            _spec("Stale digest", condition_value=4),
            "bad digest",
            "bad-digest",
        )
    with pytest.raises(ValueError, match="must change"):
        _supersede(
            experiments.get_experiment("chemist", first["entry"]["experiment_id"]),
            _spec("Replacement", condition_value=2),
            key="no-op",
        )

    with pytest.raises(ValueError, match="not found or is not accessible"):
        _lineage("other-owner", root["experiment_id"])
    with pytest.raises(ValueError, match="not found or is not accessible"):
        design_lineage.supersede_experiment(
            "other-owner",
            root["experiment_id"],
            root["input_sha256"],
            _spec("Foreign", condition_value=5),
            "foreign attempt",
            "foreign-write",
        )
    assert _counts() == counts


def test_strict_typed_changes_preserve_int_float_and_bool_distinctions():
    root = _create(condition_value=1)
    as_float = _supersede(root, _spec(condition_value=1.0), key="float-change")
    float_record = experiments.get_experiment("chemist", as_float["entry"]["experiment_id"])
    assert float_record["spec"]["intended_conditions"][0]["value"] == 1.0
    assert type(float_record["spec"]["intended_conditions"][0]["value"]) is float

    as_bool = _supersede(
        float_record,
        _spec(condition_value=True),
        key="bool-change",
    )
    bool_record = experiments.get_experiment("chemist", as_bool["entry"]["experiment_id"])
    assert bool_record["spec"]["intended_conditions"][0]["value"] is True
    assert _lineage("chemist", bool_record["experiment_id"])["count"] == 3

    with pytest.raises(ValueError, match="must change"):
        _supersede(
            bool_record,
            _spec(condition_value=True),
            key="bool-no-op",
        )


def test_invalid_links_reason_ids_and_root_initialization_roll_back():
    root = _create()
    before_counts = _counts()
    before_objects = _objects()
    invalid_specs = (
        {"executable_method_id": "missing-method"},
        {"observation_contract_id": "missing-contract"},
        {
            "observation_contract_id": "missing-contract",
            "observation_producer_id": "missing-producer",
        },
    )
    for index, changes in enumerate(invalid_specs):
        with pytest.raises(ValueError, match="not found|requires"):
            _supersede(
                root,
                _spec(f"Invalid link {index}", condition_value=index + 2, **changes),
                key=f"bad-link-{index}",
            )
        assert _counts() == before_counts
        assert _objects() == before_objects

    for index, reason in enumerate(("   ", "\x00bad", "é" * 1001, "x" * 2001, "\ud800")):
        with pytest.raises((ValueError, UnicodeEncodeError)):
            _supersede(
                root,
                _spec(f"Invalid reason {index}", condition_value=index + 10),
                key=f"bad-reason-{index}",
                reason=reason,
            )
        assert _counts() == before_counts

    accepted = _supersede(
        root,
        _spec("Unicode reason", condition_value=99),
        key="unicode-reason",
        reason="é" * 1000,
    )
    assert accepted["entry"]["reason"] == "é" * 1000
    assert len(canonical(accepted["entry"]).encode("utf-8")) <= 3000


def test_concurrent_contenders_have_one_winner_and_retry_survives_restart():
    root = _create()

    def contender(index):
        spec = _spec(f"Contender {index}", condition_value=index + 20)
        try:
            return (
                "ok",
                index,
                spec,
                _supersede(root, spec, key=f"contender-{index}", reason=f"candidate {index}"),
            )
        except ValueError as error:
            return ("error", index, str(error))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(contender, (1, 2)))
    winners = [outcome for outcome in outcomes if outcome[0] == "ok"]
    losers = [outcome for outcome in outcomes if outcome[0] == "error"]
    assert len(winners) == 1
    assert len(losers) == 1
    assert "current design" in losers[0][2] or "head conflict" in losers[0][2]
    _, winner_index, winner_spec, winner_receipt = winners[0]
    winner = experiments.get_experiment("chemist", winner_receipt["entry"]["experiment_id"])
    assert winner["spec"]["title"] == winner_spec.title
    assert winner["spec"]["source"] == winner_spec.source
    assert (
        winner["spec"]["intended_conditions"]
        == winner_spec.model_dump(mode="json")["intended_conditions"]
    )
    assert _counts()["experiments"] == 2

    later = _supersede(
        winner,
        _spec("After contention", condition_value=30),
        key="after-contention",
    )
    assert later["entry"]["revision"] == 3
    # The original winner key is a durable request identity, not a pointer to
    # the current head; it must replay after a later version exists.
    replay = _supersede(
        root,
        winner_spec,
        key=f"contender-{winner_index}",
        reason=f"candidate {winner_index}",
    )
    assert replay == winner_receipt

    code = """
import json
from measure_service.design_lineage import get_experiment_design_lineage
import os
root = os.environ['ROOT_ID']
print(json.dumps(get_experiment_design_lineage('chemist', root), sort_keys=True))
"""
    environment = os.environ.copy()
    environment["ROOT_ID"] = root["experiment_id"]
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=Path(__file__).parents[1],
    )
    assert result.returncode == 0, result.stderr
    restarted = json.loads(result.stdout)
    current = _lineage("chemist", root["experiment_id"])
    assert restarted == current


def test_lineage_paging_strict_offsets_and_future_empty_reads():
    root = _create()
    current = root
    for revision in range(2, 5):
        receipt = _supersede(
            current,
            _spec(f"Version {revision}", condition_value=revision),
            key=f"version-{revision}",
        )
        current = experiments.get_experiment("chemist", receipt["entry"]["experiment_id"])

    first = _lineage("chemist", root["experiment_id"], limit=2)
    second = _lineage("chemist", root["experiment_id"], offset=2, limit=2)
    future = _lineage("chemist", root["experiment_id"], offset=999, limit=2)
    overflow = _lineage("chemist", root["experiment_id"], offset=10**100, limit=20)
    assert [entry["revision"] for entry in first["entries"]] == [1, 2]
    assert first["next_offset"] == 2
    assert [entry["revision"] for entry in second["entries"]] == [3, 4]
    assert second["next_offset"] is None
    assert future["entries"] == []
    assert future["next_offset"] is None
    assert future["count"] == 4
    assert overflow["entries"] == []
    assert overflow["next_offset"] is None
    assert first["current_experiment_id"] == current["experiment_id"]
    assert first["current_entry_sha256"] == second["current_entry_sha256"]

    for offset, limit in ((True, 2), (-1, 2), (0, False), (0, 0), (0, 21), (0, 1.0)):
        with pytest.raises(ValueError):
            _lineage("chemist", root["experiment_id"], offset=offset, limit=limit)

    for invalid_id in ("", " ", "\x00id", "\ud800", "x" * 101):
        with pytest.raises((ValueError, UnicodeEncodeError)):
            _lineage("chemist", invalid_id)


def test_maximum_fifty_versions_and_overflow_is_atomic():
    root = _create()
    current = root
    for revision in range(2, 51):
        receipt = _supersede(
            current,
            _spec(f"Version {revision}", condition_value=revision),
            key=f"max-version-{revision}",
        )
        current = experiments.get_experiment("chemist", receipt["entry"]["experiment_id"])
    lineage = _lineage("chemist", root["experiment_id"], limit=20)
    assert lineage["count"] == 50
    assert lineage["entries"][0]["revision"] == 1
    assert lineage["entries"][-1]["revision"] == 20
    before = _counts()
    with pytest.raises(ValueError, match="50 versions"):
        _supersede(
            current,
            _spec("Version 51", condition_value=51),
            key="over-version-limit",
        )
    assert _counts() == before
    assert _lineage("chemist", current["experiment_id"], offset=40, limit=20)["count"] == 50


def test_successful_replay_is_bound_to_historical_entry_after_later_versions():
    root = _create()
    original_spec = _spec("Replayable", condition_value=2)
    original = _supersede(root, original_spec, key="replayable", reason="original reason")
    first = experiments.get_experiment("chemist", original["entry"]["experiment_id"])
    later = _supersede(
        first,
        _spec("Later", condition_value=3),
        key="later-version",
        reason="later reason",
    )
    assert later["entry"]["revision"] == 3
    assert (
        _supersede(
            root,
            original_spec,
            key="replayable",
            reason="original reason",
        )
        == original
    )

    code = (
        "import json, os; "
        "from measure_service import design_lineage, experiments; "
        "payload=json.loads(os.environ['SPEC']); "
        "spec=experiments.ExperimentSpec.model_validate(payload); "
        "receipt=design_lineage.supersede_experiment('chemist', os.environ['ROOT'], "
        "os.environ['DIGEST'], spec, os.environ['REASON'], os.environ['KEY']); "
        "print(json.dumps(receipt, sort_keys=True))"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "ROOT": root["experiment_id"],
            "DIGEST": root["input_sha256"],
            "REASON": "original reason",
            "KEY": "replayable",
            "SPEC": canonical(original_spec.model_dump(mode="json")),
        }
    )
    restarted = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        cwd=Path(__file__).parents[1],
    )
    assert json.loads(restarted.stdout) == original


def test_tampered_retry_receipts_fail_closed_without_registry_writes():
    root = _create()
    original_spec = _spec("Receipt", condition_value=2)
    original = _supersede(root, original_spec, key="receipt-key", reason="saved reason")
    current = _supersede(
        experiments.get_experiment("chemist", original["entry"]["experiment_id"]),
        _spec("Current", condition_value=3),
        key="current-key",
    )

    def tamper(response_mutator):
        with connect() as con:
            raw = con.execute(
                "SELECT response FROM registry_receipts WHERE owner=? AND request_key=?",
                ("chemist", "receipt-key"),
            ).fetchone()[0]
            response = json.loads(raw)
            response_mutator(response)
            con.execute(
                "UPDATE registry_receipts SET response=? WHERE owner=? AND request_key=?",
                (canonical(response), "chemist", "receipt-key"),
            )
            con.commit()

    def restore():
        with connect() as con:
            con.execute(
                "UPDATE registry_receipts SET response=? WHERE owner=? AND request_key=?",
                (canonical(original), "chemist", "receipt-key"),
            )
            con.commit()

    for mutate in (
        lambda response: response["entry"].__setitem__("reason", "tampered reason"),
        lambda response: response["entry"].__setitem__("entry_sha256", "0" * 64),
        lambda response: response["entry"].__setitem__(
            "experiment_id", current["entry"]["experiment_id"]
        ),
    ):
        tamper(mutate)
        before = db_path().read_bytes()
        with pytest.raises(ValueError, match="inconsistent|historical|binding"):
            _supersede(
                root,
                original_spec,
                key="receipt-key",
                reason="saved reason",
            )
        assert db_path().read_bytes() == before
        restore()

    with connect() as con:
        before_raw = con.execute(
            "SELECT response FROM registry_receipts WHERE owner=? AND request_key=?",
            ("chemist", "receipt-key"),
        ).fetchone()[0]
        con.execute(
            "UPDATE registry_receipts SET response=? WHERE owner=? AND request_key=?",
            ("x" * 64_001, "chemist", "receipt-key"),
        )
        con.commit()
    before = db_path().read_bytes()
    with pytest.raises(ValueError, match="receipt"):
        _supersede(root, original_spec, key="receipt-key", reason="saved reason")
    assert db_path().read_bytes() == before
    with connect() as con:
        con.execute(
            "UPDATE registry_receipts SET response=? WHERE owner=? AND request_key=?",
            (before_raw, "chemist", "receipt-key"),
        )
        con.commit()

    with connect() as con:
        con.execute(
            "UPDATE registry_receipts SET response=? WHERE owner=? AND request_key=?",
            (canonical(original), "chemist", "receipt-key"),
        )
        con.execute(
            "UPDATE registry_receipts SET digest=? WHERE owner=? AND request_key=?",
            ("0" * 64, "chemist", "receipt-key"),
        )
        con.commit()
    before = db_path().read_bytes()
    with pytest.raises(ValueError, match="different registry request"):
        _supersede(root, original_spec, key="receipt-key", reason="saved reason")
    assert db_path().read_bytes() == before

    with connect() as con:
        con.execute(
            "UPDATE registry_receipts SET digest=? WHERE owner=? AND request_key=?",
            (
                sha256(
                    {
                        "operation": "supersede_experiment",
                        "experiment_id": root["experiment_id"],
                        "expected_input_sha256": root["input_sha256"],
                        "spec": _requested(original_spec),
                        "reason": "saved reason",
                    }
                ),
                "chemist",
                "receipt-key",
            ),
        )
        con.execute(
            "UPDATE registry_receipts SET response=? WHERE owner=? AND request_key=?",
            ("[" * 1000, "chemist", "receipt-key"),
        )
        con.commit()
    before = db_path().read_bytes()
    with pytest.raises(ValueError, match="receipt"):
        _supersede(root, original_spec, key="receipt-key", reason="saved reason")
    assert db_path().read_bytes() == before


@pytest.mark.parametrize(
    "tamper",
    ("raw-experiment", "lineage-payload", "gap", "mixed-owner", "deleted-tail"),
)
def test_tampered_lineage_or_raw_experiment_is_rejected_without_read_writes(tamper):
    root = _create()
    second_receipt = _supersede(root, _spec("Second", condition_value=2), key="second")
    second = experiments.get_experiment("chemist", second_receipt["entry"]["experiment_id"])
    third_receipt = _supersede(second, _spec("Third", condition_value=3), key="third")
    third_id = third_receipt["entry"]["experiment_id"]

    with connect() as con:
        if tamper == "raw-experiment":
            row = con.execute(
                "SELECT payload FROM experiments WHERE id=?", (root["experiment_id"],)
            ).fetchone()
            payload = json.loads(row[0])
            payload["title"] = "tampered raw title"
            con.execute(
                "UPDATE experiments SET payload=? WHERE id=?",
                (canonical(payload), root["experiment_id"]),
            )
        elif tamper == "lineage-payload":
            row = con.execute(
                "SELECT payload FROM design_lineage_versions WHERE experiment_id=?",
                (second["experiment_id"],),
            ).fetchone()
            payload = json.loads(row[0])
            payload["reason"] = "tampered lineage reason"
            con.execute(
                "UPDATE design_lineage_versions SET payload=? WHERE experiment_id=?",
                (canonical(payload), second["experiment_id"]),
            )
        elif tamper == "gap":
            con.execute(
                "UPDATE design_lineage_versions SET revision=4 WHERE experiment_id=?",
                (third_id,),
            )
        elif tamper == "mixed-owner":
            con.execute(
                "UPDATE design_lineage_versions SET owner='other-owner' WHERE experiment_id=?",
                (second["experiment_id"],),
            )
        else:
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute("DELETE FROM design_lineage_versions WHERE experiment_id=?", (third_id,))
        con.commit()

    before = db_path().read_bytes()
    with pytest.raises(ValueError, match="inconsistent|digest|contiguous|owner|anchor|payload"):
        _lineage("chemist", root["experiment_id"])
    assert db_path().read_bytes() == before
