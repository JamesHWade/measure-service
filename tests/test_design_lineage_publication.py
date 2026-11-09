"""Verify the frozen, inspectable experiment-design publication."""

import csv
import hashlib
import io
import json
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

REFERENCE = Path(__file__).parents[1] / "docs/reference-data/design-lineage-example"
EXECUTION_HEAD = "02a86c612a730153712da570a9fffd5cae477ce8"
EXECUTION_WHEEL_SHA256 = "b104c1a86e79968a8f91d7179adf47f879cae7619380297f8dd089d7727e64bd"


def _json(name):
    return json.loads((REFERENCE / name).read_text(encoding="utf-8"))


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.links = []
        self.ids = set()
        self.table = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "a":
            self.links.append(attrs.get("href"))
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


def _without_latest_revision(record):
    return {key: value for key, value in record.items() if key != "latest_revision"}


def _display(value):
    return "not reported" if value is None else str(value)


def test_publication_manifest_inventory_and_qualified_execution():
    publication = _json("publication.json")
    assert publication["profile"] == "measure-design-lineage-publication/1"
    assert publication["schema_version"] == "1.0"
    assets = publication["files"]
    assert isinstance(assets, dict)
    assert len(assets) == 14

    manifest = _json("manifest.json")
    assert manifest["profile"] == "measure-experiment-design-example/1/manifest"
    assert manifest["schema_version"] == "1.0"
    assert len(manifest["files"]) == 11
    original_assets = {entry["path"] for entry in manifest["files"]}
    expected_assets = original_assets | {
        "manifest.json",
        "execution.json",
        "independent-review.json",
    }
    assert set(assets) == expected_assets
    present = {path.name for path in REFERENCE.iterdir() if path.is_file()}
    assert present == expected_assets | {"publication.json"}

    for name, identity in assets.items():
        path = REFERENCE / name
        assert not Path(name).is_absolute()
        assert ".." not in Path(name).parts
        assert path.is_file()
        assert not path.is_symlink()
        data = path.read_bytes()
        assert len(data) == identity["size_bytes"]
        assert hashlib.sha256(data).hexdigest() == identity["sha256"]

    for original in manifest["files"]:
        data = (REFERENCE / original["path"]).read_bytes()
        assert len(data) == original["bytes"]
        assert hashlib.sha256(data).hexdigest() == original["sha256"]

    execution = _json("execution.json")
    assert execution["profile"] == "measure-design-lineage-execution/1"
    assert execution["source"]["head"] == EXECUTION_HEAD
    assert execution["wheel_sha256"] == EXECUTION_WHEEL_SHA256
    assert execution["returncode"] == 0
    assert execution["installed_package_members_verified"] == 78
    assert execution["installed_tests_passed"] == 1344
    assert execution["optional_BO_tests_deselected_locally"] == 5
    assert publication["execution_head"] == EXECUTION_HEAD
    assert publication["execution_wheel_sha256"] == EXECUTION_WHEEL_SHA256
    review = _json("independent-review.json")
    assert review["profile"] == "measure-design-lineage-independent-artifact-review/1"
    assert review["decision"] == "accepted"
    assert review["findings"] == []
    assert review["independent_artifact_checks"] == publication["independent_checks"] == 789
    assert review["source_head"] == EXECUTION_HEAD
    assert review["wheel_sha256"] == EXECUTION_WHEEL_SHA256
    assert publication["original_execution_preserved"] is True

    public_bytes = b"".join((REFERENCE / name).read_bytes() for name in present)
    public_text = public_bytes.decode("utf-8")
    assert "/Users/" not in public_text
    assert "privateapps" not in public_text.lower()


