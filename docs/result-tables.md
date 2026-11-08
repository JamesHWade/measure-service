# Import and retrieve reported outcome tables over MCP

Send UTF8 CSV text and an explicit mapping to import outcomes across existing
owned experiment attempts. One file commits atomically with its raw source,
result links, corrections and retry receipt. The service does not match sample
labels to attempts, infer missing states or convert units.

The [format contract](result-table-format.md) describes numeric syntax,
columns/constants, quoting and source coordinates. The [transaction contract](result-table-import.md)
describes ownership, exact revisions, corrections and crash recovery. The
Python registry and public MCP interfaces use the same implementation.

## Send explicit text and target IDs

`measurement_preview_result_table` parses a source without changing storage.
`measurement_import_result_table` accepts the same `spec` and a required retry
key. Replace the placeholder attempt ID below with an existing owned attempt
and use its current revision:

```json
{
  "spec": {
    "source_name": "reported.csv",
    "csv_text": "attempt,revision,metric,status,unit,value,explanation\r\nexisting-attempt,1,blank_response,observed,mAU,0,\r\nexisting-attempt,1,impurity_response,missing,mAU,,No result reported\r\n",
    "mapping": {
      "columns": {
        "attempt_id": "attempt",
        "expected_attempt_revision": "revision",
        "metric": "metric",
        "status": "status",
        "unit": "unit",
        "value": "value",
        "explanation": "explanation"
      },
      "constants": {}
    },
    "delimiter": ","
  },
  "idempotency_key": "reported-table-1"
}
```

For a preview, omit `idempotency_key`; optional `offset` and `limit` select up to
20 preview rows. Preview validates the complete file, then returns one page. It
does not look up ownership, reserve revisions or promise that a later import
will be accepted. Import resolves all targets and current revisions inside the
transaction. It returns an immutable import ID and evidence receipt.

`source_name` is a label, not a server path. A client can read a specifically
selected local file and send its text. None of these tools follows filenames,
URLs, expressions or external source references. Unmapped metadata remains in
the retained raw CSV; it does not become a validated sample/replicate mapping.

To preserve a selected file's UTF8 bytes and line endings, read it with
`Path(path).read_bytes().decode("utf-8")` before building the request.

## Collect and verify the complete import

`measurement_get_result_table_import` retrieves an owned receipt.
`measurement_read_result_table_rows` pages its original normalized rows and
result bindings; `measurement_read_result_table_source` pages its exact raw
UTF8 bytes as base64. Each read checks stored source/result closure without R.

Use `collect_result_table_import(client, import_id)` from
`measure_service.result_table_client` with an initialized MCP `ClientSession`.
It returns `receipt`, `csv_text` and `rows`. The collector checks profiles, page
IDs and offsets, byte lengths, raw/mapping/input/row digests and the complete
ordered result-binding digest. It rejects incomplete, reordered or inconsistent
transfers. The server's 12,000-byte source page can split a UTF8 character; the
collector reassembles bytes before decoding.

Receipt/page JSON and collected row documents each have a 64,000-byte budget.
The collector checks cumulative UTF8/escaped JSON sizes before serialization,
alongside depth, node and field bounds. These checks retain the format's
separate 64,000-byte raw source and 8,000-byte mapping limits.

Row pages contain the original `result_id` and `result_sha256`. Retrieve a
result separately with `measurement_get_result` to inspect its outcome and any
later successor. A successor does not replace the old import membership.

## Correct a reported outcome explicitly

Supply `supersedes_result_id` and an explanation in the next table. The named
result must be a current result from that same owned attempt. A corrected
attempt's metadata needs its current revision counter; old result IDs do not
become current again merely because the revision changed.

A new logical import needs a new key. Retry a lost response with the exact
original request and key; its acknowledgement remains stable after restart,
attempt revision or result correction. Reusing the key after editing the
source, mapping or declared name rejects. A file that fails validation or
conflicts during the transaction commits no rows and reserves no successful
receipt.

## Keep analytical decisions explicit

Imported values retain `caller_reported` or `stored_analysis_summary` origin.
Their units and analyte labels remain caller declarations. CSV import does not
create an analytical observation, freeze a training snapshot, consume an
optimizer result or generate Gaussian-producer evidence.

To reassess corrected results, explicitly select their IDs under the existing
[observation contract](observation-evidence.md). Prior observations and
snapshots keep the exact outcomes they recorded. A physical retry remains a
separate attempt; recording or correcting a table does not actuate equipment.

`measurement_registry_capabilities` advertises the profile, atomicity, limits
and separate analytical/optimizer boundaries without requiring an R backend.
Only table import is a retry-safe write. Preview and source/row/receipt reads
are read-only operations under the existing MCP identity policy.

The [complete example](result-table-example.md) imports all seven outcome
states, rejects a partial file atomically, records an explicit correction and
preserves its earlier observation snapshot across restart.
