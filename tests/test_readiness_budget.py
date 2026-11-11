"""Cold reads can finish inside the bounded HTTP readiness window."""

import sqlite3
import time

import pytest

from measure_service import operational_readiness as readiness
from measure_service.storage import _connect_at


@pytest.mark.parametrize(("delay", "expected"), [(0.3, True), (0.8, False)])
def test_cold_read_uses_inner_budget_without_changing_store(tmp_path, monkeypatch, delay, expected):
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "connect")
    monkeypatch.setenv("MEASURE_MCP_TRUST_CONNECT_HEADERS", "1")
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    path = tmp_path / "measure.sqlite3"
    _connect_at(path).close()
    before = path.read_bytes()
    original_connect = sqlite3.connect

    class ColdConnection:
        def __init__(self, connection):
            self.connection = connection
            self.first_read = True

        def execute(self, *args, **kwargs):
            if self.first_read:
                self.first_read = False
                time.sleep(delay)
            return self.connection.execute(*args, **kwargs)

        def set_progress_handler(self, *args):
            return self.connection.set_progress_handler(*args)

        def close(self):
            self.connection.close()

    monkeypatch.setattr(
        readiness.sqlite3,
        "connect",
        lambda *args, **kwargs: ColdConnection(original_connect(*args, **kwargs)),
    )
    started = time.monotonic()
    assert readiness.storage_ready() is expected
    assert time.monotonic() - started < 1.0
    assert path.read_bytes() == before


def test_unreadable_store_still_reports_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "connect")
    monkeypatch.setenv("MEASURE_MCP_TRUST_CONNECT_HEADERS", "1")
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    path = tmp_path / "measure.sqlite3"
    _connect_at(path).close()
    before = path.read_bytes()

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic unavailable storage")

    monkeypatch.setattr(readiness.sqlite3, "connect", unavailable)
    assert readiness.storage_ready() is False
    assert path.read_bytes() == before
