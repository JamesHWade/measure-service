"""Installed, public-MCP coverage for the mapped outcome-table example."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

EXPECTED_OUTPUTS = {
    "initial.csv",
    "correction.csv",
    "rejected.csv",
    "initial-import.json",
    "correction-import.json",
    "results.json",
    "observations.json",
    "snapshot.json",
    "outcome-rows.csv",
    "workflow_receipt.json",
    "index.html",
    "manifest.json",
}


def _script() -> Path:
    return Path(__file__).parents[1] / "examples" / "result_tables.py"


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "MEASURE_MCP_DATA_DIR", "MEASURE_MCP_IDENTITY", "MEASURE_MCP_R"):
        environment.pop(name, None)
    return environment


def _run_cli(output: Path, data: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_script()),
            "--output-dir",
            str(output),
            "--data-dir",
            str(data),
        ],
        cwd=output.parent,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=90,
    )


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("corrected_assessment", "display"),
    [
        ("not_accepted", "not accepted"),
        ('not_accepted<script>&"', "not accepted&lt;script&gt;&amp;&quot;"),
    ],
)
def test_html_document_displays_readable_escaped_assessments(corrected_assessment, display):
    render = runpy.run_path(str(_script()))["_html_document"]
    receipt = {
        "status_counts": {},
        "row_count": 0,
        "source_bytes": 0,
        "source_sha256": "a" * 64,
        "result_bindings_sha256": "b" * 64,
    }
    state = {
        "counts": {
            "initial_rows": 0,
            "corrected_rows": 0,
            "retained_result_records": 0,
            "attempts": 0,
            "outcome_states": 0,
        },
        "initial_import": {"receipt": receipt, "rows": []},
        "correction_import": {"receipt": receipt, "rows": []},
        "verification": {
            "old_assessment": "accepted",
            "corrected_assessment": corrected_assessment,
        },
        "method": {"method_id": "renderer-test"},
        "scope": "Constructed renderer-only fixture; no workflow execution.",
    }

    document = render(state).decode("utf-8")

    assert f"<strong>accepted → {display}</strong>assessment history" in document
    assert "<script" not in document.lower()
    assert state["verification"]["corrected_assessment"] == corrected_assessment


def test_installed_public_workflow_retains_rows_corrections_and_artifact_hashes(tmp_path):
    output = tmp_path / "result-table-output"
    data = tmp_path / "result-table-data"
    completed = _run_cli(output, data)
    assert completed.returncode == 0, completed.stderr

    assert set(path.name for path in output.iterdir()) == EXPECTED_OUTPUTS
    assert not (output / "EXAMPLE_INCOMPLETE").exists()
    assert data.is_dir()

    receipt = _json(output / "workflow_receipt.json")
    assert receipt["profile"] == "measure-result-table-example/1"
    assert receipt["counts"] == {
        "initial_rows": 11,
        "corrected_rows": 1,
        "retained_result_records": 12,
        "attempts": 2,
        "outcome_states": 7,
    }
    assert receipt["verification"]["rejected_import_persisted"] is False
    assert receipt["verification"]["old_snapshot_unchanged"] is True
    assert receipt["verification"]["old_assessment"] == "accepted"
    assert receipt["verification"]["corrected_assessment"] == "not_accepted"
    assert receipt["verification"]["corrected_current_head_separate"] is True
    assert receipt["verification"]["old_pending_unknowns_not_zero"] is True
    assert receipt["verification"]["source_encoding"] == "UTF-8"
    assert receipt["verification"]["source_line_endings"] == "CRLF"
    assert receipt["verification"]["r_backend_used"] is False
    assert receipt["sources"]["initial.csv"]["unmapped_columns"] == ["plate", "notes"]
    assert [attempt["spec"]["replicate_kind"] for attempt in receipt["experiment"]["attempts"]] == [
        "technical",
        "technical",
    ]
    assert all(attempt["spec"]["fidelity"] == 1.0 for attempt in receipt["experiment"]["attempts"])

    initial_import = _json(output / "initial-import.json")
    correction_import = _json(output / "correction-import.json")
    assert initial_import["receipt"]["row_count"] == 11
    assert correction_import["receipt"]["row_count"] == 1
    assert len(initial_import["rows"]) == 11
    assert len(correction_import["rows"]) == 1
    assert (
        initial_import["collector"]["csv_text_sha256"] == initial_import["receipt"]["source_sha256"]
    )
    assert (
        correction_import["collector"]["csv_text_sha256"]
        == correction_import["receipt"]["source_sha256"]
    )

    initial_source = (output / "initial.csv").read_bytes()
    assert "Δ" in initial_source.decode("utf-8")
    assert b"Quoted note" in initial_source
    assert b"\r\n" in initial_source
    assert b"\n" not in initial_source.replace(b"\r\n", b"")

    results = _json(output / "results.json")
    assert len(results["records"]) == 12
    assert len({record["result_id"] for record in results["records"]}) == 12
    assert {record["outcome"]["status"] for record in results["records"]} >= {
        "observed",
        "not_detected",
        "below_limit",
        "censored",
        "missing",
        "failed",
        "infeasible",
    }
    assert any(
        record["outcome"]["status"] == "observed" and record["outcome"].get("value") == 0
        for record in results["records"]
    )

    observations = _json(output / "observations.json")
    assert observations["old"]["observation"]["assessment"]["analytical_acceptance"] == "accepted"
    assert (
        observations["corrected"]["observation"]["assessment"]["analytical_acceptance"]
        == "not_accepted"
    )
    assert observations["old_after_correction"] == observations["old"]
    assert observations["snapshot_after_correction"] == _json(output / "snapshot.json")["snapshot"]

    outcome_rows = (output / "outcome-rows.csv").read_text(encoding="utf-8")
    assert len(outcome_rows.splitlines()) == 13
    assert "result_id" in outcome_rows.splitlines()[0]

    manifest = _json(output / "manifest.json")
    manifest_paths = {entry["path"] for entry in manifest["files"]}
    assert manifest_paths == EXPECTED_OUTPUTS - {"manifest.json"}
    for entry in manifest["files"]:
        content = (output / entry["path"]).read_bytes()
        assert entry["bytes"] == len(content)
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()

    html = (output / "index.html").read_text(encoding="utf-8")
    assert "<main" in html
    assert 'role="region"' in html
    assert 'tabindex="0"' in html
    assert "initial.csv" in html and "correction.csv" in html
    assert '<th scope="col">Unit</th>' in html
    retained = initial_import["rows"]
    first_time = next(
        row["result"]
        for row in retained
        if row["result"]["metric"] == "retention_time" and row["result"]["analyte_reference"] == "A"
    )
    second_time = next(
        row["result"]
        for row in retained
        if row["result"]["metric"] == "retention_time" and row["result"]["analyte_reference"] == "B"
    )
    assert f"<td>{first_time['value']}</td><td>min</td>" in html
    assert f"<td>{second_time['value']}</td><td>s</td>" in html
    assert "<td>not reported</td><td>mg/L</td><td>—</td><td>0.05</td>" in html
    assert "<td>not reported</td><td>mg/L</td><td>0.1</td><td>0.2</td>" in html
    assert "<script" not in html.lower()
    assert "http://" not in html and "https://" not in html


@pytest.mark.parametrize("kind", ["existing", "nested", "dangling-symlink"])
def test_preflight_rejects_conflicts_before_creating_any_target(tmp_path, kind):
    output = tmp_path / "output"
    data = tmp_path / "data"
    if kind == "existing":
        output.mkdir()
        sentinel = output / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
    elif kind == "nested":
        data = output / "data"
    else:
        dangling_target = tmp_path / "missing-target"
        output.symlink_to(dangling_target, target_is_directory=True)

    completed = _run_cli(output, data)

    assert completed.returncode != 0
    if kind == "existing":
        assert (output / "sentinel.txt").read_text(encoding="utf-8") == "keep"
        assert not data.exists()
    elif kind == "nested":
        assert not output.exists()
        assert not data.exists()
    else:
        assert output.is_symlink()
        assert not data.exists()
