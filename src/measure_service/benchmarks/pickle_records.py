"""Restricted, data-only decoder for archived result pickle records.

This module interprets a deliberately small pickle opcode grammar. It does not
load Python objects through :mod:`pickle`, resolve arbitrary globals, or call
serialized constructors. The only supported reductions are NumPy's scalar and
dtype representations, decoded from their literal dtype state and raw bytes.
"""

from __future__ import annotations

import pickletools
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

__all__ = ["ArchiveRecordDecodeError", "decode_archive_record"]

_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_OPERATIONS = 10_000_000
_DEFAULT_MAX_ITEMS = 10_000_000
_DEFAULT_MAX_DEPTH = 128
_DEFAULT_MAX_MEMO = 1_000_000
_DEFAULT_MAX_STACK = 1_000_000

_MARK = object()
_ALLOWED_GLOBALS = {
    ("numpy.core.multiarray", "scalar"),
    ("numpy._core.multiarray", "scalar"),
    ("numpy", "dtype"),
}
_DTYPE_FORMATS = {
    "b1": "?",
    "i1": "b",
    "i2": "h",
    "i4": "i",
    "i8": "q",
    "u1": "B",
    "u2": "H",
    "u4": "I",
    "u8": "Q",
    "f4": "f",
    "f8": "d",
}


class ArchiveRecordDecodeError(ValueError):
    """Raised when a pickle record is malformed or outside the safe grammar."""


@dataclass(frozen=True, slots=True)
class _Global:
    module: str
    name: str


@dataclass(slots=True)
class _NumpyDType:
    code: str
    byteorder: str | None = None
    built: bool = False


