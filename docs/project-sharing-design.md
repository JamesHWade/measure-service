# Proposed projects, sharing and deletion

**Status: design proposal for issue #105; not implemented or approved.** The
current service still isolates records by principal and has no sharing or
deletion API. This note proposes the contract and acceptance gates for later
implementation. Keep the issue open until that implementation is accepted.

The proposed unit of sharing is a project that owns a complete set of linked
records. Membership belongs to the service. Authentication continues to belong
to the existing trusted host boundary. The authenticated actor and the selected
project are separate values; a project ID is never a credential.

## Existing boundaries that the change must cover

These are current implementation facts, not proposed APIs:

| Boundary | Source and present behavior | Consequence for implementation |
| --- | --- | --- |
| Identity | [`server.identity`](../src/measure_service/server.py) returns `local` for stdio or `connect:<GUID>` for HTTP. [`connect_hosting`](../src/measure_service/connect_hosting.py) resolves supported host claims before dispatch. | Preserve immutable actor identity and the proxy trust requirements in [deployment](deployment.md). Do not replace the actor with a project name. |
| Core storage | [`storage._connect_at`](../src/measure_service/storage.py) creates `datasets`, `analyses`, methods, experiments, observations, retention and campaign tables with `owner` columns. `dataset_sources` and `attempt_revisions` inherit through their parent IDs. | Migrate every record family and inherited child; changing just dataset lookups leaves other access paths open. |
| Analytical reads | [`dataset_for`, `source_for`, `analysis_for`](../src/measure_service/server.py) filter by owner; `source_for` joins its parent. `save_analysis` and worker execution use that owner. | Authorization must cover raw sources, result pages, comparisons and derived writes, including completion after worker execution. |
| Registry links | [`method_registry.method_link`](../src/measure_service/method_registry.py) and [`campaign_protocol_registry._task_links`](../src/measure_service/campaign_protocol_registry.py) resolve linked records under the same owner. | Every linked input must belong to the selected project, even if the actor can access both projects. |
| Retry receipts | [`storage.registry_mutation`](../src/measure_service/storage.py) commits records and `registry_receipts` together; its primary key is `(owner, request_key)`. Replay may return the stored response before running the mutation callback. | Authorization and tombstone checks must precede receipt replay, outside the callback too. |
| Operational claims | [`batch_schema`](../src/measure_service/batch_schema.py) owns batch items, runs, run selections, attempts and control events. [`campaign_suggestion_attempts`](../src/measure_service/campaign_suggestion_attempts.py) owns suggestion claims/outcomes and also looks up receipts. | Migrate claim/retry paths as well as registry writes. Preserve exact revision and attempt-token checks. |
| Optional evidence | [`result_table_schema`](../src/measure_service/result_table_schema.py), [`design_lineage_schema`](../src/measure_service/design_lineage_schema.py) and [`import_origins`](../src/measure_service/import_origins.py) retain source rows, design history and archive mappings. Some children inherit ownership by joins. | Include these components in scope, dependency and schema validation. JSON bindings matter as well as foreign keys. |
| Captured pages | [`CaptureCache`](../src/measure_service/capture_cache.py), [`batch_exports.read_export`](../src/measure_service/batch_exports.py) and the evidence cache in [`server`](../src/measure_service/server.py) key captures by store identity and owner. Page reads can use retained bytes without rereading the source. | A membership check only at capture creation cannot enforce later revocation or deletion. |
| Recovery | [`store_operations`](../src/measure_service/store_operations.py) checks physical schema contracts. [Full-store recovery](store-recovery.md) preserves all owners and receipts; [batch import](batch-snapshots.md) creates new local IDs without historical execution authority. | Update recovery validators and import scope rules with the migration. Neither backup bytes nor archive identities grant membership. |

## Membership and operations

Use service-held `projects` and `project_memberships`, with immutable project
IDs, immutable actor IDs, explicit roles and monotonically increasing access
revisions. Keep a separate project data revision for deletion previews. New
records carry `project_id` and `created_by`; mutation events carry the actual
actor, time, operation and relevant revision. Authorship is provenance, not an
access grant. Do not record credentials or raw request bodies in audit events.

Each project has exactly one owner. Create gives ownership to the authenticated
creator. Ownership transfer is an atomic, revision-checked operation to an
existing active member, demoting the former owner to admin. An owner cannot
leave or be removed without transfer or project deletion. Personal migration
projects have one owner and cannot add members; users create a shared project
explicitly. This prevents an accidental invitation from sharing an entire
legacy personal collection.

