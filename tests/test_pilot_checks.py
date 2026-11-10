"""Exercise the fixed pilot protocol against a real synthetic two-account proxy."""

import asyncio
import copy
import uuid
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from pilot_host_support import free_port, synthetic_host

from measure_service.pilot_checks import (
    PilotCheckError,
    prepare_pilot,
    validate_checkpoint,
    verify_pilot,
)


@asynccontextmanager
async def clients(port, *, same_account=False):
    async with AsyncExitStack() as stack:
        sessions = []
        for account in ("a", "a" if same_account else "b"):
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers={"Authorization": f"Bearer synthetic-account-{account}"},
                    timeout=30,
                    trust_env=False,
                )
            )
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            sessions.append(session)
        yield sessions


class RecordingClient:
    def __init__(self, client, *, denied_dataset=None, evidence_unavailable=False):
        self.client = client
        self.calls = []
        self.denied_dataset = denied_dataset
        self.evidence_unavailable = evidence_unavailable

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if (
            name == "measurement_inspect_dataset"
            and arguments.get("dataset_id") == self.denied_dataset
        ):
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text="Unrelated storage failure")],
            )
        if self.evidence_unavailable and name == "measurement_get_chromatogram_evidence":
            return CallToolResult(
                isError=True,
                content=[
                    TextContent(type="text", text="Analysis was not found or is not accessible")
                ],
            )
        return await self.client.call_tool(name, arguments)


class NoCalls:
    async def call_tool(self, *_args, **_kwargs):
        raise AssertionError("Invalid checkpoints must fail before any remote call")


@pytest.fixture(scope="module")
def registry_pilot(tmp_path_factory):
    root = tmp_path_factory.mktemp("fixed-registry-pilot")
    port = free_port()
    with synthetic_host(root, port):

        async def prepare():
            async with clients(port) as (a, b):
                return await prepare_pilot(a, b, run_id=uuid.uuid4().hex)

        checkpoint = asyncio.run(prepare())
        yield port, checkpoint


def test_actual_two_account_prepare_and_recheck_preserve_exact_registry_receipts(registry_pilot):
    port, checkpoint = registry_pilot
    assert validate_checkpoint(checkpoint) == checkpoint
    original = copy.deepcopy(checkpoint)
    assert (
        checkpoint["accounts"]["a"]["experiment_id"] != checkpoint["accounts"]["b"]["experiment_id"]
    )

    async def exercise():
        async with clients(port) as (a, b):
            observed_a, observed_b = RecordingClient(a), RecordingClient(b)
            report = await verify_pilot(observed_a, observed_b, checkpoint)
            assert report["passed"] is True
            assert report["checks"]["idempotent_replay"] is True
            assert report["checks"]["no_duplicate_attempts_or_results"] is True
            assert report["restart_claimed"] is False
            assert report["operator_gate_required"] is True
            assert report["counts"]["datasets"] == 2
            for recorded in (observed_a, observed_b):
                names = [name for name, _ in recorded.calls]
                assert "measurement_register_csv" not in names
                assert "measurement_register_dataset" not in names
                assert not any("analyze" in name for name in names)
                first_replay = names.index("measurement_create_experiment")
                assert names.index("measurement_get_experiment") < first_replay
                assert names.index("measurement_get_attempt") < first_replay

    asyncio.run(exercise())
    assert checkpoint == original


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_tool",
        "bad_profile",
        "nonfinite",
        "duplicate_ids",
        "source_digest",
        "source_text",
        "truncated_sources",
        "experiment_spec",
    ],
)
def test_malformed_checkpoint_is_rejected_before_any_remote_calls(registry_pilot, mutation):
    _, checkpoint = registry_pilot
    altered = copy.deepcopy(checkpoint)
    account = altered["accounts"]["a"]
    if mutation == "unknown_tool":
        altered["tool"] = "measurement_register_dataset"
    elif mutation == "bad_profile":
        altered["profile"] = "other/1"
    elif mutation == "nonfinite":
        account["expected"]["attempt_list"]["count"] = float("inf")
    elif mutation == "duplicate_ids":
        account["dataset_id"] = altered["accounts"]["b"]["dataset_id"]
    elif mutation == "source_digest":
        account["expected"]["dataset_inspection"]["source"]["source_sha256"] = "0" * 64
    elif mutation == "source_text":
        account["expected"]["source_pages"][0]["text"] = "time,response\n1,9\n2,1\n3,0\n"
    elif mutation == "truncated_sources":
        account["expected"]["source_pages"] = []
    else:
        account["expected"]["experiment"]["spec"]["description"] = "Changed synthetic fixture"
    with pytest.raises(ValueError, match="Invalid pilot checkpoint"):
        asyncio.run(verify_pilot(NoCalls(), NoCalls(), altered))


def test_generic_error_cannot_be_counted_as_foreign_ownership_denial(registry_pilot):
    port, checkpoint = registry_pilot

    async def exercise():
        async with clients(port) as (a, b):
            observed_a = RecordingClient(
                a, denied_dataset=checkpoint["accounts"]["b"]["dataset_id"]
            )
            observed_b = RecordingClient(b)
            with pytest.raises(PilotCheckError):
                await verify_pilot(observed_a, observed_b, checkpoint)
            assert all(
                name != "measurement_create_experiment"
                for name, _ in observed_a.calls + observed_b.calls
            )

    asyncio.run(exercise())


def test_same_principal_configuration_stops_before_non_idempotent_import(tmp_path):
    port = free_port()
    with synthetic_host(tmp_path, port):

        async def exercise():
            async with clients(port, same_account=True) as (a, b):
                observed_a, observed_b = RecordingClient(a), RecordingClient(b)
                with pytest.raises(PilotCheckError):
                    await prepare_pilot(observed_a, observed_b, run_id=uuid.uuid4().hex)
                assert [name for name, _ in observed_a.calls + observed_b.calls] == [
                    "measurement_create_experiment",
                    "measurement_create_experiment",
                ]

        asyncio.run(exercise())


@pytest.mark.r_backend
def test_numeric_private_checkpoint_requires_complete_owned_evidence_and_never_reanalyzes(tmp_path):
    port = free_port()
    with synthetic_host(tmp_path, port, r_backend=True):

        async def exercise():
            async with clients(port) as (a, b):
                checkpoint = await prepare_pilot(a, b, run_id=uuid.uuid4().hex, numerical=True)
                assert validate_checkpoint(checkpoint) == checkpoint
                for changed in ([], checkpoint["numeric"]["a"]["records"][:1]):
                    altered = copy.deepcopy(checkpoint)
                    altered["numeric"]["a"]["records"] = changed
                    with pytest.raises(ValueError, match="Invalid pilot checkpoint"):
                        await verify_pilot(NoCalls(), NoCalls(), altered)
                observed_a, observed_b = RecordingClient(a), RecordingClient(b)
                report = await verify_pilot(observed_a, observed_b, checkpoint)
                assert report["counts"]["analyses"] == 4
                assert report["checks"]["numerical"] is True
                for name, _ in observed_a.calls + observed_b.calls:
                    assert name not in {
                        "measurement_analyze_chromatogram",
                        "measurement_analyze_sec",
                        "measurement_register_csv",
                    }
                # An always-denying evidence getter is broken, not proof of
                # isolation; positive owner reads must make this fail.
                with pytest.raises(PilotCheckError):
                    await verify_pilot(RecordingClient(a, evidence_unavailable=True), b, checkpoint)

        asyncio.run(exercise())
