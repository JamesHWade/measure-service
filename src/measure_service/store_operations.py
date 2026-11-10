"""Bounded local operator operations for complete SQLite stores.

The backup manifest binds database bytes and structural table counts. It is not
an authenticated signature, and structural checks do not validate scientific
meaning or every stored record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any

from . import batch_schema, design_lineage_schema, import_origins, result_table_schema, storage

DATABASE_NAME = "measure.sqlite3"
MANIFEST_NAME = "manifest.json"
INCOMPLETE_NAME = "INCOMPLETE"
BACKUP_PROFILE = "measure-store-backup/1"
STORE_PROFILE = "measure-store/1"

_MAX_DATABASE_BYTES = 1_000_000_000
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_TABLES = 128
_MAX_SCHEMA_OBJECTS = 512
_MAX_SCHEMA_SQL_BYTES = 1_000_000
_MAX_SCHEMA_NAME_BYTES = 128
_OPERATION_TIMEOUT_SECONDS = 120.0
_SQLITE_BUSY_TIMEOUT_SECONDS = 0.05
_SQLITE_PROGRESS_OPS = 1_000
_HASH_CHUNK_BYTES = 1024 * 1024
_BACKUP_CHUNK_BYTES = 1024 * 1024
_BACKUP_SLEEP_SECONDS = 0.01
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_SOURCE_FILES = {
    DATABASE_NAME,
    f"{DATABASE_NAME}-wal",
    f"{DATABASE_NAME}-shm",
    f"{DATABASE_NAME}-journal",
}

_OPTIONAL_SUGGESTION_TABLES = {
    "campaign_suggestion_attempts",
    "campaign_suggestion_attempt_outcomes",
    "campaign_suggestion_attempt_late_results",
}


@lru_cache(maxsize=1)
def _expected_store_schema() -> dict[str, Any]:
    """Build the deployed physical schema in memory, never from an operator store."""
    from . import campaign_suggestion_attempts

    reference = storage._connect_at(Path(":memory:"))
    try:
        campaign_suggestion_attempts.ensure_schema(reference)
        objects = batch_schema._master_objects(reference)
        tables: dict[str, tuple[Any, ...]] = {}
        for (kind, name), (physical, _table_name, sql) in objects.items():
            if kind != "table" or name.startswith("sqlite_"):
                continue
            tables[name] = (
                batch_schema._table_xinfo(reference, physical),
                batch_schema._normalized_sql(sql),
                batch_schema._index_signature(reference, physical),
                batch_schema._foreign_key_signature(reference, physical),
            )
        indexes = {
            name: batch_schema._normalized_sql(sql)
            for (kind, name), (_physical, table_name, sql) in objects.items()
            if kind == "index"
            and not name.startswith("sqlite_")
            and table_name is not None
            and batch_schema._sqlite_name(table_name) in tables
        }
        index_tables = {
            name: batch_schema._sqlite_name(table_name)
            for (kind, name), (_physical, table_name, _sql) in objects.items()
            if kind == "index"
            and not name.startswith("sqlite_")
            and table_name is not None
            and batch_schema._sqlite_name(table_name) in tables
        }
        return {"tables": tables, "indexes": indexes, "index_tables": index_tables}
    finally:
        reference.close()


class StoreOperationError(RuntimeError):
    """A path-free operator error suitable for command-line display."""


class _DeadlineExpired(Exception):
    pass


def _deadline() -> float:
    return time.monotonic() + _OPERATION_TIMEOUT_SECONDS


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _DeadlineExpired


def _progress_handler(deadline: float):
    return lambda: int(time.monotonic() >= deadline)


def _translate_deadline(message: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if os.name != "posix":
                raise StoreOperationError("Local store operations require a POSIX filesystem")
            try:
                return function(*args, **kwargs)
            except _DeadlineExpired:
                raise StoreOperationError(message) from None

        return wrapped

    return decorate


def _input_path(value: str | os.PathLike[str]) -> Path:
    try:
        raw = Path(os.fspath(value))
    except (TypeError, ValueError, OSError):
        raise StoreOperationError("A valid local directory path is required") from None
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    return raw


def _existing_directory(value: str | os.PathLike[str], *, label: str) -> Path:
    raw = _input_path(value)
    try:
        info = raw.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise StoreOperationError(f"{label} must be a non-symlink directory")
        return raw.resolve(strict=True)
    except StoreOperationError:
        raise
    except (OSError, RuntimeError):
        raise StoreOperationError(f"{label} must be an existing local directory") from None


def _new_directory_path(value: str | os.PathLike[str], *, label: str) -> Path:
    raw = _input_path(value)
    try:
        if not raw.name or os.path.lexists(raw):
            raise StoreOperationError(f"{label} must be a new directory")
        parent = raw.parent.resolve(strict=True)
        if not parent.is_dir():
            raise StoreOperationError(f"{label} parent must be a directory")
        return parent / raw.name
    except StoreOperationError:
        raise
    except (OSError, RuntimeError):
        raise StoreOperationError(f"{label} parent must be an existing local directory") from None


def _assert_disjoint(left: Path, right: Path) -> None:
    if left == right or left in right.parents or right in left.parents:
        raise StoreOperationError("Store and destination paths must be separate")


def _check_store_inventory(root: Path, deadline: float) -> dict[str, os.stat_result]:
    _check_deadline(deadline)
    if os.path.lexists(root / INCOMPLETE_NAME):
        raise StoreOperationError("Store is marked incomplete")
    found: dict[str, os.stat_result] = {}
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                _check_deadline(deadline)
                if entry.name not in _SOURCE_FILES:
                    raise StoreOperationError("Store directory contains unsupported files")
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or entry.is_symlink():
                    raise StoreOperationError("Store directory contains an unsupported file")
                if info.st_nlink != 1 or info.st_size < 0 or info.st_size > _MAX_DATABASE_BYTES:
                    raise StoreOperationError("Store file exceeds the supported bounds")
                found[entry.name] = info
    except StoreOperationError:
        raise
    except OSError:
        raise StoreOperationError("Store directory could not be inspected") from None
    database_info = found.get(DATABASE_NAME)
    if database_info is None:
        raise StoreOperationError("Store database is missing")
    if sum(item.st_size for item in found.values()) > 2 * _MAX_DATABASE_BYTES:
        raise StoreOperationError("Store files exceed the supported bounds")
    return found


def _sqlite_uri(path: Path) -> str:
    return path.as_uri() + "?mode=ro"


def _readonly_connection(path: Path, deadline: float) -> sqlite3.Connection:
    _check_deadline(deadline)
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(
            _sqlite_uri(path),
            uri=True,
            timeout=_SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        con.execute(f"PRAGMA busy_timeout={int(_SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
        con.execute("PRAGMA query_only=ON")
        con.set_progress_handler(_progress_handler(deadline), _SQLITE_PROGRESS_OPS)
        return con
    except (OSError, sqlite3.Error, ValueError):
        if con is not None:
            con.close()
        if time.monotonic() >= deadline:
            raise _DeadlineExpired
        raise StoreOperationError("Store database could not be opened read-only") from None


def _schema_preflight(con: sqlite3.Connection, deadline: float) -> list[str]:
    _check_deadline(deadline)
    row = con.execute(
        """SELECT COUNT(*), COALESCE(SUM(length(CAST(sql AS BLOB))), 0),
                  COALESCE(MAX(length(CAST(name AS BLOB))), 0),
                  SUM(CASE WHEN type='table' THEN 1 ELSE 0 END)
           FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"""
    ).fetchone()
    if row is None:
        raise StoreOperationError("Store schema is incomplete")
    object_count, sql_bytes, max_name_bytes, table_count = (int(value or 0) for value in row)
    if (
        object_count > _MAX_SCHEMA_OBJECTS
        or sql_bytes > _MAX_SCHEMA_SQL_BYTES
        or max_name_bytes > _MAX_SCHEMA_NAME_BYTES
        or table_count > _MAX_TABLES
    ):
        raise StoreOperationError("Store schema exceeds the supported bounds")
    table_rows = con.execute(
        """SELECT name, upper(substr(ltrim(sql), 1, 20))
           FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'
           ORDER BY name"""
    ).fetchall()
    table_names = [row[0] for row in table_rows]
    if any(not isinstance(name, str) for name in table_names):
        raise StoreOperationError("Store schema contains an invalid table name")
    if any(
        isinstance(sql, str) and sql.startswith("CREATE VIRTUAL TABLE") for _, sql in table_rows
    ):
        raise StoreOperationError("Store contains an unsupported virtual table")
    return table_names


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _known_table_signature(
    con: sqlite3.Connection,
    objects: dict[tuple[str, str], tuple[str, str | None, str | None]],
    table: str,
) -> tuple[Any, ...]:
    physical, _table_name, sql = objects[("table", table)]
    return (
        batch_schema._table_xinfo(con, physical),
        batch_schema._normalized_sql(sql),
        batch_schema._index_signature(con, physical),
        batch_schema._foreign_key_signature(con, physical),
    )


def _check_core_schema(con: sqlite3.Connection, table_names: list[str]) -> None:
    expected = _expected_store_schema()
    objects = batch_schema._master_objects(con)
    actual_tables = {batch_schema._sqlite_name(name) for name in table_names}
    expected_tables = set(expected["tables"])
    optional_present = actual_tables & _OPTIONAL_SUGGESTION_TABLES
    if optional_present and optional_present != _OPTIONAL_SUGGESTION_TABLES:
        raise StoreOperationError("Store durable suggestion state is incomplete")
    known_tables = expected_tables - _OPTIONAL_SUGGESTION_TABLES | optional_present
    if not known_tables <= actual_tables:
        raise StoreOperationError("Store is missing a required service table")

    suggestion_objects = {
        name for _kind, name in objects if name.startswith("campaign_suggestion_")
    }
    expected_suggestion_objects = {
        name
        for name in (*_OPTIONAL_SUGGESTION_TABLES, *expected["indexes"])
        if name.startswith("campaign_suggestion_")
    }
    if suggestion_objects - expected_suggestion_objects:
        raise StoreOperationError("Store durable suggestion schema is unsupported")

    for table in known_tables:
        if ("table", table) not in objects:
            raise StoreOperationError("Store contains a malformed service table")
        if _known_table_signature(con, objects, table) != expected["tables"][table]:
            raise StoreOperationError("Store service schema is malformed or out of date")

    expected_indexes = {
        name: sql
        for name, sql in expected["indexes"].items()
        if expected["index_tables"][name] in known_tables
    }
    actual_indexes = {
        name: batch_schema._normalized_sql(sql)
        for (kind, name), (_physical, table_name, sql) in objects.items()
        if kind == "index"
        and not name.startswith("sqlite_")
        and table_name is not None
        and batch_schema._sqlite_name(table_name) in known_tables
    }
    if actual_indexes != expected_indexes:
        raise StoreOperationError("Store service indexes are malformed or out of date")
    if any(
        kind == "trigger"
        and table_name is not None
        and batch_schema._sqlite_name(table_name) in known_tables
        for (kind, _name), (_physical, table_name, _sql) in objects.items()
    ):
        raise StoreOperationError("Store service schema contains an unsupported trigger")


def _check_component_schemas(con: sqlite3.Connection) -> None:
    try:
        if not batch_schema.validate_batch_schema(con):
            raise StoreOperationError("Store batch schema is incomplete")
        design_lineage_schema.validate_design_lineage_schema(con)
        result_table_schema.validate_result_table_schema(con)
        import_origins.validate_import_schema(con)
        # Durable campaign suggestion tables are included in the exact known
        # schema signature, including their active-claim uniqueness index.
    except StoreOperationError:
        raise
    except (sqlite3.Error, ValueError, TypeError, RecursionError):
        raise StoreOperationError("Store component schema is malformed or unsupported") from None


def _validate_database(
    database_path: Path,
    deadline: float,
    *,
    integrity: bool,
    counts: bool,
) -> dict[str, Any]:
    con = _readonly_connection(database_path, deadline)
    try:
        con.execute("BEGIN")
        page_size = int(con.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(con.execute("PRAGMA page_count").fetchone()[0])
        logical_bytes = page_size * page_count
        if page_size <= 0 or page_count <= 0 or logical_bytes > _MAX_DATABASE_BYTES:
            raise StoreOperationError("Store database exceeds the supported size")
        table_names = _schema_preflight(con, deadline)
        _check_core_schema(con, table_names)
        _check_component_schemas(con)

        if integrity:
            _check_deadline(deadline)
            result = con.execute("PRAGMA integrity_check").fetchmany(2)
            if result != [("ok",)]:
                raise StoreOperationError("Store database failed its integrity check")
            _check_deadline(deadline)
            if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StoreOperationError("Store database contains foreign-key violations")

        table_counts: dict[str, int] = {}
        if counts:
            for name in table_names:
                _check_deadline(deadline)
                value = con.execute(f"SELECT COUNT(*) FROM {_quote_identifier(name)}").fetchone()
                if value is None or isinstance(value[0], bool) or not isinstance(value[0], int):
                    raise StoreOperationError("Store table count is invalid")
                table_counts[name] = value[0]
        con.commit()
        return {
            "database_bytes": logical_bytes,
            "page_size": page_size,
            "page_count": page_count,
            "table_counts": table_counts,
        }
    except StoreOperationError:
        if con.in_transaction:
            con.rollback()
        raise
    except _DeadlineExpired:
        if con.in_transaction:
            con.rollback()
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError, RecursionError, OverflowError):
        if con.in_transaction:
            con.rollback()
        raise StoreOperationError("Store database is invalid or unsupported") from None
    finally:
        con.set_progress_handler(None, 0)
        con.close()


def _new_root(value: str | os.PathLike[str], *, label: str) -> Path:
    root = _new_directory_path(value, label=label)
    created = False
    try:
        os.mkdir(root, 0o700)
        created = True
        os.chmod(root, 0o700)
        _sync_directory(root.parent)
        marker = root / INCOMPLETE_NAME
        fd = os.open(
            marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        _sync_directory(root)
        return root
    except StoreOperationError:
        raise
    except OSError:
        if created and os.path.isdir(root) and not os.path.lexists(root / INCOMPLETE_NAME):
            try:
                fd = os.open(
                    root / INCOMPLETE_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                os.close(fd)
            except OSError:
                pass
        raise StoreOperationError(f"Could not create the new {label.lower()}") from None


def _sync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_marker_last(root: Path) -> None:
    marker = root / INCOMPLETE_NAME
    try:
        os.unlink(marker)
        _sync_directory(root)
    except OSError:
        if not os.path.lexists(marker):
            try:
                fd = os.open(
                    marker,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    os.fchmod(fd, 0o600)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                _sync_directory(root)
            except OSError:
                pass
        raise StoreOperationError("Could not finalize the new store safely") from None


def _hash_fd(fd: int, deadline: float, *, limit: int = _MAX_DATABASE_BYTES) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        _check_deadline(deadline)
        block = os.read(fd, _HASH_CHUNK_BYTES)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise StoreOperationError("Backup database exceeds the supported size")
        digest.update(block)
    return digest.hexdigest(), total


def _open_regular_file(path: Path, *, max_bytes: int) -> tuple[int, os.stat_result]:
    fd = -1
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            raise StoreOperationError("Backup contains a linked file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            fd = -1
            raise StoreOperationError("Backup contains a non-regular file")
        if info.st_size < 0 or info.st_size > max_bytes:
            os.close(fd)
            fd = -1
            raise StoreOperationError("Backup file exceeds the supported size")
        return fd, info
    except StoreOperationError:
        if fd >= 0:
            os.close(fd)
        raise
    except OSError:
        if fd >= 0:
            os.close(fd)
        raise StoreOperationError("Backup file could not be opened safely") from None


def _read_fd(fd: int, deadline: float, *, limit: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        _check_deadline(deadline)
        block = os.read(fd, min(_HASH_CHUNK_BYTES, limit + 1 - total))
        if not block:
            break
        chunks.append(block)
        total += len(block)
        if total > limit:
            raise StoreOperationError("Backup manifest exceeds the supported size")
    return b"".join(chunks)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _manifest_from_fd(fd: int, deadline: float) -> dict[str, Any]:
    raw = _read_fd(fd, deadline, limit=_MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(manifest, dict) or set(manifest) != {
            "profile",
            "database",
            "database_bytes",
            "database_sha256",
            "table_counts",
        }:
            raise ValueError("invalid fields")
        if manifest["profile"] != BACKUP_PROFILE or manifest["database"] != DATABASE_NAME:
            raise ValueError("unsupported profile")
        size = manifest["database_bytes"]
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= _MAX_DATABASE_BYTES
        ):
            raise ValueError("invalid byte count")
        digest = manifest["database_sha256"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ValueError("invalid digest")
        table_counts = manifest["table_counts"]
        if not isinstance(table_counts, dict) or len(table_counts) > _MAX_TABLES:
            raise ValueError("invalid table counts")
        for name, count in table_counts.items():
            if (
                not isinstance(name, str)
                or not name
                or len(name.encode("utf-8")) > _MAX_SCHEMA_NAME_BYTES
                or "/" in name
                or "\\" in name
                or isinstance(count, bool)
                or not isinstance(count, int)
                or not 0 <= count <= 2**63 - 1
            ):
                raise ValueError("invalid table count")
        if _canonical_json(manifest) != raw:
            raise ValueError("noncanonical manifest")
        return manifest
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise StoreOperationError("Backup manifest is malformed or unsupported") from None


def _backup_inventory(
    root: Path,
    deadline: float,
    *,
    allow_incomplete: bool = False,
    require_manifest: bool = True,
) -> None:
    _check_deadline(deadline)
    try:
        names: set[str] = set()
        allowed = {DATABASE_NAME, MANIFEST_NAME}
        if allow_incomplete:
            allowed.add(INCOMPLETE_NAME)
        with os.scandir(root) as entries:
            for entry in entries:
                _check_deadline(deadline)
                if entry.name not in allowed:
                    raise StoreOperationError("Backup directory contains unsupported files")
                if entry.is_symlink():
                    raise StoreOperationError("Backup directory contains a linked file")
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise StoreOperationError("Backup directory contains a non-regular file")
                names.add(entry.name)
        expected = {DATABASE_NAME}
        if require_manifest:
            expected.add(MANIFEST_NAME)
        if allow_incomplete and INCOMPLETE_NAME in names:
            expected.add(INCOMPLETE_NAME)
        if names != expected:
            raise StoreOperationError("Backup inventory is incomplete")
    except StoreOperationError:
        raise
    except OSError:
        raise StoreOperationError("Backup directory could not be inspected") from None


def _open_and_verify_backup(
    root: Path, deadline: float, *, allow_incomplete: bool = False
) -> tuple[dict[str, Any], int, int]:
    if not allow_incomplete and os.path.lexists(root / INCOMPLETE_NAME):
        raise StoreOperationError("Backup is marked incomplete")
    _backup_inventory(root, deadline, allow_incomplete=allow_incomplete)
    manifest_fd, manifest_stat = _open_regular_file(
        root / MANIFEST_NAME, max_bytes=_MAX_MANIFEST_BYTES
    )
    database_fd = -1
    try:
        manifest = _manifest_from_fd(manifest_fd, deadline)
        if os.fstat(manifest_fd).st_size != manifest_stat.st_size:
            raise StoreOperationError("Backup manifest changed during verification")
        database_fd, database_stat = _open_regular_file(
            root / DATABASE_NAME, max_bytes=_MAX_DATABASE_BYTES
        )
        if database_stat.st_size != manifest["database_bytes"]:
            raise StoreOperationError("Backup database size does not match its manifest")
        digest, size = _hash_fd(database_fd, deadline)
        if size != manifest["database_bytes"] or digest != manifest["database_sha256"]:
            raise StoreOperationError("Backup database digest does not match its manifest")
        # A sealed backup must be DELETE-mode and self-contained. Reject WAL
        # headers before SQLite opens the artifact, since a read-only WAL open
        # can create SHM bookkeeping files in the backup directory.
        os.lseek(database_fd, 0, os.SEEK_SET)
        header = os.read(database_fd, 100)
        os.lseek(database_fd, 0, os.SEEK_SET)
        if (
            len(header) < 20
            or header[:16] != b"SQLite format 3\x00"
            or header[18:20] != b"\x01\x01"
        ):
            raise StoreOperationError("Backup database is not a standalone SQLite file")
        _validate_database(root / DATABASE_NAME, deadline, integrity=True, counts=False)
        path_stat = (root / DATABASE_NAME).lstat()
        if (path_stat.st_dev, path_stat.st_ino) != (database_stat.st_dev, database_stat.st_ino):
            raise StoreOperationError("Backup database changed during verification")
        digest_after, size_after = _hash_fd(database_fd, deadline)
        if (digest_after, size_after) != (digest, size):
            raise StoreOperationError("Backup database changed during verification")
        _backup_inventory(root, deadline, allow_incomplete=allow_incomplete)
        counts = _validate_database(root / DATABASE_NAME, deadline, integrity=False, counts=True)[
            "table_counts"
        ]
        if counts != manifest["table_counts"]:
            raise StoreOperationError("Backup table counts do not match its manifest")
        return manifest, database_fd, manifest_fd
    except BaseException:
        if database_fd >= 0:
            os.close(database_fd)
        os.close(manifest_fd)
        raise


@_translate_deadline("Store inspection exceeded its time limit")
def inspect_store(data_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Inspect one live local store without bootstrapping or migrating it."""
    deadline = _deadline()
    try:
        root = _existing_directory(data_dir, label="Store path")
        _check_store_inventory(root, deadline)
        report = _validate_database(root / DATABASE_NAME, deadline, integrity=True, counts=True)
        _check_store_inventory(root, deadline)
        return {
            "profile": STORE_PROFILE,
            "status": "valid",
            "database_bytes": report["database_bytes"],
            "table_counts": report["table_counts"],
        }
    except _DeadlineExpired:
        raise StoreOperationError("Store inspection exceeded its time limit") from None


