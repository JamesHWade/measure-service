# Commit an outcome table without partial results

The Python `measure_service.result_tables` interface imports a
[strictly mapped table](result-table-format.md) into existing owned experiment
attempts. One table is one transaction: source text, mapping, row membership,
outcomes, corrections and the retry receipt commit together. A failure on the
last row rolls back every earlier row in that import.

Use `import_result_table(owner, spec, key)` with a `ResultTableSpec` and a new
idempotency key. The owner is the same principal that owns the attempts and any
linked analyses. The existing [registry contract](experiments.md) supplies
attempt creation, revision, result state and correction semantics.

## Exact existing attempts

Each row names an attempt ID and its expected current revision. All rows for
that attempt must agree on the revision. The service resolves ownership and
checks the revision inside the write transaction; a pure parser preview cannot
reserve or validate those references. Targets may span several experiments or
replicate groups belonging to one owner. Sample, injection and replicate
metadata remain attached to their declared attempts.

An analysis-derived outcome names an owned analysis and a direct numeric
summary field, with an empty supplied value. Its dataset/trace must be linked
to that exact attempt revision. The service copies the stored number,
provenance and warnings. A numeric zero stays zero. Units and analyte labels
remain caller declarations; linking an analysis establishes the origin of a
number without establishing chemical assignment or unit compatibility.

## Explicit corrections and source order

One current result is allowed per attempt/metric/analyte-label combination,
across attempt revisions. A correction must name the current
`supersedes_result_id` from the same attempt and provide an explanation.
Creating another attempt revision does not silently replace the old result.

Rows are applied in their recorded source order. They cannot repeat a final
attempt/metric/analyte key within the table. A correction can change its metric
or analyte label under the existing registry collision rules; it cannot collide
with another current result. Any conflict rejects the entire table.

Old results, imported row assignments and source files remain readable after
corrections. `measurement_get_result` can show a later successor; the original
table's row page still names its original result ID and content digest.
Separate analytical observation snapshots continue to retain their original
evidence. New assessment and optimizer consumption remain explicit operations.

## Retry and crash behavior

The key shares the registry's owner-scoped namespace with experiment, attempt,
outcome and other registry writes. Repeating the exact input identity with the
same key returns the original receipt after restart or subsequent corrections.
Changing the raw CSV, mapping, source name or delimiter while reusing that key
rejects. A failed transaction leaves no successful retry receipt; its key can
be used for a corrected request.

A process lost before commit leaves no partial import after SQLite recovery.
A process lost after commit returns the same recorded acknowledgement when
retried. These are single-node SQLite guarantees. They do not repeat physical
injections or turn reported lifecycle states into equipment execution.

## Inspect the immutable evidence

- `get_result_table_import(owner, import_id)` returns a verified receipt with
  source/mapping/input identities, counts and ordered result-binding digest.
- `read_result_table_rows(owner, import_id, offset, limit)` returns up to 20
  normalized rows with physical source coordinates, original result IDs and
  result digests. It preserves the imported membership through corrections.
- `read_result_table_source(owner, import_id, offset, limit)` returns up to
  12,000 raw UTF8 bytes as base64. Offsets are bytes; a chunk may split a Unicode
  character. Reassemble all bytes before UTF8 decoding and hash verification.

Reads reparse the retained source, validate the mapping and row identities,
check owned result membership and reconstruct each outcome against its
historical attempt revision and analysis links. Generated result assignments
have a separate `result_bindings_sha256`; they are not inferred from the raw
source digest. The complete receipt also has a stored internal content digest.
Inconsistent evidence rejects without a repair write.

Each result retains `provenance.table_source` with the import ID, raw and mapped
input identities, row ordinal, logical record number and physical line range.
Its original `value_origin` remains `caller_reported` or
`stored_analysis_summary`. Table import does not confer generated Gaussian
producer provenance, analytical qualification, a training membership or an
optimizer update.

The optional source component is created only by a successful import. Stores
without it retain their ordinary registry behavior. Unsupported or partial
component schemas fail validation before unrelated schema initialization. The
existing 64,000-byte outcome bound is checked again after row provenance is
added, so linked analysis metadata cannot silently bypass that limit.
