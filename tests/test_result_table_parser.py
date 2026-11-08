from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import FrozenInstanceError

import pytest

from measure_service import storage
from measure_service.result_table_models import ResultTableMapping, ResultTableSpec
from measure_service.result_table_parser import parse_result_table, preview_result_table

_HEADERS = [
    "attempt_id",
    "revision",
    "metric",
    "status",
    "unit",
    "analyte",
    "value",
    "lower",
    "upper",
    "analysis",
    "summary",
    "supersedes",
    "explanation",
]
_COLUMNS = {
    "attempt_id": "attempt_id",
    "expected_attempt_revision": "revision",
    "metric": "metric",
    "status": "status",
    "unit": "unit",
    "analyte_reference": "analyte",
    "value": "value",
    "lower_bound": "lower",
    "upper_bound": "upper",
    "analysis_id": "analysis",
    "summary_field": "summary",
    "supersedes_result_id": "supersedes",
    "explanation": "explanation",
}


def _csv(rows, *, delimiter=","):
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter, lineterminator="\n")
    writer.writerow(_HEADERS)
    writer.writerows(rows)
    return output.getvalue()


def _row(
    attempt="attempt-1",
    revision="1",
    metric="area",
    status="observed",
    unit="mAU",
    analyte="compound-1",
    value="0",
    lower="",
    upper="",
    analysis="",
    summary="",
    supersedes="",
    explanation="",
):
    return [
        attempt,
        revision,
        metric,
        status,
        unit,
        analyte,
        value,
        lower,
        upper,
        analysis,
        summary,
        supersedes,
        explanation,
    ]


def _spec(csv_text, *, columns=None, constants=None, delimiter=","):
    return ResultTableSpec(
        source_name="results.csv",
        csv_text=csv_text,
        mapping=ResultTableMapping(
            columns=_COLUMNS if columns is None else columns,
            constants={} if constants is None else constants,
        ),
        delimiter=delimiter,
    )


def test_parse_result_table_preserves_raw_identity_and_normalizes_rows():
    csv_text = (
        "\ufeffattempt,revision,metric,status,unit,analyte,value,note,raw\r\n"
        'attempt-1,1,area,observed,mAU,compound-1,0,"quoted, note",extra-1\r\n'
        'attempt-1,1,area_2,missing,1,compound-2,,"line one\nline two",extra-2\r\n'
    )
    spec = ResultTableSpec(
        source_name="results.csv",
        csv_text=csv_text,
        mapping=ResultTableMapping(
            columns={
                "attempt_id": "attempt",
                "expected_attempt_revision": "revision",
                "metric": "metric",
                "status": "status",
                "unit": "unit",
                "analyte_reference": "analyte",
                "value": "value",
                "explanation": "note",
            }
        ),
    )

    plan = parse_result_table(spec)

    assert plan.source_bytes == csv_text.encode("utf-8")
    assert plan.source_sha256 == hashlib.sha256(plan.source_bytes).hexdigest()
    assert plan.headers == (
        "attempt",
        "revision",
        "metric",
        "status",
        "unit",
        "analyte",
        "value",
        "note",
        "raw",
    )
    assert plan.unmapped_columns == ("raw",)
    assert plan.rows[0].line_start == 2
    assert plan.rows[0].line_end == 2
    assert plan.rows[1].line_start == 3
    assert plan.rows[1].line_end == 4
    assert plan.rows[0].result_spec().value == 0
    assert plan.rows[1].result_spec().status == "missing"
    assert plan.rows[1].result_spec().value is None
    assert plan.rows[0].as_dict()["result"]["metric"] == "area"
    assert "row_sha256" not in plan.rows[0].as_dict()


def test_result_table_parser_exports_contract_symbols():
    assert ResultTableMapping(columns={}).constants == {}
    assert ResultTableSpec.model_config["extra"] == "forbid"
    assert ResultTableSpec.model_validate(
        {
            "source_name": "results.csv",
            "csv_text": "attempt,revision,metric,status,unit\na,1,m,observed,1\n",
            "mapping": {
                "columns": {
                    "attempt_id": "attempt",
                    "expected_attempt_revision": "revision",
                    "metric": "metric",
                    "status": "status",
                    "unit": "unit",
                }
            },
        }
    )


def test_plan_and_preview_digests_are_canonical_and_rows_are_immutable():
    plan = parse_result_table(_spec(_csv([_row()])))

    expected_metadata = plan.metadata()
    assert plan.input_sha256 == storage.sha256(
        {"metadata": expected_metadata, "rows": [plan.rows[0].as_dict()]}
    )
    assert plan.mapping_sha256 == storage.sha256(
        ResultTableMapping(columns=_COLUMNS).model_dump(mode="json")
    )
    assert plan.rows[0].row_sha256 == storage.sha256(plan.rows[0].as_dict())
    assert plan.rows[0].result_spec() is not plan.rows[0].result_spec()
    with pytest.raises(FrozenInstanceError):
        plan.rows[0].ordinal = 4
    assert plan.metadata() == expected_metadata

    page = preview_result_table(_spec(_csv([_row(), _row(metric="height")])), limit=1)
    assert page["profile"] == "measure-result-table-plan/1"
    assert page["offset"] == 0
    assert page["limit"] == 1
    assert page["total_count"] == 2
    assert page["next_offset"] == 1
    assert page["rows"][0]["row_sha256"]
    assert "input_sha256" in page


