# Frozen effect content retention: proposed migration protocol

Status: migration design remains unimplemented and unaccepted. The isolated
retired-custody reader, irreversible blocked fold and resource fence have passed
scoped tests and independent review; this is not migration or erasure acceptance. U02 and
B production activation remain blocked on retention integration and D05 evidence.
This document does not authorize maintenance against production data.

## Problem and invariants

FrozenFieldChange.to_receipt stores original/new field values in intent_json,
summary and diff. SQLite retirement scrubs its copy, but the sole approval
JSONL retains the original. EffectJournal currently derives resource custody from
that original. Deleting it without a replacement custody record can permit an
uncertain mutation to repeat or make every unrelated resource unusable.

Preserve ActionReceipt as the sole approval authority and keep its public wire
compatible. Erasure must never make a retired receipt executable, release unknown
resource custody, reinstate superseded approval or imply provider success. No
second approval database. Retained metadata is still access-controlled data, and
hashes are not encryption. Exported copies and old backups require their own
retention policy; unlink is logical removal, not physical media sanitization.

## Proposed representation

An erased frozen receipt remains a valid ActionReceipt with the SAME action ID,
principal, run ID and proposal-scope identity, but blocked state and fixed safe
summary/diff/target labels. Its apply_payload has a reserved retired kind, original
intent digest, physical resource digest, original ledger epoch and job identity.
It contains no old/new values or record/field identifiers. This is a custody
projection, never a payload accepted by an executor or ActionProposalService.
The original immutable binding and replacement manifest digest remain auditable;
the original approval does not transfer to the projection. All historical state
changes for the retired action must be ignored by the fold, including records
appended after maintenance. Repeated proposal creation must not overwrite or
recreate the retired original. A new job or principal cannot bypass the retained
physical-resource fence. Malformed projections fail closed.

The projection must be generated from a strictly validated original receipt and
its parsed FrozenFieldChange with matching content-derived action ID. Callers
cannot supply their own resource digest or mint a projection through append.
The scanner/folder must recognize the versioned projection before exposing it;
legacy APIs may list a blocked receipt but cannot promote it.

## Serialization and atomic replacement

Current locks are on the JSONL inode. Rename alone is UNSAFE: a waiting writer can
append to the unlinked old inode. Before adding any replacement operation, migrate
all cooperating readers/writers to a stable sibling lock with an explicit lock
ordering (stable lock, then ledger inode). Preserve existing legacy blocking
semantics and bounded frozen-operation semantics. Stop every old binary before
migration; the operation requires an exclusive deployment/maintenance guard as
well as the stable lock. Do not infer stopped workers from an absent PID alone.
Reject symlinks, unexpected ownership and multi-link files before replacement.
Readers must not silently reopen a different generation mid-operation.

Under those guards:

1. Validate an operator-bound maintenance request, exact source hash/identity,
   scope/job selection and all receipt bindings; reject corrupt input rather
   than guessing which bytes are safe to drop.
2. Prepare a private same-directory candidate containing unchanged unrelated
   events, validated custody projections and a versioned maintenance receipt.
   Remove copied private values from all selected action records, not just the
   first action_proposed event. Preserve supersession/quarantine relationships.
3. Validate the entire candidate through the new reader, verify selected actions
   are permanently blocked, compare unrelated authoritative state, and prove all
   intended physical-resource fences survive. No provider calls are permitted.
4. fsync candidate, atomically replace the ledger under the stable lock, fsync
   directory, then persist the cross-domain retirement receipt. Interruption at
   any phase must reopen either a complete old generation (erasure incomplete)
   or a complete new generation (custody retained); never report erasure from a
   missing or partially written file.
5. Only report end-to-end retirement after JSONL, central SQLite, revisions,
   cards/callbacks and controlled backup/export targets have verified receipts.
   Partial completion remains denied and resumable; it does not recreate content.

An inode replacement necessarily changes startup/deployment persistence binding;
reopen/reconcile under the trusted maintenance protocol before accepting work.
Historical B01 recovery records bind byte prefixes, so blind reserialization
would invalidate them. The final protocol must preserve their quarantine effect
and bind original prefix hashes in a verified migration manifest. This is an
unresolved implementation decision, not permission to recompute recovery hashes
and silently treat corrupt history as valid.

## Required implementation and validation

Implement stable-lock compatibility and deny old-writer coexistence; versioned
projection parsing/folding; hash-bound offline candidate creation; cross-domain
retirement registration; recovery-history migration; backup/export lifecycle;
then D05 runtime integration. No production enablement before all are verified.