| Operation | Owner | Admin | Editor | Viewer |
| --- | --- | --- | --- | --- |
| Read/list records, evidence, sources and exports | Yes | Yes | Yes | Yes |
| Register inputs/methods, analyze, run/retry/cancel batches, revise/supersede records | Yes | Yes | Yes | No |
| Add/remove editors or viewers; change between those roles | Yes | Yes | No | No |
| Grant/remove admin, transfer ownership | Yes | No | No | No |
| Preview/commit record tombstones | Yes | Yes | No | No |
| Tombstone project | Yes | No | No | No |
| Purge retained bytes or operate backups | Local operator only | Local operator only | Local operator only | Local operator only |

Members may leave except the owner. Admins cannot modify themselves into owners
or alter peer admins. Membership writes require an expected access revision,
an idempotency key and an audit event in the same transaction. Initial member
addition targets an exact immutable principal already known to this service;
there is no username guessing, global account directory or email invitation
system. A missing/unavailable target gets one generic error. New authenticated
principals may establish their own private scope before being added.

All project members can download complete project evidence. There are no
per-record grants, public links, deny overrides, teams, separate export role or
selectively hidden source text in this first contract. Those alternatives add
policy intersections that the current linked evidence model does not support.
Do not put restricted data into a project whose members may see its analyses.

The host supplies a verified actor through the existing identity adapter, not
membership. A host application maps its projects to service project IDs and
performs membership changes through an authorized service actor. A host role
change has no effect here until that service membership update commits. Do not
claim automatic host-role synchronization. No arbitrary project or role header
is trusted. A later host-authoritative membership adapter would require a
separately reviewed verification, audience, expiry and revocation contract;
building an OAuth service or such an adapter is outside this proposal.

## Scope and private migration

Every project-aware data operation accepts an explicit `project_id`. The
service resolves `(authenticated_actor, project_id, required_operation)` to an
authorization context. It never treats the actor's ability to name a project as
permission. For compatibility only, omission selects that actor's private
personal project; it never selects a shared project or searches all projects.
Discovery may list the actor's memberships without revealing other projects.

Keep `project_id` separate from `created_by` in storage and function interfaces;
do not repurpose `owner` to sometimes mean a principal and sometimes a project.
Preserve original principal provenance in a clearly named legacy author field
where an exact historical acting user was not recorded. Do not fabricate
individual authors for old `local` data: stdio remains one shared local actor.

Migration is an explicit, versioned, offline operator operation after a verified
backup and stopped clients. For each distinct legacy owner across core and
optional components, create one private project and owner membership. Backfill
all owned records and child bindings to that project, retain IDs, immutable
scientific payloads, digests, revisions, timestamps and claims, and map old
retry namespaces to their original actor and private project. Reject orphaned,
cross-owner or inconsistent bindings; never silently assign them to an operator
or a common project. Do not rewrite historical evidence to make it appear
project-native.

Validate the entire component inventory and foreign keys before committing the
migration marker. Failure leaves the old store intact and usable by its old
version. Read-only opens must not migrate or create memberships. Old binaries
must be prevented from serving the migrated store: cutover requires a dedicated
new store location/configuration and retiring the old process, not faith that an
old binary understands a new version marker. Update physical schema checks,
backup verification and restore acceptance together; no mixed-version writers
or downgrade is supported.

Initially, sharing applies to newly created project records. Moving existing
records across projects is rejected. Later copying must be explicit, mint new
IDs, preserve origin metadata and include the complete input/evidence closure;
it must not copy memberships, retry receipts or execution authority. Existing
batch import remains a new private local store operation and must not inherit
access grants from an archive. No generic copy/move API is required for the
first sharing release.

## Fail-closed access, retries and captures

All reads, lists, counts, pagination, link checks and writes filter by the
selected project and active record state. Children inherit their parent's
project, including source text, revision rows, result-table rows and imported
origin records. Where a row has both its own scope and a parent, validate their
equality. An analysis inherits its dataset's project; batch inputs and all
registry evidence must resolve there. An actor who belongs to projects A and B
cannot link an A input into a B analysis. A digest identifies bytes, not access.

Unknown project/record, nonmember access, wrong-project IDs and tombstoned
records use the same not-found response without metadata, counts or candidate
IDs. An existing member with an insufficient role may receive a generic
operation-forbidden response. Do not partially return accessible records when
one required link is inaccessible or deleted.

Use `(project_id, actor_id, request_key)` as the registry retry namespace,
retaining the current cross-operation collision rule inside that namespace.
The request digest covers the operation, project and canonical inputs. Two
actors using the same key do not replay each other's responses; one actor can
reuse a key in two projects. Batch-run and suggestion reservations must carry
the same actor/project separation while preserving their existing run-specific
constraints. Authorization, current role and live response/dependency bindings
are checked before any replay. Demotion to viewer cannot replay an editor
operation to recover its old response through the mutation endpoint.

