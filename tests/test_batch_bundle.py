from __future__ import annotations

import io
import os
import stat
import struct
import time
import zipfile
import zlib

import pytest
from snapshot_store_support import make_snapshot_store


def _custom_bundle(
    snapshot_bytes,
    *,
    snapshot_payload=None,
    snapshot_compression=zipfile.ZIP_DEFLATED,
    manifest_name="manifest.json",
    snapshot_name="snapshot.json",
    manifest_attr=None,
    snapshot_attr=None,
):
    from measure_service import batch_bundle

    manifest = batch_bundle._manifest_bytes(batch_bundle._manifest_for_snapshot(snapshot_bytes))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        manifest_info = zipfile.ZipInfo(manifest_name)
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        if manifest_attr is not None:
            manifest_info.external_attr = manifest_attr
        archive.writestr(manifest_info, manifest)
        snapshot_info = zipfile.ZipInfo(snapshot_name)
        snapshot_info.compress_type = snapshot_compression
        if snapshot_attr is not None:
            snapshot_info.external_attr = snapshot_attr
        archive.writestr(
            snapshot_info,
            snapshot_bytes if snapshot_payload is None else snapshot_payload,
        )
    return output.getvalue()


def _central_positions(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        cursor = archive.start_dir
        positions = {}
        for info in infos:
            positions[info.filename] = cursor
            name_length = struct.unpack_from("<H", data, cursor + 28)[0]
            extra_length = struct.unpack_from("<H", data, cursor + 30)[0]
            comment_length = struct.unpack_from("<H", data, cursor + 32)[0]
            cursor += 46 + name_length + extra_length + comment_length
    return infos, positions


def _set_member_flags(data, name, flags):
    value = bytearray(data)
    infos, central = _central_positions(value)
    info = next(item for item in infos if item.filename == name)
    struct.pack_into("<H", value, info.header_offset + 6, flags)
    struct.pack_into("<H", value, central[name] + 8, flags)
    return bytes(value)


def _set_member_lengths(data, name, *, size, crc):
    value = bytearray(data)
    infos, central = _central_positions(value)
    info = next(item for item in infos if item.filename == name)
    struct.pack_into("<I", value, info.header_offset + 14, crc)
    struct.pack_into("<I", value, info.header_offset + 22, size)
    struct.pack_into("<I", value, central[name] + 16, crc)
    struct.pack_into("<I", value, central[name] + 24, size)
    return bytes(value)


def _stored_bundle(snapshot_bytes):
    return _custom_bundle(snapshot_bytes, snapshot_compression=zipfile.ZIP_STORED)


def _member_data_bounds(data, name):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = next(item for item in archive.infolist() if item.filename == name)
    name_length = struct.unpack_from("<H", data, info.header_offset + 26)[0]
    extra_length = struct.unpack_from("<H", data, info.header_offset + 28)[0]
    start = info.header_offset + 30 + name_length + extra_length
    return start, start + info.compress_size


@pytest.fixture
def snapshot_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    from measure_service.batch_capture import capture_snapshot

    state = make_snapshot_store()
    return capture_snapshot(
        "owner", state["batch_id"], state["batch"]["revision"], "self_contained"
    )


def test_bundle_is_deterministic_and_roundtrips(snapshot_bytes):
    from measure_service.batch_bundle import pack_bundle, unpack_bundle

    first = pack_bundle(snapshot_bytes)
    second = pack_bundle(snapshot_bytes)
    assert first == second
    assert unpack_bundle(first) == snapshot_bytes
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert {info.filename for info in archive.infolist()} == {"manifest.json", "snapshot.json"}
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())