class _DataOnlyDecoder:
    def __init__(
        self,
        payload: bytes,
        *,
        max_bytes: int,
        max_operations: int,
        max_items: int,
        max_depth: int,
        max_memo: int,
        max_stack: int,
    ) -> None:
        self.payload = payload
        self.max_operations = max_operations
        self.max_items = max_items
        self.max_depth = max_depth
        self.max_memo = max_memo
        self.max_stack = max_stack
        self.stack: list[Any] = []
        self.memo: dict[int, Any] = {}
        self.item_count = 0
        self.operation_count = 0
        self.protocol = 0
        self.saw_protocol = False
        self.frame_end: int | None = None
        self.last_position = 0

        if len(payload) > max_bytes:
            raise ArchiveRecordDecodeError(
                f"archive pickle exceeds max_bytes ({len(payload)} > {max_bytes})"
            )

    def fail(self, message: str, position: int | None = None) -> None:
        if position is None:
            position = self.last_position
        raise ArchiveRecordDecodeError(
            f"invalid archive pickle at byte offset {position}: {message}"
        )

    def charge_items(self, count: int, position: int) -> None:
        self.item_count += count
        if self.item_count > self.max_items:
            self.fail(f"max_items limit exceeded ({self.max_items})", position)

    def push(self, value: Any, position: int) -> None:
        if len(self.stack) >= self.max_stack:
            self.fail(f"max_stack limit exceeded ({self.max_stack})", position)
        self.stack.append(value)

    def pop(self, count: int, opname: str, position: int) -> list[Any]:
        if count < 0 or len(self.stack) < count:
            self.fail(f"stack underflow in {opname}", position)
        if count == 0:
            return []
        values = self.stack[-count:]
        del self.stack[-count:]
        return values

    def pop_marked(
        self,
        opname: str,
        position: int,
        *,
        key_value_pairs: bool = False,
    ) -> list[Any]:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] is _MARK:
                count = len(self.stack) - index - 1
                if key_value_pairs and count % 2 != 0:
                    self.fail(f"{opname} requires key/value pairs", position)
                self.charge_items(count, position)
                values = self.stack[index + 1 :]
                del self.stack[index:]
                return values
        self.fail(f"missing MARK for {opname}", position)
        raise AssertionError("unreachable")

    def pop_marked_container(
        self,
        expected_type: type,
        opname: str,
        position: int,
        *,
        key_value_pairs: bool = False,
    ) -> list[Any]:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] is _MARK:
                if index == 0 or type(self.stack[index - 1]) is not expected_type:
                    self.fail(f"{opname} requires a {expected_type.__name__}", position)
                count = len(self.stack) - index - 1
                if key_value_pairs and count % 2 != 0:
                    self.fail(f"{opname} requires key/value pairs", position)
                self.charge_items(count, position)
                values = self.stack[index + 1 :]
                del self.stack[index:]
                return values
        self.fail(f"missing MARK for {opname}", position)
        raise AssertionError("unreachable")

    def store_memo(self, index: int, opname: str, position: int) -> None:
        if not self.stack or self.stack[-1] is _MARK:
            self.fail(f"{opname} requires a value on the stack", position)
        if index < 0 or index >= self.max_memo:
            self.fail(f"memo index exceeds max_memo ({self.max_memo})", position)
        if index in self.memo:
            self.fail(f"duplicate memo index {index}", position)
        if len(self.memo) >= self.max_memo:
            self.fail(f"max_memo limit exceeded ({self.max_memo})", position)
        self.memo[index] = self.stack[-1]

    def load_memo(self, index: int, position: int) -> None:
        try:
            value = self.memo[index]
        except KeyError:
            self.fail(f"invalid memo reference {index}", position)
            raise AssertionError("unreachable")
        self.push(value, position)

    def make_global(self, module: str, name: str, position: int) -> _Global:
        if (module, name) not in _ALLOWED_GLOBALS:
            self.fail(f"unsupported global {module}.{name}", position)
        return _Global(module, name)

    def decode_dtype(self, callable_: _Global, args: Any, position: int) -> _NumpyDType:
        if callable_ != _Global("numpy", "dtype"):
            self.fail("unsupported REDUCE callable", position)
        if type(args) is not tuple or len(args) != 3:
            self.fail("numpy.dtype REDUCE requires three literal arguments", position)
        code, align, copy = args
        if type(code) is not str or code not in _DTYPE_FORMATS:
            self.fail("unsupported NumPy scalar dtype", position)
        if type(align) is not bool or type(copy) is not bool or (align, copy) != (False, True):
            self.fail("unsupported numpy.dtype flags", position)
        return _NumpyDType(code)

    def build_dtype(self, dtype: Any, state: Any, position: int) -> None:
        if not isinstance(dtype, _NumpyDType):
            self.fail("unsupported BUILD target", position)
        if dtype.built:
            self.fail("duplicate NumPy dtype BUILD", position)
        if type(state) is not tuple or len(state) != 8:
            self.fail("NumPy dtype BUILD state must contain eight fields", position)
        version, byteorder, subarray, names, fields, _, _, flags = state
        if (
            type(version) is not int
            or version != 3
            or type(byteorder) is not str
            or byteorder not in {"<", ">", "=", "|"}
            or state[2:5] != (None, None, None)
            or type(state[5]) is not int
            or state[5] != -1
            or type(state[6]) is not int
            or state[6] != -1
            or type(flags) is not int
            or flags != 0
        ):
            self.fail("unsupported NumPy dtype BUILD state", position)
        # Keep the bindings above explicit: these are the only accepted dtype
        # state fields, and all subarray/name/field metadata must be absent.
        _ = subarray, names, fields
        if byteorder == "|" and struct.calcsize(_DTYPE_FORMATS[dtype.code]) != 1:
            self.fail("byteorder '|' is only valid for one-byte NumPy scalars", position)
        dtype.byteorder = byteorder
        dtype.built = True

    def decode_numpy_scalar(self, callable_: _Global, args: Any, position: int) -> Any:
        if callable_.name != "scalar" or callable_.module not in {
            "numpy.core.multiarray",
            "numpy._core.multiarray",
        }:
            self.fail("unsupported REDUCE callable", position)
        if type(args) is not tuple or len(args) != 2:
            self.fail("NumPy scalar REDUCE requires dtype and raw bytes", position)
        dtype, raw = args
        if not isinstance(dtype, _NumpyDType) or not dtype.built:
            self.fail("NumPy scalar requires a built literal dtype", position)
        if type(raw) is not bytes:
            self.fail("NumPy scalar payload must be raw bytes", position)
        byteorder = dtype.byteorder
        if byteorder is None:
            self.fail("NumPy scalar dtype has no byteorder state", position)
        format_code = _DTYPE_FORMATS[dtype.code]
        byte_count = struct.calcsize(format_code)
        if len(raw) != byte_count:
            self.fail(f"NumPy scalar byte length does not match dtype {dtype.code}", position)
        prefix = "=" if byteorder == "|" else byteorder
        try:
            return struct.unpack(prefix + format_code, raw)[0]
        except struct.error:
            self.fail("invalid NumPy scalar bytes", position)
            raise AssertionError("unreachable")

    def apply_reduce(self, position: int) -> None:
        callable_, args = self.pop(2, "REDUCE", position)
        if not isinstance(callable_, _Global) or type(args) is not tuple:
            self.fail("unsupported REDUCE operands", position)
        if callable_ == _Global("numpy", "dtype"):
            result = self.decode_dtype(callable_, args, position)
        else:
            result = self.decode_numpy_scalar(callable_, args, position)
        self.push(result, position)

    def add_dict_item(
        self,
        target: dict[Any, Any],
        key: Any,
        value: Any,
        position: int,
        *,
        charge_items: bool = True,
    ) -> None:
        if not _is_literal_dictionary_key(key):
            self.fail("dictionary key is outside the literal data grammar", position)
        try:
            if key in target:
                self.fail("duplicate dictionary key", position)
            if charge_items:
                self.charge_items(2, position)
            target[key] = value
        except TypeError:
            self.fail("unhashable dictionary key", position)

    def check_frame(self, opname: str, arg: Any, position: int) -> None:
        if self.frame_end is not None:
            if position > self.frame_end:
                self.fail("FRAME length ends inside an opcode", position)
            if position == self.frame_end:
                self.frame_end = None
        if opname != "FRAME":
            return
        if self.frame_end is not None:
            self.fail("nested or overlapping FRAME", position)
        if type(arg) is not int or arg <= 0:
            self.fail("FRAME length must be positive", position)
        end = position + 9 + arg
        if end > len(self.payload):
            self.fail("FRAME length exceeds the payload", position)
        self.frame_end = end

    def decode(
        self,
    ) -> dict[Any, Any] | list[Any] | tuple[Any, ...] | str | int | float | bool | None:
        stop_position: int | None = None
        try:
            for opcode, arg, position in pickletools.genops(self.payload):
                self.last_position = position
                self.operation_count += 1
                if self.operation_count > self.max_operations:
                    self.fail(f"max_operations limit exceeded ({self.max_operations})", position)
                opname = opcode.name
                self.check_frame(opname, arg, position)

                if opname == "PROTO":
                    if self.saw_protocol or position != 0 or type(arg) is not int:
                        self.fail("PROTO must appear once at the start", position)
                    if arg > 5:
                        self.fail(f"unsupported pickle protocol {arg}", position)
                    self.protocol = arg
                    self.saw_protocol = True
                    continue
                if opcode.proto > self.protocol:
                    if not self.saw_protocol and self.protocol == 0 and opcode.proto == 1:
                        self.protocol = 1
                    else:
                        self.fail(f"opcode {opname} requires protocol {opcode.proto}", position)
                if opname == "FRAME":
                    continue
                if opname == "STOP":
                    if len(self.stack) != 1 or self.stack[0] is _MARK:
                        self.fail("STOP requires exactly one result value", position)
                    stop_position = position
                    break
                if opname == "MARK":
                    self.push(_MARK, position)
                    continue
                if opname == "NONE":
                    self.push(None, position)
                    continue
                if opname == "NEWTRUE":
                    self.push(True, position)
                    continue
                if opname == "NEWFALSE":
                    self.push(False, position)
                    continue
                if opname in {"INT", "BININT", "BININT1", "BININT2", "LONG", "LONG1", "LONG4"}:
                    if opname == "INT" and type(arg) is bool:
                        self.push(arg, position)
                        continue
                    if type(arg) is not int:
                        self.fail(f"invalid integer argument for {opname}", position)
                    self.push(arg, position)
                    continue
                if opname in {"FLOAT", "BINFLOAT"}:
                    if type(arg) is not float:
                        self.fail(f"invalid float argument for {opname}", position)
                    self.push(arg, position)
                    continue
                if opname in {"UNICODE", "SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8"}:
                    if type(arg) is not str:
                        self.fail(f"invalid string argument for {opname}", position)
                    self.push(arg, position)
                    continue
                if opname in {"SHORT_BINBYTES", "BINBYTES", "BINBYTES8"}:
                    if type(arg) is not bytes:
                        self.fail(f"invalid byte argument for {opname}", position)
                    self.push(arg, position)
                    continue
                if opname == "EMPTY_LIST":
                    self.push([], position)
                    continue
                if opname == "LIST":
                    values = self.pop_marked(opname, position)
                    self.push(values, position)
                    continue
                if opname == "APPEND":
                    if len(self.stack) < 2 or type(self.stack[-2]) is not list:
                        self.fail("APPEND requires a list and one value", position)
                    self.charge_items(1, position)
                    value = self.stack.pop()
                    self.stack[-1].append(value)
                    continue
                if opname == "APPENDS":
                    values = self.pop_marked_container(list, opname, position)
                    self.stack[-1].extend(values)
                    continue
                if opname == "EMPTY_DICT":
                    self.push({}, position)
                    continue
                if opname == "DICT":
                    values = self.pop_marked(opname, position, key_value_pairs=True)
                    result: dict[Any, Any] = {}
                    for index in range(0, len(values), 2):
                        self.add_dict_item(
                            result,
                            values[index],
                            values[index + 1],
                            position,
                            charge_items=False,
                        )
                    self.push(result, position)
                    continue
                if opname == "SETITEM":
                    if len(self.stack) < 3 or type(self.stack[-3]) is not dict:
                        self.fail("SETITEM requires a dict, key and value", position)
                    target, key, value = self.stack[-3:]
                    self.add_dict_item(target, key, value, position)
                    del self.stack[-2:]
                    continue
                if opname == "SETITEMS":
                    values = self.pop_marked_container(dict, opname, position, key_value_pairs=True)
                    target = self.stack[-1]
                    for index in range(0, len(values), 2):
                        self.add_dict_item(
                            target,
                            values[index],
                            values[index + 1],
                            position,
                            charge_items=False,
                        )
                    continue
                if opname == "EMPTY_TUPLE":
                    self.push((), position)
                    continue
                if opname == "TUPLE":
                    values = self.pop_marked(opname, position)
                    self.push(tuple(values), position)
                    continue
                if opname in {"TUPLE1", "TUPLE2", "TUPLE3"}:
                    count = int(opname[-1])
                    self.charge_items(count, position)
                    values = self.pop(count, opname, position)
                    self.push(tuple(values), position)
                    continue
                if opname == "GLOBAL":
                    if type(arg) is not str or " " not in arg:
                        self.fail("invalid GLOBAL name", position)
                    module, name = arg.split(" ", 1)
                    self.push(self.make_global(module, name, position), position)
                    continue
                if opname == "STACK_GLOBAL":
                    module, name = self.pop(2, opname, position)
                    if type(module) is not str or type(name) is not str:
                        self.fail("STACK_GLOBAL requires literal module and name strings", position)
                    self.push(self.make_global(module, name, position), position)
                    continue
                if opname == "REDUCE":
                    self.apply_reduce(position)
                    continue
                if opname == "BUILD":
                    if len(self.stack) < 2:
                        self.fail("BUILD requires an instance and state", position)
                    instance, state = self.stack[-2:]
                    self.build_dtype(instance, state, position)
                    del self.stack[-1:]
                    continue
                if opname == "MEMOIZE":
                    index = len(self.memo)
                    self.store_memo(index, opname, position)
                    continue
                if opname in {"BINPUT", "LONG_BINPUT", "PUT"}:
                    if type(arg) is not int:
                        self.fail(f"invalid memo index for {opname}", position)
                    self.store_memo(arg, opname, position)
                    continue
                if opname in {"BINGET", "LONG_BINGET", "GET"}:
                    if type(arg) is not int:
                        self.fail(f"invalid memo reference for {opname}", position)
                    self.load_memo(arg, position)
                    continue

                self.fail(f"unsupported opcode {opname}", position)
        except ArchiveRecordDecodeError:
            raise
        except (IndexError, OverflowError, struct.error, UnicodeError, ValueError):
            self.fail("malformed or truncated opcode stream")

        if stop_position is None:
            self.fail("missing STOP opcode")
        if stop_position + 1 != len(self.payload):
            self.fail("trailing data after STOP", stop_position)
        if self.frame_end is not None and self.frame_end != len(self.payload):
            self.fail("FRAME length does not end at a valid boundary", stop_position)
        result = self.stack[0]
        _validate_result(result, self.max_depth, self.fail)
        return result