def test_lineage_and_retained_records_bind_to_canonical_hashes():
    designs = _json("designs.json")["records"]
    lineage = _json("lineage.json")
    attempts = _json("attempts.json")
    results = _json("results.json")["records"]
    observations = _json("observations.json")
    snapshot = _json("snapshot.json")["snapshot"]
    workflow = _json("workflow_receipt.json")

    assert len(designs) == 3
    assert len(lineage["entries"]) == 3
    assert lineage["count"] == lineage["current_revision"] == 3
    assert lineage["offset"] == 0
    assert lineage["limit"] == 3
    assert lineage["next_offset"] is None

    designs_by_id = {record["experiment_id"]: record for record in designs}
    entries = lineage["entries"]
    for index, entry in enumerate(entries):
        design = designs_by_id[entry["experiment_id"]]
        assert entry["profile"] == "measure-experiment-design-version/1"
        assert entry["schema_version"] == "1.0"
        assert entry["revision"] == index + 1
        assert entry["root_experiment_id"] == entries[0]["root_experiment_id"]
        assert entry["experiment_input_sha256"] == design["input_sha256"]
        assert (
            _sha({key: value for key, value in entry.items() if key != "entry_sha256"})
            == entry["entry_sha256"]
        )
        if index == 0:
            assert entry["supersedes_experiment_id"] is None
            assert entry["supersedes_entry_sha256"] is None
        else:
            assert entry["supersedes_experiment_id"] == entries[index - 1]["experiment_id"]
            assert entry["supersedes_entry_sha256"] == entries[index - 1]["entry_sha256"]

    assert lineage["root_experiment_id"] == entries[0]["experiment_id"]
    assert lineage["selected_experiment_id"] == entries[0]["experiment_id"]
    assert lineage["selected_revision"] == 1
    assert lineage["current_experiment_id"] == entries[-1]["experiment_id"]
    assert lineage["current_entry_sha256"] == entries[-1]["entry_sha256"]

    for design in designs:
        retained = dict(design["spec"])
        for field in ("executable_method", "observation_contract", "observation_producer"):
            if design[field] is not None:
                retained[field] = design[field]
        assert _sha(retained) == design["input_sha256"]

    assert len(attempts["records"]) == attempts["total_revisions"] == 5
    attempt_ids = {record["attempt_id"] for record in attempts["records"]}
    assert len(attempt_ids) == 3
    for attempt_id in attempt_ids:
        revisions = [record for record in attempts["records"] if record["attempt_id"] == attempt_id]
        assert [record["revision"] for record in revisions] == list(range(1, len(revisions) + 1))
        assert all(record["latest_revision"] == len(revisions) for record in revisions)
    revision_views = attempts["revision_views"]
    assert _without_latest_revision(
        revision_views["old_revision_1_before"]
    ) == _without_latest_revision(revision_views["old_revision_1_after"])
    assert _without_latest_revision(
        revision_views["v2_revision_1_before"]
    ) == _without_latest_revision(revision_views["v2_revision_1_after"])

    assert len(results) == 8
    assert len({record["result_id"] for record in results}) == 8
    assert all(_sha(record["outcome"]) == record["input_sha256"] for record in results)
    result_attempt_ids = {record["attempt_id"] for record in results}
    assert result_attempt_ids < attempt_ids
    late_attempt_ids = attempt_ids - result_attempt_ids
    assert len(late_attempt_ids) == 1
    assert not any(record["attempt_id"] in late_attempt_ids for record in results)
    assert not any(
        record["experiment_id"] == entries[-1]["experiment_id"] for record in attempts["records"]
    )

    assert len(observations["records"]) == 2
    assert observations["old_after_later_work"] == observations["records"][0]
    assert observations["snapshot_after_later_work"] == snapshot
    assert snapshot["members"] and len(snapshot["members"]) == 1
    assert workflow["snapshot"] == snapshot
    assert workflow["counts"] == {
        "assessments": 2,
        "attempt_revisions": 5,
        "attempts": 3,
        "designs": 3,
        "results": 8,
        "snapshots": 1,
    }
    assert all(
        value is True
        for key, value in workflow["verification"].items()
        if key.endswith("unchanged")
    )
    assert workflow["verification"]["v3_has_no_implicit_attempt"] is True
    assert workflow["verification"]["old_design_attempts_remain_explicit"] is True


