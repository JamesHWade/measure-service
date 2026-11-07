"""Optional import-origin component remains explicit, bounded and owner-scoped."""

from __future__ import annotations

import sqlite3

import pytest

from measure_service import import_origins
from measure_service.storage import canonical, sha256


def _receipt(import_id: str = "import-1") -> dict:
    return {
        "profile": import_origins.RESTORE_PROFILE,
        "schema_version": "1.0",
        "snapshot_sha256": "a" * 64,
        "source_batch_id": "batch-old",
        "new_batch_id": "batch-new",
        "import_id": import_id,
        "restored_at": "2026-11-07T12:00:00+00:00",
        "dataset_map": {"dataset-old": "dataset-new"},
        "analysis_map": {"analysis-old": "analysis-new"},
        "limits": {
            "max_snapshot_bytes": 64 * 1024 * 1024,
            "max_archive_bytes": 64 * 1024 * 1024 + 256 * 1024,
        },
        "no_execution_performed": True,
    }


def _seed(con: sqlite3.Connection) -> tuple[dict, dict, dict]:
    dataset_payload = {"title": "synthetic", "traces": []}
    dataset_digest = sha256(dataset_payload)
    dataset_created = "2026-11-01T00:00:00+00:00"
    con.execute(
        "INSERT INTO datasets VALUES (?, ?, ?, ?, ?)",
        ("dataset-new", "local", dataset_digest, canonical(dataset_payload), dataset_created),
    )
    result = {"kind": "chromatogram", "warnings": [], "peak_count": 0}
    provenance = {"input_sha256": dataset_digest, "method_sha256": "b" * 64}
    con.execute(
        "INSERT INTO analyses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "analysis-new",
            "local",
            "dataset-new",
            "trace",
            "chromatogram",
            canonical({}),
            canonical(result),
            canonical(provenance),
            "2026-11-01T00:01:00+00:00",
        ),
    )
    receipt = _receipt()
    imported_at = receipt["restored_at"]
    dataset_origin = {
        "profile": import_origins.PROFILE,
        "import_id": receipt["import_id"],
        "snapshot_sha256": receipt["snapshot_sha256"],
        "source_batch_id": receipt["source_batch_id"],
        "source_record_id": "dataset-old",
        "local_record_id": "dataset-new",
        "record_kind": "dataset",
        "imported_at": imported_at,
        "original_record_sha256": import_origins.dataset_core_sha256(
            "dataset-old",
            dataset_digest,
            dataset_created,
            dataset_payload,
            None,
        ),
        "preserved_content_sha256": dataset_digest,
    }
    analysis_origin = {
        "profile": import_origins.PROFILE,
        "import_id": receipt["import_id"],
        "snapshot_sha256": receipt["snapshot_sha256"],
        "source_batch_id": receipt["source_batch_id"],
        "source_record_id": "analysis-old",
        "local_record_id": "analysis-new",
        "record_kind": "analysis",
        "imported_at": imported_at,
        "original_record_sha256": import_origins.analysis_core_sha256(
            "analysis-old",
            "dataset-old",
            "trace",
            "chromatogram",
            {},
            result,
            provenance,
            "2026-11-01T00:01:00+00:00",
        ),
        "preserved_content_sha256": sha256(result),
    }
    return receipt, dataset_origin, analysis_origin


