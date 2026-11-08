"""Atomic table ingestion, immutable source lineage and historical corrections."""

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from measure_service import experiments, result_tables, server
from measure_service.result_table_models import ResultTableMapping, ResultTableSpec
from measure_service.storage import canonical, connect, db_path, sha256


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEASURE_MCP_R", "/missing-r-table-import")
    return tmp_path


def _attempt(owner="a", key="one", **changes):
    design = experiments.create_experiment(
        owner, experiments.ExperimentSpec(title=key, source="manual"), f"design-{key}"
    )
    return experiments.record_attempt(
        owner,
        design["experiment_id"],
        experiments.AttemptSpec(status="completed", **changes),
        f"attempt-{key}",
    )


def _spec(*rows, constants=None, columns=None, source_name="reported.csv"):
    return ResultTableSpec(
        source_name=source_name,
        csv_text="attempt,revision,metric,status,unit,value,explanation\r\n"
        + "\r\n".join(rows)
        + "\r\n",
        mapping=ResultTableMapping(
            columns=columns
            or {
                "attempt_id": "attempt",
                "expected_attempt_revision": "revision",
                "metric": "metric",
                "status": "status",
                "unit": "unit",
                "value": "value",
                "explanation": "explanation",
            },
            constants=constants or {},
        ),
    )


def _counts():
    with connect() as con:
        tables = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return {
            name: con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in (
                "experiment_results",
                "registry_receipts",
                "result_table_imports",
                "result_table_rows",
                "analytical_observations",
                "observation_snapshots",
            )
            if name in tables
        }


def test_multi_attempt_import_retains_raw_rows_zeros_and_missing_states(store):
    first, second = _attempt(key="first"), _attempt(key="second")
    text = _spec(
        f"{first['attempt_id']},1,area,observed,mAU*min,0,",
        f'{second["attempt_id"]},1,area,missing,mAU*min,,"No vial, reported\nby operator"',
    )
    receipt = result_tables.import_result_table("a", text, "import")
    assert receipt == result_tables.get_result_table_import("a", receipt["import_id"])
    assert receipt["row_count"] == receipt["attempt_count"] == 2
    assert receipt["status_counts"] == {"observed": 1, "missing": 1}
    page = result_tables.read_result_table_rows("a", receipt["import_id"])
    assert [row["ordinal"] for row in page["rows"]] == [0, 1]
    assert (page["rows"][1]["line_start"], page["rows"][1]["line_end"]) == (3, 4)
    outcomes = [experiments.get_result("a", row["result_id"]) for row in page["rows"]]
    assert outcomes[0]["outcome"]["value"] == 0
    assert outcomes[1]["outcome"]["value"] is None
    for result, row in zip(outcomes, page["rows"]):
        provenance = result["outcome"]["provenance"]
        assert provenance["value_origin"] == "caller_reported"
        assert provenance["table_source"]["ordinal"] == row["ordinal"]
        assert (
            provenance["table_source"]["source_sha256"]
            == hashlib.sha256(text.csv_text.encode()).hexdigest()
        )
        assert result["input_sha256"] == row["result_sha256"]
    source = result_tables.read_result_table_source("a", receipt["import_id"])
    assert base64.b64decode(source["content_base64"]) == text.csv_text.encode()
    counts = _counts()
    assert counts["analytical_observations"] == counts["observation_snapshots"] == 0


def test_last_row_foreign_owner_rolls_back_every_table_and_receipt(store):
    first, other = _attempt(), _attempt("b", "other")
    before = _counts()
    bad = _spec(
        f"{first['attempt_id']},1,area,observed,1,2,",
        f"{other['attempt_id']},1,area,observed,1,3,",
    )
    with pytest.raises(ValueError, match="accessible"):
        result_tables.import_result_table("a", bad, "all-or-none")
    assert _counts() == before  # Also proves newly created optional DDL rolled back.
    good = _spec(f"{first['attempt_id']},1,area,observed,1,2,")
    assert result_tables.import_result_table("a", good, "all-or-none")["row_count"] == 1


def test_stale_final_attempt_rolls_back_prior_result_and_existing_source_tables(store):
    first, second = _attempt(key="first"), _attempt(key="second")
    result_tables.import_result_table(
        "a", _spec(f"{first['attempt_id']},1,seed,observed,1,0,"), "seed"
    )
    experiments.revise_attempt(
        "a",
        second["attempt_id"],
        1,
        experiments.AttemptSpec(status="completed"),
        "Metadata",
        "revise",
    )
    before = _counts()
    bad = _spec(
        f"{first['attempt_id']},1,new,observed,1,2,",
        f"{second['attempt_id']},1,new,observed,1,3,",
    )
    with pytest.raises(ValueError, match="revision conflict"):
        result_tables.import_result_table("a", bad, "stale")
    assert _counts() == before


