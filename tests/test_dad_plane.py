"""Exercise owned DAD reads and the digest of the portable evidence."""

import asyncio

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from measure_service import acquisition_store, analysis_runtime, dad_plane, server
from measure_service.acquisition_models import CSV_MEMBER_NAMES
from measure_service.storage import sha256


def test_reader_scopes_native_files_before_dispatch_and_hashes_the_returned_view(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    files = {name: "header\r\n" for name in CSV_MEMBER_NAMES}
    original = acquisition_store.register_acquisition(
        "alice", files, worker=lambda _: {"summary": {}}
    )
    calls = []

    def worker(request):
        calls.append(request)
        return analysis_runtime._WorkerResult(
            {
                "native_point_count": 9000,
                "array_id": "a",
                "stream_id": "s",
                "injection_id": "i",
                "channel_id": "c",
                "source_id": None,
                "time_unit": "min",
                "wavelength_unit": "nm",
                "value_unit": None,
                "points": [
                    {
                        "point_index": 1,
                        "time": 1,
                        "wavelength": 250,
                        "value": 0,
                        "source_row": None,
                        "saturated": None,
                    }
                ],
                "warnings": [],
                "packages": {"measure": "dev"},
                "installed_r_code_md5": {},
                "r_version": "test",
            },
            "a" * 64,
        )

    monkeypatch.setattr(analysis_runtime, "run_worker", worker)
    acquisition_id = original["acquisition_id"]
    with pytest.raises(ValueError, match="not found or is not accessible"):
        dad_plane.read_dad_plane("bob", acquisition_id, "a", 1)
    with pytest.raises(ValueError, match="axis"):
        dad_plane.read_dad_plane("alice", acquisition_id, "a", True)
    assert not calls
    result = dad_plane.read_dad_plane("alice", acquisition_id, "a", 1)
    assert calls == [
        {"operation": "read_dad_plane", "files": files, "array_id": "a", "time_axis": 1}
    ]
    assert result.pop("evidence_sha256") == sha256(result)
    assert result["input_sha256"] == original["input_sha256"]
    assert result["provenance"]["worker_sha256"] == "a" * 64
    assert result["points"][0]["value"] == 0
    assert result["points"][0]["source_row"] is None

    region = dad_plane.read_dad_plane("alice", acquisition_id, "a", 1, time_range=[1, 1])
    assert region["profile"] == "measure-dad-region/1"
    assert region["selection"] == {"time_range": [1.0, 1.0], "wavelength_range": None}
    assert calls[-1]["time_range"] == [1.0, 1.0]
    assert "wavelength_range" not in calls[-1]
    assert region["native_point_count"] == 9000
    assert region.pop("evidence_sha256") == sha256(region)
    assert "selection" not in result and "native_point_count" not in result


@pytest.mark.parametrize(
    "value", [[True, 2], ["1", 2], [1], [3, 2], [1, float("nan")], [0, 10**400], [0, 2**55 + 1]]
)
def test_invalid_region_bounds(value):
    with pytest.raises(ValueError):
        dad_plane.validate_range(value)
    with pytest.raises(ValueError):
        dad_plane.validate_range([0, 250], wavelength=True)


def test_dad_tool_requires_explicit_native_axes_and_uses_authenticated_owner(monkeypatch):
    monkeypatch.setattr(server.mcp, "_selected_groups", frozenset({"dad"}))
    monkeypatch.setenv("MEASURE_MCP_IDENTITY", "local")
    array_id = "detector-" + "a" * 128
    calls = []

    def reader(owner, acquisition_id, array_id, time_axis, **bounds):
        calls.append((owner, acquisition_id, array_id, time_axis, bounds))
        return {"array_id": array_id}

    monkeypatch.setattr(dad_plane, "read_dad_plane", reader)

    async def exercise():
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
            tools = (await client.list_tools()).tools
            assert [tool.name for tool in tools] == ["measurement_read_dad_plane"]
            assert tools[0].annotations.readOnlyHint
            arguments = {"acquisition_id": "owned", "array_id": array_id}
            assert (await client.call_tool(tools[0].name, arguments)).isError
            assert (await client.call_tool(tools[0].name, {**arguments, "time_axis": 3})).isError
            result = await client.call_tool(tools[0].name, {**arguments, "time_axis": 2})
            assert not result.isError and result.structuredContent == {"array_id": array_id}
            args = {**arguments, "time_axis": 1, "time_range": [1, 1]}
            assert not (await client.call_tool(tools[0].name, args)).isError
            assert (await client.call_tool(tools[0].name, {**args, "time_range": [1]})).isError

    asyncio.run(exercise())
    assert calls == [
        ("local", "owned", array_id, 2, {"time_range": None, "wavelength_range": None}),
        ("local", "owned", array_id, 1, {"time_range": [1, 1], "wavelength_range": None}),
    ]


def test_array_preview_cannot_make_a_previously_valid_registration_too_large(monkeypatch):
    from measure_service import acquisition_models

    old = {"summary": {"counts": {"arrays": 1}}}
    monkeypatch.setattr(
        acquisition_models, "MAX_SUMMARY_BYTES", len(acquisition_models.canonical(old))
    )
    added = {"summary": {**old["summary"], "arrays": [{"array_id": "long identity"}]}}
    assert acquisition_models.validate_summary(added) == old
    assert "arrays" in added["summary"]
    with pytest.raises(ValueError, match="byte limit"):
        acquisition_models.validate_summary({**added, "warnings": ["too large"]})