def _is_container(value: Any) -> bool:
    return type(value) in {list, tuple, dict}


def _iter_children(value: Any) -> Iterator[Any]:
    if type(value) is dict:
        for key, item in value.items():
            yield key
            yield item
    else:
        yield from value


def _is_literal_dictionary_key(value: Any) -> bool:
    """Allow scalar keys only, avoiding recursive hashing of tuple DAGs."""
    return type(value) in {str, int, float, bool, type(None)}


def _validate_result(
    root: Any,
    max_depth: int,
    fail: Any,
) -> None:
    """Reject internal values, cycles and excessive container nesting."""
    if not _is_container(root):
        if not _is_plain_scalar(root):
            fail("result contains a value outside the literal data grammar")
        return

    root_id = id(root)
    active = {root_id}
    heights: dict[int, int] = {}
    frames: list[list[Any]] = [[root, iter(_iter_children(root)), 0]]
    while frames:
        value, children, maximum_child_height = frames[-1]
        try:
            child = next(children)
        except StopIteration:
            height = maximum_child_height + 1
            if height > max_depth:
                fail(f"max_depth limit exceeded ({max_depth})")
            heights[id(value)] = height
            active.remove(id(value))
            frames.pop()
            if frames:
                frames[-1][2] = max(frames[-1][2], height)
            continue

        if _is_container(child):
            child_id = id(child)
            if child_id in active:
                fail("result contains a cycle")
            child_height = heights.get(child_id)
            if child_height is None:
                active.add(child_id)
                frames.append([child, iter(_iter_children(child)), 0])
            else:
                frames[-1][2] = max(frames[-1][2], child_height)
        elif not _is_plain_scalar(child):
            fail("result contains a value outside the literal data grammar")