@_translate_deadline("Store initialization exceeded its time limit")
def initialize_store(data_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Create and validate a brand-new local store in a new private directory."""
    deadline = _deadline()
    root = _new_root(data_dir, label="store")
    database_path = root / DATABASE_NAME
    try:
        _check_deadline(deadline)
        con = storage._connect_at(database_path)
        con.close()
        os.chmod(database_path, 0o600)
        _sync_file(database_path)
        _sync_directory(root)
        report = _validate_database(database_path, deadline, integrity=True, counts=True)
        _remove_marker_last(root)
        return {
            "profile": STORE_PROFILE,
            "status": "initialized",
            "database_bytes": report["database_bytes"],
            "table_counts": report["table_counts"],
        }
    except StoreOperationError:
        raise
    except _DeadlineExpired:
        raise StoreOperationError("Store initialization exceeded its time limit") from None
    except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError):
        raise StoreOperationError(
            "New store initialization failed; the directory remains incomplete"
        ) from None


@_translate_deadline("Backup exceeded its bounded time limit")
def backup_store(
    data_dir: str | os.PathLike[str], output_dir: str | os.PathLike[str]
) -> dict[str, Any]:
    """Create a sealed, online-consistent SQLite backup in a new directory."""
    deadline = _deadline()
    source = _existing_directory(data_dir, label="Store path")
    target = _new_directory_path(output_dir, label="Backup path")
    _assert_disjoint(source, target)
    _check_store_inventory(source, deadline)
    # Validate the live source read-only before reserving an output. The final
    # snapshot receives the full structural and integrity checks below.
    _validate_database(source / DATABASE_NAME, deadline, integrity=False, counts=False)
    root = _new_root(target, label="backup")
    database_path = root / DATABASE_NAME
    source_con: sqlite3.Connection | None = None
    target_con: sqlite3.Connection | None = None
    try:
        source_con = _readonly_connection(source / DATABASE_NAME, deadline)
        target_fd = os.open(
            database_path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(target_fd, 0o600)
        finally:
            os.close(target_fd)
        target_con = sqlite3.connect(database_path, timeout=_SQLITE_BUSY_TIMEOUT_SECONDS)
        target_con.execute(f"PRAGMA busy_timeout={int(_SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
        target_con.execute("PRAGMA journal_mode=DELETE")

        page_size = int(source_con.execute("PRAGMA page_size").fetchone()[0])
        if page_size <= 0:
            raise StoreOperationError("Store page size is invalid")
        page_limit = _MAX_DATABASE_BYTES // page_size
        pages_per_step = max(1, min(256, _BACKUP_CHUNK_BYTES // page_size))

        def progress(_status: int, _remaining: int, total: int) -> None:
            _check_deadline(deadline)
            if total > page_limit:
                raise StoreOperationError("Store database exceeds the supported size")

        source_con.backup(
            target_con,
            pages=pages_per_step,
            progress=progress,
            sleep=_BACKUP_SLEEP_SECONDS,
        )
        # A WAL source carries its journal-mode header through SQLite backup.
        # The new artifact is sealed as a standalone DELETE-mode database.
        result = target_con.execute("PRAGMA journal_mode=DELETE").fetchone()
        if result is None or result[0].casefold() != "delete":
            raise StoreOperationError("Could not seal backup database")
        target_con.close()
        target_con = None
        source_con.close()
        source_con = None
        os.chmod(database_path, 0o600)
        size = database_path.stat().st_size
        if size <= 0 or size > _MAX_DATABASE_BYTES:
            raise StoreOperationError("Backup database exceeds the supported size")
        _sync_file(database_path)
        _backup_inventory(root, deadline, allow_incomplete=True, require_manifest=False)
        report = _validate_database(database_path, deadline, integrity=True, counts=True)
        digest_fd, digest_stat = _open_regular_file(database_path, max_bytes=_MAX_DATABASE_BYTES)
        try:
            database_sha256, digest_size = _hash_fd(digest_fd, deadline)
        finally:
            os.close(digest_fd)
        if digest_size != digest_stat.st_size or digest_size != size:
            raise StoreOperationError("Backup database changed while sealing")
        manifest = {
            "profile": BACKUP_PROFILE,
            "database": DATABASE_NAME,
            "database_bytes": size,
            "database_sha256": database_sha256,
            "table_counts": report["table_counts"],
        }
        raw_manifest = _canonical_json(manifest)
        if len(raw_manifest) > _MAX_MANIFEST_BYTES:
            raise StoreOperationError("Backup manifest exceeds the supported size")
        manifest_path = root / MANIFEST_NAME
        manifest_fd = os.open(
            manifest_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(manifest_fd, 0o600)
            view = memoryview(raw_manifest)
            while view:
                _check_deadline(deadline)
                written = os.write(manifest_fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(manifest_fd)
        finally:
            os.close(manifest_fd)
        _sync_directory(root)
        verified, verify_database_fd, verify_manifest_fd = _open_and_verify_backup(
            root, deadline, allow_incomplete=True
        )
        os.close(verify_database_fd)
        os.close(verify_manifest_fd)
        if verified != manifest:
            raise StoreOperationError("Backup manifest changed while sealing")
        _remove_marker_last(root)
        return {
            "profile": BACKUP_PROFILE,
            "status": "created",
            "database_bytes": size,
            "database_sha256": database_sha256,
            "table_counts": report["table_counts"],
        }
    except StoreOperationError:
        raise
    except _DeadlineExpired:
        raise StoreOperationError(
            "Backup exceeded its time limit; the output remains incomplete"
        ) from None
    except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError):
        raise StoreOperationError(
            "Backup failed; the output directory remains incomplete"
        ) from None
    finally:
        if target_con is not None:
            target_con.close()
        if source_con is not None:
            source_con.close()


@_translate_deadline("Backup verification exceeded its time limit")
def verify_backup(backup_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Verify a sealed full-store backup without changing its database bytes."""
    deadline = _deadline()
    try:
        root = _existing_directory(backup_dir, label="Backup path")
        manifest, database_fd, manifest_fd = _open_and_verify_backup(root, deadline)
        try:
            _backup_inventory(root, deadline)
        finally:
            os.close(database_fd)
            os.close(manifest_fd)
        return {
            "profile": BACKUP_PROFILE,
            "status": "verified",
            "database_bytes": manifest["database_bytes"],
            "database_sha256": manifest["database_sha256"],
            "table_counts": manifest["table_counts"],
        }
    except _DeadlineExpired:
        raise StoreOperationError("Backup verification exceeded its time limit") from None


@_translate_deadline("Restore exceeded its bounded time limit")
def restore_store(
    backup_dir: str | os.PathLike[str], data_dir: str | os.PathLike[str]
) -> dict[str, Any]:
    """Copy a verified sealed database byte-for-byte into a new store root."""
    deadline = _deadline()
    backup_root = _existing_directory(backup_dir, label="Backup path")
    target = _new_directory_path(data_dir, label="Restore path")
    _assert_disjoint(backup_root, target)
    manifest, source_fd, manifest_fd = _open_and_verify_backup(backup_root, deadline)
    root: Path | None = None
    try:
        root = _new_root(target, label="store")
        destination = root / DATABASE_NAME
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(destination_fd, 0o600)
            os.lseek(source_fd, 0, os.SEEK_SET)
            copied = 0
            digest = hashlib.sha256()
            while True:
                _check_deadline(deadline)
                block = os.read(source_fd, _HASH_CHUNK_BYTES)
                if not block:
                    break
                copied += len(block)
                if copied > _MAX_DATABASE_BYTES:
                    raise StoreOperationError("Backup database exceeds the supported size")
                digest.update(block)
                view = memoryview(block)
                while view:
                    _check_deadline(deadline)
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
            if (
                copied != manifest["database_bytes"]
                or digest.hexdigest() != manifest["database_sha256"]
            ):
                raise StoreOperationError("Backup changed while restoring")
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
        os.chmod(destination, 0o600)
        _sync_file(destination)
        copied_fd, copied_stat = _open_regular_file(destination, max_bytes=_MAX_DATABASE_BYTES)
        try:
            copied_hash, copied_size = _hash_fd(copied_fd, deadline)
        finally:
            os.close(copied_fd)
        if (
            copied_size != manifest["database_bytes"]
            or copied_hash != manifest["database_sha256"]
            or copied_stat.st_size != copied_size
        ):
            raise StoreOperationError("Restored database does not match the verified backup")
        report = _validate_database(destination, deadline, integrity=True, counts=True)
        if report["table_counts"] != manifest["table_counts"]:
            raise StoreOperationError("Restored database structure does not match its manifest")
        _sync_directory(root)
        _backup_inventory(backup_root, deadline)
        _remove_marker_last(root)
        return {
            "profile": STORE_PROFILE,
            "status": "restored",
            "database_bytes": copied_size,
            "database_sha256": copied_hash,
            "table_counts": report["table_counts"],
        }
    except StoreOperationError:
        raise
    except _DeadlineExpired:
        raise StoreOperationError(
            "Restore exceeded its time limit; the target remains incomplete"
        ) from None
    except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError):
        raise StoreOperationError("Restore failed; the target remains incomplete") from None
    finally:
        os.close(source_fd)
        os.close(manifest_fd)


__all__ = [
    "BACKUP_PROFILE",
    "DATABASE_NAME",
    "INCOMPLETE_NAME",
    "MANIFEST_NAME",
    "STORE_PROFILE",
    "StoreOperationError",
    "backup_store",
    "initialize_store",
    "inspect_store",
    "restore_store",
    "verify_backup",
]
