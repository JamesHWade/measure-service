"""Bounded access to the public MoSeS result archive.

Importing this module never performs network or archive I/O. Call
``fetch_archive`` explicitly to download the pinned Zenodo object. Archive
members are read in memory only and are never extracted to disk.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO, Callable

MOSES_RECORD_ID = "22109729"
MOSES_DOI = "10.5281/zenodo.22109729"
MOSES_ARCHIVE_URL = "https://zenodo.org/api/records/22109729/files/results.zip/content"
MOSES_ARCHIVE_SIZE_BYTES = 1_485_278_454
MOSES_ARCHIVE_MD5 = "25b40b0f56467d83ee5ba434a8872cbb"

DEFAULT_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_HEX_MD5 = re.compile(r"^[0-9a-fA-F]{32}$")


@dataclass(frozen=True)
class _ArchiveLimits:
    """Limits kept injectable so tests can exercise abuse checks cheaply."""

    max_archive_bytes: int = 4 * 1024**3
    max_members: int = 100_000
    max_member_uncompressed_bytes: int = 16 * 1024**3
    max_total_uncompressed_bytes: int = 100 * 1024**3
    max_compression_ratio: float = 1_000.0


_DEFAULT_LIMITS = _ArchiveLimits()


def fetch_archive(cache_dir: str | os.PathLike[str]) -> Path:
    """Fetch the pinned ZIP into ``cache_dir`` and return its verified path.

    The network request is made only when this function is called. A cache
    entry is reused only after its byte count and published MD5 match. New
    bytes are streamed to a temporary file in the cache directory and replace
    the cache entry atomically after size and MD5 validation.
    """

    return _fetch_archive(
        cache_dir,
        url=MOSES_ARCHIVE_URL,
        expected_size=MOSES_ARCHIVE_SIZE_BYTES,
        expected_md5=MOSES_ARCHIVE_MD5,
        opener=urllib.request.urlopen,
    )


def inspect_archive(path: str | os.PathLike[str], *, verify: bool = True) -> dict[str, object]:
    """Return source hashes and a sorted, metadata-only ZIP member manifest.

    With ``verify=True`` (the default), the archive must match the published
    Zenodo size and MD5. ZIP payloads are not decompressed during inspection;
    member CRCs are checked when a selected member is read with
    :func:`read_member`.
    """

    if not isinstance(verify, bool):
        raise TypeError("verify must be a bool")
    return _inspect_archive(
        path,
        verify=verify,
        expected_size=MOSES_ARCHIVE_SIZE_BYTES,
        expected_md5=MOSES_ARCHIVE_MD5,
    )


def read_member(
    path: str | os.PathLike[str],
    member: str,
    *,
    max_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
) -> bytes:
    """Read one safe ZIP member into memory, bounded by ``max_bytes``.

    The selected member is streamed with a strict byte limit. Reading through
    EOF makes ``zipfile`` check its CRC. No archive member is written to disk.
    """

    return _read_member(path, member, max_bytes=max_bytes)


def _fetch_archive(
    cache_dir: str | os.PathLike[str],
    *,
    url: str,
    expected_size: int,
    expected_md5: str,
    opener: Callable[..., BinaryIO],
    limits: _ArchiveLimits = _DEFAULT_LIMITS,
    chunk_size: int = _HASH_CHUNK_BYTES,
) -> Path:
    _validate_expected(expected_size, expected_md5)
    _validate_limits(limits)
    if expected_size > limits.max_archive_bytes:
        raise ValueError("expected archive exceeds the configured archive byte limit")
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")

    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"moses-record-{MOSES_RECORD_ID}-results.zip"

    if _matches_expected_file(destination, expected_size, expected_md5, limits):
        return destination

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".part", dir=directory
    )
    temporary_path = Path(temporary_name)
    md5 = hashlib.md5(usedforsecurity=False)
    byte_count = 0
    try:
        with os.fdopen(descriptor, "wb") as output, opener(url, timeout=60) as response:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > expected_size or byte_count > limits.max_archive_bytes:
                    raise ValueError("download exceeds the expected archive size")
                output.write(chunk)
                md5.update(chunk)
            output.flush()
            os.fsync(output.fileno())

        if byte_count != expected_size:
            raise ValueError(
                f"download size mismatch: expected {expected_size}, observed {byte_count}"
            )
        observed_md5 = md5.hexdigest()
        if observed_md5.lower() != expected_md5.lower():
            raise ValueError(
                f"download MD5 mismatch: expected {expected_md5.lower()}, observed {observed_md5}"
            )

        os.replace(temporary_path, destination)
        return destination
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _matches_expected_file(
    path: Path,
    expected_size: int,
    expected_md5: str,
    limits: _ArchiveLimits,
) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        return False
    try:
        observed = _hash_file(path, max_bytes=limits.max_archive_bytes)
    except (OSError, ValueError):
        return False
    return (
        observed["size_bytes"] == expected_size and observed["md5"].lower() == expected_md5.lower()
    )


def _inspect_archive(
    path: str | os.PathLike[str],
    *,
    verify: bool,
    expected_size: int,
    expected_md5: str,
    limits: _ArchiveLimits = _DEFAULT_LIMITS,
) -> dict[str, object]:
    _validate_expected(expected_size, expected_md5)
    _validate_limits(limits)
    if not isinstance(verify, bool):
        raise TypeError("verify must be a bool")

    archive_path = Path(path)
    observed = _hash_file(archive_path, max_bytes=limits.max_archive_bytes)
    matches_expected = (
        observed["size_bytes"] == expected_size and observed["md5"].lower() == expected_md5.lower()
    )
    if verify and not matches_expected:
        raise ValueError(
            "archive does not match the expected source: "
            f"expected {expected_size} bytes and MD5 {expected_md5.lower()}, "
            f"observed {observed['size_bytes']} bytes and MD5 {observed['md5']}"
        )

    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            members, aggregates, manifest_members = _validated_members(archive, limits)
    except (zipfile.BadZipFile, EOFError, OSError, zipfile.LargeZipFile) as error:
        raise ValueError(f"invalid or truncated ZIP archive: {archive_path}") from error

    return {
        "schema_version": 1,
        "source": {
            "doi": MOSES_DOI,
            "url": MOSES_ARCHIVE_URL,
            "expected": {"size_bytes": expected_size, "md5": expected_md5.lower()},
            "observed": observed,
            "matches_expected": matches_expected,
        },
        "archive": {
            **aggregates,
            "members": manifest_members,
        },
    }


def _read_member(
    path: str | os.PathLike[str],
    member: str,
    *,
    max_bytes: int,
    limits: _ArchiveLimits = _DEFAULT_LIMITS,
) -> bytes:
    if not isinstance(member, str):
        raise TypeError("member must be a str")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    _validate_limits(limits)

    _validate_member_name(member)
    archive_path = Path(path)
    if archive_path.stat().st_size > limits.max_archive_bytes:
        raise ValueError("archive exceeds the configured archive byte limit")

    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            infos, _, _ = _validated_members(archive, limits)
            by_name = {info.filename: info for info in infos}
            info = by_name.get(member)
            if info is None:
                # Names with a trailing slash are directories and are never
                # readable payloads; normalized aliases are rejected above.
                raise KeyError(f"ZIP member not found: {member}")
            if _is_directory(info):
                raise ValueError(f"ZIP member is a directory: {member}")
            if info.file_size > max_bytes:
                raise ValueError(f"ZIP member exceeds max_bytes: {info.file_size} > {max_bytes}")
            chunks: list[bytes] = []
            byte_count = 0
            with archive.open(info, mode="r") as stream:
                while True:
                    chunk = stream.read(min(_READ_CHUNK_BYTES, max_bytes - byte_count + 1))
                    if not chunk:
                        break
                    byte_count += len(chunk)
                    if byte_count > max_bytes:
                        raise ValueError(f"ZIP member exceeds max_bytes: {member}")
                    chunks.append(chunk)
            if byte_count != info.file_size:
                raise zipfile.BadZipFile(
                    f"ZIP member size mismatch for {member}: "
                    f"expected {info.file_size}, observed {byte_count}"
                )
            return b"".join(chunks)
    except (zipfile.BadZipFile, EOFError, OSError, zipfile.LargeZipFile) as error:
        raise ValueError(f"unable to read ZIP member {member!r}: {error}") from error


def _validated_members(
    archive: zipfile.ZipFile,
    limits: _ArchiveLimits,
) -> tuple[list[zipfile.ZipInfo], dict[str, int], list[dict[str, int | str]]]:
    infos = archive.infolist()
    if len(infos) > limits.max_members:
        raise ValueError(f"archive has too many members: {len(infos)} > {limits.max_members}")

    seen: set[str] = set()
    total_uncompressed = 0
    total_compressed = 0
    file_count = 0
    directory_count = 0
    for info in infos:
        canonical_name = _validate_member_name(info.orig_filename)
        if canonical_name in seen:
            raise ValueError(f"duplicate ZIP member name: {info.orig_filename}")
        seen.add(canonical_name)
        _validate_zip_info(info)

        if info.file_size < 0 or info.compress_size < 0:
            raise ValueError(f"invalid ZIP member sizes: {info.orig_filename}")
        if info.file_size > limits.max_member_uncompressed_bytes:
            raise ValueError(
                "ZIP member exceeds the configured uncompressed limit: "
                f"{info.orig_filename} ({info.file_size} bytes)"
            )
        total_uncompressed += info.file_size
        total_compressed += info.compress_size
        if total_uncompressed > limits.max_total_uncompressed_bytes:
            raise ValueError("archive exceeds the configured total uncompressed limit")
        if _is_directory(info):
            directory_count += 1
        else:
            file_count += 1

        if info.file_size and not info.compress_size:
            raise ValueError(f"non-empty ZIP member has no compressed data: {info.filename}")
        if info.compress_size:
            ratio = info.file_size / info.compress_size
            if ratio > limits.max_compression_ratio:
                raise ValueError(
                    f"ZIP member exceeds the configured compression ratio: "
                    f"{info.filename} ({ratio:.1f}:1)"
                )
        if info.flag_bits & 0x1:
            raise ValueError(f"encrypted ZIP member is unsupported: {info.filename}")

    if total_compressed > limits.max_archive_bytes:
        raise ValueError("archive exceeds the configured total compressed limit")

    members = sorted(infos, key=lambda entry: entry.filename)
    manifest_members = [
        {
            "name": info.filename,
            "size_bytes": info.file_size,
            "compressed_size_bytes": info.compress_size,
        }
        for info in members
    ]
    aggregates = {
        "member_count": len(infos),
        "file_count": file_count,
        "directory_count": directory_count,
        "total_uncompressed_bytes": total_uncompressed,
        "total_compressed_bytes": total_compressed,
    }
    return members, aggregates, manifest_members


def _validate_member_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("ZIP member names must be strings")
    if not name or "\x00" in name or "\\" in name:
        raise ValueError(f"unsafe ZIP member name: {name!r}")
    if name.startswith("/") or PureWindowsPath(name).drive:
        raise ValueError(f"absolute ZIP member name: {name!r}")

    parts = name.split("/")
    if name.endswith("/"):
        parts = parts[:-1]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe ZIP member path: {name!r}")
    return "/".join(parts)


def _validate_zip_info(info: zipfile.ZipInfo) -> None:
    raw_name = info.orig_filename
    if raw_name != info.filename:
        raise ValueError(f"ZIP member name contains a NUL byte: {raw_name!r}")

    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if info.create_system == 3 and stat.S_ISLNK(mode):
        raise ValueError(f"symbolic-link ZIP member is unsupported: {info.filename}")
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ValueError(f"special-file ZIP member is unsupported: {info.filename}")


def _is_directory(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return info.is_dir() or stat.S_ISDIR(mode)


def _hash_file(path: Path, *, max_bytes: int) -> dict[str, int | str]:
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            byte_count += len(chunk)
            if byte_count > max_bytes:
                raise ValueError(f"file exceeds the configured byte limit: {path}")
            md5.update(chunk)
            sha256.update(chunk)
    return {
        "size_bytes": byte_count,
        "md5": md5.hexdigest(),
        "sha256": sha256.hexdigest(),
    }


def _validate_expected(expected_size: int, expected_md5: str) -> None:
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
        raise ValueError("expected_size must be a non-negative integer")
    if not isinstance(expected_md5, str) or not _HEX_MD5.fullmatch(expected_md5):
        raise ValueError("expected_md5 must be a 32-character hexadecimal digest")


def _validate_limits(limits: _ArchiveLimits) -> None:
    numeric_limits = (
        limits.max_archive_bytes,
        limits.max_members,
        limits.max_member_uncompressed_bytes,
        limits.max_total_uncompressed_bytes,
    )
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 1
        for value in numeric_limits
    ):
        raise ValueError("archive byte and member limits must be positive integers")
    if (
        not isinstance(limits.max_compression_ratio, (float, int))
        or isinstance(limits.max_compression_ratio, bool)
        or not math.isfinite(limits.max_compression_ratio)
        or limits.max_compression_ratio < 1
    ):
        raise ValueError("max_compression_ratio must be a finite number of at least 1")
