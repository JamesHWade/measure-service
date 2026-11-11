"""MCP result contract: declared retry hints and record ids in text content.

Every call goes through an in-memory MCP client session connected to the
registered server, so results carry the SDK's own text rendering. The R worker
is replaced by a constructed response; no numerical analysis runs here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from measure_service import batch_exports, server
from measure_service.capture_cache import CaptureCache


@pytest.fixture(autouse=True)
def isolated_service(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("MEASURE_MCP_R", str(tmp_path / "unavailable-R"))
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "local")
    monkeypatch.setattr(
        batch_exports, "_CAPTURE_CACHE", CaptureCache(max_bytes=batch_exports.MAX_EXPORT_BYTES)
    )


def _trace() -> dict[str, Any]:
    return {
        "trace_id": "trace-uv-254",
        "sample_id": "sample-constructed",
        "injection_id": "injection-constructed",
        "channel": "UV 254 nm",
        "separation": "LC",
        "detector": "UV",
        "axis_unit": "min",
        "signal_unit": "mAU",
        "axis": [0.0, 1.0, 2.0, 3.0, 4.0],
        "signal": [0.0, 2.0, 5.0, 2.0, 0.0],
    }


def _constructed_worker(payload: dict) -> dict[str, Any]:
    """Stand in for R with a fixed chromatogram result; it computes nothing."""
    assert payload["operation"] == "chromatogram"
    trace = payload["trace"]
    return {
        "kind": "chromatogram",
        "peak_count": 1,
        "peaks": [
            {
                "peak_id": 1,
                "location": 2.0,
                "height": 5.0,
                "left_base": 0.0,
                "right_base": 4.0,
                "prominence": 5.0,
                "area": 9.0,
            }
        ],
        "processed_trace": {"axis": trace["axis"], "signal": trace["signal"]},
        "warnings": ["Constructed worker response; no R calculation was performed."],
        "notes": [],
    }


def _value(response: Any) -> dict[str, Any]:
    assert not response.isError, response.content
    assert isinstance(response.structuredContent, dict)
    return response.structuredContent


def _text(response: Any) -> str:
    blocks = [block.text for block in response.content if isinstance(block, TextContent)]
    assert blocks, "The tool result has no text content"
    return "\n".join(blocks)


def _record_ids(value: Any, path: str = "") -> dict[str, str]:
    """Collect every string held by an ``id``, ``*_id`` or ``*_ids`` field."""
    found: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}" if path else key
            if isinstance(item, str) and (key == "id" or key.endswith("_id")):
                found[here] = item
            elif isinstance(item, list) and key.endswith("_ids"):
                found.update(
                    {f"{here}[{index}]": entry for index, entry in enumerate(item) if entry}
                )
            else:
                found.update(_record_ids(item, here))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.update(_record_ids(item, f"{path}[{index}]"))
    return found


async def _register_dataset(client: Any) -> Any:
    return await client.call_tool(
        "measurement_register_dataset",
        {"dataset": {"title": "Constructed contract fixture", "traces": [_trace()]}},
    )


def test_export_retry_hint_matches_repeated_identical_calls():
    async def exercise() -> None:
        async with create_connected_server_and_client_session(server.mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            annotations = tools["measurement_export_chromatogram_batch"].annotations
            dataset = _value(await _register_dataset(client))
            batch = _value(
                await client.call_tool(
                    "measurement_create_chromatogram_batch",
                    {
                        "batch": {
                            "title": "Unexecuted constructed declaration",
                            "method": {},
                            "inputs": [
                                {
                                    "dataset_id": dataset["dataset_id"],
                                    "trace_id": "trace-uv-254",
                                    "role": "reference",
                                }
                            ],
                        },
                        "idempotency_key": "create",
                    },
                )
            )
            arguments = {
                "batch_id": batch["batch_id"],
                "expected_revision": batch["revision"],
                "mode": "references",
            }
            first = _value(
                await client.call_tool("measurement_export_chromatogram_batch", arguments)
            )
            second = _value(
                await client.call_tool("measurement_export_chromatogram_batch", arguments)
            )
            unchanged = _value(
                await client.call_tool(
                    "measurement_get_chromatogram_batch", {"batch_id": batch["batch_id"]}
                )
            )

            # Each capture records its own time, so identical arguments name a
            # different snapshot. The batch itself is not modified.
            assert first["snapshot_sha256"] != second["snapshot_sha256"]
            assert {key: value for key, value in first.items() if key != "snapshot_sha256"} == {
                key: value for key, value in second.items() if key != "snapshot_sha256"
            }
            assert unchanged == batch
            assert annotations.readOnlyHint is True
            assert annotations.idempotentHint is (first == second)

    asyncio.run(exercise())


def test_text_content_names_every_record_id_in_the_structured_result(monkeypatch):
    monkeypatch.setattr(server, "run_worker", _constructed_worker)
    checked: dict[str, set[str]] = {}

    def named(tool: str, response: Any, *expected: str) -> dict[str, Any]:
        structured = _value(response)
        text = _text(response)
        identifiers = _record_ids(structured)
        assert set(expected) <= set(identifiers), (tool, sorted(identifiers))
        missing = {field: value for field, value in identifiers.items() if value not in text}
        assert not missing, f"{tool} text content omits record ids: {missing}"
        checked[tool] = set(identifiers)
        return structured

    async def exercise() -> None:
        async with create_connected_server_and_client_session(server.mcp) as client:
            dataset = named(
                "measurement_register_dataset", await _register_dataset(client), "dataset_id"
            )
            inspected = named(
                "measurement_inspect_dataset",
                await client.call_tool(
                    "measurement_inspect_dataset", {"dataset_id": dataset["dataset_id"]}
                ),
                "dataset_id",
            )
            assert inspected["dataset_id"] == dataset["dataset_id"]
            analysis = named(
                "measurement_analyze_chromatogram",
                await client.call_tool(
                    "measurement_analyze_chromatogram",
                    {
                        "dataset_id": dataset["dataset_id"],
                        "trace_id": "trace-uv-254",
                        "method": {},
                    },
                ),
                "analysis_id",
                "dataset_id",
            )
            retrieved = named(
                "measurement_get_analysis",
                await client.call_tool(
                    "measurement_get_analysis", {"analysis_id": analysis["analysis_id"]}
                ),
                "analysis_id",
                "dataset_id",
            )
            assert retrieved["analysis_id"] == analysis["analysis_id"]
            experiment = named(
                "measurement_create_experiment",
                await client.call_tool(
                    "measurement_create_experiment",
                    {
                        "spec": {"title": "Constructed contract experiment", "source": "manual"},
                        "idempotency_key": "create-experiment",
                    },
                ),
                "experiment_id",
            )
            named(
                "measurement_record_attempt",
                await client.call_tool(
                    "measurement_record_attempt",
                    {
                        "experiment_id": experiment["experiment_id"],
                        "spec": {
                            "status": "completed",
                            "measurements": [
                                {
                                    "dataset_id": dataset["dataset_id"],
                                    "trace_id": "trace-uv-254",
                                }
                            ],
                        },
                        "idempotency_key": "record-attempt",
                    },
                ),
                "attempt_id",
                "experiment_id",
            )

    asyncio.run(exercise())
    assert len(checked) == 6
