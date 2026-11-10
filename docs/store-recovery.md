# Back up and restore a service store

Use `measure-service-store` on a POSIX service host to inspect, initialize, back up,
verify or restore a complete single-node store. These operator commands accept
local paths; MCP tools do not expose them to remote callers. Every backup
contains every owner's data, record IDs, immutable history and retry receipts.
Protect it with the same access policy as the live database. Linux and macOS
are the tested host families for these operator commands. Other operating
systems fail before any path mutation; existing MCP workflows are unchanged.

## Initialize and inspect

Install the service distribution, then choose an absent directory whose parent
already exists:

```sh
measure-service-store initialize --data-dir /srv/measure/new-store
measure-service-store inspect --data-dir /srv/measure/new-store
```

Initialization reserves a new private directory. It never opens an existing
store for migration, repair or replacement. Inspection opens an existing SQLite
database read-only and checks structure and integrity without creating tables.
JSON output describes the operation; errors go to standard error and return a
nonzero exit status. Operator inspection can disclose aggregate table counts.
Known service tables must match the installed implementation's physical
contracts, including keys, foreign keys, checks and indexes. Unsupported or
older layouts fail closed; these commands do not silently upgrade them.

This profile supports one `measure.sqlite3` database and its ordinary SQLite
rollback-journal, WAL and SHM sidecars. It rejects unexpected files and incomplete operations. Keep
reports and other files in separate directories.

## Create and verify a backup

Use a separate, absent destination outside the live store:

```sh
measure-service-store backup --data-dir /srv/measure/live-store \
  --output-dir /srv/measure-backups/new-backup
measure-service-store verify --backup-dir /srv/measure-backups/new-backup
```

The online SQLite backup includes committed WAL changes and obtains a coherent
database snapshot while clients continue using the live store, including an
active write transaction in the default DELETE journal mode. Uncommitted
transactions are excluded. Inspection and backup issue no application writes,
schema changes or checkpoints against the source. SQLite may maintain WAL/SHM
bookkeeping; a read-only connection does not promise unchanged sidecar bytes.
Do not set SQLite's `immutable=1` option on a live WAL database.

The completed backup contains a standalone database and a digest manifest.
Verification rejects altered bytes, malformed manifests, missing or extra files,
linked directory leaves and linked database, manifest or sidecar files. Parent
directories are trusted operator locations and are resolved before use. Digests
detect inconsistency with the retained manifest; they are
not signatures or protection against someone rewriting the entire backup.
Integrity and schema checks do not independently validate scientific results.

Operations support databases up to 1,000,000,000 bytes, manifests up to 65,536
bytes, 128 tables and 512 non-internal schema objects. The whole operation has a cooperative
120-second deadline, including copying and verification. These bounds do not
interrupt an indefinitely stalled operating-system
filesystem call. An interrupted destination retains `INCOMPLETE` and must not
be used as a live store. Inspect and retain that failed directory separately;
choose another new path for the next attempt. The commands do not delete or
repair failed destinations.

## Restore and cut over

First verify the retained backup. Stop clients and the old service before
cutover, then restore into a different absent directory:

```sh
measure-service-store restore --backup-dir /srv/measure-backups/new-backup \
  --data-dir /srv/measure/restored-store
measure-service-store inspect --data-dir /srv/measure/restored-store
```

Restore copies the exact sealed database bytes. It preserves all owners, IDs,
record revisions, idempotency receipts and unresolved execution claims. It does
not replay tools, finish interrupted work or grant a new execution attempt.
Use the relevant explicit recovery operation after checking any historical
running or unknown state. Point `MEASURE_MCP_DATA_DIR` at the restored store and
start only one service against it. Never run the original and restored copies
as independent writers expecting their histories to synchronize.

Opening the restored store with a newer service can perform normal schema
initialization or supported component migration. Retain the original verified
backup before doing so; restore itself does not migrate it. This is full-store
operator recovery, whereas [analytical batch import](batch-snapshots.md)
intentionally creates new local IDs and omits historical execution authority.

New directories and files use private POSIX permissions. Existing filesystem
access, encrypted storage, off-host transfer,
backup scheduling, retention and deletion policy remain the operator's
responsibility. A concurrently hostile process with the same filesystem
privileges is outside this local operator boundary.

See [deployment](deployment.md) for HTTP and storage limits.
Run the [installed recovery example](store-recovery-example.md) to retain a
complete synthetic source, backup, restored store and public-MCP replay proof.