def test_historical_import_receipt_rows_and_source_survive_attempt_and_result_corrections(store):
    attempt = _attempt()
    original = _spec(f"{attempt['attempt_id']},1,area,observed,1,0,")
    receipt = result_tables.import_result_table("a", original, "original")
    rows = result_tables.read_result_table_rows("a", receipt["import_id"])
    source = result_tables.read_result_table_source("a", receipt["import_id"])
    previous = rows["rows"][0]["result_id"]
    experiments.revise_attempt(
        "a",
        attempt["attempt_id"],
        1,
        experiments.AttemptSpec(status="completed"),
        "Correct time",
        "revise",
    )
    correction = _spec(
        f"{attempt['attempt_id']},2,area,observed,1,3,Correct integration",
        constants={"supersedes_result_id": previous},
    )
    new = result_tables.import_result_table("a", correction, "correct")
    new_result = result_tables.read_result_table_rows("a", new["import_id"])["rows"][0]
    assert (
        experiments.get_result("a", previous)["superseded_by_result_id"] == new_result["result_id"]
    )
    assert result_tables.get_result_table_import("a", receipt["import_id"]) == receipt
    assert result_tables.import_result_table("a", original, "original") == receipt
    assert result_tables.read_result_table_rows("a", receipt["import_id"]) == rows
    assert result_tables.read_result_table_source("a", receipt["import_id"]) == source
    with pytest.raises(ValueError, match="different registry request"):
        result_tables.import_result_table(
            "a", original.model_copy(update={"source_name": "renamed"}), "original"
        )


def test_same_key_concurrency_has_one_import_and_one_set_of_results(store):
    attempt = _attempt()
    spec = _spec(f"{attempt['attempt_id']},1,area,observed,1,0,")
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: result_tables.import_result_table("a", spec, "same"), range(4))
        )
    assert all(result == responses[0] for result in responses)
    assert _counts()["result_table_imports"] == _counts()["experiment_results"] == 1


def test_competing_corrections_have_one_winner_and_no_losing_import(store):
    attempt = _attempt()
    receipt = result_tables.import_result_table(
        "a", _spec(f"{attempt['attempt_id']},1,area,observed,1,0,"), "initial"
    )
    previous = result_tables.read_result_table_rows("a", receipt["import_id"])["rows"][0][
        "result_id"
    ]

    def correct(index):
        spec = _spec(
            f"{attempt['attempt_id']},1,area,observed,1,{index},Correction",
            constants={"supersedes_result_id": previous},
        )
        try:
            return result_tables.import_result_table("a", spec, f"correction-{index}")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(correct, [1, 2]))
    assert sum(result is not None for result in results) == 1
    assert _counts()["result_table_imports"] == _counts()["experiment_results"] == 2


def test_analysis_derived_values_keep_their_origin_and_historical_link(store):
    dataset = server.register_dataset(
        "a",
        server.Dataset(
            title="synthetic",
            traces=[
                server.Trace(
                    trace_id="uv",
                    sample_id="s",
                    injection_id="i",
                    channel="uv",
                    separation="LC",
                    detector="UV",
                    axis_unit="min",
                    signal_unit="mAU",
                    axis=[0, 1, 2],
                    signal=[0, 2, 0],
                )
            ],
        ),
    )
    analysis = server.save_analysis(
        "a",
        dataset["dataset_id"],
        "uv",
        "test-summary",
        {},
        {"summary": {"area": 0}, "warnings": ["Caller test fixture"]},
        dataset["input_sha256"],
    )
    attempt = _attempt(
        measurements=[experiments.MeasurementLink(dataset_id=dataset["dataset_id"], trace_id="uv")]
    )
    spec = _spec(
        f"{attempt['attempt_id']},1,area,observed,mAU*min,,",
        constants={"analysis_id": analysis["analysis_id"], "summary_field": "area"},
    )
    receipt = result_tables.import_result_table("a", spec, "analysis")
    row = result_tables.read_result_table_rows("a", receipt["import_id"])["rows"][0]
    result = experiments.get_result("a", row["result_id"])["outcome"]
    assert result["value"] == 0
    assert result["provenance"]["value_origin"] == "stored_analysis_summary"
    assert result["provenance"]["analysis_warnings"] == ["Caller test fixture"]
    assert result["provenance"]["unit_origin"] == "caller_declared"


