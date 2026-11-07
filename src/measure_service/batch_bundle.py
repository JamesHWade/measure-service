"""Deterministic, bounded ZIP transport for portable batch snapshots.

The archive is deliberately a very small container: it has exactly one
canonical manifest member and one canonical snapshot member.  It never
extracts files and it never treats archive contents as executable authority.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import zipfile
import zlib
from pathlib import Path
from typing import Any

from . import batch_snapshot
from .storage import canonical

BUNDLE_PROFILE = "measure-chromatogram-batch-bundle/1"
BUNDLE_SCHEMA_VERSION = "1.0"
MAX_BUNDLE_MANIFEST_BYTES = 16 * 1024
_MEMBER_NAMES = frozenset({"manifest.json", "snapshot.json"})
_CHUNK_BYTES = 64 * 1024
_LOCAL_SIGNATURE = b"PK\x03\x04"
_CENTRAL_SIGNATURE = b"PK\x01\x02"
_EOCD_SIGNATURE = b"PK\x05\x06"
_ALLOWED_FLAG_BITS = 0x800  # UTF-8 names; no encryption or data descriptors.


def _fail(message: str) -> None:
    raise ValueError(message)


def _strict_manifest(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        _fail("bundle manifest must be a JSON object")
    fields = {
        "profile",
        "schema_version",
        "snapshot_sha256",
        "size_bytes",
        "mode",
        "batch_id",
        "batch_revision",
    }
    if set(value) != fields:
        _fail("bundle manifest has unexpected fields")
    if value["profile"] != BUNDLE_PROFILE or value["schema_version"] != BUNDLE_SCHEMA_VERSION:
        _fail("unsupported bundle profile or schema version")
    digest = value["snapshot_sha256"]
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        _fail("bundle manifest has an invalid snapshot digest")
    if (
        type(value["size_bytes"]) is not int
        or not 1 <= value["size_bytes"] <= batch_snapshot.MAX_SNAPSHOT_BYTES
    ):
        _fail("bundle manifest has an invalid snapshot size")
    if value["mode"] not in {"references", "self_contained"}:
        _fail("bundle manifest has an invalid snapshot mode")
    if (
        type(value["batch_id"]) is not str
        or not value["batch_id"].strip()
        or len(value["batch_id"]) > 200
    ):
        _fail("bundle manifest has an invalid batch ID")
    if type(value["batch_revision"]) is not int or value["batch_revision"] < 1:
        _fail("bundle manifest has an invalid batch revision")
    return value


def _manifest_for_snapshot(snapshot_bytes: bytes) -> dict[str, Any]:
    snapshot = batch_snapshot.decode_snapshot(snapshot_bytes)
    manifest = {
        "profile": BUNDLE_PROFILE,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "size_bytes": len(snapshot_bytes),
        "mode": snapshot["mode"],
        "batch_id": snapshot["batch"]["batch_id"],
        "batch_revision": snapshot["batch"]["revision"],
    }
    return _strict_manifest(manifest)


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    encoded = canonical(manifest).encode("utf-8")
    if len(encoded) > MAX_BUNDLE_MANIFEST_BYTES:
        _fail("bundle manifest exceeds its byte limit")
    return encoded


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.internal_attr = 0
    info.comment = b""
    info.extra = b""
    return info


def pack_bundle(snapshot_bytes: bytes) -> bytes:
    """Validate a snapshot and package it in a deterministic two-member ZIP."""
    if type(snapshot_bytes) is not bytes:
        _fail("snapshot_bytes must be bytes")
    if not 1 <= len(snapshot_bytes) <= batch_snapshot.MAX_SNAPSHOT_BYTES:
        _fail("snapshot exceeds its byte limit")
    manifest = _manifest_for_snapshot(snapshot_bytes)
    manifest_bytes = _manifest_bytes(manifest)
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as archive:
            archive.writestr(_zip_info("manifest.json"), manifest_bytes)
            archive.writestr(_zip_info("snapshot.json"), snapshot_bytes)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise ValueError("could not create batch bundle") from exc
    result = output.getvalue()
    if len(result) > batch_snapshot.MAX_ARCHIVE_BYTES:
        _fail("bundle exceeds its byte limit")
    return result


def _member_is_safe(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if name not in _MEMBER_NAMES or name.startswith("/") or ".." in Path(name).parts:
        _fail("bundle contains an unsafe member name")
    if info.is_dir() or name.endswith("/"):
        _fail("bundle members must be regular files")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        _fail("bundle uses an unsupported compression method")
    if info.flag_bits & ~_ALLOWED_FLAG_BITS:
        _fail("bundle member flags are unsupported")
    if info.extra or info.comment:
        _fail("bundle members must not contain extra fields")
    mode = (info.external_attr >> 16) & 0o170000
    if mode and mode != stat.S_IFREG:
        _fail("bundle members must be regular non-symlink files")
    if info.file_size < 0 or info.compress_size < 0:
        _fail("bundle member sizes are invalid")


def _validate_archive_layout(
    archive_bytes: bytes,
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    eocd: int,
) -> dict[str, bytes]:
    """Validate local/central metadata and return bounded raw member bytes."""
    if not infos or min(info.header_offset for info in infos) != 0:
        _fail("bundle must not contain an archive prefix")
    local_end = 0
    central_cursor = archive.start_dir
    raw_members: dict[str, bytes] = {}
    for info in infos:
        offset = info.header_offset
        if offset != local_end or offset + 30 > archive.start_dir:
            _fail("bundle local members are not contiguous")
        if archive_bytes[offset : offset + 4] != _LOCAL_SIGNATURE:
            _fail("bundle local header is malformed")
        local_flags = int.from_bytes(archive_bytes[offset + 6 : offset + 8], "little")
        local_method = int.from_bytes(archive_bytes[offset + 8 : offset + 10], "little")
        local_crc = int.from_bytes(archive_bytes[offset + 14 : offset + 18], "little")
        local_compressed = int.from_bytes(archive_bytes[offset + 18 : offset + 22], "little")
        local_size = int.from_bytes(archive_bytes[offset + 22 : offset + 26], "little")
        name_length = int.from_bytes(archive_bytes[offset + 26 : offset + 28], "little")
        extra_length = int.from_bytes(archive_bytes[offset + 28 : offset + 30], "little")
        name_start = offset + 30
        name_end = name_start + name_length
        data_start = name_end + extra_length
        if data_start > archive.start_dir or extra_length:
            _fail("bundle local headers must not contain extra fields")
        try:
            local_name = archive_bytes[name_start:name_end].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("bundle local filename is not UTF-8") from exc
        if local_name != info.filename:
            _fail("bundle local and central filenames disagree")
        if local_flags & ~_ALLOWED_FLAG_BITS:
            _fail("bundle local header flags are unsupported")
        if (
            local_flags != info.flag_bits
            or local_method != info.compress_type
            or local_crc != info.CRC
            or local_compressed != info.compress_size
            or local_size != info.file_size
        ):
            _fail("bundle local and central metadata disagree")
        data_end = data_start + info.compress_size
        if data_end > archive.start_dir:
            _fail("bundle member data overlaps its central directory")
        raw_members[info.filename] = archive_bytes[data_start:data_end]
        local_end = data_end

        if (
            central_cursor + 46 > eocd
            or archive_bytes[central_cursor : central_cursor + 4] != _CENTRAL_SIGNATURE
        ):
            _fail("bundle central directory is malformed")
        central_name_length = int.from_bytes(
            archive_bytes[central_cursor + 28 : central_cursor + 30], "little"
        )
        central_extra_length = int.from_bytes(
            archive_bytes[central_cursor + 30 : central_cursor + 32], "little"
        )
        central_comment_length = int.from_bytes(
            archive_bytes[central_cursor + 32 : central_cursor + 34], "little"
        )
        central_name_start = central_cursor + 46
        central_name_end = central_name_start + central_name_length
        central_end = central_name_end + central_extra_length + central_comment_length
        if central_end > eocd or central_extra_length or central_comment_length:
            _fail("bundle central members must not contain extras or comments")
        try:
            central_name = archive_bytes[central_name_start:central_name_end].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("bundle central filename is not UTF-8") from exc
        central_flags = int.from_bytes(
            archive_bytes[central_cursor + 8 : central_cursor + 10], "little"
        )
        central_method = int.from_bytes(
            archive_bytes[central_cursor + 10 : central_cursor + 12], "little"
        )
        central_crc = int.from_bytes(
            archive_bytes[central_cursor + 16 : central_cursor + 20], "little"
        )
        central_compressed = int.from_bytes(
            archive_bytes[central_cursor + 20 : central_cursor + 24], "little"
        )
        central_size = int.from_bytes(
            archive_bytes[central_cursor + 24 : central_cursor + 28], "little"
        )
        central_offset = int.from_bytes(
            archive_bytes[central_cursor + 42 : central_cursor + 46], "little"
        )
        if (
            central_name != info.filename
            or central_flags != info.flag_bits
            or central_method != info.compress_type
            or central_crc != info.CRC
            or central_compressed != info.compress_size
            or central_size != info.file_size
            or central_offset != info.header_offset
        ):
            _fail("bundle central metadata disagrees with parsed members")
        central_cursor = central_end
    if local_end != archive.start_dir or central_cursor != eocd:
        _fail("bundle contains unexamined archive bytes")
    return raw_members


def _decode_member(raw: bytes, info: zipfile.ZipInfo, maximum: int) -> bytes:
    if info.file_size > maximum:
        _fail("bundle member exceeds its byte limit")
    if len(raw) != info.compress_size:
        _fail("bundle member compressed length is invalid")
    if info.compress_type == zipfile.ZIP_STORED:
        decoded = raw
    elif info.compress_type == zipfile.ZIP_DEFLATED:
        try:
            decompressor = zlib.decompressobj(-15)
            decoded = decompressor.decompress(raw, maximum + 1)
        except zlib.error as exc:
            raise ValueError("bundle member failed deflate integrity checks") from exc
        if len(decoded) > maximum or decompressor.unconsumed_tail:
            _fail("bundle member exceeds its decompression limit")
        if not decompressor.eof or decompressor.unused_data:
            _fail("bundle member contains an incomplete or trailing deflate stream")
        try:
            decoded += decompressor.flush(maximum - len(decoded) + 1)
        except zlib.error as exc:
            raise ValueError("bundle member failed deflate integrity checks") from exc
    else:  # defensive: _member_is_safe rejects this first.
        _fail("bundle uses an unsupported compression method")
    if len(decoded) != info.file_size or (zlib.crc32(decoded) & 0xFFFFFFFF) != info.CRC:
        _fail("bundle member CRC or length does not match its metadata")
    return decoded


def _preflight_central_directory(archive_bytes: bytes) -> tuple[int, int]:
    """Validate the physical two-record central directory before ZipFile parses it."""
    eocd = archive_bytes.rfind(_EOCD_SIGNATURE)
    if eocd < 0 or eocd + 22 > len(archive_bytes):
        _fail("bundle has an invalid end-of-central-directory record")
    disk_number = int.from_bytes(archive_bytes[eocd + 4 : eocd + 6], "little")
    central_disk = int.from_bytes(archive_bytes[eocd + 6 : eocd + 8], "little")
    entries_on_disk = int.from_bytes(archive_bytes[eocd + 8 : eocd + 10], "little")
    entries_total = int.from_bytes(archive_bytes[eocd + 10 : eocd + 12], "little")
    central_size = int.from_bytes(archive_bytes[eocd + 12 : eocd + 16], "little")
    central_offset = int.from_bytes(archive_bytes[eocd + 16 : eocd + 20], "little")
    comment_length = int.from_bytes(archive_bytes[eocd + 20 : eocd + 22], "little")
    if (
        disk_number != 0
        or central_disk != 0
        or entries_on_disk != len(_MEMBER_NAMES)
        or entries_total != len(_MEMBER_NAMES)
        or comment_length != 0
        or eocd + 22 + comment_length != len(archive_bytes)
        or central_offset > eocd
        or central_size != eocd - central_offset
    ):
        _fail("bundle must be a single-disk exact archive")

    cursor = central_offset
    names: set[str] = set()
    for _ in range(len(_MEMBER_NAMES)):
        if cursor + 46 > eocd or archive_bytes[cursor : cursor + 4] != _CENTRAL_SIGNATURE:
            _fail("bundle central directory is malformed")
        name_length = int.from_bytes(archive_bytes[cursor + 28 : cursor + 30], "little")
        extra_length = int.from_bytes(archive_bytes[cursor + 30 : cursor + 32], "little")
        comment_length = int.from_bytes(archive_bytes[cursor + 32 : cursor + 34], "little")
        record_end = cursor + 46 + name_length + extra_length + comment_length
        if record_end > eocd or extra_length or comment_length:
            _fail("bundle central members must not contain extras or comments")
        name_start = cursor + 46
        name_end = name_start + name_length
        try:
            name = archive_bytes[name_start:name_end].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("bundle central filename is not UTF-8") from exc
        if name not in _MEMBER_NAMES or name in names:
            _fail("bundle must contain exactly manifest.json and snapshot.json")
        names.add(name)
        cursor = record_end
    if cursor != eocd:
        _fail("bundle central directory contains unexamined records")
    return eocd, central_offset


def unpack_bundle(archive_bytes: bytes) -> bytes:
    """Validate an archive and return its exact canonical snapshot bytes."""
    if type(archive_bytes) is not bytes:
        _fail("archive must be bytes")
    if not 1 <= len(archive_bytes) <= batch_snapshot.MAX_ARCHIVE_BYTES:
        _fail("bundle exceeds its byte limit")
    eocd, central_offset = _preflight_central_directory(archive_bytes)
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r")
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise ValueError("bundle is not a valid ZIP archive") from exc
    with archive:
        if archive.comment:
            _fail("bundle archive comments are not allowed")
        # ``zipfile`` intentionally accepts arbitrary bytes after EOCD.  An
        # exact two-member container must reject that trailing data rather
        # than silently treating it as an unexamined archive extension.
        if eocd < archive.start_dir or central_offset != archive.start_dir:
            _fail("bundle has an invalid end-of-central-directory record")
        comment_length = int.from_bytes(archive_bytes[eocd + 20 : eocd + 22], "little")
        if eocd + 22 + comment_length != len(archive_bytes):
            _fail("bundle contains trailing data")
        infos = archive.infolist()
        if len(infos) != len(_MEMBER_NAMES) or {info.filename for info in infos} != _MEMBER_NAMES:
            _fail("bundle must contain exactly manifest.json and snapshot.json")
        for info in infos:
            _member_is_safe(info)
        by_name = {info.filename: info for info in infos}
        raw_members = _validate_archive_layout(archive_bytes, archive, infos, eocd)
        manifest_bytes = _decode_member(
            raw_members["manifest.json"], by_name["manifest.json"], MAX_BUNDLE_MANIFEST_BYTES
        )
        snapshot_bytes = _decode_member(
            raw_members["snapshot.json"],
            by_name["snapshot.json"],
            batch_snapshot.MAX_SNAPSHOT_BYTES,
        )
    manifest = batch_snapshot._decode_json(
        manifest_bytes, "bundle manifest", MAX_BUNDLE_MANIFEST_BYTES
    )
    manifest = _strict_manifest(manifest)
    snapshot_manifest = batch_snapshot.snapshot_manifest(snapshot_bytes)
    if (
        manifest["snapshot_sha256"] != snapshot_manifest["snapshot_sha256"]
        or manifest["size_bytes"] != len(snapshot_bytes)
        or manifest["mode"] != snapshot_manifest["mode"]
        or manifest["batch_id"] != snapshot_manifest["batch_id"]
        or manifest["batch_revision"] != snapshot_manifest["batch_revision"]
    ):
        _fail("bundle manifest does not match its snapshot")
    return snapshot_bytes


def write_bundle(snapshot_bytes: bytes, destination: Path) -> dict[str, Any]:
    """Validate and write one new archive without replacing an existing path."""
    archive_bytes = pack_bundle(snapshot_bytes)
    destination = Path(destination)
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except (FileExistsError, IsADirectoryError) as exc:
        raise ValueError("bundle destination already exists") from exc
    except OSError as exc:
        raise ValueError("bundle destination cannot be created") from exc
    try:
        handle = os.fdopen(descriptor, "wb")
        descriptor = -1
        with handle:
            handle.write(archive_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        if descriptor != -1:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ValueError("bundle destination could not be written") from exc
    return {
        "sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "size_bytes": len(archive_bytes),
        "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
    }


def read_bundle(source: Path) -> bytes:
    """Read one bounded regular file and validate its complete ZIP contents."""
    source = Path(source)
    descriptor = None
    try:
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            _fail("bundle source must be a regular non-symlink file")
        if info.st_size > batch_snapshot.MAX_ARCHIVE_BYTES:
            _fail("bundle source exceeds its byte limit")
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > batch_snapshot.MAX_ARCHIVE_BYTES:
            _fail("bundle source must be a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            while True:
                chunk = handle.read(min(_CHUNK_BYTES, batch_snapshot.MAX_ARCHIVE_BYTES - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > batch_snapshot.MAX_ARCHIVE_BYTES:
                    _fail("bundle source exceeds its byte limit")
                chunks.append(chunk)
            final = os.fstat(handle.fileno())
            if final.st_size != opened.st_size or total != opened.st_size:
                _fail("bundle source changed while it was being read")
        return unpack_bundle(b"".join(chunks))
    except FileNotFoundError as exc:
        raise ValueError("bundle source was not found") from exc
    except OSError as exc:
        raise ValueError("bundle source could not be read") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


__all__ = [
    "BUNDLE_PROFILE",
    "BUNDLE_SCHEMA_VERSION",
    "MAX_BUNDLE_MANIFEST_BYTES",
    "pack_bundle",
    "unpack_bundle",
    "write_bundle",
    "read_bundle",
]
