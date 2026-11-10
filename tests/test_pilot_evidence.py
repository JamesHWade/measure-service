from __future__ import annotations

import concurrent.futures
import os
import stat
import subprocess
import sys
import threading

import pytest

from measure_service import pilot_evidence
from measure_service.pilot_evidence import EvidenceDirectory, EvidenceError, read_checkpoint


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _private_checkpoint(path, content: bytes, *, mode: int = 0o600) -> None:
    path.write_bytes(content)
    path.chmod(mode)


def _deep_value(levels: int) -> dict:
    value: object = 0
    for _ in range(levels):
        value = [value]
    return {"value": value}


def test_creates_new_private_directory_and_files_under_permissive_umask(tmp_path):
    destination = tmp_path / "pilot-evidence"
    previous_umask = os.umask(0)
    try:
        evidence = EvidenceDirectory(str(destination))
    finally:
        os.umask(previous_umask)

    try:
        assert _mode(destination) == 0o700
        assert _mode(destination / "INCOMPLETE") == 0o600
        assert _mode(destination / "events.jsonl") == 0o600
        assert (destination / "INCOMPLETE").read_bytes() == b""
        assert (destination / "events.jsonl").read_bytes() == b""
    finally:
        evidence.close()


def test_constructor_syncs_the_parent_after_publishing_the_new_leaf(tmp_path, monkeypatch):
    parent_metadata = tmp_path.stat()
    observed_directory_syncs = []
    original_fsync = os.fsync

    def observed_fsync(fd):
        metadata = os.fstat(fd)
        if stat.S_ISDIR(metadata.st_mode):
            observed_directory_syncs.append((metadata.st_dev, metadata.st_ino))
        return original_fsync(fd)

    monkeypatch.setattr(pilot_evidence.os, "fsync", observed_fsync)
    evidence = EvidenceDirectory(str(tmp_path / "evidence"))
    try:
        assert (parent_metadata.st_dev, parent_metadata.st_ino) in observed_directory_syncs
    finally:
        evidence.close()


def test_resolves_symlinked_parent_but_requires_parent_to_exist(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias = tmp_path / "parent-alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    evidence = EvidenceDirectory(str(alias / "pilot"))
    evidence.close()

    assert (real_parent / "pilot" / "INCOMPLETE").is_file()
    missing_parent = tmp_path / "missing" / "nested"
    with pytest.raises(EvidenceError, match="existing directory"):
        EvidenceDirectory(str(missing_parent / "pilot"))
    assert not missing_parent.exists()


def test_refuses_existing_directory_or_symlink_without_mutating_target(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("unchanged")

    with pytest.raises(EvidenceError, match="already exists"):
        EvidenceDirectory(str(existing))
    assert sentinel.read_text() == "unchanged"
    assert sorted(entry.name for entry in existing.iterdir()) == ["keep.txt"]

    target = tmp_path / "target"
    target.mkdir()
    target_file = target / "keep.txt"
    target_file.write_text("unchanged")
    link = tmp_path / "linked-leaf"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(EvidenceError):
        EvidenceDirectory(str(link))
    assert link.is_symlink()
    assert target_file.read_text() == "unchanged"
    assert sorted(entry.name for entry in target.iterdir()) == ["keep.txt"]


def test_concurrent_exclusive_creation_has_one_owner_and_no_partial_winner(tmp_path):
    destination = tmp_path / "raced"
    start = threading.Barrier(2)

    def create():
        start.wait()
        try:
            return EvidenceDirectory(str(destination))
        except EvidenceError as error:
            return error

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: create(), range(2)))

    owners = [result for result in results if isinstance(result, EvidenceDirectory)]
    errors = [result for result in results if isinstance(result, EvidenceError)]
    assert len(owners) == 1
    assert len(errors) == 1
    try:
        assert sorted(entry.name for entry in destination.iterdir()) == [
            "INCOMPLETE",
            "events.jsonl",
        ]
    finally:
        owners[0].close()


