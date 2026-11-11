# Analysis response limits

`measurement_read_analysis_data` returns rows for stored peak lists and aligned
arrays for processed traces and SEC distributions. Each response contains the
analysis ID, data kind, requested offset, total row count, continuation offset
and one data page.

The service validates `offset` as a plain integer greater than or equal to
zero. `limit` must be a plain integer from 1 through 1,000; the tool defaults
to 100 rows. Booleans and fractional values are not accepted as integers.
`count` always describes the complete stored data. `next_offset` advances by
the number of rows actually returned, so callers can request the next page
without skipping or repeating rows when the byte limit shortens a page. Empty
and past-the-end pages have no continuation offset.

The complete page envelope is serialized as compact, sorted-key JSON with
non-ASCII characters encoded directly as UTF-8. It is limited to 48,000 UTF-8
bytes, including response metadata and data. Array columns remain aligned and
must have equal lengths. Stored peak rows must be JSON objects. Array entries
must be finite numbers. Malformed stored data fails closed. If one row cannot
fit with the response envelope, the tool reports a bounded error so the caller
can identify the result as unreadable through this page interface.

The 48,000-byte limit applies to that compact JSON response representation.
The analysis-data tool uses that same compact JSON for its sole SDK text
rendering when it agrees exactly with the structured result. Other tool text
is unchanged. MCP returns both text and structured content, so this limit does
not bound the total transport response size.

Backend warning metadata is capped at 32 messages and 2,000 UTF-8 bytes per
message. Longer messages end with `[warning text truncated]`; excess messages
are summarized with an explicit omission marker. A single warning string is
normalized to a one-element list, and null warnings become an empty list.
Chromatogram processing can append one additional bounded spacing warning.
It preserves the backend's original omission count and the processing warning,
so the combined list contains at most 33 messages.
These limits affect warning metadata only; reported numerical results,
methods and provenance fields are unchanged.