@pytest.mark.parametrize(
    "target", ["metadata", "source", "membership", "result", "binding", "timestamp"]
)
def test_corrupt_stored_closure_rejects_without_repair_or_partial_read(store, target):
    attempt = _attempt()
    spec = _spec(f"{attempt['attempt_id']},1,area,observed,1,2,")
    receipt = result_tables.import_result_table("a", spec, "original")
    import_id = receipt["import_id"]
    row = result_tables.read_result_table_rows("a", import_id)["rows"][0]
    with connect() as con:
        if target == "metadata":
            con.execute("UPDATE result_table_imports SET metadata='{}' WHERE id=?", (import_id,))
        elif target == "source":
            con.execute(
                "UPDATE result_table_imports SET source_text=source_text || 'bad' WHERE id=?",
                (import_id,),
            )
        elif target == "membership":
            con.execute("DELETE FROM result_table_rows WHERE import_id=?", (import_id,))
        elif target == "result":
            result = experiments.get_result("a", row["result_id"])["outcome"]
            result["value"] = 7
            digest = sha256(result)
            con.execute(
                "UPDATE experiment_results SET payload=?,digest=? WHERE id=?",
                (canonical(result), digest, row["result_id"]),
            )
            con.execute(
                "UPDATE result_table_rows SET result_sha256=? WHERE result_id=?",
                (digest, row["result_id"]),
            )
        elif target == "timestamp":
            con.execute(
                "UPDATE result_table_imports SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                (import_id,),
            )
        else:
            # Identical result payload under a new ID passes row semantics,
            # but must not replace the assignment frozen by the receipt.
            con.execute(
                "INSERT INTO experiment_results SELECT 'replacement',owner,attempt_id,attempt_revision,metric,analyte_reference,supersedes_id,payload,digest,created_at FROM experiment_results WHERE id=?",
                (row["result_id"],),
            )
            con.execute(
                "UPDATE result_table_rows SET result_id='replacement' WHERE import_id=?",
                (import_id,),
            )
    before = db_path().read_bytes()
    with pytest.raises(ValueError):
        result_tables.get_result_table_import("a", import_id)
    assert db_path().read_bytes() == before


def test_source_paging_uses_utf8_bytes_and_owned_reads(store):
    attempt = _attempt()
    spec = _spec(f"{attempt['attempt_id']},1,area,missing,1,,Échantillon absent")
    receipt = result_tables.import_result_table("a", spec, "unicode")
    raw = spec.csv_text.encode()
    offset = raw.index("É".encode()) + 1
    part = result_tables.read_result_table_source("a", receipt["import_id"], offset, 1)
    assert base64.b64decode(part["content_base64"]) == raw[offset : offset + 1]
    terminal = result_tables.read_result_table_source("a", receipt["import_id"], len(raw), 1)
    assert terminal["content_base64"] == "" and terminal["next_offset"] is None
    for call in (
        result_tables.get_result_table_import,
        result_tables.read_result_table_rows,
        result_tables.read_result_table_source,
    ):
        with pytest.raises(ValueError, match="accessible"):
            call("b", receipt["import_id"])


def test_direct_result_api_does_not_gain_table_or_generated_provenance(store):
    attempt = _attempt()
    result = experiments.record_results(
        "a",
        attempt["attempt_id"],
        1,
        [experiments.ResultSpec(metric="zero", status="observed", unit="1", value=0)],
        "direct",
    )["results"][0]
    assert result["outcome"]["provenance"]["value_origin"] == "caller_reported"
    assert "table_source" not in result["outcome"]["provenance"]
    assert "result_table_imports" not in _counts()


@pytest.mark.parametrize("revision", [True, 1.0, "1", 0])
def test_registry_write_revision_counters_reject_coercion_without_writes(store, revision):
    attempt = _attempt()
    before = _counts()
    with pytest.raises(ValueError, match="positive integer"):
        experiments.revise_attempt(
            "a",
            attempt["attempt_id"],
            revision,
            experiments.AttemptSpec(status="completed"),
            "Correction",
            "bad-revision",
        )
    with pytest.raises(ValueError, match="positive integer"):
        experiments.record_results(
            "a",
            attempt["attempt_id"],
            revision,
            [experiments.ResultSpec(metric="zero", status="observed", unit="1", value=0)],
            "bad-results-revision",
        )
    assert _counts() == before


