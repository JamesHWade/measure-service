"""Security and data-shape tests for restricted archived pickle decoding."""

from __future__ import annotations

import math
import pickle
import shlex
import struct

import pytest

from measure_service.benchmarks.pickle_records import (
    ArchiveRecordDecodeError,
    decode_archive_record,
)


def _short_unicode(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 255:
        raise ValueError("fixture string is too long")
    return b"\x8c" + bytes([len(encoded)]) + encoded


def _integer(value: int) -> bytes:
    if 0 <= value <= 255:
        return b"K" + bytes([value])
    return b"J" + struct.pack("<i", value)


def _literal(value: int | str | None) -> bytes:
    if value is None:
        return b"N"
    if type(value) is int:
        return _integer(value)
    if type(value) is str:
        return _short_unicode(value)
    raise TypeError(f"unsupported test literal {type(value).__name__}")


def _numpy_scalar_pickle(
    raw: bytes,
    *,
    dtype: str = "f8",
    byteorder: str = "<",
    state: tuple[object, ...] | None = None,
) -> bytes:
    if state is None:
        state = (3, byteorder, None, None, None, -1, -1, 0)
    state_bytes = b"(" + b"".join(_literal(value) for value in state) + b"t"
    scalar = _short_unicode("numpy.core.multiarray") + _short_unicode("scalar") + b"\x93"
    dtype_reduce = (
        _short_unicode("numpy")
        + _short_unicode("dtype")
        + b"\x93"
        + _short_unicode(dtype)
        + b"\x89\x88\x87R"
    )
    raw_bytes = b"C" + bytes([len(raw)]) + raw
    return b"\x80\x04" + scalar + dtype_reduce + state_bytes + b"b" + raw_bytes + b"\x86R."


@pytest.mark.parametrize("protocol", [0, 1, 2, 3, 4, 5])
def test_decodes_literal_nested_records_and_preserves_shared_references(protocol):
    shared = [2, "x"]
    record = {
        "label": "synthetic",
        "count": 3,
        "score": 1.25,
        "flags": (True, False, None),
        "shared_a": shared,
        "shared_b": shared,
    }

    decoded = decode_archive_record(pickle.dumps(record, protocol=protocol))

    assert decoded == record
    assert decoded["shared_a"] is decoded["shared_b"]


def test_decodes_numpy_float64_from_literal_dtype_state_and_raw_bytes():
    value = 12.75
    payload = _numpy_scalar_pickle(struct.pack("<d", value))

    assert decode_archive_record(payload) == value


def test_numpy_float64_byteorder_is_read_from_dtype_state():
    value = -0.125

    decoded = decode_archive_record(
        _numpy_scalar_pickle(
            struct.pack(">d", value),
            byteorder=">",
        )
    )

    assert decoded == value


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_nonfinite_numpy_floats_are_preserved_for_explicit_caller_handling(value):
    decoded = decode_archive_record(_numpy_scalar_pickle(struct.pack("<d", value)))

    if math.isnan(value):
        assert math.isnan(decoded)
    else:
        assert decoded == value


def test_rejects_numpy_dtype_state_with_wrong_shape_or_values():
    valid_prefix = (3, "<", None, None, None, -1, -1, 0)
    invalid_states = [
        valid_prefix[:-1],
        (2, *valid_prefix[1:]),
        (3, "?", *valid_prefix[2:]),
    ]

    for state in invalid_states:
        with pytest.raises(ArchiveRecordDecodeError, match="dtype BUILD state"):
            decode_archive_record(_numpy_scalar_pickle(struct.pack("<d", 1.0), state=state))


def test_rejects_unsupported_dtype_and_wrong_raw_byte_length():
    with pytest.raises(ArchiveRecordDecodeError, match="unsupported NumPy scalar dtype"):
        decode_archive_record(_numpy_scalar_pickle(struct.pack("<d", 1.0), dtype="O8"))

    with pytest.raises(ArchiveRecordDecodeError, match="byte length"):
        decode_archive_record(_numpy_scalar_pickle(b"short"))


@pytest.mark.parametrize("use_stack_global", [False, True])
def test_rejects_unrecognized_reduction_without_executing_it(tmp_path, use_stack_global):
    marker = tmp_path / "should-not-exist"
    command = f"touch {shlex.quote(str(marker))}"
    if use_stack_global:
        global_opcode = _short_unicode("posix") + _short_unicode("system") + b"\x93"
    else:
        global_opcode = b"cposix\nsystem\n"
    payload = b"\x80\x04" + global_opcode + _short_unicode(command) + b"\x85R."

    with pytest.raises(ArchiveRecordDecodeError, match="unsupported global posix.system"):
        decode_archive_record(payload)

    assert not marker.exists()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"\x80\x04N)R.", "unsupported REDUCE operands"),
        (b"\x80\x04NNb.", "unsupported BUILD target"),
    ],
)
def test_rejects_every_non_numpy_reduce_or_build(payload, message):
    with pytest.raises(ArchiveRecordDecodeError, match=message):
        decode_archive_record(payload)