def test_history_csv_retains_exact_design_attempt_and_result_rows():
    entries = _json("lineage.json")["entries"]
    attempts = _json("attempts.json")["records"]
    results = _json("results.json")["records"]
    raw = (REFERENCE / "history.csv").read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"), newline="")))
    assert len(rows) == 16
    assert Counter(row["record_type"] for row in rows) == Counter(
        {"design": 3, "attempt": 5, "result": 8}
    )

    expected = []
    for entry in entries:
        expected.append(
            {
                "record_type": "design",
                "record_id": entry["experiment_id"],
                "parent_id": entry["supersedes_experiment_id"] or "",
                "revision": str(entry["revision"]),
                "status": "immutable-design",
                "metric": "",
                "analyte": "",
                "value": "",
                "unit": "",
                "reason": entry["reason"],
                "input_sha256": entry["experiment_input_sha256"],
                "digest": entry["entry_sha256"],
            }
        )
    for record in attempts:
        expected.append(
            {
                "record_type": "attempt",
                "record_id": record["attempt_id"],
                "parent_id": record["experiment_id"],
                "revision": str(record["revision"]),
                "status": record["spec"]["status"],
                "metric": "",
                "analyte": "",
                "value": "",
                "unit": "",
                "reason": record["reason"],
                "input_sha256": record["input_sha256"],
                "digest": "",
            }
        )
    for record in results:
        outcome = record["outcome"]
        expected.append(
            {
                "record_type": "result",
                "record_id": record["result_id"],
                "parent_id": record["attempt_id"],
                "revision": str(record["attempt_revision"]),
                "status": outcome["status"],
                "metric": outcome["metric"],
                "analyte": outcome["analyte_reference"],
                "value": _display(outcome["value"]),
                "unit": outcome["unit"],
                "reason": outcome["explanation"],
                "input_sha256": record["input_sha256"],
                "digest": "",
            }
        )
    assert rows == expected


def test_html_replays_exact_tables_and_resolves_only_local_links():
    document = (REFERENCE / "index.html").read_text(encoding="utf-8")
    parsed = _HTML()
    parsed.feed(document)
    assert [table[0] for table in parsed.tables] == [
        ["Revision", "Experiment ID", "Replacement reason", "Input SHA-256"],
        ["Attempt ID", "Design ID", "Revision", "Status", "Input SHA-256"],
        ["Result ID", "Attempt ID", "Metric", "Analyte", "Status", "Value", "Unit", "Explanation"],
        ["Observation ID", "Attempt ID", "Assessment", "Fidelity role"],
    ]

    lineage = _json("lineage.json")["entries"]
    expected_lineage = [
        {
            "Revision": str(entry["revision"]),
            "Experiment ID": entry["experiment_id"],
            "Replacement reason": entry["reason"],
            "Input SHA-256": entry["experiment_input_sha256"],
        }
        for entry in lineage
    ]
    attempts = _json("attempts.json")["records"]
    expected_attempts = [
        {
            "Attempt ID": record["attempt_id"],
            "Design ID": record["experiment_id"],
            "Revision": str(record["revision"]),
            "Status": record["spec"]["status"],
            "Input SHA-256": record["input_sha256"],
        }
        for record in attempts
    ]
    results = _json("results.json")["records"]
    expected_results = [
        {
            "Result ID": record["result_id"],
            "Attempt ID": record["attempt_id"],
            "Metric": record["outcome"]["metric"],
            "Analyte": record["outcome"]["analyte_reference"],
            "Status": record["outcome"]["status"],
            "Value": _display(record["outcome"]["value"]),
            "Unit": record["outcome"]["unit"],
            "Explanation": record["outcome"]["explanation"],
        }
        for record in results
    ]
    observations = _json("observations.json")["records"]
    expected_observations = [
        {
            "Observation ID": record["observation_id"],
            "Attempt ID": record["attempt_id"],
            "Assessment": record["observation"]["assessment"]["analytical_acceptance"],
            "Fidelity role": record["observation"]["evidence"]["fidelity_role"],
        }
        for record in observations
    ]
    visible = [
        [dict(zip(table[0], row, strict=True)) for row in table[1:]] for table in parsed.tables
    ]
    assert visible == [expected_lineage, expected_attempts, expected_results, expected_observations]

    for href in parsed.links:
        assert href is not None
        target = urlsplit(href)
        assert not target.scheme and not target.netloc and not target.query
        if target.path:
            path = (REFERENCE / unquote(target.path)).resolve(strict=True)
            assert path.is_relative_to(REFERENCE.resolve())
            assert path.is_file()
        if target.fragment:
            assert target.fragment in parsed.ids
    assert "<script" not in document.lower()
    assert "/Users/" not in document
    assert "privateapps" not in document.lower()
