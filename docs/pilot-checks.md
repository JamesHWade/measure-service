# Check two-account ownership and evidence continuity

The pilot checks exercise two initialized MCP clients using new synthetic
records. They do not authenticate either client. The transport must supply
actual independent account credentials; the hosted CLI rejects fabricated
trusted identity headers.

Preparation uses the same synthetic experiment specification and idempotency
key in both accounts and requires different record IDs. It creates one dataset,
one experiment and completed attempt per account, with an observed zero and a
below-limit outcome. Numerical mode uses the numerical fixture's CSV dataset
and two analyses per account. Registry-only mode needs no R.

Both clients must successfully retrieve their own records before the checks
accept cross-account denials. Foreign dataset/source/experiment/attempt/result
reads are tested in both directions. Foreign measurement links are rejected.
Numerical mode also checks analysis/evidence ownership and foreign analysis
links. Only the expected inaccessible-record errors count as denials; generic
tool failures and lost connections fail the workflow.

The private checkpoint freezes synthetic record identities and exact
successful responses, rather than granting general tool execution authority.
Its profile, fields, bounds and record bindings are validated before any
verification call. Verification reconstructs the fixed supported operations;
it never accepts a caller-selected tool or file path from the checkpoint.
Protect checkpoints against modification: they are evidence, not signatures.

Verification checks owned historical records and bidirectional foreign-read
denials before replaying the original idempotent registry writes. Replayed
responses must exactly match their originals. Attempt/result listings must
remain unchanged. Dataset import and analytical operations are never replayed,
since those operations do not have retry keys. Numerical historical records
retain their original provenance and warnings. The current backend capability
snapshot must also match the original numerical run; backend drift requires
separate requalification.

A successful verification proves continuity at the selected endpoint. It does
not prove the operator restarted a host, moved a volume or restored a backup.
Record those events separately using the host worksheet.
The fixed aggregate report omits account identities, record IDs and remote
error text. Keep raw checkpoints and operation journals private.