Tests must cover old/new writer overlap, aliased paths/hardlinks, interrupted
candidate writes and fsync/rename/directory-sync, stale maintenance requests,
selected/unrelated receipts, superseded receipts, every receipt state, malformed
or forged projections, old/new jobs for the same physical field, corrupt history
with recovery markers, backup rollback, private-value absence in owned outputs,
and repeat retirement. Actual subprocess crashes and concurrent writers are
required where the guarantee crosses processes. Verify no provider operation
occurs during any maintenance or failed admission path.

## Review decisions and implementation contracts

Independent Opus 5 review identified nine concrete design decisions. These are
implementation contracts, not evidence that migration works. Read-path code must
land before candidate generation; replacement remains disabled until validation.

- **Projection v1:** reserved `apply_payload.kind = brief-field-retired-v1` with
  exact keys `kind`, `intent_sha256`, `physical_resource_sha256`, `ledger_epoch`,
  `job_id`, `original_proposal_binding`, `maintenance_id`. Hash fields are 64
  lowercase hex characters. The enclosing action ID must equal `action_frozen`
  plus intent_sha256. Unknown versions or malformed projections globally deny
  effect admission because their physical scope cannot be trusted. Valid
  projections fence only the matching physical resource; an unrelated resource
  must remain usable. No general append/propose API can mint this kind.
- **Public receipt:** retain original principal/run/action/proposal identity and
  target_system `brief_field_change`; use non-empty fixed `Retired field change`
  title, `retired` target_object and `Content retired; execution forbidden.`
  summary/diff. State is always `blocked`. The folder sets an irrevocable
  `retired` flag independent of supersession/quarantine. State events cannot
  change that state. get_frozen_receipt, resolve, approve and apply reject it with
  fixed `frozen_content_retired`; listing may show only the safe projection.
- **Two bindings:** retain original_proposal_binding strictly as historical
  evidence. A separate computed proposal_binding validates the projected event's
  current bytes. That hash never carries old approval forward. The maintenance
  manifest binds original event hash, original binding, projected event hash and
  selected identity. Supersession relationships remain identity edges; retiring
  a predecessor never resurrects a superseded approval. Tests must cover retiring
  either or both ends and attempts to append new state after retirement.
- **Recovery format:** a new versioned migration receipt records original file
  hash/length, original valid recovery prefix hashes/epochs, projection hashes,
  output generation and operator-bound request. New strict parsing validates the
  migration receipt and reproduces quarantine boundaries. It must NOT reinterpret
  discarded corrupt events as authoritative. Design of a verifiable witness for
  removed prefixes remains a blocking implementation item: ordinary recomputed
  checksums alone cannot prove historical authorization. No v1 recovery marker
  may simply be rehashed to fit changed bytes.
- **Locking and process exclusion:** sibling `<ledger-name>.coordination.lock`,
  private owned regular file, locked before opening the ledger inode. Shared
  stable locks allow ordinary reads/appends under existing inode semantics;
  maintenance takes exclusive stable and inode locks. Frozen bounded deadlines
  cover BOTH lock acquisitions. Legacy blocking callers keep blocking semantics.
  A generation token alone CANNOT detect an old binary that ignores it. Positive
  exclusion must come from the trusted supervisor's stop-and-reap receipts and
  sealed restart/intake configuration for all managed writers, plus an operator
  declaration of the managed deployment scope. Unknown/unmanaged writers block
  maintenance. This is not protection against a malicious same-UID/root process.
- **Domain receipt:** add an explicit `approval_ledger` retirement target, with
  logical ledger identity, source/output generation and maintenance_id. A
  per-job receipt binds projection selection and surviving resource custody.
  It is evidence of erasure only, never approval or provider settlement.
  SQLite is scrubbed first (already conservative); JSONL follows; domain receipt
  is finalized only after validation of both and owned copy disposition. Restart
  with a missing receipt reports partial retirement and performs no external IO.
- **Owned copies:** migration backups and product-created exports require a
  registered inventory containing opaque identity, generation, retirement jobs,
  lifecycle state and deletion/expiry receipt. A backup predating a retirement
  tombstone cannot become active without applying that tombstone and revalidating
  all domains. Third-party/provider copies and uncontrolled offline media are
  outside the local erasure claim. This does not invent a universal retention
  duration: the existing per-job source deadlines and operator policy govern it.
- **Product completion:** B03 must expose verified settlement distinct from
  erasure. An end-to-end retired-and-settled case must become ready, whereas any
  unknown or partial case remains blocked. A permanent blocked runtime is not
  completion of U02/B03/D05 or this migration.

