"""Contract tests for complete public result-table import collection."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from measure_service import result_table_client as client_module
from measure_service.result_table_client import collect_result_table_import
from measure_service.result_table_models import ResultTableMapping, ResultTableSpec
from measure_service.result_table_parser import parse_result_table

IMPORT_ID = "import-1"
INPUT_SHA256 = "1" * 64
RESULT_IDS = ("result-1", "result-2")
RESULT_DIGESTS = ("2" * 64, "3" * 64)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _source() -> bytes:
    return b"attempt,revision,metric,status,unit,value\r\na-1,1,area,observed,1,0\r\na-1,1,area2,observed,1,2\r\n"


def _result(metric: str, value: int) -> dict:
    return {
        "metric": metric,
        "analyte_reference": None,
        "status": "observed",
        "unit": "1",
        "value": value,
        "lower_bound": None,
        "upper_bound": None,
        "analysis_id": None,
        "summary_field": None,
        "supersedes_result_id": None,
        "explanation": "",
    }


def _mapping() -> dict:
    return {
        "columns": {
            "attempt_id": "attempt",
            "expected_attempt_revision": "revision",
            "metric": "metric",
            "status": "status",
            "unit": "unit",
            "value": "value",
        },
        "constants": {},
    }


def _base_row(ordinal: int, result: dict) -> dict:
    return {
        "ordinal": ordinal,
        "record_number": ordinal + 2,
        "line_start": ordinal + 2,
        "line_end": ordinal + 2,
        "attempt_id": "a-1",
        "expected_attempt_revision": 1,
        "result": result,
    }


@dataclass(frozen=True)
class FakePlanRow:
    ordinal: int
    record_number: int
    line_start: int
    line_end: int
    attempt_id: str
    expected_attempt_revision: int
    result: dict
    row_sha256: str
    result_json: str


@dataclass(frozen=True)
class FakePlan:
    source_bytes: bytes
    source_sha256: str
    mapping_sha256: str
    input_sha256: str
    source_name: str
    delimiter: str
    headers: tuple[str, ...]
    unmapped_columns: tuple[str, ...]
    mapping_json: str
    rows: tuple[FakePlanRow, ...]
    _metadata: dict

    def metadata(self) -> dict:
        return copy.deepcopy(self._metadata)


def _fixture() -> tuple[dict, FakePlan, list[dict]]:
    source = _source()
    mapping = _mapping()
    results = [_result("area", 0), _result("area2", 2)]
    rows = []
    pages_rows = []
    for ordinal, result in enumerate(results):
        base = _base_row(ordinal, result)
        row_digest = _sha(base)
        plan_row = FakePlanRow(
            **base,
            row_sha256=row_digest,
            result_json=_canonical(result),
        )
        rows.append(plan_row)
        pages_rows.append(
            {
                **base,
                "row_sha256": row_digest,
                "result_id": RESULT_IDS[ordinal],
                "result_sha256": RESULT_DIGESTS[ordinal],
            }
        )
    source_sha256 = hashlib.sha256(source).hexdigest()
    mapping_sha256 = _sha(mapping)
    metadata = {
        "profile": "measure-result-table-plan/1",
        "schema_version": "1.0",
        "source_name": "results.csv",
        "delimiter": ",",
        "source_bytes": len(source),
        "source_sha256": source_sha256,
        "mapping": mapping,
        "mapping_sha256": mapping_sha256,
        "headers": ["attempt", "revision", "metric", "status", "unit", "value"],
        "unmapped_columns": [],
        "row_count": 2,
        "attempt_count": 1,
        "status_counts": {"observed": 2},
    }
    bindings = [
        {key: page_row[key] for key in ("ordinal", "row_sha256", "result_id", "result_sha256")}
        for page_row in pages_rows
    ]
    receipt = {
        **metadata,
        "profile": "measure-result-table-import/1",
        "import_id": IMPORT_ID,
        "input_sha256": INPUT_SHA256,
        "created_at": "2026-11-08T00:00:00+00:00",
        "result_bindings_sha256": _sha(bindings),
    }
    plan = FakePlan(
        source_bytes=source,
        source_sha256=source_sha256,
        mapping_sha256=mapping_sha256,
        input_sha256=INPUT_SHA256,
        source_name="results.csv",
        delimiter=",",
        headers=tuple(metadata["headers"]),
        unmapped_columns=(),
        mapping_json=_canonical(mapping),
        rows=tuple(rows),
        _metadata=metadata,
    )
    return receipt, plan, pages_rows


def _real_parser_fixture():
    source = _source()
    plan = parse_result_table(
        ResultTableSpec(
            source_name="results.csv",
            csv_text=source.decode("utf-8"),
            mapping=ResultTableMapping(**_mapping()),
        )
    )
    rows = []
    result_sha256s = ("4" * 64, "5" * 64)
    for ordinal, parsed_row in enumerate(plan.rows):
        rows.append(
            {
                **parsed_row.as_dict(),
                "row_sha256": parsed_row.row_sha256,
                "result_id": f"result-{ordinal + 1}",
                "result_sha256": result_sha256s[ordinal],
            }
        )
    bindings = [
        {key: row[key] for key in ("ordinal", "row_sha256", "result_id", "result_sha256")}
        for row in rows
    ]
    receipt = {
        **plan.metadata(),
        "profile": "measure-result-table-import/1",
        "import_id": IMPORT_ID,
        "input_sha256": plan.input_sha256,
        "created_at": "2026-11-08T00:00:00+00:00",
        "result_bindings_sha256": _sha(bindings),
    }
    return receipt, plan, rows


class FakeClient:
    def __init__(self, receipt: dict, source: bytes, rows: list[dict]):
        self.receipt = receipt
        self.source = source
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []
        self.mutate = None

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "measurement_get_result_table_import":
            response = copy.deepcopy(self.receipt)
        elif name == "measurement_read_result_table_source":
            offset = arguments["offset"]
            chunk = self.source[offset : offset + 3]
            end = offset + len(chunk)
            response = {
                "profile": "measure-result-table-source/1",
                "import_id": IMPORT_ID,
                "input_sha256": self.receipt["input_sha256"],
                "source_sha256": self.receipt["source_sha256"],
                "offset": offset,
                "total_bytes": len(self.source),
                "next_offset": end if end < len(self.source) else None,
                "encoding": "base64",
                "content_base64": base64.b64encode(chunk).decode(),
            }
        elif name == "measurement_read_result_table_rows":
            offset = arguments["offset"]
            chunk = self.rows[offset : offset + 1]
            end = offset + len(chunk)
            response = {
                "profile": "measure-result-table-rows/1",
                "import_id": IMPORT_ID,
                "input_sha256": self.receipt["input_sha256"],
                "offset": offset,
                "limit": arguments["limit"],
                "total_count": len(self.rows),
                "next_offset": end if end < len(self.rows) else None,
                "rows": copy.deepcopy(chunk),
            }
        else:
            raise AssertionError(f"unexpected public tool: {name}")
        if self.mutate is not None:
            response = self.mutate(name, response)
        return SimpleNamespace(isError=False, structuredContent=response, content=[])


@pytest.fixture
def declared(monkeypatch):
    receipt, plan, rows = _fixture()
    monkeypatch.setattr(client_module, "_make_plan", lambda _receipt, _csv_text: plan)
    client = FakeClient(receipt, plan.source_bytes, rows)
    return client, receipt, plan


def test_collects_exact_source_rows_and_normalized_plan(declared):
    client, receipt, plan = declared
    collected = asyncio.run(collect_result_table_import(client, IMPORT_ID))

    assert collected["receipt"] == receipt
    assert collected["csv_text"] == plan.source_bytes.decode("utf-8")
    assert collected["rows"] == client.rows
    assert [name for name, _ in client.calls] == [
        "measurement_get_result_table_import",
        *(["measurement_read_result_table_source"] * ((len(plan.source_bytes) + 2) // 3)),
        "measurement_read_result_table_rows",
        "measurement_read_result_table_rows",
    ]
    source_calls = [
        arguments
        for name, arguments in client.calls
        if name == "measurement_read_result_table_source"
    ]
    row_calls = [
        arguments
        for name, arguments in client.calls
        if name == "measurement_read_result_table_rows"
    ]
    assert all(arguments["limit"] == client_module.MAX_SOURCE_PAGE for arguments in source_calls)
    assert all(arguments["limit"] == client_module.MAX_ROW_PAGE for arguments in row_calls)


def test_collects_against_the_real_normalized_parser_plan():
    receipt, plan, rows = _real_parser_fixture()
    client = FakeClient(receipt, plan.source_bytes, rows)

    collected = asyncio.run(collect_result_table_import(client, IMPORT_ID))

    assert collected["receipt"] == receipt
    assert collected["csv_text"] == plan.source_bytes.decode("utf-8")
    assert collected["rows"] == rows


@pytest.mark.parametrize(
    "mutation",
    [
        "source_truncated",
        "source_wrong_import",
        "row_duplicate_id",
        "row_duplicate_digest",
        "binding_digest",
    ],
)
def test_collector_fails_closed_on_page_or_binding_tampering(declared, mutation):
    client, receipt, _plan = declared

    def mutate(name, response):
        if mutation == "source_truncated" and name == "measurement_read_result_table_source":
            response["next_offset"] = None
        elif mutation == "source_wrong_import" and name == "measurement_read_result_table_source":
            response["import_id"] = "other-import"
        elif mutation == "row_duplicate_id" and name == "measurement_read_result_table_rows":
            response["rows"][0]["result_id"] = client.rows[0]["result_id"]
        elif (
            mutation == "row_duplicate_digest"
            and name == "measurement_read_result_table_rows"
            and response["offset"] == 1
        ):
            response["rows"][0]["result_sha256"] = client.rows[0]["result_sha256"]
        elif mutation == "binding_digest" and name == "measurement_get_result_table_import":
            response["result_bindings_sha256"] = "f" * 64
        return response

    client.mutate = mutate
    with pytest.raises(ValueError):
        asyncio.run(collect_result_table_import(client, IMPORT_ID))


def test_collector_rejects_normalized_plan_drift_before_returning_rows(declared, monkeypatch):
    client, _receipt, plan = declared
    bad_metadata = plan.metadata()
    bad_metadata["headers"] = ["changed"]
    monkeypatch.setattr(
        client_module,
        "_make_plan",
        lambda _receipt, _csv_text: FakePlan(
            source_bytes=plan.source_bytes,
            source_sha256=plan.source_sha256,
            mapping_sha256=plan.mapping_sha256,
            input_sha256=plan.input_sha256,
            source_name=plan.source_name,
            delimiter=plan.delimiter,
            headers=("changed",),
            unmapped_columns=plan.unmapped_columns,
            mapping_json=plan.mapping_json,
            rows=plan.rows,
            _metadata=bad_metadata,
        ),
    )
    with pytest.raises(ValueError, match="headers"):
        asyncio.run(collect_result_table_import(client, IMPORT_ID))


def test_collector_rejects_oversized_timestamp_before_serialization(declared, monkeypatch):
    client, _receipt, _plan = declared
    client.receipt["created_at"] = "x" * 1_000_000
    monkeypatch.setattr(
        client_module.json,
        "dumps",
        lambda *_args, **_kwargs: pytest.fail("oversized receipt was serialized"),
    )

    with pytest.raises(ValueError, match="JSON string bound"):
        asyncio.run(collect_result_table_import(client, IMPORT_ID))
    assert [name for name, _ in client.calls] == ["measurement_get_result_table_import"]


@pytest.mark.parametrize("created_at", ["2026-11-08T00:00:00", "not-a-timestamp"])
def test_collector_requires_aware_iso_timestamp(declared, created_at):
    client, _receipt, _plan = declared
    client.receipt["created_at"] = created_at

    with pytest.raises(ValueError, match="(ISO timestamp|timezone offset)"):
        asyncio.run(collect_result_table_import(client, IMPORT_ID))
    assert [name for name, _ in client.calls] == ["measurement_get_result_table_import"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_name", "s" * 201, "source_name"),
        ("unmapped_columns", ["missing-header"], "unmapped_columns"),
        (
            "status_counts",
            {"observed": 1, "unexpected": 1},
            "unknown outcome status",
        ),
    ],
)
def test_collector_rejects_oversized_or_inconsistent_receipt_metadata(
    declared, field, value, message
):
    client, _receipt, _plan = declared
    client.receipt[field] = value

    with pytest.raises(ValueError, match=message):
        asyncio.run(collect_result_table_import(client, IMPORT_ID))
    assert [name for name, _ in client.calls] == ["measurement_get_result_table_import"]


def test_collector_rejects_mapping_over_its_canonical_byte_bound(declared):
    client, _receipt, _plan = declared
    client.receipt["mapping"]["constants"] = {"explanation": "x" * 8_001}

    with pytest.raises(ValueError, match="mapping.*canonical JSON"):
        asyncio.run(collect_result_table_import(client, IMPORT_ID))
    assert [name for name, _ in client.calls] == ["measurement_get_result_table_import"]


def test_collector_rejects_oversized_source_page_before_decoding(declared):
    client, _receipt, _plan = declared

    def mutate(name, response):
        if name == "measurement_read_result_table_source":
            response["content_base64"] = "A" * 64_001
        return response

    client.mutate = mutate
    with pytest.raises(ValueError, match="string beyond the JSON string bound"):
        asyncio.run(collect_result_table_import(client, IMPORT_ID))


@pytest.mark.parametrize(
    "mutation_kind",
    ["oversized_page", "deep_result", "node_flood", "nonfinite_result"],
)
def test_collector_rejects_unbounded_or_non_json_row_pages(declared, mutation_kind):
    client, _receipt, _plan = declared

    def mutate(name, response):
        if mutation_kind == "oversized_page" and name == "measurement_read_result_table_rows":
            response["rows"][0]["result"]["explanation"] = "x" * 64_001
        elif mutation_kind == "deep_result" and name == "measurement_read_result_table_rows":
            nested: dict[str, object] = {"leaf": 1}
            for _ in range(40):
                nested = {"nested": nested}
            response["rows"][0]["result"]["nested"] = nested
        elif mutation_kind == "node_flood" and name == "measurement_read_result_table_rows":
            response["rows"][0]["result"]["nodes"] = [None] * 10_001
        elif mutation_kind == "nonfinite_result" and name == "measurement_read_result_table_rows":
            response["rows"][0]["result"]["value"] = float("nan")
        return response

    client.mutate = mutate
    with pytest.raises(ValueError, match="(bound|non-finite)"):
        asyncio.run(collect_result_table_import(client, IMPORT_ID))


def test_collector_rejects_aggregate_row_documents_before_accumulating(declared):
    client, receipt, _plan = declared
    rows = []
    for ordinal in range(50):
        result = _result(f"metric-{ordinal}", ordinal)
        result["explanation"] = "x" * 1_500
        base = _base_row(ordinal, result)
        base["attempt_id"] = f"attempt-{ordinal}"
        base["record_number"] = ordinal + 2
        base["line_start"] = ordinal + 2
        base["line_end"] = ordinal + 2
        row_digest = _sha(base)
        rows.append(
            {
                **base,
                "row_sha256": row_digest,
                "result_id": f"result-{ordinal}",
                "result_sha256": f"{ordinal + 1:064x}",
            }
        )
    client.rows = rows
    client.receipt = copy.deepcopy(receipt)
    client.receipt["row_count"] = len(rows)
    client.receipt["attempt_count"] = len(rows)
    client.receipt["status_counts"] = {"observed": len(rows)}
    bindings = [
        {key: row[key] for key in ("ordinal", "row_sha256", "result_id", "result_sha256")}
        for row in rows
    ]
    client.receipt["result_bindings_sha256"] = _sha(bindings)

    with pytest.raises(ValueError, match="collected .* rows exceed"):
        asyncio.run(collect_result_table_import(client, IMPORT_ID))


@pytest.mark.parametrize(
    "value",
    [
        ["x" * 40_000, "y" * 40_000],
        {"a": "é" * 20_000, "b": "ñ" * 20_000},
        ["\x01" * 11_000],
        [10**800] * 100,
    ],
)
def test_aggregate_json_budget_rejects_before_serialization(value, monkeypatch):
    monkeypatch.setattr(
        client_module.json,
        "dumps",
        lambda *_a, **_k: pytest.fail("oversized aggregate was serialized"),
    )
    with pytest.raises(ValueError, match="canonical JSON limit"):
        client_module._canonical(value, "untrusted page")


def test_mapping_budget_is_exact_before_serialization(monkeypatch):
    assert len(client_module._canonical({"v": "x" * 7_992}, max_bytes=8_000).encode()) == 8_000
    monkeypatch.setattr(
        client_module.json,
        "dumps",
        lambda *_a, **_k: pytest.fail("oversized mapping was serialized"),
    )
    with pytest.raises(ValueError, match="8000-byte canonical JSON limit"):
        client_module._canonical({"v": "x" * 7_993}, "mapping", max_bytes=8_000)