Write authorization, membership revision, record checks, revision CAS, writes
and receipt commit occur in one serialized transaction. For worker calls that
leave the transaction, retain actor/project/access revision with the claim and
recheck before accepting results. Revocation/demotion prevents new claims and
subsequent scientific result commits by the old claim. A service-internal
terminal event may record that the claim was invalidated, without storing or
returning the late scientific payload. It grants no new retry/execution
authority. Ordinary authorized recovery must resolve interrupted claims.

Revocation takes effect at the authorization transaction boundary: operations
serialized before it may complete; later operations fail. For reads, use a
coherent authorization/data snapshot and bound the response. Bytes already
authorized and sent cannot be recalled. Do not promise instantaneous network
revocation or termination of a worker already computing.

Capture keys include store identity, project, actor, project access revision
and project content-visibility revision. Every page request rechecks current
membership and those revisions before touching bytes. Membership changes
invalidate old captures conservatively for all members of that project;
tombstones increment the visibility revision and invalidate every capture in
that project. Re-adding a user cannot revive their old capture. New scientific
records alone do not invalidate immutable captures. No cross-actor capture
reuse, even for members of the same project.

Retain the current five-minute idle lifetime, global byte/count ceilings and
four-capture-per-actor/store budget across all their projects. Do not multiply
the actor's allowance by the number of projects or let one actor evict another
actor's unexpired capture. Failed cache invalidation must not bypass durable
revision checks. Restart and database replacement still invalidate captures.
Membership revocation and deletion cannot retract previously downloaded
archives, reports or client copies; those are controlled by their recipients.

## Deletion, history and physical retention

Separate three operations: scientific supersession, access-removing tombstone,
and operator purge. Supersession creates a linked successor and keeps the
original readable; it is appropriate for corrected methods/settings/results.
A tombstone removes normal access without rewriting evidence as if it never
existed. Purge removes retained payloads under an explicit operator policy.

The first deletion release supports (a) a leaf record with no active dependents
and (b) an entire quiescent project. A dataset with analyses, or an analysis used
by a batch/observation/import receipt, is not a leaf. Reject its single-record
delete with a bounded dependency preview visible only to authorized members.
Do not silently cascade or make live dependents point to hidden evidence.
Project deletion tombstones all records, derived results, source text, evidence
and receipts as one visibility operation. Readers never see an intermediate
partial project. Minimal deletion status is visible only to its deleting owner
through a dedicated status/retry operation, not normal project data tools.

A preview produces a bounded plan containing target IDs/kinds, expected data
and access revisions, dependency counts, active-claim blockers and a plan digest.
It is not an authorization grant. Commit reauthorizes owner/admin as applicable,
checks revisions and the exact plan, and records actor, target, UTC time,
bounded reason and request key atomically. New dependencies or writes make the
plan stale. Oversized closures reject before mutation. Running, unknown or
unresolved claims block deletion until explicit cancellation/recovery has made
the scope quiescent; deletion is not execution cancellation.

Dependency discovery must include embedded JSON IDs/digests and source/result
bindings, not just SQLite foreign keys: observation snapshots, result-table
receipts, design lineage, batch snapshots, campaign consumptions and import
origins can retain evidence indirectly. A later selected-subgraph deletion
would require a complete typed dependency manifest and an explicit preview of
every descendant. It is deferred, not an implicit cascade in the leaf API.

Tombstone state overrides success receipts. A retry of an earlier creation or
analysis mutation must not return deleted payloads or recreate them. Retain a
minimal spent-key marker in the actor/project namespace with request digest and
terminal deletion state; same-request replay returns a bounded deleted outcome
only to an actor still authorized to that scope, and a different request under
that key remains a conflict. Ordinary read access stays not-found. A repeated
delete has one terminal receipt and no second effect. Project-deletion retries
are limited to the deleting owner and original key. Historical private receipts
retain their old semantics for live records after migration.

Tombstones initially retain payloads inside the protected store. They are not
secure erasure. Purge is an explicit local operator command, never arbitrary
filesystem paths accepted over MCP. It may purge only tombstoned scopes with no
live references or unresolved claims and must remove source text, numeric
payloads, copied evidence and full response receipts together. Keep only a
documented minimal audit/spent-key record; digests and actor IDs can themselves
be sensitive. Purge preview reports these retained fields. Policy must specify
the retention delay and audit lifetime before purge is enabled; this design
sets no universal number of days and offers no automatic expiry promise.

SQLite free pages, journal/WAL files, filesystem snapshots and backup copies
are separate from logical row deletion. Operators must implement and verify
their chosen byte-removal procedure; deleting rows alone does not establish
forensic erasure. Existing full-store backups contain all projects and old
payloads. They are not edited in place. Backup retention/destruction and any
holds are operator policy, documented separately from the API.