def test_rejects_memo_references_to_missing_entries():
    with pytest.raises(ArchiveRecordDecodeError, match="invalid memo reference"):
        decode_archive_record(b"\x80\x04h\x00.")

    duplicate_memo = b"\x80\x04K\x01q\x00K\x02q\x00."
    with pytest.raises(ArchiveRecordDecodeError, match="duplicate memo index"):
        decode_archive_record(duplicate_memo)


def test_rejects_cycles_but_accepts_shared_acyclic_values():
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ArchiveRecordDecodeError, match="cycle"):
        decode_archive_record(pickle.dumps(cyclic, protocol=4))

    shared = {"value": 1}
    decoded = decode_archive_record(pickle.dumps((shared, shared), protocol=4))
    assert decoded[0] is decoded[1]


def test_rejects_duplicate_dictionary_keys():
    payload = b"\x80\x04} (K\x01K\x02K\x01K\x03u.".replace(b" ", b"")

    with pytest.raises(ArchiveRecordDecodeError, match="duplicate dictionary key"):
        decode_archive_record(payload)


def test_rejects_memoized_tuple_dag_dictionary_keys_before_hashing():
    payload = b"\x80\x04}K\x01\x85\x94"
    for memo_index in range(30):
        payload += b"h" + bytes([memo_index]) + b"\x86\x94"
    payload += b"K\x00s."

    with pytest.raises(ArchiveRecordDecodeError, match="dictionary key is outside"):
        decode_archive_record(payload, max_items=1000, max_operations=1000)


def test_rejects_bytes_that_would_escape_the_data_only_result():
    with pytest.raises(ArchiveRecordDecodeError, match="literal data grammar"):
        decode_archive_record(pickle.dumps(b"not an archive scalar", protocol=4))


def test_rejects_truncated_trailing_and_invalid_frame_data():
    valid = pickle.dumps({"ok": [1]}, protocol=4)

    with pytest.raises(ArchiveRecordDecodeError, match="FRAME length exceeds"):
        decode_archive_record(valid[:-1])
    with pytest.raises(ArchiveRecordDecodeError, match="malformed or truncated"):
        decode_archive_record(b"\x80\x04K\x01")
    with pytest.raises(ArchiveRecordDecodeError, match="trailing data"):
        decode_archive_record(valid + b".")

    # The frame ends after the first byte of BININT1, splitting its operand.
    split_opcode = b"\x80\x04\x95" + struct.pack("<Q", 1) + b"K\x01."
    with pytest.raises(ArchiveRecordDecodeError, match="FRAME length ends inside"):
        decode_archive_record(split_opcode)

    oversized_frame = b"\x80\x04\x95" + struct.pack("<Q", 100) + b"K\x01."
    with pytest.raises(ArchiveRecordDecodeError, match="FRAME length exceeds"):
        decode_archive_record(oversized_frame)


def test_enforces_byte_operation_item_memo_stack_and_depth_limits():
    payload = pickle.dumps({"values": [1, 2, 3]}, protocol=4)

    with pytest.raises(ArchiveRecordDecodeError, match="max_bytes"):
        decode_archive_record(payload, max_bytes=1)
    with pytest.raises(ArchiveRecordDecodeError, match="max_operations"):
        decode_archive_record(payload, max_operations=1)
    with pytest.raises(ArchiveRecordDecodeError, match="max_items"):
        decode_archive_record(payload, max_items=0)
    with pytest.raises(ArchiveRecordDecodeError, match="max_memo"):
        decode_archive_record(payload, max_memo=0)
    with pytest.raises(ArchiveRecordDecodeError, match="max_stack"):
        decode_archive_record(payload, max_stack=1)
    with pytest.raises(ArchiveRecordDecodeError, match="max_depth"):
        decode_archive_record(pickle.dumps([[[1]]], protocol=4), max_depth=2)


def test_limit_arguments_must_be_integer_bounds():
    with pytest.raises(ValueError, match="max_operations must be a positive integer"):
        decode_archive_record(b".", max_operations=True)
