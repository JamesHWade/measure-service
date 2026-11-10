"""Bad deployment settings fail before storage or unsafe diagnostic output."""

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "name,value",
    [
        ("MEASURE_MCP_HTTP_ALLOWED_HOSTS", ["https://user:PRIVATE_SENTINEL@example.invalid"]),
        ("MEASURE_MCP_HTTP_ALLOWED_HOSTS", ["example.invalid:PRIVATE_SENTINEL"]),
        ("MEASURE_MCP_HTTP_ALLOWED_ORIGINS", ["https://user:PRIVATE_SENTINEL@example.invalid"]),
        ("MEASURE_MCP_HTTP_ALLOWED_ORIGINS", ["https://example.invalid/?PRIVATE_SENTINEL"]),
    ],
)
def test_bad_authority_configuration_fails_startup_without_echoing_values(tmp_path, name, value):
    store = tmp_path / "must-not-exist"
    env = dict(os.environ, MEASURE_MCP_DATA_DIR=str(store), MEASURE_MCP_IDENTITY="local")
    env.pop("MEASURE_MCP_HTTP_ALLOWED_HOSTS", None)
    env.pop("MEASURE_MCP_HTTP_ALLOWED_ORIGINS", None)
    env[name] = json.dumps(value)
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-m", "measure_service"],
        input="",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=15,
    )
    assert completed.returncode != 0
    assert name in completed.stderr
    assert "PRIVATE_SENTINEL" not in completed.stderr
    assert not completed.stdout
    assert not store.exists()
