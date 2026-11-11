# Export and import chromatogram batch evidence

A batch snapshot preserves a quiescent batch's declaration, item states,
attempts, fixed run selections and control events. A self-contained snapshot
also carries complete input datasets, retained CSV/TSV source text and stored
chromatogram analyses. Export and import use recorded evidence without R.

Import creates a **new local store and a new pending batch**. Imported historical
analyses remain separate from analyses produced by subsequently running that
batch. Archived runs, failures, cancellation and retry events stay in the
retained archive; they do not become active local claims or replay receipts.

## Capture through MCP

Read `measurement_get_chromatogram_batch` first. Supply its current revision and
explicitly choose `references` or `self_contained`:

```python
from pathlib import Path
from measure_service.batch_bundle import write_bundle
from measure_service.batch_snapshot_client import collect_batch_snapshot

# client is an initialized MCP ClientSession; batch is the owned batch record.
snapshot = await collect_batch_snapshot(
    client, batch["batch_id"], batch["revision"], "self_contained"
)
receipt = write_bundle(snapshot, Path("batch-evidence.zip"))
```

The collector calls `measurement_export_chromatogram_batch`, verifies the
returned identity, revision and limits, and reads immutable base64 byte pages
with `measurement_read_chromatogram_batch_export`. It checks every cursor and
the final byte digest before accepting the document. The destination archive
must be a new file.

The capture reads one coherent SQLite transaction. A revision conflict or any
running item rejects export. An expired claim still requires explicit recovery
through the ordinary batch API. Capture does not cancel, recover or rerun work.
All traces in each selected dataset are retained, including unselected traces,
so the original dataset digest remains meaningful.

Each capture records its own `captured_at` time. Repeating an export with the
same arguments therefore returns a different `snapshot_sha256`, and the tool is
declared read-only but not idempotent. Read pages with the digest returned by
the capture being collected; a retried export starts a new snapshot.

| Mode | Raw datasets and retained source text | Stored result arrays | Local import |
| --- | --- | --- | --- |
| `references` | Identities and metadata only | Digests and provenance only | Rejected |
| `self_contained` | Complete dataset closure and exact retained text | Full stored results | New store only |

Originally structured JSON inputs have no retained CSV/TSV source asset. Export
records that absence; it does not fabricate text or fetch external assets.

Captured bytes live in an owner- and store-bound process cache: at most four
captures and 128 MiB. One capture runs at a time. Export does not block the MCP
event loop while constructing the document. Byte pages contain at most 48,000
decoded bytes. Restart, eviction or store replacement invalidates cached bytes;
the caller must explicitly capture again. An interrupted collector never
silently substitutes a different snapshot.

## Import into a new local store

Run this command in the installed service environment:

```sh
python -m measure_service.batch_restore batch-evidence.zip \
  --destination /path/to/new-batch-store
```

This is a local operator command. MCP accepts neither archive uploads nor
server filesystem paths. Import validates the entire archive before reserving
the destination, which must not already exist, even as an empty directory or
symlink. The command does not change the process's configured data directory.

Successful import writes `source-snapshot.zip`, `measure.sqlite3` and
`restore-receipt.json`. A directory containing `INCOMPLETE` records an unfinished
import and must not be used as a completed store. A failed import leaves that
directory for inspection; the command never recursively removes caller files
or overwrites an existing destination.

The receipt maps original dataset and analysis IDs to new local IDs and names
the new pending batch. Input bytes, original methods, results, timestamps and
scientific provenance remain unchanged. The separate import-origin component
records their archived identity. Original principal identities, execution
tokens and idempotency keys grant no authority in the new store.

Start a separate service on the imported store:

```sh
MEASURE_MCP_DATA_DIR=/path/to/new-batch-store python -m measure_service
```

Use `measurement_get_analytical_import` with the receipt's `import_id` to retrieve
the owned mapping without R. `measurement_get_analysis` includes `import_origin`
for an imported analysis and `input_import_origin` for its imported dataset.
A newly executed analysis has only the latter. These fields leave original
scientific provenance untouched. The optional component is absent from ordinary
stores; reads do not create or repair it.

Running the new pending batch is a separate, explicit operation requiring the
usual R backend. A copied historical result establishes availability of retained
evidence. Numerical reproduction requires fresh execution and an explicitly
declared comparison rule.

## Format and limits

The document profile is `measure-chromatogram-batch-snapshot/1`, schema version
`1.0`. The ZIP profile is `measure-chromatogram-batch-bundle/1` and contains
exactly `manifest.json` and `snapshot.json`. Canonical JSON, strict field sets,
finite numbers, unique IDs, dataset/source/result/evidence digests and
cross-record bindings are checked before import. Digests establish content
identity; they do not authenticate an archive's historical author.

The snapshot limit is 64 MiB, with 256 KiB additional archive overhead. A
snapshot may contain at most 32 items, datasets and analyses, 256 attempts,
256 runs, 1,024 control events and 500,000 total raw points across the complete
dataset closure. Existing per-record limits still apply. Over-budget capture
rejects instead of truncating. Archive readers bound actual decompressed bytes
and reject unknown members, duplicate names, encrypted files, unsupported
compression, symlinks, traversal paths, malformed JSON and integrity failures.
No files are extracted from the ZIP.

This format covers scalar single-trace chromatogram processing. It does not
carry linked component correspondences or calibration revisions, restore
campaign/optimizer or historical operational state, establish physical
validation, or claim MOCCA2 interoperability. Those limits are explicit in the
snapshot's availability fields.

Run the [complete synthetic example](batch-snapshot-example.md) to retain a
partially completed source batch, import its evidence, and compare explicit
fresh computations with the two original successes.