def test_bundle_detects_corruption_and_unexpected_members(snapshot_bytes):
    from measure_service.batch_bundle import pack_bundle, unpack_bundle

    original = pack_bundle(snapshot_bytes)
    with zipfile.ZipFile(io.BytesIO(original)) as source:
        manifest = source.read("manifest.json")
        corrupted_snapshot = bytearray(source.read("snapshot.json"))
    corrupted_snapshot[-1] ^= 1
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(rebuilt, "w") as output:
        output.writestr("manifest.json", manifest)
        output.writestr("snapshot.json", bytes(corrupted_snapshot))
    with pytest.raises(ValueError):
        unpack_bundle(rebuilt.getvalue())

    extra = io.BytesIO()
    with zipfile.ZipFile(extra, "w") as output:
        output.writestr("manifest.json", b"{}")
        output.writestr("snapshot.json", snapshot_bytes)
        output.writestr("unexpected", b"no")
    with pytest.raises(ValueError):
        unpack_bundle(extra.getvalue())

    with pytest.raises(ValueError):
        unpack_bundle(pack_bundle(snapshot_bytes) + b"trailing")


def test_bundle_file_io_is_exclusive_and_rejects_symlinks(snapshot_bytes, tmp_path):
    from measure_service.batch_bundle import read_bundle, write_bundle

    destination = tmp_path / "snapshot.zip"
    result = write_bundle(snapshot_bytes, destination)
    assert result["snapshot_sha256"]
    assert read_bundle(destination) == snapshot_bytes
    with pytest.raises(ValueError):
        write_bundle(snapshot_bytes, destination)

    symlink = tmp_path / "link.zip"
    symlink.symlink_to(destination)
    with pytest.raises(ValueError):
        read_bundle(symlink)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"manifest_name": "../manifest.json"},
        {"snapshot_name": "nested/snapshot.json"},
        {"manifest_attr": (stat.S_IFLNK | 0o777) << 16},
        {"snapshot_attr": (stat.S_IFLNK | 0o777) << 16},
        {"snapshot_compression": zipfile.ZIP_BZIP2},
    ],
)
def test_bundle_rejects_unsafe_members(snapshot_bytes, kwargs):
    from measure_service.batch_bundle import unpack_bundle

    with pytest.raises(ValueError):
        unpack_bundle(_custom_bundle(snapshot_bytes, **kwargs))


def test_bundle_rejects_duplicate_and_encrypted_members(snapshot_bytes):
    from measure_service import batch_bundle
    from measure_service.batch_bundle import unpack_bundle

    manifest = batch_bundle._manifest_bytes(batch_bundle._manifest_for_snapshot(snapshot_bytes))
    output = io.BytesIO()
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("manifest.json", manifest)
            archive.writestr("manifest.json", manifest)
            archive.writestr("snapshot.json", snapshot_bytes)
    with pytest.raises(ValueError):
        unpack_bundle(output.getvalue())

    encrypted = _set_member_flags(batch_bundle.pack_bundle(snapshot_bytes), "snapshot.json", 1)
    with pytest.raises(ValueError):
        unpack_bundle(encrypted)


def test_bundle_checks_crc_lengths_and_actual_decompressed_stream(snapshot_bytes):
    from measure_service import batch_bundle
    from measure_service.batch_bundle import unpack_bundle

    stored = bytearray(_stored_bundle(snapshot_bytes))
    start, _ = _member_data_bounds(stored, "snapshot.json")
    stored[start] ^= 1
    with pytest.raises(ValueError):
        unpack_bundle(bytes(stored))

    forged = _set_member_lengths(
        _stored_bundle(snapshot_bytes),
        "snapshot.json",
        size=len(snapshot_bytes) - 1,
        crc=zlib.crc32(snapshot_bytes) & 0xFFFFFFFF,
    )
    with pytest.raises(ValueError):
        unpack_bundle(forged)

    hidden = _custom_bundle(snapshot_bytes, snapshot_payload=snapshot_bytes + b"x" * 1_000_000)
    hidden = _set_member_lengths(
        hidden,
        "snapshot.json",
        size=len(snapshot_bytes),
        crc=zlib.crc32(snapshot_bytes) & 0xFFFFFFFF,
    )
    with pytest.raises(ValueError):
        unpack_bundle(hidden)

    oversized = _custom_bundle(
        snapshot_bytes,
        snapshot_payload=b"0" * (batch_bundle.batch_snapshot.MAX_SNAPSHOT_BYTES + 1),
    )
    with pytest.raises(ValueError):
        unpack_bundle(oversized)


