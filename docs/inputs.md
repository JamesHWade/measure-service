# Input formats

The client supplies data in tool arguments. The service does not open a
caller-supplied path, fetch a URL, or resolve an uploaded asset. Convert or
extract files on the client side before calling these tools. An authenticated
asset bridge is planned separately.

| Input | Tool | Mapping |
| --- | --- | --- |
| Numeric arrays | `measurement_register_dataset` | A title and a list of traces |
| One axis and signal | `measurement_register_csv` | Axis column, signal column, trace metadata |
| Shared axis, several signals | `measurement_register_wide_csv` | Axis column and a signal-column/metadata entry per channel |
| Row groups with independent axes | `measurement_register_long_csv` | Axis/signal columns, group columns and explicit group-value/metadata entries |

Delimited tools accept comma, tab or semicolon; the caller selects the
delimiter. They do not guess columns, units, detector type or group identity.
Extra unmapped columns are ignored. Extra row fields, missing selected numeric
values, duplicate headers and unmapped row groups are rejected. Rows within
each trace must already be in increasing axis order. No sorting, interpolation,
imputation or resampling occurs during registration.

## Trace metadata

Every trace declares `trace_id`, `sample_id`, `injection_id`, `channel`,
`separation`, `detector`, `axis_unit` and `signal_unit`. Separation is one of
`LC`, `GC`, `CE`, `SEC` or `other`. Optional fields are `sample_type`,
`batch_id` and `run_order`; the default sample type is `unknown`.

Trace IDs are unique within a dataset. An injection/channel pair must also be
unique. Channels belonging to one injection must agree on sample ID, sample
type, batch, run order and separation. Distinct injections can share a sample
ID. Registration records these assertions; it does not verify laboratory IDs
or synchronize detector clocks.

A complete single-signal request to `measurement_register_csv`:

```json
{
  "title": "LC scouting injection",
  "csv_text": "time,uv\n0,0\n1,2\n2,0\n",
  "axis_column": "time",
  "signal_column": "uv",
  "metadata": {
    "trace_id": "uv-254",
    "sample_id": "sample-01",
    "injection_id": "injection-01",
    "channel": "uv-254",
    "separation": "LC",
    "detector": "UV",
    "axis_unit": "min",
    "signal_unit": "mAU"
  }
}
```

For wide input, replace `signal_column` and `metadata` with `channels`, for
example `[{"column": "uv", "metadata": ...}]`, supplying a complete metadata
object for each signal. For long input, supply `group_columns` and `groups`;
each group's `values` is an ordered list of strings matching the corresponding
row fields. Groups may have different axis values and lengths. See the live
[MCP schemas](tools.md) for exact argument shapes.

## Limits and retained data

| Item | Limit |
| --- | --- |
| Traces per dataset | 32 |
| Points per trace | 3–50,000 |
| Total points per dataset | 150,000 |
| Raw delimited text | 4,000,000 UTF-8 bytes |
| Normalized dataset | 4,000,000 serialized UTF-8 bytes |
| Analysis array page | 1,000 entries |
| Source-text page | 1,000 characters |

Axis and signal arrays must have equal lengths and finite numeric values.
Axes must be strictly increasing and unique. Registering an irregular grid is
allowed; some preprocessing methods operate in sample-index space and return
an irregular-grid warning. Unit strings are declarations, not a unit-conversion
system. SEC accepts axis units `min` or `mL`.

Delimited registration retains the original text, exact UTF-8 SHA-256 digest
and explicit column/group mapping separately from the canonical numeric
record. Structured registration retains the normalized dataset, not the exact
JSON formatting of the request. `measurement_inspect_dataset` returns metadata
without arrays. `measurement_read_source` pages retained text using character
offsets; its digest covers the complete original text. It errors for structured
input because no original delimited text exists.

A repeated dataset-registration call creates a new ID. Input digests support
comparison; they do not make registration idempotent.
