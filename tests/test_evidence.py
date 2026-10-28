import json

import pytest

from measure_service.benchmarks.evidence import VerifiedBundle, staged_bundle

CORE = {"report.json", "rows.csv"}


def bundle(path):
    with staged_bundle(path) as stage:
        (stage / "report.json").write_text(json.dumps({"profile": "synthetic/1"}))
        (stage / "rows.csv").write_text("id,value\nfirst,0\nsecond,2\n")
    return VerifiedBundle(path, required_files=CORE, profile="synthetic/1")


def test_atomic_roundtrip_keeps_zero_and_ignores_optional_files(tmp_path):
    root = tmp_path / "input"
    evidence = bundle(root)
    (root / "optional.png").write_bytes(b"not an input")
    assert list(evidence.iter_csv("rows.csv")) == [
        {"id": "first", "value": "0"},
        {"id": "second", "value": "2"},
    ]
    evidence.verify_unchanged()
    assert set(evidence.complete["files"]) == CORE
    assert evidence.file_digest("rows.csv") == evidence.complete["files"]["rows.csv"]["sha256"]


def test_resealed_mid_analysis_change_is_detected(tmp_path):
    root = tmp_path / "input"
    evidence = bundle(root)
    replacement = tmp_path / "replacement"
    bundle(replacement)
    (replacement / "rows.csv").write_text("id,value\nfirst,99\n")
    # Resealing a changed input is a different evidence identity, not permission
    # to splice new rows into an analysis already in progress.
    from measure_service.benchmarks.evidence import file_sha256

    complete = json.loads((replacement / "complete.json").read_text())
    complete["files"]["rows.csv"] = {
        "sha256": file_sha256(replacement / "rows.csv"),
        "size_bytes": (replacement / "rows.csv").stat().st_size,
    }
    (root / "rows.csv").write_bytes((replacement / "rows.csv").read_bytes())
    (root / "complete.json").write_text(json.dumps(complete))
    with pytest.raises(ValueError, match="changed during"):
        evidence.verify_unchanged()


def test_corrupt_core_and_unsupported_profile_fail_before_analysis(tmp_path):
    root = tmp_path / "input"
    bundle(root)
    with pytest.raises(ValueError, match="profile"):
        VerifiedBundle(root, required_files=CORE, profile="other/1")
    (root / "rows.csv").write_text("changed")
    with pytest.raises(ValueError, match="checksum"):
        VerifiedBundle(root, required_files=CORE, profile="synthetic/1")


@pytest.mark.parametrize("text", ["id,id\na,b\n", "id,value\na\n", "id,value\na,b,c\n"])
def test_malformed_csv_cannot_be_consumed_from_a_validly_hashed_bundle(tmp_path, text):
    root = tmp_path / "input"
    with staged_bundle(root) as stage:
        (stage / "report.json").write_text(json.dumps({"profile": "synthetic/1"}))
        (stage / "rows.csv").write_text(text)
    evidence = VerifiedBundle(root, required_files=CORE, profile="synthetic/1")
    with pytest.raises(ValueError, match="header|malformed"):
        list(evidence.iter_csv("rows.csv"))


def test_failed_writer_never_publishes_partial_evidence(tmp_path):
    target = tmp_path / "output"
    with pytest.raises(RuntimeError, match="calculation failed"):
        with staged_bundle(target) as stage:
            (stage / "partial.csv").write_text("half of the result")
            raise RuntimeError("calculation failed")
    assert not target.exists()
    assert not list(tmp_path.glob(".measure-evidence-*"))


def test_input_and_existing_output_are_preserved(tmp_path):
    source = tmp_path / "input"
    bundle(source)
    with pytest.raises(ValueError, match="separate"):
        with staged_bundle(source / "nested", input_dirs=[source]):
            pytest.fail("input path must never be writable output")
    with pytest.raises(ValueError, match="preserved"):
        with staged_bundle(source):
            pytest.fail("existing evidence must never be overwritten")


def test_output_created_during_analysis_is_not_overwritten(tmp_path):
    target = tmp_path / "output"
    with pytest.raises(ValueError, match="preserved"):
        with staged_bundle(target) as stage:
            (stage / "rows.csv").write_text("new result")
            target.mkdir()
            (target / "other.txt").write_text("another writer")
    assert (target / "other.txt").read_text() == "another writer"
    assert not list(tmp_path.glob(".measure-evidence-*"))
