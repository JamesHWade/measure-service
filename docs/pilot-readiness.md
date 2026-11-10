# Prepare an invited single-node pilot

Start with trace analysis, relative SEC, analytical batches and experiment
evidence for a small invited group. Declare supported formats and scientific
limits before accepting measurements. Synthetic optimization remains a
separately qualified research workflow; an analytical-service pilot does not
establish full MoSeS reproduction or instrument control.

## Required deployment evidence

Record these results for the actual host, authentication proxy and MCP client:

| Check | Required evidence |
| --- | --- |
| Authentication | A real authenticated client connects; unauthenticated requests fail at the proxy |
| Identity boundary | Caller-supplied identity headers are stripped; verified identity is injected; direct upstream access is blocked |
| Ownership | Two invited accounts cannot retrieve or link each other's datasets, results or evidence |
| Client input | A representative authorized trace reaches the service with exact units, metadata and retained source identity |
| Backend discovery | Required R/SEC APIs and an independently checked numerical example pass in the installed host environment |
| Request limits | Oversized and interrupted uploads do not create records; a saturated service returns bounded errors and remains observable |
| Persistence | Host restart preserves records and retry receipts on the configured private volume |
| Recovery | A verified backup restores into a new store; original receipts replay without new outcomes |
| Operations | An operator receives useful failure alerts and can follow the documented recovery and cutover procedure |
| Data policy | Access, encrypted storage where required, backup location, retention, deletion and incident ownership are explicit |

Local HTTP tests simulate the identity proxy. They do not satisfy the first
three rows for an actual deployment. The [deployment guide](deployment.md)
states the current identity and transport boundaries.

## Before inviting researchers

1. Install the selected service distribution and compatible numerical backends.
   Record resolved versions and source identities as evidence; do not infer
   compatibility from an unchanged development version string.
2. Initialize a new private persistent store with the operator command in
   [store recovery](store-recovery.md). Keep reports and backup directories
   outside the store.
3. Configure the trusted proxy and loopback-only upstream. Apply upstream
   header, rate and connection limits in addition to the service's body and
   in-flight request limits. Configure process supervision and host resources.
4. Run the matrix above through the intended client. Retain outcomes, warnings,
   actual runtime identities and failures. A health response or HTTP 200 alone
   is insufficient: inspect the MCP result's `isError` field too.
5. Demonstrate a restore using synthetic pilot data before accepting real data.
   Schedule protected backups and verify that an operator can access them after
   host failure. Do not publish full-store backups from researcher datasets.
6. Agree a small pilot cohort, supported workflow and feedback route. Keep
   unsupported vendor formats, compound identification, physical retention
   fitting and instrument execution explicit in onboarding.

The current storage design supports one node. Multiple processes have separate
HTTP and R capacity limits. Storage quotas, host-wide resource scheduling,
detached execution, automated backup scheduling, deletion/retention APIs and
multi-node persistence remain separate work. Do not imply these capabilities
from a working local example.
