# Read-only store connections

`registry_connection()` and `registry_connection(write=False)` open an
initialized SQLite store without creating its data directory, changing its
schema, or running a migration. The connection uses SQLite `mode=ro` and
`PRAGMA query_only=ON`. Reads include committed data that remains in a SQLite
WAL file; the connection does not use `immutable=1`.

The read path rejects a missing store, a symlinked store directory or database,
and a data directory containing an `INCOMPLETE` marker. A missing store error names the operator initialization command. Create a new
store directory and configure the service to use it:

```sh
measure-service-store initialize --data-dir /path/to/new-store
```

Set `MEASURE_MCP_DATA_DIR` to the new directory. The operator command requires
a new directory, including when the previously configured path is an existing
empty directory.

The writable path remains explicit: `connect()` and
`registry_connection(write=True)` may initialize the store and run supported
schema migrations. A read of a legacy campaign-proposal schema reports that a
writable migration is required and leaves the database unchanged. Run the
following explicit writable command against that existing store after retaining
a verified full-store backup, then retry the read:

```sh
MEASURE_MCP_DATA_DIR=/path/to/store python -c \
  'from measure_service.storage import connect; connect().close()'
```
 Do not use `measure-service-store initialize` on an existing data
directory; that command creates a new store and requires a new directory.

`db_path(create=False)` returns the configured database path without creating
the data directory. Use it when a caller needs to capture store identity while
preserving read-only behavior.