def test_optional_component_is_created_only_explicitly_and_reads_are_owned(tmp_path):
    database = tmp_path / "measure.sqlite3"
    con = sqlite3.connect(database)
    con.execute(
        "CREATE TABLE datasets (id TEXT PRIMARY KEY, owner TEXT, digest TEXT, payload TEXT, created_at TEXT)"
    )
    con.execute(
        "CREATE TABLE analyses (id TEXT PRIMARY KEY, owner TEXT, dataset_id TEXT, trace_id TEXT, kind TEXT, method TEXT, result TEXT, provenance TEXT, created_at TEXT)"
    )
    assert (
        con.execute("SELECT 1 FROM sqlite_master WHERE name='analytical_imports'").fetchone()
        is None
    )
    receipt, dataset_origin, analysis_origin = _seed(con)
    import_origins.record_import(con, "local", receipt, [dataset_origin, analysis_origin])
    con.commit()

    assert (
        import_origins.get_import_origin(con, "local", "dataset", "dataset-new") == dataset_origin
    )
    assert (
        import_origins.get_import_origin(con, "local", "analysis", "analysis-new")
        == analysis_origin
    )
    assert import_origins.get_import_origin(con, "other", "dataset", "dataset-new") is None
    assert import_origins.get_import_receipt(con, "local", receipt["import_id"]) == receipt
    assert import_origins.get_import_receipt(con, "other", receipt["import_id"]) is None
    con.close()


def test_origin_reads_reject_tampered_current_content_or_origin_rows(tmp_path):
    con = sqlite3.connect(tmp_path / "measure.sqlite3")
    con.execute(
        "CREATE TABLE datasets (id TEXT PRIMARY KEY, owner TEXT, digest TEXT, payload TEXT, created_at TEXT)"
    )
    con.execute(
        "CREATE TABLE analyses (id TEXT PRIMARY KEY, owner TEXT, dataset_id TEXT, trace_id TEXT, kind TEXT, method TEXT, result TEXT, provenance TEXT, created_at TEXT)"
    )
    receipt, dataset_origin, analysis_origin = _seed(con)
    import_origins.record_import(con, "local", receipt, [dataset_origin, analysis_origin])
    con.commit()

    con.execute(
        "UPDATE analyses SET provenance=? WHERE id=?",
        (canonical({"changed": True}), "analysis-new"),
    )
    with pytest.raises(ValueError, match="origin"):
        import_origins.get_import_origin(con, "local", "analysis", "analysis-new")
    con.execute(
        "UPDATE analyses SET provenance=? WHERE id=?",
        (
            canonical(
                {
                    "input_sha256": dataset_origin["preserved_content_sha256"],
                    "method_sha256": "b" * 64,
                }
            ),
            "analysis-new",
        ),
    )
    con.execute(
        "UPDATE analytical_import_records SET preserved_content_sha256=? WHERE record_kind='dataset'",
        ("c" * 64,),
    )
    with pytest.raises(ValueError, match="dataset content digest"):
        import_origins.get_import_origin(con, "local", "dataset", "dataset-new")
    con.close()


def test_import_schema_and_receipt_roll_back_with_caller_transaction(tmp_path):
    con = sqlite3.connect(tmp_path / "measure.sqlite3")
    con.execute(
        "CREATE TABLE datasets (id TEXT PRIMARY KEY, owner TEXT, digest TEXT, payload TEXT, created_at TEXT)"
    )
    con.execute(
        "CREATE TABLE analyses (id TEXT PRIMARY KEY, owner TEXT, dataset_id TEXT, trace_id TEXT, kind TEXT, method TEXT, result TEXT, provenance TEXT, created_at TEXT)"
    )
    receipt, dataset_origin, analysis_origin = _seed(con)
    con.commit()
    con.execute("BEGIN")
    import_origins.record_import(con, "local", receipt, [dataset_origin, analysis_origin])
    con.rollback()
    assert import_origins.get_import_receipt(con, "local", receipt["import_id"]) is None
    assert import_origins.get_import_origin(con, "local", "dataset", "dataset-new") is None
    con.close()


def test_receipt_reads_require_connection_and_exact_membership_maps(tmp_path):
    database, receipt = _component_database(tmp_path)
    con = sqlite3.connect(database)
    with pytest.raises(TypeError, match="sqlite3.Connection"):
        import_origins.get_import_receipt(  # type: ignore[arg-type]
            "local", receipt["import_id"], "unexpected"
        )

    altered = dict(receipt)
    altered["dataset_map"] = {**receipt["dataset_map"], "dataset-extra": "local-extra"}
    con.execute(
        "UPDATE analytical_imports SET receipt=? WHERE id=?",
        (canonical(altered), receipt["import_id"]),
    )
    con.commit()
    with pytest.raises(ValueError, match="memberships"):
        import_origins.get_import_receipt(con, "local", receipt["import_id"])
    con.close()