Restoring a pre-deletion or pre-revocation backup can resurrect content and
access. Before serving a restored store, the operator must reconcile a protected
post-backup deletion/membership event ledger (or decline that restore), validate
the resulting tombstones and grants, clear captures and retire the old writer.
Exact-byte restore by itself does not enforce later deletion. A deployment must
not advertise purge across backups until that ledger, retention process and
recovery drill exist. Exported ZIPs and client copies remain outside that claim.

## Tool annotations

New deletion commits and membership removal/demotion use
`readOnlyHint=false`, `destructiveHint=true`, `idempotentHint=true` when their
required retry key provides the stated semantics, and `openWorldHint=false`.
Previews and status reads are read-only and non-destructive. Grant/transfer
operations must state their access-changing effect explicitly; transfer and
role replacement are conservatively destructive. For a generic role-update
tool, use the destructive annotation even if a particular call only grants.

An append-only scientific supersede operation can remain non-destructive only
when it preserves the original data and access, and its description states
that behavior. A tool that hides/replaces prior content or access is destructive
even if storage retains an audit copy. Regenerate the tool reference and test
the registered annotations with the implementation; this note changes none.

## Phases and acceptance before implementation is enabled

1. **Review this contract and inventory.** Agree on service-held membership,
   role operations, private migration, actor-specific retries, leaf/project
   deletion and operator retention boundaries. Enumerate every tool and table,
   including optional components and embedded dependencies. This document alone
   satisfies neither runtime isolation nor host acceptance.
2. **Migrate and enforce project scope.** Implement the explicit migration,
   authorization context, membership CAS, all reads/writes/claims/receipts,
   capture revalidation and operator schema support together. Keep shared
   project creation disabled until the complete isolation suite passes.
3. **Enable tombstones.** Implement dependency previews, quiescence checks,
   revision-checked leaf/project commits, deleted retry outcomes and capture
   invalidation. Keep deletion disabled until every read/replay path is covered.
4. **Add policy-bound operator purge.** Require documented retention and backup
   reconciliation, with an installed restore/purge drill. Selected-subgraph
   deletion, host-supplied memberships and cross-project copying remain separate
   future decisions rather than prerequisites for new shared projects.

Required invariant tests, specified here before runtime work:

| Test family | Required assertions |
| --- | --- |
| Role matrix | Exercise every operation for owner/admin/editor/viewer/nonmember; admin cannot promote itself or alter another admin; last-owner loss is impossible; concurrent ownership transfer has one winner. |
| Scope closure | Two actors and two projects, including an actor in both: guessed IDs, equal content digests, raw sources, counts, pages, origin mappings, comparisons and every registry link cannot cross scope. Denials match missing-record responses and make no writes. |
| Migration | Multiple owners, `local`, every optional component, absent optional components, orphan/cross-owner references, old receipts and running claims. Verify private grants, exact evidence bytes/digests and replay; injected failure rolls back, read-only access creates nothing, old process cutover is tested. |
| Retry isolation | Same key across actors/projects is independent; changed payload within one namespace conflicts; revocation, viewer demotion and tombstones defeat replay; registry, batch-run and suggestion claim paths obey the same boundary. |
| Races | Synchronize revocation with mutation, worker completion and page reads; ensure the documented serialization order, no late scientific commit, no unauthorized receipt and no revived capture on re-addition. |
| Capture/export | Revoke or delete between manifest and page; all later pages fail generically. Test actor/project/store mismatch, expiry, store replacement, per-actor capacity across projects and immutable bytes for authorized readers. |
| Deletion graph | Reject non-leaf datasets/analyses and unresolved claims. Include JSON-only references, result tables, snapshots, lineage, imports and campaign consumption. A new dependent invalidates preview; project visibility changes atomically. No dangling readable evidence or receipt payload survives. |
| Tombstone retries | Old successful keys cannot recreate or disclose content, new keys cannot target deleted inputs, repeated delete is terminal, nonmembers receive no tombstone metadata. |
| Purge/recovery | Inspect all payload/source/receipt locations and retained audit fields. Exercise verified backup before deletion, reconciliation on restore, capture invalidation and refusal to serve unreconciled restoration. State what filesystem/backup bytes were actually removed. |
| MCP contract | Validate explicit project selection and private fallback, registered destructive/idempotent annotations, generated reference and installed two-actor HTTP/stdio behavior. Real-host identity delivery remains a separate acceptance test. |

Run the repository checks in [CONTRIBUTING](../CONTRIBUTING.md) for each runtime
slice, plus installed migration/recovery tests outside the checkout. R-backed
checks are needed when worker completion or analytical adapters change. None of
these access, retention or synthetic tests establishes scientific equivalence,
physical instrument qualification or production-host authentication.