| Interruption boundary | Authoritative state on reopen | Required behavior |
| --- | --- | --- |
| Before SQLite scrub | Existing complete domains | No erasure claim |
| After SQLite scrub, before JSONL candidate | Raw JSONL, scrubbed SQLite | Partial retirement; no new effect |
| During candidate write/validation | Original JSONL generation | Partial retirement; quarantine incomplete candidate |
| Candidate synced, before replacement | Original JSONL generation | Verify exact source again; no stale replacement |
| Replacement done, directory sync uncertain | Whichever complete generation is present | Verify hashes and generation; no claim from prior return path |
| New generation durable, receipt absent | Validated new generation, retained custody | Finish receipts idempotently; no provider IO |
| Domain receipt done, owned copies pending | New generation plus outstanding copies | Local scrub only; not complete erasure |
| All erasure receipts done, effect unknown | New generation plus unknown custody | B03 reconciliation required; never blind retry |
| All erasure receipts and verified settlement done | Consistent current domains | Eligible for complete startup validation, not automatic authorization |

The recovery witness, supervisor integration and copy registry still require
implementation and independent validation. They are not waived by the other
specified constants. Do not execute replacement while any is unresolved.

## Implemented reader boundary

`frozen_custody.py` validates the v1 projection, the strict scanner rejects invalid
versions, and the receipt fold cannot restore a retired approval through history,
duplicates or supersession. Existing proposal/apply/ephemeral authority paths
reject it. EffectJournal reads only its resource digest for custody, preserving
unrelated-resource availability. No writer can create this format through the
normal frozen proposal API. Synthetic hand-authored files are reader fixtures,
not validated migration receipts. Generation replacement, recovery witness,
owned-copy registry and full retirement workflow remain absent. The optional
stable-lock primitive is described below.

## Implemented coordination boundary

`LedgerCoordination` now supplies an explicitly installed stable lock and optional
RunLedger integration. Coordinated readers/writers acquire it before opening the
ledger inode, reject lost/replaced/aliased/non-private domain files, and share one
bounded lock deadline for frozen operations. Default constructors remain legacy.
The product factory now accepts explicit managed domains as described below;
no production deployment has opted in. Synthetic subprocess tests cover a waiting
writer across data replacement and lock substitution. This is not an old-worker
exclusion proof or a replacement API.

Coordinated frozen proposal/state fsync failures now preserve the original error;
tests verify retained custody and no duplicate proposal on retry. Body I/O errors
are distinct from lock admission errors. Repeated install keeps the same lock
inode and synchronizes it and its directory. These repairs passed scoped review;
they do not close the integration gates below. Assess read contention before
activation. Do not blindly
change strict readers to shared inode locks: existing legacy appends themselves
use shared inode locks, so reader/writer exclusion must be assessed together.
The stable lock inode must never be replaced while a domain is active. Missing
processes or a held lock do not prove that arbitrary old/manual writers are absent.

## Managed host construction

`Settings.managed_ledger_paths` is an immutable tuple of distinct absolute direct
paths. `OSTAG_MANAGED_LEDGER_PATHS` accepts the corresponding JSON array of path
strings; its default is `[]`, retaining legacy behavior. This is an opt-in host
configuration, not a new permission or a migration command.

All 11 product construction sites now use `configured_ledger`: main CLI,
runner, auto-base, ambient followups, and Monitor service/scheduler. Monitor's
ledger is derived from its database directory and may differ from the main
configured ledger. List both paths when both domains are managed. The factory
validates every configured domain, even when constructing an unrelated ledger,
so a missing configured data/lock file cannot silently disable the policy.
Symlink aliases do not select an uncoordinated fallback; hardlinked managed
files fail the coordinator's single-link checks.

The factory never installs a lock, creates a missing managed domain, or rewrites data.
Paths omitted from the configured domain list retain legacy creation and writing
behavior; deployment must enumerate every intended managed domain.
Provisioning remains a separate offline operation requiring the exclusion
protocol. Within the selected policy, supplied workflow, approval and Monitor
ledger instances must match the expected path and coordinator. Server startup
checks configured domains before creating databases or starting background
owners, checks injected Monitor/scheduler ledgers, and rejects unknown injected
transport/daily-scheduler owners under the managed policy.

Direct library callers and old binaries can still construct an uncoordinated
RunLedger. This change closes current product construction paths, not arbitrary
same-UID/manual writers. Trusted stop/reap receipts and sealed restart/intake
remain prerequisites for replacement. No lock installation, content retirement,
production configuration or feature activation is performed by this change.