def _is_plain_scalar(value: Any) -> bool:
    return value is None or type(value) in {bool, int, float, str}


def _validate_limit(name: str, value: int, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        relation = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {relation} integer")


def decode_archive_record(
    payload: bytes,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_operations: int = _DEFAULT_MAX_OPERATIONS,
    max_items: int = _DEFAULT_MAX_ITEMS,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    max_memo: int = _DEFAULT_MAX_MEMO,
    max_stack: int = _DEFAULT_MAX_STACK,
) -> dict[Any, Any] | list[Any] | tuple[Any, ...] | str | int | float | bool | None:
    """Decode a literal archive record without executing pickle constructors.

    The decoder accepts Python literal dictionaries, lists, tuples, strings,
    integers, floats, booleans and ``None``. It also recognizes NumPy scalar
    records built from the allowlisted ``numpy.dtype`` and ``multiarray.scalar``
    globals, including their expected dtype state and raw scalar bytes. It
    rejects all other globals, reductions, object builds, persistent IDs,
    extension opcodes, cycles, duplicate dictionary keys and trailing data.

    The limits bound encoded bytes, opcode count, total container elements,
    nesting depth, memo entries and stack size. NaN and infinite floating-point
    values are preserved as Python floats for the caller to handle explicitly.
    """
    if type(payload) is not bytes:
        raise TypeError("payload must be bytes")
    _validate_limit("max_bytes", max_bytes, allow_zero=True)
    _validate_limit("max_operations", max_operations)
    _validate_limit("max_items", max_items, allow_zero=True)
    _validate_limit("max_depth", max_depth, allow_zero=True)
    _validate_limit("max_memo", max_memo, allow_zero=True)
    _validate_limit("max_stack", max_stack)
    decoder = _DataOnlyDecoder(
        payload,
        max_bytes=max_bytes,
        max_operations=max_operations,
        max_items=max_items,
        max_depth=max_depth,
        max_memo=max_memo,
        max_stack=max_stack,
    )
    return decoder.decode()
