"""A synthetic proxy process used only by pilot transport integration tests."""

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

# This fixture is not a deployment/authentication implementation. Two fixed
# synthetic tokens represent two principals only to exercise the real client.
PROXY_CODE = r"""
import json, os, uvicorn
from measure_service.server import app as upstream

class SimulatedProxy:
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await upstream(scope, receive, send)
        headers = [(k,v) for k,v in scope['headers'] if k.lower() != b'rstudio-connect-credentials']
        token = next((v for k,v in headers if k.lower() == b'authorization'), b'')
        users = {
            b'Bearer synthetic-account-a': '113ab98e-05bd-494d-a467-2889a2f27b21',
            b'Bearer synthetic-account-b': 'e9528af1-9a4c-4b73-b062-14bab16c8c1a',
        }
        if token not in users:
            await send({'type':'http.response.start','status':401,'headers':[(b'content-type',b'application/json')]})
            return await send({'type':'http.response.body','body':b'{"error":"unauthorized"}'})
        headers.append((b'rstudio-connect-credentials',json.dumps({'user':users[token]}).encode()))
        return await upstream(dict(scope, headers=headers), receive, send)

uvicorn.run(SimulatedProxy(),host='127.0.0.1',port=int(os.environ['PILOT_TEST_PORT']),log_level='critical',access_log=False)
"""


def free_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


@contextmanager
def synthetic_host(directory, port, *, r_backend=False, store=None):
    environment = dict(
        os.environ,
        MEASURE_MCP_DATA_DIR=str(store if store is not None else directory / "store"),
        MEASURE_MCP_IDENTITY="connect",
        MEASURE_MCP_TRUST_CONNECT_HEADERS="1",
        MEASURE_MCP_R=(
            os.environ.get("MEASURE_MCP_TEST_R", "R")
            if r_backend
            else str(directory / "unavailable-R")
        ),
        PILOT_TEST_PORT=str(port),
        MEASURE_MCP_MEASURE_REVISION="synthetic-private-operator-metadata",
    )
    environment.pop("PYTHONPATH", None)
    process = subprocess.Popen(
        [sys.executable, "-c", PROXY_CODE],
        cwd=directory,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            assert process.poll() is None, "Synthetic proxy exited during startup"
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                assert time.monotonic() < deadline, "Synthetic proxy did not start"
                time.sleep(0.02)
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def client_environment(port):
    return dict(
        os.environ,
        MEASURE_PILOT_URL=f"http://127.0.0.1:{port}/mcp",
        MEASURE_PILOT_HEADERS_A='{"Authorization":"Bearer synthetic-account-a"}',
        MEASURE_PILOT_HEADERS_B='{"Authorization":"Bearer synthetic-account-b"}',
    )
