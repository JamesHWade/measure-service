"""Owner-scoped provenance for datasets and analyses imported from snapshots.

The import-origin component is deliberately optional.  Ordinary service
stores do not create these tables; :func:`measure_service.batch_restore`
creates and validates them only while restoring a self-contained archive.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import Any

from .batch_schema import (
    _foreign_key_signature,
    _index_signature,
    _master_objects,
    _normalized_sql,
    _sqlite_name,
    _table_xinfo,
)
from .storage import canonical, sha256

PROFILE = "measure-analytical-import-origin/1"
RESTORE_PROFILE = "measure-chromatogram-batch-restore/1"
SCHEMA_NAME = "measure-analytical-import-origin"
SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 64 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KINDS = {"dataset", "analysis"}
_DESCRIPTOR_FIELDS = {
    "profile",
    "import_id",
    "snapshot_sha256",
    "source_batch_id",
    "source_record_id",
    "local_record_id",
    "record_kind",
    "imported_at",
    "original_record_sha256",
    "preserved_content_sha256",
}
_RECEIPT_REQUIRED = {
    "profile",
    "schema_version",
    "snapshot_sha256",
    "source_batch_id",
    "new_batch_id",
    "import_id",
    "restored_at",
    "dataset_map",
    "analysis_map",
    "limits",
    "no_execution_performed",
}
_LIMIT_FIELDS = {"max_snapshot_bytes", "max_archive_bytes"}
_EXPECTED_LIMITS = {
    "max_snapshot_bytes": 64 * 1024 * 1024,
    "max_archive_bytes": 64 * 1024 * 1024 + 256 * 1024,
}

_IMPORT_TABLES = (
    "measure_import_schema",
    "analytical_imports",
    "analytical_import_records",
)
_IMPORT_INDEXES = ("analytical_import_records_owner_lookup",)
_IMPORT_DDL_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS measure_import_schema (
      name TEXT PRIMARY KEY, version INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS analytical_imports (
      id TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      profile TEXT NOT NULL,
      snapshot_sha256 TEXT NOT NULL,
      source_batch_id TEXT NOT NULL,
      new_batch_id TEXT NOT NULL,
      restored_at TEXT NOT NULL,
      receipt TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS analytical_import_records (
      import_id TEXT NOT NULL,
      owner TEXT NOT NULL,
      record_kind TEXT NOT NULL CHECK(record_kind IN ('dataset', 'analysis')),
      source_record_id TEXT NOT NULL,
      local_record_id TEXT NOT NULL,
      imported_at TEXT NOT NULL,
      original_record_sha256 TEXT NOT NULL,
      preserved_content_sha256 TEXT NOT NULL,
      PRIMARY KEY(import_id, record_kind, local_record_id),
      UNIQUE(owner, record_kind, local_record_id),
      FOREIGN KEY(import_id) REFERENCES analytical_imports(id)
    )""",
    """CREATE INDEX IF NOT EXISTS analytical_import_records_owner_lookup
      ON analytical_import_records(owner, record_kind, local_record_id)""",
)


def _text(value: Any, label: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be nonempty text")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 value")
    return value


def dataset_core_sha256(
    source_record_id: str,
    input_sha256: str,
    created_at: str,
    payload: Mapping[str, Any],
    source: Mapping[str, Any] | None,
) -> str:
    """Hash the complete portable identity of one original dataset.

    The source identifier is deliberately supplied separately from the local
    identifier.  A restore can therefore verify that a new local row still
    represents the exact source record without copying the source row's ID.
    """
    return sha256(
        {
            "dataset_id": source_record_id,
            "input_sha256": input_sha256,
            "created_at": created_at,
            "payload": payload,
            "source": source,
        }
    )


def analysis_core_sha256(
    source_record_id: str,
    source_dataset_id: str,
    trace_id: str,
    kind: str,
    method: Mapping[str, Any],
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
    created_at: str,
) -> str:
    """Hash the complete portable identity of one original analysis."""
    return sha256(
        {
            "analysis_id": source_record_id,
            "dataset_id": source_dataset_id,
            "trace_id": trace_id,
            "kind": kind,
            "method": method,
            "result": result,
            "provenance": provenance,
            "created_at": created_at,
        }
    )


