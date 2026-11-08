# Map an outcome table explicitly

Parse one UTF8 CSV table into typed outcomes for existing experiment attempts.
Every data record becomes one outcome. The parser retains the exact source
bytes and records each row's logical ordinal and physical line range. It does
not read a path, look up an attempt, create a database, or infer a measurement.

```python
from measure_service.result_table_models import ResultTableMapping, ResultTableSpec
from measure_service.result_table_parser import preview_result_table

spec = ResultTableSpec(
    source_name="reported-outcomes.csv",
    csv_text=(
        "attempt,revision,signal,state,number,reason,plate\r\n"
        "existing-attempt,1,blank_response,observed,0,,A01\r\n"
        "existing-attempt,1,impurity_response,missing,,No result reported,A01\r\n"
    ),
    mapping=ResultTableMapping(
        columns={
            "attempt_id": "attempt",
            "expected_attempt_revision": "revision",
            "metric": "signal",
            "status": "state",
            "value": "number",
            "explanation": "reason",
        },
        constants={"unit": "mAU"},
    ),
)
preview = preview_result_table(spec)
assert preview["rows"][0]["result"]["value"] == 0
assert preview["rows"][1]["result"]["value"] is None
assert preview["unmapped_columns"] == ["plate"]
```

`existing-attempt` is a placeholder for a real attempt ID when importing. The
pure preview accepts the label because it does not resolve registry ownership
or current revisions. A successful preview does not reserve an attempt.

## Columns and constants

`mapping.columns` maps service field names to exact CSV headers.
`mapping.constants` supplies native JSON values for the entire table. Bind a
field once, using either a column or a constant. Column bindings are one-to-one;
there are no positional guesses, expressions or transformations.

Required fields are `attempt_id`, `expected_attempt_revision`, `metric`,
`status` and `unit`. Optional fields are `analyte_reference`, `value`,
`lower_bound`, `upper_bound`, `analysis_id`, `summary_field`,
`supersedes_result_id` and `explanation`. These use the existing
[result semantics](experiments.md#outcome-states).

Header names are unique, nonempty and at most 200 characters. Names and text
cells are not trimmed or case-folded. Unmapped columns remain in the raw
source; their labels are listed in metadata. An unmapped sample or plate label
does not become a validated experiment/sample link.

An empty optional cell omits that field. An empty required cell is invalid.
`observed` requires a finite numeric value, including zero, or an owned stored
analysis ID and direct numeric summary field with no supplied value. The
registry copies analysis values when committing. A preview retains that
request and does not compute the value.

Other states remain explicit: `not_detected`, `below_limit`, `censored`,
`missing`, `failed` and `infeasible`. Every non-observed state needs an
explanation; applicable censoring bounds follow the result contract. An empty
cell, `NA`, `ND`, a failed measurement or an unavailable value is never silently
converted to zero or to another status.

Numeric CSV cells use JSON number syntax: decimal numbers and scientific
notation with no surrounding whitespace, locale separators or expressions.
NaN, infinity and nonzero underflow to binary64 zero reject. Revision cells
are positive decimal integers without a sign, fraction or exponent. Native
constants retain their JSON types; a numeric string is not a numeric constant.
Units and analyte labels are caller declarations; parsing performs no conversion
or chemical assignment.

## Source records and identities

Choose comma, semicolon or tab explicitly with `delimiter`; comma is the
default. Quoted delimiters/newlines, LF/CRLF and an optional leading UTF8 BOM
are retained in the source digest. The Python CSV reader uses `strict=True`.
Quotes inside unquoted fields reject; quote the whole cell and double literal
quotes within it.
Blank data records, ragged rows, duplicate headers, invalid Unicode and NUL
bytes reject. No global CSV parser setting is changed.

`ordinal` starts at zero for the first outcome. `record_number` starts at two
because the header is record one. `line_start` and `line_end` are one-based
physical CSV line positions; a quoted multiline record can span several lines.
Those coordinates identify records, rather than asserting that every line is
one scientific observation.

The raw source SHA256 covers every original UTF8 byte, including unused
columns. The mapping digest covers its canonical columns/constants object.
The input digest covers source metadata and all complete normalized rows.
Reordering rows or changing a source name, line ending or unused cell changes
the input identity. Reordering JSON mapping keys does not. Content digests
identify bytes and normalized plans; they do not authenticate their author.

Rows for one attempt must all name the same expected revision. A table cannot
contain duplicate target `(attempt_id, metric, analyte_reference)` keys. Rows
retain source order, including when an import records explicit corrections.

## Bounds

| Item | Limit |
| --- | ---: |
| Raw UTF8 source | 64,000 bytes |
| Data records / outcomes | 50 |
| Header columns | 64 |
| Canonical mapping | 8,000 bytes |
| Complete normalized plan | 48,000 bytes |
| Numeric cell text | 100 characters |
| Preview page | 20 rows |

The existing outcome field and 64,000-byte record bounds also apply. Splitting
a larger input into separate imports creates separate transaction boundaries;
it does not make a multi-file operation atomic.