def test_writes_allowlisted_json_canonically_and_refuses_overwrite(tmp_path):
    destination = tmp_path / "evidence"
    evidence = EvidenceDirectory(str(destination))
    try:
        evidence.write_json("checkpoint.json", {"z": 2, "a": "μ"})
        target = destination / "checkpoint.json"
        assert target.read_bytes() == '{"a":"μ","z":2}'.encode("utf-8")
        assert _mode(target) == 0o600
        assert read_checkpoint(str(target)) == {"a": "μ", "z": 2}

        before = target.read_bytes()
        with pytest.raises(EvidenceError, match="already exists"):
            evidence.write_json("checkpoint.json", {"replacement": True})
        assert target.read_bytes() == before
        with pytest.raises(EvidenceError, match="filename"):
            evidence.write_json("notes.json", {})
        assert not (destination / "notes.json").exists()
    finally:
        evidence.close()


@pytest.mark.parametrize(
    "value",
    [
        {"value": float("inf")},
        {"value": float("nan")},
        _deep_value(34),
        {f"chunk-{index}": "private-data" * 5_461 for index in range(34)},
    ],
)
def test_invalid_or_oversized_write_does_not_create_file_or_expose_payload(tmp_path, value):
    destination = tmp_path / "evidence"
    evidence = EvidenceDirectory(str(destination))
    try:
        with pytest.raises(EvidenceError) as error:
            evidence.write_json("checkpoint.json", value)
        assert str(destination) not in str(error.value)
        assert "private-data" not in str(error.value)
        assert not (destination / "checkpoint.json").exists()
        assert (destination / "INCOMPLETE").exists()
    finally:
        evidence.close()


def test_event_append_is_canonical_durable_and_retains_incomplete_marker(tmp_path, monkeypatch):
    destination = tmp_path / "evidence"
    evidence = EvidenceDirectory(str(destination))
    original_fsync = os.fsync
    calls = []

    def observed_fsync(fd):
        calls.append(fd)
        return original_fsync(fd)

    monkeypatch.setattr(pilot_evidence.os, "fsync", observed_fsync)
    try:
        evidence.append_event({"z": 1, "a": "event"})
        assert (destination / "events.jsonl").read_bytes() == b'{"a":"event","z":1}\n'
        assert calls
        assert (destination / "INCOMPLETE").exists()
    finally:
        evidence.close()


def test_event_and_total_log_budgets_fail_without_appending_partial_event(tmp_path, monkeypatch):
    destination = tmp_path / "evidence"
    evidence = EvidenceDirectory(str(destination))
    log_path = destination / "events.jsonl"
    monkeypatch.setattr(pilot_evidence, "MAX_EVENT_BYTES", 5)
    try:
        with pytest.raises(EvidenceError, match="bounded canonical JSON"):
            evidence.append_event({"event": "too-large"})
        assert log_path.read_bytes() == b""

        monkeypatch.setattr(pilot_evidence, "MAX_EVENT_BYTES", 100)
        monkeypatch.setattr(pilot_evidence, "MAX_EVENTS_BYTES", 8)
        evidence.append_event({"a": 1})
        before = log_path.read_bytes()
        with pytest.raises(EvidenceError, match="size limit"):
            evidence.append_event({"b": 2})
        assert log_path.read_bytes() == before
        assert (destination / "INCOMPLETE").exists()
    finally:
        evidence.close()


def test_completion_removes_marker_syncs_directory_and_closes_write_state(tmp_path, monkeypatch):
    destination = tmp_path / "evidence"
    evidence = EvidenceDirectory(str(destination))
    original_fsync = os.fsync
    calls = []

    def observed_fsync(fd):
        calls.append(fd)
        return original_fsync(fd)

    monkeypatch.setattr(pilot_evidence.os, "fsync", observed_fsync)
    evidence.complete()
    assert not (destination / "INCOMPLETE").exists()
    assert calls
    with pytest.raises(EvidenceError, match="complete"):
        evidence.append_event({"later": True})
    evidence.close()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"outer":{"duplicate":1,"duplicate":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":' + b"[" * 34 + b"0" + b"]" * 34 + b"}",
        b'{"value":' + b" " * (pilot_evidence.MAX_CHECKPOINT_BYTES + 1),
    ],
)
def test_checkpoint_rejects_ambiguous_nonfinite_deep_or_oversized_json(tmp_path, raw):
    target = tmp_path / "checkpoint.json"
    _private_checkpoint(target, raw)

    with pytest.raises(EvidenceError):
        read_checkpoint(str(target))
    assert target.read_bytes() == raw