@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
def test_process_loss_at_transaction_boundary_retries_once(store, phase):
    attempt = _attempt()
    spec = _spec(f"{attempt['attempt_id']},1,area,observed,1,0,")
    source = store / "request.json"
    source.write_text(json.dumps(spec.model_dump(mode="json")))
    before = _counts()
    code = """
import json,os,sys
from pathlib import Path
from measure_service import result_tables
from measure_service.result_table_models import ResultTableSpec
spec=ResultTableSpec.model_validate(json.loads(Path(sys.argv[1]).read_text()))
if sys.argv[2]=='before_commit':
    original=result_tables.registry_mutation
    def boundary(owner,key,request,action):
        def crash(con):
            action(con)
            os._exit(73)
        return original(owner,key,request,crash)
    result_tables.registry_mutation=boundary
result_tables.import_result_table('a',spec,'interrupted')
os._exit(74)
"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    lost = subprocess.run(
        [sys.executable, "-c", code, str(source), phase], env=env, capture_output=True, timeout=30
    )
    assert lost.returncode == (73 if phase == "before_commit" else 74), lost.stderr.decode()
    if phase == "before_commit":
        assert _counts() == before
    receipt = result_tables.import_result_table("a", spec, "interrupted")
    assert result_tables.import_result_table("a", spec, "interrupted") == receipt
    assert _counts()["experiment_results"] == _counts()["result_table_imports"] == 1


def test_table_source_metadata_byte_bound_rolls_back_the_whole_import(store):
    dataset = server.register_dataset(
        "a",
        server.Dataset(
            title="bounded evidence",
            traces=[
                server.Trace(
                    trace_id="uv",
                    sample_id="s",
                    injection_id="i",
                    channel="uv",
                    separation="LC",
                    detector="UV",
                    axis_unit="min",
                    signal_unit="mAU",
                    axis=[0, 1, 2],
                    signal=[0, 2, 0],
                )
            ],
        ),
    )
    analysis = server.save_analysis(
        "a",
        dataset["dataset_id"],
        "uv",
        "fixture",
        {},
        {"summary": {"area": 1}},
        dataset["input_sha256"],
    )
    attempt = _attempt(
        measurements=[experiments.MeasurementLink(dataset_id=dataset["dataset_id"], trace_id="uv")]
    )
    requested = experiments.ResultSpec(
        metric="area",
        status="observed",
        unit="1",
        analysis_id=analysis["analysis_id"],
        summary_field="area",
    )
    with connect() as con:
        con.row_factory = sqlite3.Row
        historical = experiments._attempt(con, "a", attempt["attempt_id"], 1)
        base = experiments._outcome(con, "a", historical, requested)
        # Pad existing analysis provenance so an ordinary outcome fits exactly,
        # then show that adding the service-built table origin is rechecked.
        padding = 64_000 - len(canonical(base).encode()) - len(',"padding":""')
        provenance = json.loads(
            con.execute(
                "SELECT provenance FROM analyses WHERE id=?", (analysis["analysis_id"],)
            ).fetchone()[0]
        )
        provenance["padding"] = "x" * padding
        con.execute(
            "UPDATE analyses SET provenance=? WHERE id=?",
            (canonical(provenance), analysis["analysis_id"]),
        )
        assert (
            len(canonical(experiments._outcome(con, "a", historical, requested)).encode()) == 64_000
        )
    before = _counts()
    spec = ResultTableSpec(
        source_name="linked.csv",
        csv_text=f"metric,analysis,summary,value\nfirst,,,0\narea,{analysis['analysis_id']},area,\n",
        mapping=ResultTableMapping(
            columns={
                "metric": "metric",
                "analysis_id": "analysis",
                "summary_field": "summary",
                "value": "value",
            },
            constants={
                "attempt_id": attempt["attempt_id"],
                "expected_attempt_revision": 1,
                "status": "observed",
                "unit": "1",
            },
        ),
    )
    with pytest.raises(ValueError, match="byte limit"):
        result_tables.import_result_table("a", spec, "oversized")
    assert _counts() == before


def test_historical_attempt_digest_is_verified_before_import_and_on_reads(store):
    attempt = _attempt()
    original = _spec(f"{attempt['attempt_id']},1,area,observed,1,2,")
    receipt = result_tables.import_result_table("a", original, "original")
    with connect() as con:
        raw = con.execute(
            "SELECT payload FROM attempt_revisions WHERE attempt_id=? AND revision=1",
            (attempt["attempt_id"],),
        ).fetchone()[0]
        changed = json.loads(raw)
        changed["instrument_reference"] = "Unrecorded instrument correction"
        con.execute(
            "UPDATE attempt_revisions SET payload=? WHERE attempt_id=? AND revision=1",
            (canonical(changed), attempt["attempt_id"]),
        )
    before = db_path().read_bytes()
    with pytest.raises(ValueError, match="historical attempt digest"):
        result_tables.get_result_table_import("a", receipt["import_id"])
    with pytest.raises(ValueError, match="historical attempt digest"):
        result_tables.import_result_table(
            "a", _spec(f"{attempt['attempt_id']},1,new,observed,1,3,"), "invalid-history"
        )
    assert db_path().read_bytes() == before


def test_future_revision_outside_sqlite_integer_range_is_a_conflict_without_writes(store):
    attempt = _attempt()
    before = _counts()
    spec = _spec(f"{attempt['attempt_id']},{'9' * 100},area,observed,1,0,")
    with pytest.raises(ValueError, match="revision was not found"):
        result_tables.import_result_table("a", spec, "future")
    assert _counts() == before
