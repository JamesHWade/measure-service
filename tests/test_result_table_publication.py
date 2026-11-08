"""Frozen public evidence retains exact source/result joins and scientific display."""

import csv
import hashlib
import io
import json
import runpy
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from measure_service.result_table_models import ResultTableSpec
from measure_service.result_table_parser import parse_result_table
from measure_service.storage import sha256

REFERENCE = Path(__file__).parents[1] / "docs/reference-data/result-table-example"


def _json(name):
    return json.loads((REFERENCE / name).read_text())


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.links, self.ids = [], [], set()
        self.table = self.row = self.cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "a":
            self.links.append(attrs["href"])
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"th", "td"} and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"th", "td"} and self.cell is not None:
            self.row.append("".join(self.cell))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def test_publication_manifest_and_original_execution_outputs_match():
    publication = _json("publication.json")
    assert publication["profile"] == "measure-result-table-publication/1"
    present = {p.name for p in REFERENCE.iterdir() if p.is_file()}
    assert present == set(publication["files"]) | {"publication.json"}
    assert len(publication["files"]) == 15
    for name, identity in publication["files"].items():
        data = (REFERENCE / name).read_bytes()
        assert not (REFERENCE / name).is_symlink()
        assert len(data) == identity["size_bytes"]
        assert hashlib.sha256(data).hexdigest() == identity["sha256"]
    for original in _json("manifest.json")["files"]:
        data = (REFERENCE / original["path"]).read_bytes()
        assert len(data) == original["bytes"]
        assert hashlib.sha256(data).hexdigest() == original["sha256"]
    execution = _json("execution.json")
    assert execution["source"]["head"] == publication["execution_head"]
    assert execution["wheel_sha256"] == publication["execution_wheel_sha256"]
    assert execution["returncode"] == 0
    assert _json("independent-review.json")["decision"] == "accepted"


def test_raw_mapping_rows_and_generated_results_retain_complete_evidence():
    records = {r["result_id"]: r for r in _json("results.json")["records"]}
    assert len(records) == 12
    all_rows = []
    for stem in ["initial", "correction"]:
        imported = _json(f"{stem}-import.json")
        receipt, rows = imported["receipt"], imported["rows"]
        raw = (REFERENCE / f"{stem}.csv").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == receipt["source_sha256"]
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
        plan = parse_result_table(
            ResultTableSpec(
                source_name=receipt["source_name"],
                csv_text=raw.decode("utf-8"),
                delimiter=receipt["delimiter"],
                mapping=receipt["mapping"],
            )
        )
        assert plan.input_sha256 == receipt["input_sha256"]
        assert plan.mapping_sha256 == receipt["mapping_sha256"]
        assert dict(Counter(r["result"]["status"] for r in rows)) == receipt["status_counts"]
        bindings = []
        for parsed, row in zip(plan.rows, rows, strict=True):
            assert parsed.as_dict() == {key: row[key] for key in parsed.as_dict()}
            assert parsed.row_sha256 == row["row_sha256"]
            record = records[row["result_id"]]
            assert sha256(record["outcome"]) == record["input_sha256"] == row["result_sha256"]
            assert record["attempt_id"] == row["attempt_id"]
            assert record["attempt_revision"] == row["expected_attempt_revision"]
            origin = record["outcome"]["provenance"]
            assert origin["value_origin"] == "caller_reported"
            source = origin["table_source"]
            assert source["import_id"] == receipt["import_id"]
            assert source["source_sha256"] == receipt["source_sha256"]
            assert source["row_sha256"] == row["row_sha256"]
            bindings.append(
                {k: row[k] for k in ("ordinal", "row_sha256", "result_id", "result_sha256")}
            )
        assert sha256(bindings) == receipt["result_bindings_sha256"]
        all_rows.extend(rows)
    assert {row["result_id"] for row in all_rows} == set(records)
    assert set(Counter(row["result"]["status"] for row in all_rows)) == {
        "observed",
        "not_detected",
        "below_limit",
        "censored",
        "missing",
        "failed",
        "infeasible",
    }
    assert sum(r["superseded_by_result_id"] is not None for r in records.values()) == 1
    observed_zero = [
        r
        for r in records.values()
        if r["outcome"]["status"] == "observed" and r["outcome"]["value"] == 0
    ]
    assert len(observed_zero) == 2
    assert all(
        r["outcome"]["value"] is None
        for r in records.values()
        if r["outcome"]["status"] != "observed"
    )


def test_frozen_observation_and_atomic_failure_remain_separate_from_correction():
    observations, workflow = _json("observations.json"), _json("workflow_receipt.json")
    assert observations["old"] == observations["old_after_correction"]
    assert observations["snapshot_after_correction"] == _json("snapshot.json")["snapshot"]
    assert observations["old"]["observation"]["assessment"]["analytical_acceptance"] == "accepted"
    assert (
        observations["corrected"]["observation"]["assessment"]["analytical_acceptance"]
        == "not_accepted"
    )
    assert workflow["counts"] == {
        "initial_rows": 11,
        "corrected_rows": 1,
        "retained_result_records": 12,
        "attempts": 2,
        "outcome_states": 7,
    }
    assert workflow["verification"]["rejected_import_persisted"] is False
    assert workflow["verification"]["old_snapshot_unchanged"] is True
    assert workflow["verification"]["r_backend_used"] is False
    rejected = list(
        csv.DictReader(io.StringIO((REFERENCE / "rejected.csv").read_bytes().decode(), newline=""))
    )
    assert len(rejected) == 2


def test_html_retains_exact_values_units_bounds_and_resolving_local_links():
    document = (REFERENCE / "report.html").read_text()
    parsed = _HTML()
    parsed.feed(document)
    (table,) = [t for t in parsed.tables if "Unit" in t[0] and "Metric" in t[0]]
    visible = [dict(zip(table[0], row, strict=True)) for row in table[1:]]
    assert len(visible) == 12
    stored = {r["result_id"]: r["outcome"] for r in _json("results.json")["records"]}
    for row in visible:
        result = stored[row["Result ID"]]
        assert row["Metric"] == result["metric"]
        assert row["Status"] == result["status"]
        assert row["Unit"] == result["unit"]
        assert row["Value"] == ("not reported" if result["value"] is None else str(result["value"]))
        for label, key in [("Lower bound", "lower_bound"), ("Upper bound", "upper_bound")]:
            assert row[label] == ("—" if result[key] is None else str(result[key]))
    assert len(parsed.links) >= 20
    for href in parsed.links:
        target = urlsplit(href)
        assert not target.scheme and not target.netloc and not target.query
        if target.path:
            path = (REFERENCE / unquote(target.path)).resolve(strict=True)
            assert path.is_relative_to(REFERENCE.resolve()) and path.is_file()
        else:
            assert target.fragment in parsed.ids
    assert "<script" not in document.lower()


def test_presentation_replays_retained_rows_without_changing_original_report():
    script = Path(__file__).parents[1] / "examples/result_tables.py"
    publication = _json("publication.json")
    presentation = publication["presentation"]
    assert presentation["original_report"] == "index.html"
    assert presentation["renderer_source_sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()
    state = _json("workflow_receipt.json")
    for stem in ("initial", "correction"):
        state[f"{stem}_import"] = _json(f"{stem}-import.json")
    renderer = runpy.run_path(str(script))["_html_document"]
    assert renderer(state) == (REFERENCE / presentation["report"]).read_bytes()
    assert b"accepted \xe2\x86\x92 not accepted" in renderer(state)