def _component_database(tmp_path):
    database = tmp_path / "measure.sqlite3"
    con = sqlite3.connect(database)
    con.execute(
        "CREATE TABLE datasets (id TEXT PRIMARY KEY, owner TEXT, digest TEXT, payload TEXT, created_at TEXT)"
    )
    con.execute(
        "CREATE TABLE analyses (id TEXT PRIMARY KEY, owner TEXT, dataset_id TEXT, trace_id TEXT, kind TEXT, method TEXT, result TEXT, provenance TEXT, created_at TEXT)"
    )
    receipt, dataset_origin, analysis_origin = _seed(con)
    import_origins.record_import(con, "local", receipt, [dataset_origin, analysis_origin])
    con.commit()
    con.close()
    return database, receipt


def test_read_validation_does_not_adopt_or_mutate_unmarked_namespace(tmp_path):
    database = tmp_path / "measure.sqlite3"
    con = sqlite3.connect(database)
    con.execute("CREATE TABLE analytical_imports (id TEXT)")
    con.commit()
    con.close()
    before = database.read_bytes()

    con = sqlite3.connect(database)
    with pytest.raises(ValueError, match="Unmarked"):
        import_origins.validate_import_schema(con)
    with pytest.raises(ValueError, match="Unmarked"):
        import_origins.get_import_receipt(con, "local", "missing")
    con.close()
    assert database.read_bytes() == before


@pytest.mark.parametrize("tamper", ["wrong-version", "missing-marker", "extra-marker"])
def test_read_validation_rejects_marker_tampering_without_writes(tmp_path, tamper):
    database, _ = _component_database(tmp_path)
    con = sqlite3.connect(database)
    if tamper == "wrong-version":
        con.execute(
            "UPDATE measure_import_schema SET version=999 WHERE name=?",
            (import_origins.SCHEMA_NAME,),
        )
    elif tamper == "missing-marker":
        con.execute(
            "DELETE FROM measure_import_schema WHERE name=?",
            (import_origins.SCHEMA_NAME,),
        )
    else:
        con.execute(
            "INSERT INTO measure_import_schema(name, version) VALUES (?, ?)",
            ("unexpected", import_origins.SCHEMA_VERSION),
        )
    con.commit()
    con.close()
    before = database.read_bytes()

    con = sqlite3.connect(database)
    with pytest.raises(ValueError):
        import_origins.validate_import_schema(con)
    with pytest.raises(ValueError):
        import_origins.get_import_origin(con, "local", "dataset", "dataset-new")
    con.close()
    assert database.read_bytes() == before


@pytest.mark.parametrize("definition", ["altered-affinity", "case-alias-view"])
def test_read_validation_rejects_physical_schema_collisions_without_writes(tmp_path, definition):
    database = tmp_path / "measure.sqlite3"
    con = sqlite3.connect(database)
    if definition == "altered-affinity":
        con.execute(
            "CREATE TABLE measure_import_schema(name TEXT PRIMARY KEY, version TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO measure_import_schema(name, version) VALUES (?, ?)",
            (import_origins.SCHEMA_NAME, str(import_origins.SCHEMA_VERSION)),
        )
    else:
        con.execute("CREATE VIEW MEASURE_IMPORT_SCHEMA AS SELECT 'wrong' AS name, 1 AS version")
    con.commit()
    con.close()
    before = database.read_bytes()

    con = sqlite3.connect(database)
    with pytest.raises(ValueError):
        import_origins.validate_import_schema(con)
    con.close()
    assert database.read_bytes() == before
