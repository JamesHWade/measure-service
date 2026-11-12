# Record actual invited-pilot acceptance

Copy this worksheet to a private operator location. Record actual observations,
UTC times, responsible people and evidence locations there; never commit host
URLs, account details, credentials or researcher data. A checked box is an
operator declaration, not an automated result. Leave untested items pending.
Use [the executable client checks](pilot-client.md) alongside this worksheet.

## Declare the pilot

- Operator and incident owner:
- Selected host, supervisor, service account and private persistent volume:
- Authentication proxy, actual client and supported sign-in method:
- Selected service distribution and observed installed R/backend identities:
- Two invited test accounts and cohort size:
- Supported input formats, units, scientific workflow and feedback route:
- Resource/storage budget and access/retention/deletion policy owner:

## Verify the actual boundary

| Observation | Status | Private evidence and UTC time |
| --- | --- | --- |
| Both accounts sign in using the intended client | pending | |
| Public Host/Origin admission matches the intended proxy/client and rejects unapproved values | pending | |
| Missing/expired credentials are rejected at the actual proxy | pending | |
| Caller-supplied trusted identity headers are stripped and verified GUIDs injected | pending | |
| Direct upstream access is blocked from unauthorized networks/clients | pending | |
| Two-account pilot preparation passes, including independent numerical checks | pending | |
| Representative authorized input arrives with correct source identity, units and metadata | pending | |
| The actual client handles MCP `isError` and warnings visibly | pending | |
| Proxy upload/body/header/rate/connection limits and interrupted uploads behave as configured | pending | |
| Host/process resource and storage limits and full-volume behavior are understood | pending | |

Test spoof stripping using only your synthetic accounts/records. Confirm that a
caller-authenticated account cannot change its identity by supplying the other
account's GUID. The CLI's unauthenticated probes do not perform this
provider-specific check. Never expose the upstream service to make testing
easier; validate the intended network boundary.

## Prove restart and recovery

| Observation | Status | Private evidence and UTC time |
| --- | --- | --- |
| Actual process/host restart recorded by the supervisor | pending | |
| Same persistent volume is attached after restart | pending | |
| `verify` passes against the original checkpoint after restart | pending | |
| Protected off-host backup exists and an operator can access it after host loss | pending | |
| Backup schedule, retention and failure notification have been exercised | pending | |
| Old writer stopped before controlled new-store restore/cutover | pending | |
| `verify` passes against the original checkpoint after restored-volume cutover | pending | |
| Interrupted/running/unknown execution claims are inspected and explicitly recovered | pending | |
| Failure alert reaches the incident owner and recovery runbook is followed | pending | |

Record the backup manifest digest and protected location privately. A full-store
backup contains all owners' data and retry receipts. Readiness, an HTTP 200, a
successful reconnect and a backup command exit are insufficient evidence for
these rows. See [store recovery](store-recovery.md) and
[HTTP operations](http-operations.md) for the implemented boundaries.

## Decide whether to invite

- Every required row passed, or a documented scope reduction approved by the operator:
- Remaining limits communicated to the invited cohort:
- Access, encryption where required, backup retention/deletion, support and incident ownership agreed:
- Invitation decision, operator and UTC time:

Track remaining actual-host acceptance in [service issue #116](https://github.com/JamesHWade/measure-service/issues/116)
until actual hosted evidence is recorded. Broader
scientific, instrument and optimization qualification stays separate. The
service remains single-node; a pilot does not establish multi-node durability,
provider-independent OAuth, general vendor ingestion or physical execution.
