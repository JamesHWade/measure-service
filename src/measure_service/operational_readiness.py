"""A bounded, read-only storage probe, separate from analytical capabilities."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def storage_ready() -> bool:
    """Observe an initialized readable store without creating or migrating it.

    This probe establishes neither write capacity nor numerical backend health.
    HTTP identity still requires the separately qualified trusted proxy boundary.
    """
    if (
        os.environ.get("MEASURE_MCP_IDENTITY", "local") != "connect"
        or os.environ.get("MEASURE_MCP_TRUST_CONNECT_HEADERS") != "1"
    ):
        return False
    root = Path(
        os.environ.get(
            "MEASURE_MCP_DATA_DIR", str(Path.home() / ".local" / "share" / "measure-mcp")
        )
    )
    path = root / "measure.sqlite3"
    try:
        if (
            root.is_symlink()
            or not root.is_dir()
            or os.path.lexists(root / "INCOMPLETE")
            or path.is_symlink()
            or not path.is_file()
        ):
            return False
        # mode=ro observes committed WAL data; immutable=1 would ignore it.
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
        ) as con:
            deadline = time.monotonic() + 0.2
            con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            try:
                for table, columns in (
                    ("datasets", ("id", "owner", "digest", "payload", "created_at")),
                    (
                        "analyses",
                        (
                            "id",
                            "owner",
                            "dataset_id",
                            "trace_id",
                            "kind",
                            "method",
                            "result",
                            "provenance",
                            "created_at",
                        ),
                    ),
                    ("experiments", ("id", "owner", "payload", "digest", "created_at")),
                    (
                        "registry_receipts",
                        ("owner", "request_key", "digest", "response", "created_at"),
                    ),
                ):
                    kind = con.execute(
                        "SELECT type FROM sqlite_master WHERE name=? LIMIT 1", (table,)
                    ).fetchone()
                    if kind != ("table",):
                        return False
                    # Identifiers are fixed here, never caller inputs. LIMIT 0
                    # prepares the required columns without reading any records.
                    selected = ",".join(f'"{table}"."{column}"' for column in columns)
                    con.execute(f'SELECT {selected} FROM "{table}" LIMIT 0')
                return True
            finally:
                con.set_progress_handler(None, 0)
    except (OSError, sqlite3.Error, ValueError):
        return False