@pytest.mark.parametrize("delimiter", [";", "\t"])
def test_delimiters_are_explicit_and_rows_keep_source_order(delimiter):
    plan = parse_result_table(
        _spec(
            _csv(
                [
                    _row(metric="first"),
                    _row(metric="second", attempt="attempt-2"),
                ],
                delimiter=delimiter,
            ),
            delimiter=delimiter,
        )
    )
    assert plan.delimiter == delimiter
    assert [row.ordinal for row in plan.rows] == [0, 1]
    assert [row.result_spec().metric for row in plan.rows] == ["first", "second"]


def test_outcome_semantics_preserve_zero_null_censoring_and_analysis_values():
    rows = [
        _row(metric="zero", value="0"),
        _row(
            metric="missing",
            status="missing",
            value="",
            explanation="not acquired",
            analyte="compound-2",
        ),
        _row(
            metric="censored",
            status="censored",
            value="",
            lower="0",
            upper="10",
            explanation="outside range",
            analyte="compound-3",
        ),
        _row(
            metric="failed",
            status="failed",
            value="",
            explanation="instrument error",
            analyte="compound-4",
        ),
        _row(
            metric="derived",
            value="",
            analysis="analysis-1",
            summary="peak_area",
            analyte="compound-5",
        ),
    ]
    plan = parse_result_table(_spec(_csv(rows)))
    results = [row.result_spec() for row in plan.rows]
    assert results[0].value == 0
    assert results[1].value is None and results[1].explanation == "not acquired"
    assert results[2].lower_bound == 0 and results[2].upper_bound == 10
    assert results[3].status == "failed"
    assert results[4].analysis_id == "analysis-1"
    assert results[4].summary_field == "peak_area" and results[4].value is None
    assert plan.metadata()["status_counts"] == {
        "observed": 2,
        "missing": 1,
        "censored": 1,
        "failed": 1,
    }


def test_strings_are_not_trimmed_or_case_folded_and_status_aliases_fail():
    result = (
        parse_result_table(_spec(_csv([_row(metric=" area ", unit=" mAU ")]))).rows[0].result_spec()
    )
    assert result.metric == " area "
    assert result.unit == " mAU "
    with pytest.raises(ValueError):
        parse_result_table(_spec(_csv([_row(status="Observed")])))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("value", "true"),
        ("value", "NaN"),
        ("value", "1,2"),
        ("value", "1 + 2"),
        ("value", "1e309"),
        ("value", "1e-324"),
        ("value", "1."),
        ("value", "+1"),
        ("value", "0x1"),
        ("revision", "1.0"),
        ("revision", "+1"),
        ("revision", "1e1"),
        ("revision", "0"),
    ],
)
def test_numeric_and_revision_cells_are_strict(field, value):
    row = _row(value=value) if field == "value" else _row(revision=value)
    with pytest.raises(ValueError):
        parse_result_table(_spec(_csv([row])))


def test_numeric_boundaries_accept_binary64_subnormal_and_reject_long_text():
    plan = parse_result_table(_spec(_csv([_row(value="5e-324")])))
    assert plan.rows[0].result_spec().value == 5e-324
    with pytest.raises(ValueError):
        parse_result_table(_spec(_csv([_row(value="1" * 101)])))
    with pytest.raises(ValueError):
        parse_result_table(_spec(_csv([_row(attempt=" " * 101)])))


def test_native_constants_use_strict_typed_values_and_may_supply_required_fields():
    plan = parse_result_table(
        _spec(
            "metric,status\narea,observed\n",
            columns={"metric": "metric", "status": "status"},
            constants={
                "attempt_id": "attempt-constant",
                "expected_attempt_revision": 2,
                "unit": "mAU",
                "value": 0,
            },
        )
    )
    assert plan.rows[0].attempt_id == "attempt-constant"
    assert plan.rows[0].expected_attempt_revision == 2
    assert plan.rows[0].result_spec().value == 0
    with pytest.raises(ValueError):
        parse_result_table(
            _spec(
                "metric,status\narea,observed\n",
                columns={"metric": "metric", "status": "status"},
                constants={
                    "attempt_id": "attempt-constant",
                    "expected_attempt_revision": 2,
                    "unit": "mAU",
                    "value": "0",
                },
            )
        )


