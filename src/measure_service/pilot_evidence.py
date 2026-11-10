"""Private, bounded local evidence files for an invited pilot workflow.

This module performs local POSIX filesystem I/O only. The evidence directory's
parent must already exist and be trusted. Parent symlinks are resolved before
the leaf is created, so a symlinked parent path is accepted; the new leaf itself
is created relative to the resolved parent with exclusive ``openat``-style
operations. Parent directories are never created recursively.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

from .bounded_json import MAX_JSON_DEPTH, preflight_json

MAX_CHECKPOINT_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_EVENTS_BYTES = 16 * 1024 * 1024
_ALLOWED_JSON_FILES = frozenset({"checkpoint.json", "report.json"})
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIRECTORY_MODE = 0o700


class EvidenceError(ValueError):
    """A bounded evidence I/O error with no user data or path in its message."""


def _check_secure_posix() -> None:
    """Require the POSIX primitives used to bind operations to directory FDs."""
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NONBLOCK")
        or os.open not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.unlink not in os.supports_dir_fd
    ):
        raise EvidenceError("secure evidence I/O requires POSIX directory-relative file operations")


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_flags(access: int) -> int:
    return access | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _open_parent(path: str) -> tuple[int, str]:
    try:
        candidate = Path(path)
        leaf = candidate.name
        if not leaf or leaf in {".", ".."} or os.sep in leaf:
            raise EvidenceError("evidence path must name a new directory leaf")
        parent = candidate.parent.resolve(strict=True)
        parent_fd = os.open(parent, _directory_flags())
    except EvidenceError:
        raise
    except (OSError, TypeError, ValueError, RuntimeError):
        raise EvidenceError("evidence parent must be an existing directory") from None
    if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
        os.close(parent_fd)
        raise EvidenceError("evidence parent must be an existing directory")
    return parent_fd, leaf


def _check_private_regular_file(file_stat: os.stat_result) -> bool:
    return (
        stat.S_ISREG(file_stat.st_mode)
        and file_stat.st_nlink == 1
        and stat.S_IMODE(file_stat.st_mode) == _PRIVATE_FILE_MODE
    )


def _write_all(fd: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("short file write")
        remaining = remaining[written:]


def _canonical_bytes(value: dict[str, Any], label: str, limit: int) -> bytes:
    if type(value) is not dict:
        raise EvidenceError(f"{label} must be a JSON object")

    def reject(_message: str) -> EvidenceError:
        return EvidenceError(f"{label} is not bounded canonical JSON")

    try:
        preflight_json(value, label, max_bytes=limit, fail=reject)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except EvidenceError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise EvidenceError(f"{label} is not bounded canonical JSON") from None
    if len(encoded) > limit:
        raise EvidenceError(f"{label} exceeds the size limit")
    return encoded


def _create_private_file(directory_fd: int, name: str) -> None:
    try:
        fd = os.open(
            name,
            _file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            _PRIVATE_FILE_MODE,
            dir_fd=directory_fd,
        )
    except FileExistsError:
        raise EvidenceError("evidence file already exists") from None
    except OSError:
        raise EvidenceError("evidence file could not be created") from None
    try:
        os.fchmod(fd, _PRIVATE_FILE_MODE)
        if not _check_private_regular_file(os.fstat(fd)):
            raise EvidenceError("evidence file is not a private regular file")
        os.fsync(fd)
    except EvidenceError:
        raise
    except OSError:
        raise EvidenceError("evidence file could not be secured") from None
    finally:
        os.close(fd)


def _sync_directory(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError:
        raise EvidenceError("evidence directory could not be synchronized") from None


class EvidenceDirectory:
    """Create and retain one private, incomplete local evidence directory.

    ``path`` must identify a new leaf under an existing trusted parent. The
    parent path is resolved first, following parent symlinks, and the leaf is
    created with mode 0700. The constructor creates ``INCOMPLETE`` and
    ``events.jsonl`` with mode 0600. Failures leave all created entries in
    place so the incomplete state remains inspectable.
    """

    def __init__(self, path: str):
        _check_secure_posix()
        parent_fd, leaf = _open_parent(path)
        directory_fd = -1
        try:
            try:
                os.mkdir(leaf, _PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
            except FileExistsError:
                raise EvidenceError("evidence directory already exists") from None
            except OSError:
                raise EvidenceError("evidence directory could not be created") from None

            try:
                directory_fd = os.open(leaf, _directory_flags(), dir_fd=parent_fd)
                entry_stat = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                raise EvidenceError("evidence directory could not be opened safely") from None
            directory_stat = os.fstat(directory_fd)
            if not stat.S_ISDIR(entry_stat.st_mode) or (entry_stat.st_dev, entry_stat.st_ino) != (
                directory_stat.st_dev,
                directory_stat.st_ino,
            ):
                raise EvidenceError("evidence directory changed during creation")

            try:
                os.fchmod(directory_fd, _PRIVATE_DIRECTORY_MODE)
            except OSError:
                raise EvidenceError("evidence directory could not be secured") from None
            if stat.S_IMODE(os.fstat(directory_fd).st_mode) != _PRIVATE_DIRECTORY_MODE:
                raise EvidenceError("evidence directory is not private")

            _create_private_file(directory_fd, "INCOMPLETE")
            _create_private_file(directory_fd, "events.jsonl")
            _sync_directory(directory_fd)
            _sync_directory(parent_fd)
        except BaseException:
            if directory_fd >= 0:
                os.close(directory_fd)
            raise
        finally:
            os.close(parent_fd)

        self._directory_fd = directory_fd
        self._lock = threading.RLock()
        self._completed = False
        self._closed = False

    def _require_open_incomplete(self) -> None:
        if self._closed:
            raise EvidenceError("evidence directory is closed")
        if self._completed:
            raise EvidenceError("evidence directory is complete")

    def write_json(self, name: str, value: dict) -> None:
        """Exclusively write one bounded canonical checkpoint or report."""
        if type(name) is not str or name not in _ALLOWED_JSON_FILES:
            raise EvidenceError("evidence filename is not allowed")
        limit = MAX_CHECKPOINT_BYTES
        content = _canonical_bytes(value, "evidence JSON", limit)
        with self._lock:
            self._require_open_incomplete()
            try:
                fd = os.open(
                    name,
                    _file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                    _PRIVATE_FILE_MODE,
                    dir_fd=self._directory_fd,
                )
            except FileExistsError:
                raise EvidenceError("evidence file already exists") from None
            except OSError:
                raise EvidenceError("evidence file could not be created") from None
            try:
                os.fchmod(fd, _PRIVATE_FILE_MODE)
                if not _check_private_regular_file(os.fstat(fd)):
                    raise EvidenceError("evidence file is not a private regular file")
                _write_all(fd, content)
                os.fsync(fd)
                _sync_directory(self._directory_fd)
            except EvidenceError:
                raise
            except OSError:
                raise EvidenceError("evidence JSON could not be written") from None
            finally:
                os.close(fd)

    def append_event(self, value: dict) -> None:
        """Append one bounded canonical JSON object and durably sync the log."""
        content = _canonical_bytes(value, "evidence event", MAX_EVENT_BYTES) + b"\n"
        with self._lock:
            self._require_open_incomplete()
            try:
                fd = os.open(
                    "events.jsonl",
                    _file_flags(os.O_WRONLY | os.O_APPEND),
                    dir_fd=self._directory_fd,
                )
            except OSError:
                raise EvidenceError("evidence event log is unavailable") from None
            try:
                metadata = os.fstat(fd)
                if not _check_private_regular_file(metadata):
                    raise EvidenceError("evidence event log is not a private regular file")
                if metadata.st_size + len(content) > MAX_EVENTS_BYTES:
                    raise EvidenceError("evidence event log exceeds the size limit")
                with os.fdopen(fd, "ab") as stream:
                    fd = -1
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            except EvidenceError:
                raise
            except OSError:
                raise EvidenceError("evidence event could not be written") from None
            finally:
                if fd >= 0:
                    os.close(fd)

    def complete(self) -> None:
        """Remove the incomplete marker and sync the directory entry change."""
        with self._lock:
            self._require_open_incomplete()
            try:
                marker_fd = os.open(
                    "INCOMPLETE",
                    _file_flags(os.O_RDONLY),
                    dir_fd=self._directory_fd,
                )
            except OSError:
                raise EvidenceError("incomplete marker is unavailable") from None
            try:
                if not _check_private_regular_file(os.fstat(marker_fd)):
                    raise EvidenceError("incomplete marker is not a private regular file")
            finally:
                os.close(marker_fd)
            try:
                os.unlink("INCOMPLETE", dir_fd=self._directory_fd)
            except OSError:
                raise EvidenceError("incomplete marker could not be removed") from None
            try:
                _sync_directory(self._directory_fd)
            except EvidenceError:
                # The completed state was not durably published. Restore the
                # failure marker where the filesystem still permits it; an
                # underlying I/O failure can also prevent this repair.
                try:
                    _create_private_file(self._directory_fd, "INCOMPLETE")
                    _sync_directory(self._directory_fd)
                except EvidenceError:
                    pass
                raise
            self._completed = True

    def close(self) -> None:
        """Close the directory handle without changing retained evidence."""
        with self._lock:
            if not self._closed:
                os.close(self._directory_fd)
                self._closed = True

    def __enter__(self) -> EvidenceDirectory:
        with self._lock:
            self._require_open_incomplete()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("checkpoint contains duplicate JSON object keys")
        result[key] = value
    return result


def _reject_nonfinite(token: str) -> None:
    raise EvidenceError("checkpoint contains a non-finite JSON number")


def _check_input_depth(raw: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth - 1 > MAX_JSON_DEPTH:
                raise EvidenceError("checkpoint exceeds the JSON nesting limit")
        elif byte in (0x7D, 0x5D):
            depth -= 1


def _read_bounded(fd: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= maximum:
        chunk = os.read(fd, min(64 * 1024, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > maximum:
        raise EvidenceError("checkpoint exceeds the size limit")
    return b"".join(chunks)


def read_checkpoint(path: str) -> dict:
    """Read one private regular checkpoint without following or reopening it."""
    _check_secure_posix()
    try:
        parent_fd, leaf = _open_parent(path)
    except EvidenceError:
        raise EvidenceError("checkpoint is unavailable") from None
    try:
        try:
            fd = os.open(leaf, _file_flags(os.O_RDONLY), dir_fd=parent_fd)
        except OSError:
            raise EvidenceError("checkpoint is unavailable") from None
    finally:
        os.close(parent_fd)

    try:
        metadata = os.fstat(fd)
        if not _check_private_regular_file(metadata):
            raise EvidenceError("checkpoint is not a private regular file")
        if metadata.st_size > MAX_CHECKPOINT_BYTES:
            raise EvidenceError("checkpoint exceeds the size limit")
        raw = _read_bounded(fd, MAX_CHECKPOINT_BYTES)
    except EvidenceError:
        raise
    except OSError:
        raise EvidenceError("checkpoint could not be read safely") from None
    finally:
        os.close(fd)

    try:
        _check_input_depth(raw)
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
        if type(value) is not dict:
            raise EvidenceError("checkpoint must be a JSON object")

        def reject(_message: str) -> EvidenceError:
            return EvidenceError("checkpoint is not valid bounded JSON")

        preflight_json(value, "checkpoint", max_bytes=MAX_CHECKPOINT_BYTES, fail=reject)
    except EvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise EvidenceError("checkpoint is not valid bounded JSON") from None
    return value


__all__ = ["EvidenceDirectory", "EvidenceError", "read_checkpoint"]