def test_bundle_rejects_local_extras_prefix_and_bad_eocd(snapshot_bytes):
    from measure_service.batch_bundle import pack_bundle, unpack_bundle

    original = pack_bundle(snapshot_bytes)
    value = bytearray(original)
    extra = b"xx"
    name_length = struct.unpack_from("<H", value, 26)[0]
    insertion = 30 + name_length
    struct.pack_into("<H", value, 28, len(extra))
    value[insertion:insertion] = extra
    with pytest.raises(ValueError):
        unpack_bundle(bytes(value))

    with pytest.raises(ValueError):
        unpack_bundle(b"prefix" + original)

    broken = bytearray(original)
    eocd = broken.rfind(b"PK\x05\x06")
    struct.pack_into("<H", broken, eocd + 4, 1)
    with pytest.raises(ValueError):
        unpack_bundle(bytes(broken))


def test_bundle_preflights_physical_central_directory_before_zipfile(snapshot_bytes, monkeypatch):
    from measure_service import batch_bundle
    from measure_service.batch_bundle import pack_bundle, unpack_bundle

    original = pack_bundle(snapshot_bytes)
    eocd = original.rfind(b"PK\x05\x06")
    central_offset = struct.unpack_from("<I", original, eocd + 16)[0]
    central_size = struct.unpack_from("<I", original, eocd + 12)[0]
    first_name_length = struct.unpack_from("<H", original, central_offset + 28)[0]
    first_extra_length = struct.unpack_from("<H", original, central_offset + 30)[0]
    first_comment_length = struct.unpack_from("<H", original, central_offset + 32)[0]
    first_record_end = (
        central_offset + 46 + first_name_length + first_extra_length + first_comment_length
    )
    first_record = original[central_offset:first_record_end]
    extra_records = first_record * 100_000
    central = original[central_offset:eocd] + extra_records
    eocd_record = bytearray(original[eocd:])
    struct.pack_into("<I", eocd_record, 12, central_size + len(extra_records))
    malicious = original[:central_offset] + central + bytes(eocd_record)

    constructed = []

    def forbidden_constructor(*args, **kwargs):
        constructed.append((args, kwargs))
        raise AssertionError("ZipFile must not be constructed for an oversized central directory")

    monkeypatch.setattr(batch_bundle.zipfile, "ZipFile", forbidden_constructor)
    with pytest.raises(ValueError, match="central"):
        unpack_bundle(malicious)
    assert constructed == []


def test_bundle_reader_defends_against_fifo_swap(snapshot_bytes, tmp_path, monkeypatch):
    from measure_service import batch_bundle
    from measure_service.batch_bundle import read_bundle

    target = tmp_path / "raced.zip"
    target.write_bytes(batch_bundle.pack_bundle(snapshot_bytes))
    original_open = batch_bundle.os.open

    def swap(path, flags, *args, **kwargs):
        if os.fspath(path) == os.fspath(target):
            target.unlink()
            os.mkfifo(target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(batch_bundle.os, "open", swap)
    started = time.monotonic()
    with pytest.raises(ValueError):
        read_bundle(target)
    assert time.monotonic() - started < 1


def test_bundle_write_failure_does_not_delete_replacement(snapshot_bytes, tmp_path, monkeypatch):
    from measure_service import batch_bundle
    from measure_service.batch_bundle import write_bundle

    target = tmp_path / "replacement.zip"

    def fail_fsync(_descriptor):
        target.unlink()
        target.write_text("caller replacement")
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(batch_bundle.os, "fsync", fail_fsync)
    with pytest.raises(ValueError):
        write_bundle(snapshot_bytes, target)
    assert target.read_text() == "caller replacement"
