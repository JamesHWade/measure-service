"""Native acquisition registration, ownership and restart contracts."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from measure_service import acquisition_store, storage
from measure_service.acquisition_models import (
    CSV_MEMBER_NAMES,
    MAX_MEMBER_BYTES,
    acquisition_digest,
)
from measure_service.store_operations import (
    StoreOperationError,
    backup_store,
    initialize_store,
    inspect_store,
    restore_store,
)


def _files(*, altered: str | None = None) -> dict[str, str]:
    result = {name: f"{name}\n" for name in CSV_MEMBER_NAMES}
    result["manifest.dcf"] = "Format: measure-acquisition\r\nVersion: 1\r\n"
    if altered is not None:
        result["points.csv"] = altered
    return result


def _worker(payload: dict) -> dict:
    assert payload["operation"] == "inspect_acquisition"
    assert payload["files"]["manifest.dcf"].endswith("\r\n")
    return {
        "profile": "measure-acquisition-inspection/1",
        "summary": {
            "sample": {"sample_id": "sample-A"},
            "injection": {"injection_id": "injection-A", "sample_id": "sample-A"},
            "channels": [
                {
                    "channel_id": "ms-centroid",
                    "channel_name": "MS centroid",
                    "value_unit": "counts",
                }
            ],
            "sources": [
                {
                    "source_id": "synthetic-source",
                    "source_name": "synthetic.csv",
                    "source_sha256": "a" * 64,
                }
            ],
        },
    }


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    return tmp_path


def test_registers_exact_native_text_and_returns_bounded_summary(isolated_store: Path) -> None:
    files = _files()
    registered = acquisition_store.register_acquisition("chemist-a", files, worker=_worker)

    assert registered["input_sha256"] == acquisition_digest(files)
    assert set(registered) == {
        "profile",
        "acquisition_id",
        "input_sha256",
        "created_at",
        "inspection",
    }
    assert registered["inspection"] == _worker({"operation": "inspect_acquisition", "files": files})
    assert acquisition_store.acquisition_files("chemist-a", registered["acquisition_id"]) == files


def test_lookup_is_owner_scoped_and_survives_reopening_store(isolated_store: Path) -> None:
    registered = acquisition_store.register_acquisition("chemist-a", _files(), worker=_worker)
    # A fresh connection is used for every public read; closing this explicit
    # connection makes the restart boundary visible to the test.
    storage.connect().close()

    assert (
        acquisition_store.get_acquisition("chemist-a", registered["acquisition_id"]) == registered
    )
    with pytest.raises(ValueError, match="not found or is not accessible"):
        acquisition_store.get_acquisition("chemist-b", registered["acquisition_id"])
    with pytest.raises(ValueError, match="not found or is not accessible"):
        acquisition_store.acquisition_files("chemist-b", registered["acquisition_id"])


def test_same_owner_digest_is_a_safe_retry_but_changed_bytes_register_again(
    isolated_store: Path,
) -> None:
    files = _files()
    calls = 0

    def counted_worker(payload: dict) -> dict:
        nonlocal calls
        calls += 1
        return _worker(payload)

    first = acquisition_store.register_acquisition("chemist-a", files, worker=counted_worker)

    def unavailable(_payload: dict) -> dict:
        raise AssertionError("successful retries must not dispatch to the worker")

    retried = acquisition_store.register_acquisition(
        "chemist-a", dict(reversed(list(files.items()))), worker=unavailable
    )
    changed = acquisition_store.register_acquisition(
        "chemist-a", _files(altered="points\r\nchanged\r\n"), worker=_worker
    )

    assert retried == first
    assert calls == 1
    assert changed["acquisition_id"] != first["acquisition_id"]
    assert changed["input_sha256"] != first["input_sha256"]


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda value: value.pop("manifest.dcf"), "exactly 13 fixed members"),
        (
            lambda value: value.__setitem__("points.tsv", value.pop("points.csv")),
            "complete CSV or TSV",
        ),
        (
            lambda value: value.__setitem__("../points.csv", value.pop("points.csv")),
            "unsupported member",
        ),
        (lambda value: value.__setitem__("points.csv", b"bytes"), "UTF-8 text"),
    ],
)
def test_rejects_malformed_member_mappings(mutate, match) -> None:
    files = _files()
    mutate(files)
    with pytest.raises(ValueError, match=match):
        acquisition_store.register_acquisition("chemist-a", files, worker=_worker)


def test_rejects_oversized_member_before_worker(isolated_store: Path) -> None:
    files = _files()
    files["points.csv"] = "x" * (MAX_MEMBER_BYTES + 1)
    called = False

    def worker(_payload: dict) -> dict:
        nonlocal called
        called = True
        return _worker(_payload)

    with pytest.raises(ValueError, match="member exceeds"):
        acquisition_store.register_acquisition("chemist-a", files, worker=worker)
    assert not called


def test_worker_failure_leaves_no_partial_record_and_can_be_retried(isolated_store: Path) -> None:
    files = _files()

    def fail(_payload: dict) -> dict:
        raise ValueError("synthetic worker failure")

    with pytest.raises(ValueError, match="synthetic worker failure"):
        acquisition_store.register_acquisition("chemist-a", files, worker=fail)

    # Registration creates the store only after worker success; initialize it
    # explicitly to inspect that the failed attempt wrote no acquisition row.
    with storage.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM acquisitions").fetchone()[0] == 0
    recovered = acquisition_store.register_acquisition("chemist-a", files, worker=_worker)
    assert (
        acquisition_store.get_acquisition("chemist-a", recovered["acquisition_id"])["input_sha256"]
        == recovered["input_sha256"]
    )


def test_concurrent_identical_submissions_commit_one_retry_record(isolated_store: Path) -> None:
    files = _files()
    ready = threading.Barrier(2)

    def worker(payload: dict) -> dict:
        ready.wait(timeout=10)
        return _worker(payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: acquisition_store.register_acquisition("chemist-a", files, worker=worker),
                range(2),
            )
        )
    assert results[0] == results[1]


def test_backup_restore_preserves_current_acquisition_state(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = acquisition_store.register_acquisition("chemist-a", _files(), worker=_worker)
    backup = isolated_store.parent / "acquisition-backup"
    backup_store(isolated_store, backup)
    restored = isolated_store.parent / "acquisition-restored"
    restore_store(backup, restored)

    # A restored store is selected only after the operator operation has
    # finished, matching the service's normal cutover contract.
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(restored))
    assert (
        acquisition_store.get_acquisition("chemist-a", registered["acquisition_id"]) == registered
    )
    assert (
        acquisition_store.acquisition_files("chemist-a", registered["acquisition_id"]) == _files()
    )


def test_backup_inspect_and_restore_accept_a_legacy_store_without_acquisition_tables(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-source"
    initialize_store(source)
    with sqlite3.connect(source / "measure.sqlite3") as con:
        con.execute("DROP TABLE acquisition_files")
        con.execute("DROP TABLE acquisitions")

    assert inspect_store(source)["table_counts"].get("acquisitions") is None
    backup = tmp_path / "legacy-backup"
    backup_store(source, backup)
    restored = tmp_path / "legacy-restored"
    restore_store(backup, restored)
    assert inspect_store(restored)["table_counts"].get("acquisitions") is None


def test_store_recovery_rejects_a_partial_acquisition_schema(tmp_path: Path) -> None:
    source = tmp_path / "partial-source"
    initialize_store(source)
    with sqlite3.connect(source / "measure.sqlite3") as con:
        con.execute("DROP TABLE acquisition_files")
    with pytest.raises(StoreOperationError, match="Store acquisition state is incomplete"):
        inspect_store(source)
