"""Installed local MCP client setup and restart checks."""

import asyncio
import json
import os
import subprocess
import sys

import measure_service.client_cli as client_cli


def _run_cli(tmp_path, *arguments, timeout=150, overrides=None):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    for name in (
        "MEASURE_MCP_DATA_DIR",
        "MEASURE_MCP_R",
        "MEASURE_MCP_IDENTITY",
        "MEASURE_MCP_TOOL_GROUPS",
        "MEASURE_MCP_TOOL_SCHEMA_PROFILE",
        "R_LIBS",
        "R_LIBS_USER",
    ):
        environment.pop(name, None)
    environment.update(overrides or {})
    return subprocess.run(
        [sys.executable, "-m", "measure_service.client_cli", *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_claude_config_uses_absolute_symlinked_interpreter_and_writes_nothing(tmp_path):
    data_dir = tmp_path / "private data"
    completed = _run_cli(tmp_path, "config", "claude", "--data-dir", str(data_dir))

    assert completed.returncode == 0, completed.stderr
    configuration = json.loads(completed.stdout)
    server = configuration["mcpServers"]["measure-service"]
    assert server["command"] == sys.executable
    assert os.path.isabs(server["command"])
    assert server["args"] == ["-m", "measure_service"]
    assert server["env"]["MEASURE_MCP_DATA_DIR"] == str(data_dir)
    assert json.loads(server["env"]["MEASURE_MCP_TOOL_GROUPS"]) == [
        "discovery",
        "traces",
        "evidence",
    ]
    assert server["env"]["MEASURE_MCP_TOOL_SCHEMA_PROFILE"] == "function-host/1"
    assert not data_dir.exists()
    assert not list(tmp_path.iterdir())


def test_codex_config_quotes_paths_and_sets_function_host_timeout(tmp_path):
    python = tmp_path / 'python "quoted"\\bin'
    data_dir = tmp_path / 'data "quoted"\\dir'
    r_path = tmp_path / 'R "quoted"\\bin'
    r_library = tmp_path / 'R library "quoted"\\folder'
    completed = _run_cli(
        tmp_path,
        "config",
        "codex",
        "--python",
        str(python),
        "--data-dir",
        str(data_dir),
        "--include-sec",
        "--schema-profile",
        "canonical",
        "--r",
        str(r_path),
        "--r-library",
        str(r_library),
        overrides={"R_LIBS": str(tmp_path / "inherited-r-library")},
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("[mcp_servers.measure-service]\n")
    assert f"command = {json.dumps(str(python))}\n" in completed.stdout
    assert "tool_timeout_sec = 120\n" in completed.stdout
    assert "MEASURE_MCP_R = " in completed.stdout
    assert "R_LIBS = " in completed.stdout
    assert "R_LIBS_USER = " in completed.stdout
    assert 'MEASURE_MCP_TOOL_SCHEMA_PROFILE = "canonical"' in completed.stdout
    assert '\\"quoted\\"' in completed.stdout
    assert str(tmp_path / "inherited-r-library") not in completed.stdout
    if sys.version_info >= (3, 11):
        import tomllib

        configuration = tomllib.loads(completed.stdout)
        server = configuration["mcp_servers"]["measure-service"]
        assert server["command"] == str(python)
        assert server["args"] == ["-m", "measure_service"]
        assert server["tool_timeout_sec"] == 120
        assert json.loads(server["env"]["MEASURE_MCP_TOOL_GROUPS"]) == [
            "discovery",
            "traces",
            "sec",
            "evidence",
        ]
        assert server["env"]["MEASURE_MCP_R"] == str(r_path)
        assert server["env"]["R_LIBS"] == str(r_library)
        assert server["env"]["R_LIBS_USER"] == str(r_library)
    assert not data_dir.exists()
    assert not list(tmp_path.iterdir())


def test_config_rejects_relative_data_directory_without_creating_settings(tmp_path):
    completed = _run_cli(tmp_path, "config", "claude", "--data-dir", "relative/store")

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "--data-dir must be an absolute path" in completed.stderr
    assert "relative/store" not in completed.stderr
    assert not list(tmp_path.iterdir())


def test_installed_stdio_check_reports_catalog_error_and_exact_restart_continuity(tmp_path):
    completed = _run_cli(tmp_path, "check")

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["profile"] == "measure-service-client-check/1"
    assert report["status"] == "passed"
    assert report["scope"] == "local_sdk_stdio"
    assert report["sdk_stdio_tested"] is True
    assert report["selected_groups"] == ["discovery", "traces", "evidence"]
    assert report["schema_profile"] == "function-host/1"
    assert report["checks"] == {
        "initialize": "passed",
        "ping": "passed",
        "tool_listing": "passed",
        "catalog_selection": "passed",
        "synthetic_trace_import": "passed",
        "source_digest": "passed",
        "expected_disabled_tool_error": "passed",
        "restart_persistence": "passed",
        "numerical_restart_persistence": "not_run",
        "independent_numerical_references": "not_run",
    }
    assert set(report["client_acceptance"].values()) == {"pending_external_acceptance"}
    assert str(tmp_path) not in completed.stdout
    assert not list(tmp_path.iterdir())


def test_numerical_check_requires_sec_and_absolute_r_pair(tmp_path):
    no_sec = _run_cli(tmp_path, "check", "--numerical")
    no_r = _run_cli(tmp_path, "check", "--numerical", "--include-sec")

    assert no_sec.returncode == 2
    assert "--numerical requires --include-sec" in no_sec.stderr
    assert no_r.returncode == 2
    assert "--numerical requires both --r and --r-library" in no_r.stderr
    assert not list(tmp_path.iterdir())


def test_stdio_start_failure_has_sanitized_report(tmp_path):
    missing_python = tmp_path / "private" / "missing-python"
    completed = _run_cli(tmp_path, "check", "--python", str(missing_python))

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["status"] == "failed"
    assert report["sdk_stdio_tested"] is False
    assert report["failure_code"] == "client_check_failed"
    assert str(tmp_path) not in completed.stdout
    assert str(tmp_path) not in completed.stderr
    assert "Traceback" not in completed.stderr
    assert not list(tmp_path.iterdir())


def test_total_deadline_reports_failure(monkeypatch, capsys):
    async def blocked_check(**_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(client_cli, "_run_check", blocked_check)

    assert client_cli.main(["check", "--timeout", "1"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["failure_code"] == "timeout"
    assert report["checks"]["deadline"] == "failed"