def _owner(owner: Any) -> str:
    return _text(owner, "owner")


def _kind(record_kind: Any) -> str:
    if record_kind not in _KINDS:
        raise ValueError("record_kind must be dataset or analysis")
    return record_kind


def _decode(serialized: Any, label: str, *, maximum: int = MAX_RECEIPT_BYTES) -> Any:
    if not isinstance(serialized, str) or len(serialized.encode("utf-8")) > maximum:
        raise ValueError(f"Stored {label} exceeds its byte limit")
    try:
        value = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Stored {label} is malformed") from exc
    if canonical(value) != serialized:
        raise ValueError(f"Stored {label} is not canonically encoded")
    return value


def _validate_receipt_memberships(
    con: sqlite3.Connection, owner: str, receipt: Mapping[str, Any]
) -> None:
    expected = {
        (record_kind, source_id, local_id, receipt["restored_at"])
        for record_kind in ("dataset", "analysis")
        for source_id, local_id in receipt[f"{record_kind}_map"].items()
    }
    actual = {
        tuple(row)
        for row in con.execute(
            "SELECT record_kind, source_record_id, local_record_id, imported_at "
            "FROM analytical_import_records WHERE owner=? AND import_id=?",
            (owner, receipt["import_id"]),
        ).fetchall()
    }
    if actual != expected:
        raise ValueError("Stored import receipt memberships do not match its maps")


