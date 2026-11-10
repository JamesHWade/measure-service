# Demonstrate full-store recovery through MCP

Run the installed service and local operator workflow with synthetic records:

```sh
python examples/store_recovery.py --output-dir /tmp/new-measure-recovery-example
```

Choose an absent output directory on a POSIX host. The script uses the selected
Python interpreter's installed `measure-service` distribution. It deliberately
sets an unavailable R executable; this workflow needs no numerical backend,
optimizer or instrument. Installation and operator commands are described in
[store recovery](store-recovery.md).

## What executes

1. Initialize a private source store. Over MCP stdio, record an intended design,
   a completed manual attempt, an observed zero and a below-limit result.
2. Replace the intended design explicitly. Retrieve both results and the exact
   design lineage, keeping historical evidence attached to the original IDs.
3. Create a consistent online backup while the source MCP session is open.
   Close that server before restoring into a different new store.
4. Verify the backup and compare restored database bytes with it before opening
   the restored service. Replay all four original writes and six retrievals,
   comparing their canonical JSON response bytes with the originals.
5. Create a third design only in the restored store. Verify the source database
   digest and table counts remain unchanged, and the complete flat backup
   inventory retains its original digests.
6. Reject a second restore onto the existing target and check that target's
   inventory remains unchanged.

The full-store operator commands retain IDs and all owners, whereas this
example uses the single `local` stdio principal. Two-user access remains a
separate HTTP contract. A failed command is not a successful recovery; failed
examples retain `EXAMPLE_INCOMPLETE` and must be inspected separately.

## Inspect the records

The output contains:

| Path | Contents |
| --- | --- |
| `source/` | Original synthetic store, retained after cutover |
| `backup/` | Standalone SQLite snapshot and its digest manifest |
| `restored/` | Restored store with one additional explicitly recorded design |
| `records.json` | Original public requests/responses, restored replays and the new design |
| `workflow_receipt.json` | Operator receipts, source/backup identities and checked recovery claims |

New executions generate new record IDs and timestamps. Equality is checked
between each run's own retained originals and restored responses. It is not a
claim that separate executions produce identical IDs or database files.

An exact restore establishes preservation against the retained snapshot, not
independent validity of caller-reported measurements. Manifests are digest
bindings, not author signatures. These tests do not qualify actual hosted
authentication, writable/free disk capacity, physical instruments, scientific
accuracy or full MoSeS reproduction. Complete the
[pilot deployment matrix](pilot-readiness.md) on the intended host and client
before inviting researchers.
