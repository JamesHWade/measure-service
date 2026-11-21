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

## Native acquisition bundles

Use `measure::write_measure_acquisition()` to export a linked acquisition. Pass
a `files` object to `measurement_register_acquisition`. Each key is a native
member name and each value is the exact UTF-8 text of that file, including its
line endings. Supply `manifest.dcf` and all twelve tables in its declared
delimiter. The member texts can total up to 4,000,000 UTF-8 bytes. Empty
tables retain their headers. The R reader checks the native
manifest, table hashes and relationships before registration.

Keep the returned `acquisition_id` to inspect the same input later with
`measurement_get_acquisition`. An identical submission under the same owner
returns that acquisition again. Sample, source and unit fields retain the
declarations in the bundle.

For a centroid XIC, pass the acquisition ID, stream ID, polarity, integer MS
level and inclusive `mz_min` and `mz_max` to `measurement_extract_ms_trace`.
The installed `measure::measure_ms_trace()` calculates one value per selected
scan in scan-index order. A missing scan and a saturated selection both have
a null intensity and retain their distinct statuses. An acquired empty scan
has a measured sum of zero. The result records the R package and worker used.

Use the returned trace ID and evidence digest to page through `points` or
`source_rows` with `measurement_read_ms_trace`. Join by `scan_id`. The
one-based `point_index` identifies a centroid within its scan. `source_row`
retains the original row number when the acquisition declared one. Fetching
the same extraction again returns the original retained result.

### Reading a diode-array detector plane

Use `measurement_read_dad_plane` with an owned acquisition ID, an array ID
and `time_axis` (1 or 2). The other native dimension supplies wavelength.
Acquisition inspections include a bounded preview of array IDs when space
permits. The array count covers the entire acquisition. The native arrays
table supplies every ID, including those absent from an older inspection.

The response contains each supplied coordinate once, in native row order,
with its detector value, one-based point index, optional source row and
saturation flag. A missing coordinate contributes no row. A null detector
value belongs to an observed coordinate. The declared units accompany each
view. The service accepts up to 4,096 observed cells and a 2 MB evidence
document. Reading the array uses the current R worker and leaves the original
acquisition intact. Its evidence digest covers the view and worker provenance.

For a restricted deployment, `dad` enables the plane reader. Registration and
inspection use the existing `ms` tool group.

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