@pytest.mark.parametrize(
    "rows",
    [
        [_row(), _row()],
        [_row(revision="2"), _row(metric="height")],
    ],
)
def test_duplicate_outcomes_and_inconsistent_attempt_revisions_are_rejected(rows):
    with pytest.raises(ValueError):
        parse_result_table(_spec(_csv(rows)))


def test_mapping_and_csv_shape_errors_are_rejected():
    with pytest.raises(ValueError):
        parse_result_table(_spec(""))
    with pytest.raises(ValueError):
        parse_result_table(_spec("\ufeff"))
    with pytest.raises(ValueError):
        parse_result_table(_spec("attempt_id,revision,metric,status,unit\na,1,m,observed\n"))
    with pytest.raises(ValueError):
        parse_result_table(_spec("attempt_id,revision,metric,status,unit\na,1,m,observed,1\n\n"))
    with pytest.raises(ValueError):
        parse_result_table(
            _spec('attempt_id,revision,metric,status,unit\na,1,"unterminated,observed,1\n')
        )
    with pytest.raises(ValueError):
        parse_result_table(
            _spec(
                _csv([_row()]),
                columns={key: value for key, value in _COLUMNS.items() if key != "unit"},
            )
        )
    with pytest.raises(ValueError):
        ResultTableMapping(
            columns={"attempt_id": "id", "metric": "id"},
        )
    with pytest.raises(ValueError):
        ResultTableMapping(
            columns={"metric": "metric"},
            constants={"metric": "area"},
        )
    with pytest.raises(ValueError):
        ResultTableMapping(columns={"not_a_result_field": "column"})


def test_source_limits_and_unicode_rules_are_bounded():
    extra = "x" * (64_000 - len("attempt,revision,metric,status,unit,extra\na,1,m,observed,1,"))
    exact = "attempt,revision,metric,status,unit,extra\na,1,m,observed,1," + extra
    mapping = {
        "attempt_id": "attempt",
        "expected_attempt_revision": "revision",
        "metric": "metric",
        "status": "status",
        "unit": "unit",
    }
    assert len(exact.encode()) == 64_000
    assert (
        parse_result_table(_spec(exact, columns=mapping, constants={"value": 0})).source_bytes
        == exact.encode()
    )
    with pytest.raises(ValueError):
        parse_result_table(_spec(exact + "x", columns=mapping, constants={"value": 0}))
    with pytest.raises(ValueError):
        parse_result_table(
            _spec("attempt,revision,metric,status,unit\na,1,m,observed,1\x00\n", columns=mapping)
        )
    with pytest.raises(ValueError):
        parse_result_table(
            _spec("attempt,revision,metric,status,unit\na,1,m,observed,1\ud800\n", columns=mapping)
        )


def test_record_and_plan_limits_are_enforced():
    rows = [_row(metric=f"metric-{index}") for index in range(50)]
    assert len(parse_result_table(_spec(_csv(rows))).rows) == 50
    with pytest.raises(ValueError):
        parse_result_table(_spec(_csv(rows + [_row(metric="metric-50")])))
    large_rows = [_row(metric=f"metric-{index}", explanation="x" * 1800) for index in range(25)]
    with pytest.raises(ValueError, match="plan"):
        parse_result_table(_spec(_csv(large_rows)))


def test_preview_bounds_are_explicit():
    spec = _spec(_csv([_row()]))
    assert preview_result_table(spec, offset=1, limit=20)["rows"] == []
    with pytest.raises(ValueError):
        preview_result_table(spec, offset=2)
    with pytest.raises(ValueError):
        preview_result_table(spec, limit=0)
    with pytest.raises(ValueError):
        preview_result_table(spec, limit=21)
    with pytest.raises(ValueError):
        preview_result_table(spec, offset=True)


def test_oversized_character_source_rejects_before_utf8_allocation():
    class EncodingTripwire(str):
        def encode(self, *args, **kwargs):
            raise AssertionError("Oversized source must reject before UTF8 allocation")

    mapping = ResultTableMapping(columns={})
    spec = ResultTableSpec.model_construct(
        source_name="oversized.csv",
        csv_text=EncodingTripwire("x" * 64_001),
        mapping=mapping,
        delimiter=",",
    )
    with pytest.raises(ValueError, match="byte limit"):
        parse_result_table(spec)


@pytest.mark.parametrize("value", ['abc"def', '"abc"tail', ' "abc"'])
def test_unescaped_quotes_in_unmapped_fields_are_not_accepted_as_strict_csv(value):
    spec = ResultTableSpec(
        source_name="quoted.csv",
        csv_text=f"notes\n{value}\n",
        mapping=ResultTableMapping(
            columns={},
            constants={
                "attempt_id": "attempt",
                "expected_attempt_revision": 1,
                "metric": "area",
                "status": "observed",
                "unit": "1",
                "value": 0,
            },
        ),
    )
    with pytest.raises(ValueError, match="strict CSV"):
        parse_result_table(spec)
