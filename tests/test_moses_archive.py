"""Tests for safe, bounded MoSeS archive access without network requests."""

from __future__ import annotations

import hashlib
import importlib
import io
import stat
import warnings
import zipfile
from pathlib import Path

import pytest

from measure_service.benchmarks import moses_archive
from measure_service.benchmarks.moses_archive import (
    MOSES_ARCHIVE_MD5,
    MOSES_ARCHIVE_SIZE_BYTES,
    MOSES_ARCHIVE_URL,
    MOSES_DOI,
    MOSES_RECORD_ID,
    _ArchiveLimits,
    _fetch_archive,
    _inspect_archive,
    _read_member,
)


def _write_zip(path: Path, entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return path.read_bytes()


def _fetch(
    cache_dir: Path,
    payload: bytes,
    *,
    expected_size: int | None = None,
    expected_md5: str | None = None,
    opener=None,
) -> Path:
    if opener is None:

        def opener(_url, timeout):
            return io.BytesIO(payload)

    return _fetch_archive(
        cache_dir,
        url="https://example.invalid/archive.zip",
        expected_size=len(payload) if expected_size is None else expected_size,
        expected_md5=(
            hashlib.md5(payload, usedforsecurity=False).hexdigest()
            if expected_md5 is None
            else expected_md5
        ),
        opener=opener,
    )


def test_import_and_public_constants_do_not_fetch_network(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("network access must be explicit")

    monkeypatch.setattr(moses_archive.urllib.request, "urlopen", fail_if_called)
    importlib.reload(moses_archive)

    assert moses_archive.MOSES_DOI == "10.5281/zenodo.22109729"
    assert MOSES_DOI == moses_archive.MOSES_DOI
    assert MOSES_RECORD_ID == "22109729"
    assert MOSES_ARCHIVE_URL == (
        "https://zenodo.org/api/records/22109729/files/results.zip/content"
    )
    assert MOSES_ARCHIVE_SIZE_BYTES == 1_485_278_454
    assert MOSES_ARCHIVE_MD5 == "25b40b0f56467d83ee5ba434a8872cbb"


def test_inspect_reports_source_hashes_sorted_members_and_aggregates(tmp_path):
    path = tmp_path / "fixture.zip"
    archive_bytes = _write_zip(
        path,
        [("z.txt", b"last"), ("folder/a.csv", b"x,y\n1,2\n"), ("empty", b"")],
    )

    manifest = moses_archive.inspect_archive(path, verify=False)

    assert manifest["source"] == {
        "doi": MOSES_DOI,
        "url": MOSES_ARCHIVE_URL,
        "expected": {
            "size_bytes": MOSES_ARCHIVE_SIZE_BYTES,
            "md5": MOSES_ARCHIVE_MD5,
        },
        "observed": {
            "size_bytes": len(archive_bytes),
            "md5": hashlib.md5(archive_bytes, usedforsecurity=False).hexdigest(),
            "sha256": hashlib.sha256(archive_bytes).hexdigest(),
        },
        "matches_expected": False,
    }
    archive_manifest = manifest["archive"]
    assert archive_manifest["member_count"] == 3
    assert archive_manifest["file_count"] == 3
    assert archive_manifest["directory_count"] == 0
    assert archive_manifest["total_uncompressed_bytes"] == len(b"last") + len(b"x,y\n1,2\n")
    assert [item["name"] for item in archive_manifest["members"]] == [
        "empty",
        "folder/a.csv",
        "z.txt",
    ]
    assert moses_archive.read_member(path, "folder/a.csv") == b"x,y\n1,2\n"


def test_inspect_can_check_parameterized_known_bytes_fixture(tmp_path):
    path = tmp_path / "fixture.zip"
    archive_bytes = _write_zip(path, [("data.json", b'{"x": NaN, "y": Infinity}')])
    expected_md5 = hashlib.md5(archive_bytes, usedforsecurity=False).hexdigest()

    manifest = _inspect_archive(
        path,
        verify=True,
        expected_size=len(archive_bytes),
        expected_md5=expected_md5,
    )
    assert manifest["source"]["matches_expected"] is True
    assert manifest["source"]["observed"]["sha256"] == hashlib.sha256(archive_bytes).hexdigest()

    with pytest.raises(ValueError, match="does not match the expected source"):
        _inspect_archive(
            path,
            verify=True,
            expected_size=len(archive_bytes) + 1,
            expected_md5=expected_md5,
        )


def test_inspect_rejects_truncated_zip(tmp_path):
    path = tmp_path / "truncated.zip"
    path.write_bytes(_write_zip(tmp_path / "valid.zip", [("data", b"payload")])[:-10])

    with pytest.raises(ValueError, match="invalid or truncated ZIP"):
        moses_archive.inspect_archive(path, verify=False)


@pytest.mark.parametrize(
    "name",
    ["../escape", "/absolute", "C:/drive", "C:drive-relative", "a\\b", "a/./b", "a//b"],
)
def test_inspect_rejects_unsafe_member_paths(tmp_path, name):
    path = tmp_path / "unsafe.zip"
    _write_zip(path, [(name, b"payload")])

    with pytest.raises(ValueError, match="unsafe ZIP member|absolute ZIP member"):
        moses_archive.inspect_archive(path, verify=False)


def test_inspect_and_read_reject_duplicate_names(tmp_path):
    path = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        _write_zip(path, [("same.txt", b"first"), ("same.txt", b"second")])

    with pytest.raises(ValueError, match="duplicate ZIP member"):
        moses_archive.inspect_archive(path, verify=False)
    with pytest.raises(ValueError, match="duplicate ZIP member"):
        moses_archive.read_member(path, "same.txt")


def test_inspect_and_read_reject_symlink_members(tmp_path):
    path = tmp_path / "symlink.zip"
    symlink = zipfile.ZipInfo("link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    _write_zip(path, [(symlink, b"target")])

    with pytest.raises(ValueError, match="symbolic-link"):
        moses_archive.inspect_archive(path, verify=False)
    with pytest.raises(ValueError, match="symbolic-link"):
        moses_archive.read_member(path, "link")


def test_member_size_and_compression_limits_are_checked_from_metadata(tmp_path):
    path = tmp_path / "oversize.zip"
    _write_zip(path, [("large.txt", b"a" * 20)])
    small_member_limit = _ArchiveLimits(max_member_uncompressed_bytes=16)

    with pytest.raises(ValueError, match="uncompressed limit"):
        _inspect_archive(
            path,
            verify=False,
            expected_size=1,
            expected_md5="0" * 32,
            limits=small_member_limit,
        )
    with pytest.raises(ValueError, match="exceeds max_bytes"):
        _read_member(path, "large.txt", max_bytes=16)

    repetitive_path = tmp_path / "compressed.zip"
    _write_zip(repetitive_path, [("repeat.txt", b"a" * 4096)])
    restrictive_ratio = _ArchiveLimits(max_compression_ratio=10)
    with pytest.raises(ValueError, match="compression ratio"):
        _inspect_archive(
            repetitive_path,
            verify=False,
            expected_size=1,
            expected_md5="0" * 32,
            limits=restrictive_ratio,
        )


def test_read_member_checks_crc_and_does_not_extract(tmp_path):
    path = tmp_path / "crc.zip"
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("payload.bin", b"known bytes")
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("payload.bin")
    local_payload_offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    damaged = bytearray(path.read_bytes())
    damaged[local_payload_offset] ^= 0x01
    path.write_bytes(damaged)

    with pytest.raises(ValueError, match="unable to read ZIP member"):
        moses_archive.read_member(path, "payload.bin")
    assert sorted(item.name for item in tmp_path.iterdir()) == ["crc.zip"]


def test_fetch_downloads_and_publishes_only_a_verified_archive(tmp_path):
    payload = b"small known archive bytes"
    path = _fetch(tmp_path, payload)

    assert path.read_bytes() == payload
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob(".*.part"))

    with pytest.raises(ValueError, match="MD5 mismatch"):
        _fetch(
            tmp_path / "wrong-checksum",
            payload,
            expected_md5="0" * 32,
        )
    assert not list((tmp_path / "wrong-checksum").glob("*.zip"))


def test_fetch_removes_partial_file_after_interrupted_download(tmp_path):
    class InterruptedResponse(io.BytesIO):
        def __init__(self):
            super().__init__(b"partial")
            self.read_count = 0

        def read(self, size=-1):
            self.read_count += 1
            if self.read_count == 2:
                raise OSError("connection interrupted")
            return super().read(size)

    response = InterruptedResponse()
    path = tmp_path / f"moses-record-{moses_archive.MOSES_RECORD_ID}-results.zip"

    with pytest.raises(OSError, match="interrupted"):
        _fetch(
            tmp_path,
            b"partial",
            expected_size=100,
            opener=lambda _url, timeout: response,
        )

    assert not path.exists()
    assert not list(tmp_path.iterdir())


def test_fetch_reuses_only_a_checksum_verified_cache_entry(tmp_path):
    payload = b"cached content"
    cached_path = _fetch(tmp_path, payload)

    def unexpected_network(*_args, **_kwargs):
        raise AssertionError("valid cache must be reused")

    reused = _fetch(tmp_path, payload, opener=unexpected_network)
    assert reused == cached_path

    cached_path.write_bytes(b"corrupt cache")
    repaired = _fetch(tmp_path, payload)
    assert repaired == cached_path
    assert repaired.read_bytes() == payload


@pytest.mark.parametrize("value", [-1, True, 1.5, "16"])
def test_read_member_rejects_invalid_byte_limits(tmp_path, value):
    path = tmp_path / "small.zip"
    _write_zip(path, [("small.txt", b"small")])

    with pytest.raises(ValueError, match="max_bytes"):
        moses_archive.read_member(path, "small.txt", max_bytes=value)
