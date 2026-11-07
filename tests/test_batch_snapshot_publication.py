"""Publication-index checks against the frozen snapshot delivery fixture."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FROZEN_OUTPUT = (
    Path(__file__).parents[1] / "docs/reference-data/chromatogram-snapshot-example/output"
)
SCRIPT = Path(__file__).parents[1] / "scripts" / "render_batch_snapshot_publication.py"
_spec = importlib.util.spec_from_file_location("snapshot_publication_under_test", SCRIPT)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
render_publication = _module.render_publication


def _copy_fixture(tmp_path: Path) -> Path:
    if not FROZEN_OUTPUT.is_dir():
        pytest.fail(f"frozen delivery fixture is missing: {FROZEN_OUTPUT}")
    target = tmp_path / "output"
    shutil.copytree(FROZEN_OUTPUT, target)
    return target


def test_publication_is_deterministic_and_preserves_report_artifacts(tmp_path: Path) -> None:
    report_dir = _copy_fixture(tmp_path)
    before = {
        path.relative_to(report_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in report_dir.rglob("*")
        if path.is_file()
    }
    first = tmp_path / "published-a" / "index.html"
    second = tmp_path / "published-b" / "index.html"

    first_metadata = render_publication(report_dir, first)
    second_metadata = render_publication(report_dir, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_metadata["sha256"] == second_metadata["sha256"]
    assert first_metadata["source_receipt_sha256"] == second_metadata["source_receipt_sha256"]
    assert {
        path.relative_to(report_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in report_dir.rglob("*")
        if path.is_file()
    } == before
    html = first.read_text(encoding="utf-8")
    assert "2 archived successes are matched to fresh R executions" in html
    assert "5,038" in html and "4,020" in html and "1,018" in html
    assert "output/historical/000/chromatogram.svg" in html
    assert "output/fresh/003/report.html" in html
    assert "source-references.zip" in html and "source-self-contained.zip" in html
    assert 'alt="Reference historical stored chromatogram trace"' in html
    assert "Source: 2 completed / 2 pending" in html
    assert "QC and unknown source items remain pending" in html
    assert "MoSeS reproduction remain open" in html
    assert html.count('<article class="report-card">') == 6
    assert html.index("Historical import · Reference") < html.index("Fresh R · Reference")
    assert "<script" not in html.lower()
    assert "https://" not in html


def test_published_index_and_manifest_match_the_retained_reference(tmp_path: Path):
    reference = FROZEN_OUTPUT.parent
    generated = tmp_path / "index.html"
    render_publication(FROZEN_OUTPUT, generated)
    assert generated.read_bytes() == (reference / "index.html").read_bytes()
    publication = json.loads((reference / "publication.json").read_text())
    present = {
        path.relative_to(reference).as_posix()
        for path in reference.rglob("*")
        if path.is_file() and path.name != "publication.json"
    }
    assert set(publication["files"]) == present
    for relative, identity in publication["files"].items():
        artifact = reference / relative
        assert not artifact.is_symlink()
        assert artifact.stat().st_size == identity["size_bytes"]
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == identity["sha256"]
    assert publication["execution_head"] != publication["compatibility_validation"]["head"]
    assert publication["comparison_summary"]["checked_values"] == 5038
    assert publication["comparison_scope"]["original_unresolved_source_items"] == [
        {"ordinal": 2, "role": "qc", "state": "pending"},
        {"ordinal": 3, "role": "unknown", "state": "pending"},
    ]


def test_cli_escapes_receipt_text_and_rejects_hostile_paths(tmp_path: Path) -> None:
    report_dir = _copy_fixture(tmp_path)
    receipt_path = report_dir / "workflow_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["scope"] = 'Synthetic <LC> & "operator" scope'
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    destination = tmp_path / "publication" / "index.html"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--report-dir",
            str(report_dir),
            "--destination",
            str(destination),
            "--report-prefix",
            "output",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "&lt;LC&gt; &amp; &quot;operator&quot;" in destination.read_text(encoding="utf-8")
    metadata = json.loads(completed.stdout)
    assert metadata["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert len(metadata) <= 4

    hostile = json.loads((report_dir / "workflow_receipt.json").read_text(encoding="utf-8"))
    hostile["historical_imports"][0]["report"]["files"]["report.html"]["path"] = (
        "../../outside.html"
    )
    (report_dir / "workflow_receipt.json").write_text(
        json.dumps(hostile, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    rejected = tmp_path / "rejected" / "index.html"
    with pytest.raises(ValueError, match="relative report artifact path"):
        render_publication(report_dir, rejected)
    assert not rejected.exists()

    invalid_prefix = tmp_path / "invalid-prefix" / "index.html"
    with pytest.raises(ValueError, match="relative report artifact path"):
        render_publication(report_dir, invalid_prefix, report_prefix="../output")
    assert not invalid_prefix.exists()


def test_missing_receipt_contract_and_existing_destination_fail_without_writes(
    tmp_path: Path,
) -> None:
    report_dir = _copy_fixture(tmp_path)
    receipt_path = report_dir / "workflow_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    del receipt["profile"]
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="profile"):
        render_publication(report_dir, tmp_path / "missing-profile.html")

    report_dir = _copy_fixture(tmp_path / "missing-comparison-scope")
    receipt_path = report_dir / "workflow_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    del receipt["comparison_scope"]["compared_item_count"]
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="comparison scope.*compared_item_count"):
        render_publication(report_dir, tmp_path / "missing-comparison-scope.html")

    report_dir = _copy_fixture(tmp_path / "missing-assertions")
    receipt_path = report_dir / "workflow_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    del receipt["assertions"]["failed_runtime_unknown"]
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="assertions are missing"):
        render_publication(report_dir, tmp_path / "missing-assertions.html")

    report_dir = _copy_fixture(tmp_path / "second")
    destination = tmp_path / "existing.html"
    destination.write_text("caller-owned", encoding="utf-8")
    with pytest.raises(FileExistsError, match="new"):
        render_publication(report_dir, destination)
    assert destination.read_text(encoding="utf-8") == "caller-owned"