def test_checkpoint_read_requires_private_single_link_regular_file(tmp_path):
    source = tmp_path / "source.json"
    _private_checkpoint(source, b'{"ok":true}')

    linked = tmp_path / "linked.json"
    linked.symlink_to(source)
    with pytest.raises(EvidenceError):
        read_checkpoint(str(linked))

    hard_link = tmp_path / "hard-link.json"
    os.link(source, hard_link)
    with pytest.raises(EvidenceError, match="private regular file"):
        read_checkpoint(str(source))

    hard_link.unlink()
    source.chmod(0o640)
    with pytest.raises(EvidenceError, match="private regular file"):
        read_checkpoint(str(source))
    assert source.read_bytes() == b'{"ok":true}'


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO checks require POSIX")
def test_fifo_entries_are_rejected_without_blocking(tmp_path):
    checkpoint_fifo = tmp_path / "checkpoint.fifo"
    os.mkfifo(checkpoint_fifo, 0o600)
    script = """
import sys
from measure_service.pilot_evidence import EvidenceError, read_checkpoint
try:
    read_checkpoint(sys.argv[1])
except EvidenceError:
    pass
else:
    raise SystemExit("FIFO checkpoint was accepted")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(checkpoint_fifo)],
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr

    destination = tmp_path / "evidence"
    script = """
import os
import pathlib
import sys
from measure_service.pilot_evidence import EvidenceDirectory, EvidenceError
root = pathlib.Path(sys.argv[1])
evidence = EvidenceDirectory(str(root))
try:
    events = root / "events.jsonl"
    events.unlink()
    os.mkfifo(events, 0o600)
    try:
        evidence.append_event({"event": "test"})
    except EvidenceError:
        pass
    else:
        raise SystemExit("FIFO event log was accepted")
    marker = root / "INCOMPLETE"
    marker.unlink()
    os.mkfifo(marker, 0o600)
    try:
        evidence.complete()
    except EvidenceError:
        pass
    else:
        raise SystemExit("FIFO incomplete marker was accepted")
finally:
    evidence.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(destination)],
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr


def test_complete_marker_is_retained_when_event_or_json_validation_fails(tmp_path):
    destination = tmp_path / "evidence"
    evidence = EvidenceDirectory(str(destination))
    try:
        with pytest.raises(EvidenceError):
            evidence.write_json("report.json", {"v": float("nan")})
        with pytest.raises(EvidenceError):
            evidence.append_event({"v": float("inf")})
        assert (destination / "INCOMPLETE").exists()
        assert not (destination / "report.json").exists()
        assert (destination / "events.jsonl").read_bytes() == b""
    finally:
        evidence.close()


def test_completion_sync_failure_restores_incomplete_marker_when_possible(tmp_path, monkeypatch):
    from measure_service import pilot_evidence as evidence

    output = tmp_path / "completion-failure"
    with evidence.EvidenceDirectory(str(output)) as directory:
        directory.write_json("report.json", {"status": "passed"})

        def fail_sync(_fd):
            raise evidence.EvidenceError("evidence directory could not be synchronized")

        monkeypatch.setattr(evidence, "_sync_directory", fail_sync)
        with pytest.raises(evidence.EvidenceError, match="synchronized"):
            directory.complete()
        assert (output / "INCOMPLETE").is_file()
        assert (output / "INCOMPLETE").stat().st_mode & 0o777 == 0o600
        assert (output / "report.json").is_file()


def test_completion_io_failure_is_not_reported_as_success_when_marker_cannot_be_recreated(
    tmp_path, monkeypatch
):
    from measure_service import pilot_evidence as evidence

    output = tmp_path / "failed-marker-recreation"
    with evidence.EvidenceDirectory(str(output)) as directory:

        def fail_sync(_fd):
            raise evidence.EvidenceError("evidence directory could not be synchronized")

        def fail_create(_fd, _name):
            raise evidence.EvidenceError("evidence file could not be created")

        monkeypatch.setattr(evidence, "_sync_directory", fail_sync)
        monkeypatch.setattr(evidence, "_create_private_file", fail_create)
        with pytest.raises(evidence.EvidenceError, match="synchronized"):
            directory.complete()
        directory.append_event({"event": "completion_did_not_commit"})
        assert "completion_did_not_commit" in (output / "events.jsonl").read_text()