def _validate_descriptor(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _DESCRIPTOR_FIELDS:
        raise ValueError("Import origin has an unexpected shape")
    descriptor = dict(value)
    if descriptor["profile"] != PROFILE:
        raise ValueError("Unsupported import-origin profile")
    _text(descriptor["import_id"], "import_id")
    _sha(descriptor["snapshot_sha256"], "snapshot_sha256")
    _text(descriptor["source_batch_id"], "source_batch_id")
    _text(descriptor["source_record_id"], "source_record_id")
    _text(descriptor["local_record_id"], "local_record_id")
    _kind(descriptor["record_kind"])
    _text(descriptor["imported_at"], "imported_at", maximum=100)
    _sha(descriptor["original_record_sha256"], "original_record_sha256")
    _sha(descriptor["preserved_content_sha256"], "preserved_content_sha256")
    return descriptor


def _validate_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RECEIPT_REQUIRED:
        raise ValueError("Import receipt has an unexpected shape")
    receipt = dict(value)
    if receipt["profile"] != RESTORE_PROFILE or receipt["schema_version"] != "1.0":
        raise ValueError("Unsupported batch-restore receipt profile")
    for key in ("snapshot_sha256",):
        _sha(receipt[key], key)
    for key in ("source_batch_id", "new_batch_id", "import_id", "restored_at"):
        _text(receipt[key], key, maximum=100 if key == "restored_at" else 200)
    if receipt["no_execution_performed"] is not True:
        raise ValueError("Import receipt must declare no_execution_performed")
    for key in ("dataset_map", "analysis_map"):
        mapping = receipt[key]
        if not isinstance(mapping, dict):
            raise ValueError(f"{key} must be a JSON object")
        if key == "dataset_map" and not mapping:
            raise ValueError("dataset_map must contain at least one record")
        if len(mapping) > 32:
            raise ValueError(f"{key} exceeds the snapshot record bound")
        local_ids = set()
        for source, local in mapping.items():
            _text(source, f"{key} source id")
            _text(local, f"{key} local id")
            if local in local_ids:
                raise ValueError(f"{key} local IDs must be unique")
            local_ids.add(local)
    if (
        not isinstance(receipt["limits"], dict)
        or set(receipt["limits"]) != _LIMIT_FIELDS
        or receipt["limits"] != _EXPECTED_LIMITS
    ):
        raise ValueError("limits do not match the supported restore budgets")
    return receipt


def _create_component_schema(con: sqlite3.Connection) -> None:
    """Create the optional component in the caller's transaction."""
    con.execute(
        "INSERT INTO measure_import_schema(name, version) VALUES (?, ?)",
        (SCHEMA_NAME, SCHEMA_VERSION),
    )
    for statement in _IMPORT_DDL_STATEMENTS[1:]:
        con.execute(statement)


@lru_cache(maxsize=1)
def _expected_signature() -> dict[str, object]:
    """Build an isolated physical signature from the deployed import DDL."""
    reference = sqlite3.connect(":memory:")
    try:
        reference.execute("PRAGMA foreign_keys=ON")
        reference.execute(_IMPORT_DDL_STATEMENTS[0])
        reference.execute(
            "INSERT INTO measure_import_schema(name, version) VALUES (?, ?)",
            (SCHEMA_NAME, SCHEMA_VERSION),
        )
        for statement in _IMPORT_DDL_STATEMENTS[1:]:
            reference.execute(statement)
        objects = _master_objects(reference)
        tables = {
            table: (
                _table_xinfo(reference, objects[("table", _sqlite_name(table))][0]),
                _normalized_sql(objects[("table", _sqlite_name(table))][2]),
                _index_signature(reference, objects[("table", _sqlite_name(table))][0]),
                _foreign_key_signature(reference, objects[("table", _sqlite_name(table))][0]),
            )
            for table in _IMPORT_TABLES
        }
        indexes = {
            name: _normalized_sql(objects[("index", _sqlite_name(name))][2])
            for name in _IMPORT_INDEXES
        }
        return {
            "tables": tables,
            "indexes": indexes,
            "table_names": frozenset(_sqlite_name(table) for table in _IMPORT_TABLES),
            "index_names": frozenset(_sqlite_name(index) for index in _IMPORT_INDEXES),
        }
    finally:
        reference.close()


def _incompatible(detail: str = "physical definition") -> ValueError:
    return ValueError(f"{SCHEMA_NAME} has an incompatible schema ({detail})")


def validate_import_schema(con: sqlite3.Connection) -> bool:
    """Validate the optional component without creating or mutating anything.

    ``False`` means that no object in the import-origin namespace exists.  A
    partial, unmarked, version-mismatched, or physically altered namespace is
    rejected rather than repaired or adopted by a read path.
    """
    if not isinstance(con, sqlite3.Connection):
        raise TypeError("validate_import_schema requires a sqlite3.Connection")
    expected = _expected_signature()
    objects = _master_objects(con)
    expected_tables = expected["table_names"]
    expected_indexes = expected["index_names"]
    reserved = {*expected_tables, *expected_indexes}
    if any(
        kind == "trigger" and table_name is not None and _sqlite_name(table_name) in expected_tables
        for (kind, _name), (_physical, table_name, _sql) in objects.items()
    ):
        raise _incompatible("unexpected trigger")
    for (kind, name), _object in objects.items():
        if name in reserved:
            expected_kind = "table" if name in expected_tables else "index"
            if kind != expected_kind:
                raise ValueError("A reserved import-origin name is occupied by another object")
    names = {name for (kind, name) in objects if kind == "table"}
    marker_name = _sqlite_name("measure_import_schema")
    namespace_present = any(
        name == marker_name or name.startswith("analytical_import") for (_kind, name) in objects
    )
    if marker_name not in names:
        if namespace_present:
            raise ValueError("Unmarked import-origin tables cannot be interpreted")
        return False
    unexpected = {
        name
        for (kind, name) in objects
        if (name == marker_name or name.startswith("analytical_import")) and name not in reserved
    }
    if unexpected:
        raise ValueError(f"Unsupported import-origin schema objects: {sorted(unexpected)}")
    marker_physical = objects[("table", marker_name)][0]
    if _table_xinfo(con, marker_physical) != expected["tables"]["measure_import_schema"][0]:
        raise _incompatible("metadata table")
    rows = con.execute(
        f'SELECT name, version FROM "{marker_physical.replace(chr(34), chr(34) * 2)}"'
    ).fetchall()
    if [tuple(row) for row in rows] != [(SCHEMA_NAME, SCHEMA_VERSION)]:
        raise ValueError(f"Unsupported {SCHEMA_NAME} schema version or metadata rows")
    for table in _IMPORT_TABLES:
        normalized = _sqlite_name(table)
        if normalized not in names:
            raise ValueError(f"Stored import-origin schema is missing table {table}")
        physical = objects[("table", normalized)][0]
        actual = (
            _table_xinfo(con, physical),
            _normalized_sql(objects[("table", normalized)][2]),
            _index_signature(con, physical),
            _foreign_key_signature(con, physical),
        )
        if actual != expected["tables"][table]:
            raise _incompatible(table)
    actual_indexes = {
        name: _normalized_sql(sql)
        for (kind, name), (_physical, table_name, sql) in objects.items()
        if kind == "index"
        and not name.startswith("sqlite_")
        and table_name is not None
        and _sqlite_name(table_name) in expected_tables
    }
    if actual_indexes != expected["indexes"]:
        raise _incompatible("index definitions")
    return True


def ensure_import_schema(con: sqlite3.Connection) -> None:
    """Create the optional component only in a genuinely new namespace."""
    if not isinstance(con, sqlite3.Connection):
        raise TypeError("ensure_import_schema requires a sqlite3.Connection")
    if validate_import_schema(con):
        return
    con.execute(_IMPORT_DDL_STATEMENTS[0])
    _create_component_schema(con)
    if not validate_import_schema(con):
        raise RuntimeError("Import-origin schema initialization did not complete")


def _component_exists(con: sqlite3.Connection) -> bool:
    """Return whether the optional component is present, validating it if so."""
    return validate_import_schema(con)


def record_import(
    con: sqlite3.Connection,
    owner: str,
    receipt: Mapping[str, Any],
    origins: Iterable[Mapping[str, Any]],
) -> None:
    """Insert one validated restore receipt and its origin memberships.

    The caller owns the surrounding transaction.  This helper intentionally
    does not commit so restored analytical rows and provenance are atomic with
    the new pending batch.
    """
    owner = _owner(owner)
    receipt = _validate_receipt(receipt)
    serialized = canonical(receipt)
    if len(serialized.encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise ValueError("Import receipt exceeds its byte limit")
    ensure_import_schema(con)
    import_id = receipt["import_id"]
    imported_at = receipt["restored_at"]
    origin_rows = []
    seen_members: dict[str, dict[str, str]] = {"dataset": {}, "analysis": {}}
    for raw in origins:
        descriptor = _validate_descriptor(raw)
        if descriptor["import_id"] != import_id:
            raise ValueError("Import origin import_id does not match its receipt")
        if descriptor["snapshot_sha256"] != receipt["snapshot_sha256"]:
            raise ValueError("Import origin snapshot does not match its receipt")
        if descriptor["source_batch_id"] != receipt["source_batch_id"]:
            raise ValueError("Import origin source batch does not match its receipt")
        if descriptor["imported_at"] != imported_at:
            raise ValueError("Import origin timestamp does not match its receipt")
        mapping = receipt[f"{descriptor['record_kind']}_map"]
        if mapping.get(descriptor["source_record_id"]) != descriptor["local_record_id"]:
            raise ValueError("Import origin record is not a receipt member")
        if descriptor["source_record_id"] in seen_members[descriptor["record_kind"]]:
            raise ValueError("Import origin membership is duplicated")
        seen_members[descriptor["record_kind"]][descriptor["source_record_id"]] = descriptor[
            "local_record_id"
        ]
        origin_rows.append(descriptor)
    if any(seen_members[kind] != receipt[f"{kind}_map"] for kind in seen_members):
        raise ValueError("Import origin membership does not close the receipt maps")
    origin_by_local = {
        (descriptor["record_kind"], descriptor["local_record_id"]): descriptor
        for descriptor in origin_rows
    }
    for descriptor in origin_rows:
        _verify_current_record(
            con,
            owner,
            descriptor,
            receipt,
            origin_rows=origin_by_local,
        )
    con.execute(
        """INSERT INTO analytical_imports(
          id, owner, profile, snapshot_sha256, source_batch_id, new_batch_id,
          restored_at, receipt
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            import_id,
            owner,
            receipt["profile"],
            receipt["snapshot_sha256"],
            receipt["source_batch_id"],
            receipt["new_batch_id"],
            imported_at,
            serialized,
        ),
    )
    for descriptor in origin_rows:
        con.execute(
            """INSERT INTO analytical_import_records(
              import_id, owner, record_kind, source_record_id, local_record_id,
              imported_at, original_record_sha256, preserved_content_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                descriptor["import_id"],
                owner,
                descriptor["record_kind"],
                descriptor["source_record_id"],
                descriptor["local_record_id"],
                descriptor["imported_at"],
                descriptor["original_record_sha256"],
                descriptor["preserved_content_sha256"],
            ),
        )


def _verify_current_record(
    con: sqlite3.Connection,
    owner: str,
    descriptor: dict[str, Any],
    receipt: Mapping[str, Any],
    *,
    origin_rows: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> None:
    kind = descriptor["record_kind"]
    local_id = descriptor["local_record_id"]
    if kind == "dataset":
        row = con.execute(
            "SELECT digest, payload, created_at FROM datasets WHERE id=? AND owner=?",
            (local_id, owner),
        ).fetchone()
        if row is None:
            raise ValueError("Imported dataset is not owned by this principal")
        try:
            payload = json.loads(row[1])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Imported dataset content is malformed") from exc
        if not isinstance(payload, dict):
            raise ValueError("Imported dataset content is not an object")
        if (
            canonical(payload) != row[1]
            or row[0] != descriptor["preserved_content_sha256"]
            or sha256(payload) != row[0]
        ):
            raise ValueError("Imported dataset content digest does not match its origin")
        try:
            source_row = con.execute(
                "SELECT kind, digest, content, mapping FROM dataset_sources WHERE dataset_id=?",
                (local_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            source_row = None
        if source_row is None:
            source = None
        else:
            try:
                mapping = json.loads(source_row[3])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Imported dataset source mapping is malformed") from exc
            if not isinstance(mapping, dict) or canonical(mapping) != source_row[3]:
                raise ValueError("Imported dataset source mapping is not canonical")
            _text(source_row[0], "stored source kind")
            _sha(source_row[1], "stored source digest")
            source = {
                "kind": source_row[0],
                "source_sha256": source_row[1],
                "mapping": mapping,
                "content": source_row[2],
            }
            if (
                not isinstance(source["content"], str)
                or hashlib.sha256(source["content"].encode("utf-8")).hexdigest()
                != source["source_sha256"]
            ):
                raise ValueError("Imported dataset source content digest does not match its origin")
        expected_record = dataset_core_sha256(
            descriptor["source_record_id"],
            row[0],
            row[2],
            payload,
            source,
        )
        if descriptor["original_record_sha256"] != expected_record:
            raise ValueError("Imported dataset record origin digest does not match its origin")
        return
    row = con.execute(
        "SELECT dataset_id, trace_id, kind, method, result, provenance, created_at "
        "FROM analyses WHERE id=? AND owner=?",
        (local_id, owner),
    ).fetchone()
    if row is None:
        raise ValueError("Imported analysis is not owned by this principal")
    dataset_row = con.execute(
        "SELECT digest, payload FROM datasets WHERE id=? AND owner=?",
        (row[0], owner),
    ).fetchone()
    if dataset_row is None:
        raise ValueError("Imported analysis dataset is not owned by this principal")
    try:
        method = json.loads(row[3])
        result = json.loads(row[4])
        provenance = json.loads(row[5])
        dataset_payload = json.loads(dataset_row[1])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Imported analysis content is malformed") from exc
    if not all(isinstance(value, dict) for value in (method, result, provenance, dataset_payload)):
        raise ValueError("Imported analysis content is not object-shaped")
    if row[2] != "chromatogram":
        raise ValueError("Imported analysis kind is unsupported")
    if (
        canonical(method) != row[3]
        or canonical(result) != row[4]
        or canonical(provenance) != row[5]
        or canonical(dataset_payload) != dataset_row[1]
    ):
        raise ValueError("Imported analysis content is not canonical")
    if sha256(result) != descriptor["preserved_content_sha256"]:
        raise ValueError("Imported analysis result digest does not match its origin")
    if (
        sha256(dataset_payload) != dataset_row[0]
        or provenance.get("input_sha256") != dataset_row[0]
    ):
        raise ValueError("Imported analysis input dataset does not match its origin")
    source_dataset_ids = [
        source_id for source_id, mapped_id in receipt["dataset_map"].items() if mapped_id == row[0]
    ]
    if len(source_dataset_ids) != 1:
        raise ValueError("Imported analysis dataset is not a receipt member")
    source_dataset_id = source_dataset_ids[0]

    # An imported analysis and its input dataset must be members of the same
    # receipt.  During record_import the descriptors are still in memory; on a
    # read, recover the linked descriptor from the durable membership table.
    linked_dataset: Mapping[str, Any] | None
    if origin_rows is not None:
        linked_dataset = origin_rows.get(("dataset", row[0]))
    else:
        linked_row = con.execute(
            """SELECT r.import_id, i.profile, i.snapshot_sha256, i.source_batch_id,
              i.new_batch_id, i.restored_at, r.source_record_id, r.local_record_id,
              r.record_kind, r.imported_at, r.original_record_sha256,
              r.preserved_content_sha256, i.receipt
            FROM analytical_import_records r
            JOIN analytical_imports i ON i.id=r.import_id AND i.owner=r.owner
            WHERE r.owner=? AND r.record_kind='dataset' AND r.local_record_id=?
              AND r.import_id=?""",
            (owner, row[0], descriptor["import_id"]),
        ).fetchone()
        if linked_row is None:
            raise ValueError("Imported analysis input dataset membership is missing")
        linked_dataset = {
            "profile": PROFILE,
            "import_id": linked_row[0],
            "snapshot_sha256": linked_row[2],
            "source_batch_id": linked_row[3],
            "source_record_id": linked_row[6],
            "local_record_id": linked_row[7],
            "record_kind": linked_row[8],
            "imported_at": linked_row[9],
            "original_record_sha256": linked_row[10],
            "preserved_content_sha256": linked_row[11],
        }
        linked_receipt = _validate_receipt(_decode(linked_row[12], "import receipt"))
        if linked_receipt != dict(receipt):
            raise ValueError("Imported analysis input dataset receipt disagrees")
    if linked_dataset is None:
        raise ValueError("Imported analysis input dataset membership is missing")
    linked_dataset = _validate_descriptor(linked_dataset)
    if (
        linked_dataset["import_id"] != descriptor["import_id"]
        or linked_dataset["local_record_id"] != row[0]
        or linked_dataset["source_record_id"] != source_dataset_id
    ):
        raise ValueError("Imported analysis input dataset origin disagrees with its receipt")
    if origin_rows is None:
        _verify_current_record(con, owner, linked_dataset, receipt)

    expected_record = analysis_core_sha256(
        descriptor["source_record_id"],
        source_dataset_id,
        row[1],
        row[2],
        method,
        result,
        provenance,
        row[6],
    )
    if descriptor["original_record_sha256"] != expected_record:
        raise ValueError("Imported analysis record origin digest does not match its origin")


def get_import_origin(
    con: sqlite3.Connection, owner: str, record_kind: str, local_record_id: str
) -> dict[str, Any] | None:
    """Return one owned import-origin descriptor, or ``None`` if unavailable."""
    owner = _owner(owner)
    record_kind = _kind(record_kind)
    local_record_id = _text(local_record_id, "local_record_id")
    if not _component_exists(con):
        return None
    row = con.execute(
        """SELECT r.import_id, i.profile, i.snapshot_sha256, i.source_batch_id,
          i.new_batch_id, i.restored_at, r.source_record_id, r.local_record_id,
          r.record_kind, r.imported_at, r.original_record_sha256,
          r.preserved_content_sha256, i.receipt
        FROM analytical_import_records r
        JOIN analytical_imports i ON i.id=r.import_id AND i.owner=r.owner
        WHERE r.owner=? AND r.record_kind=? AND r.local_record_id=?""",
        (owner, record_kind, local_record_id),
    ).fetchone()
    if row is None:
        orphan = con.execute(
            "SELECT import_id FROM analytical_import_records "
            "WHERE owner=? AND record_kind=? AND local_record_id=?",
            (owner, record_kind, local_record_id),
        ).fetchone()
        if orphan is not None:
            raise ValueError("Imported record receipt is missing")
        for receipt_row in con.execute(
            "SELECT receipt FROM analytical_imports WHERE owner=?", (owner,)
        ):
            receipt = _validate_receipt(_decode(receipt_row[0], "import receipt"))
            if local_record_id in receipt[f"{record_kind}_map"].values():
                raise ValueError("Imported record membership is missing")
        return None
    descriptor = {
        "profile": PROFILE,
        "import_id": row[0],
        "snapshot_sha256": row[2],
        "source_batch_id": row[3],
        "source_record_id": row[6],
        "local_record_id": row[7],
        "record_kind": row[8],
        "imported_at": row[9],
        "original_record_sha256": row[10],
        "preserved_content_sha256": row[11],
    }
    descriptor = _validate_descriptor(descriptor)
    receipt = _validate_receipt(_decode(row[12], "import receipt"))
    if (
        row[1] != receipt["profile"]
        or receipt["import_id"] != descriptor["import_id"]
        or receipt["snapshot_sha256"] != descriptor["snapshot_sha256"]
        or receipt["source_batch_id"] != descriptor["source_batch_id"]
        or row[4] != receipt["new_batch_id"]
        or row[5] != receipt["restored_at"]
        or descriptor["imported_at"] != receipt["restored_at"]
        or receipt[f"{record_kind}_map"].get(descriptor["source_record_id"]) != local_record_id
    ):
        raise ValueError("Imported record is not consistent with its receipt")
    _validate_receipt_memberships(con, owner, receipt)
    _verify_current_record(con, owner, descriptor, receipt)
    return descriptor


def get_import_receipt(
    con: sqlite3.Connection, owner: str, import_id: str
) -> dict[str, Any] | None:
    """Read an owned restore receipt in the caller's read transaction.

    Store selection and connection lifetime belong to the public service
    layer.  Keeping this helper connection-explicit prevents a provenance
    read from silently selecting or initializing a configured store.
    """
    if not isinstance(con, sqlite3.Connection):
        raise TypeError("get_import_receipt requires a sqlite3.Connection")
    owner = _owner(owner)
    identifier = _text(import_id, "import_id")
    if not _component_exists(con):
        return None
    row = con.execute(
        "SELECT profile, snapshot_sha256, source_batch_id, new_batch_id, restored_at, receipt "
        "FROM analytical_imports WHERE owner=? AND id=?",
        (owner, identifier),
    ).fetchone()
    if row is None:
        return None
    receipt = _validate_receipt(_decode(row[5], "import receipt"))
    if (
        receipt["import_id"] != identifier
        or row[0] != receipt["profile"]
        or row[1] != receipt["snapshot_sha256"]
        or row[2] != receipt["source_batch_id"]
        or row[3] != receipt["new_batch_id"]
        or row[4] != receipt["restored_at"]
    ):
        raise ValueError("Stored import receipt columns do not match its content")
    _validate_receipt_memberships(con, owner, receipt)
    return receipt


__all__ = [
    "PROFILE",
    "RESTORE_PROFILE",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "dataset_core_sha256",
    "analysis_core_sha256",
    "ensure_import_schema",
    "validate_import_schema",
    "get_import_origin",
    "get_import_receipt",
    "record_import",
]
