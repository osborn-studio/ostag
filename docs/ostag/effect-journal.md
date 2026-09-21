# Effect journal (U02 draft; not activated)

This module is an unfinished development candidate. Production registration,
verified external writes/readback (B02), evidence-based settlement (B03), full
backup/restore validation (D05), and live acceptance remain required. Passing
local synthetic-provider tests does not satisfy those gates.

## Authority and durable ordering

Existing ActionReceipt/JSONL remains the sole approval authority. A stored
`applying` receipt cannot mint execution permission. ApprovalService creates a
30-second, single-claim invocation only after its durable transition to applying.
The invocation binds the exact receipt, principal, ledger instance, attempt and
lifetime; a copied asynchronous context becomes invalid when its parent exits.

The journal rechecks current proposal grants, revision, original observation and
ledger epoch. It stores the original immutable action intent and begun attempt
in one synchronous central Brief SQLite transaction, with a monotonic fence.
The trusted provider port is called only after that commit and fresh checks.
A transport response becomes a PendingEffect and an evidence hash, never an
applied receipt. Errors, cancellation, failed observation persistence and crashes
retain conservative custody. A begun row is not proof that a provider was called,
and absence of a row is not proof that a provider did nothing.

Custody is bound to the physical field resource, independent of job, user,
namespace, desired value and attempt. Changing those identifiers cannot authorize
an unresolved repeat. JSONL is checked as well as SQLite so restoring an older
central snapshot cannot erase later receipt custody. Ledger recovery changes the
epoch and stops this execution path pending explicit reconciliation. There is no
current public operation to release custody or to rehydrate a dispatch permit.
B03 must supply that protocol before useful repeated production writes.

## Schema migration and operational boundaries

Initial BriefStore remains schema2; ordinary delivery remains schema4. Explicit
`enable_effects(backup_path=...)` accepts only an existing owned schema4 database.
It creates a new, exclusive mode0600 backup through SQLite's online backup API,
checks schema/integrity and fsyncs the file and directory before migration.
The source uses an exclusive transaction, rechecks schema and its connection's
data_version, and rejects an intervening commit. Existing schema definitions and
rows are retained. Failed DDL rolls back rather than leaving a partial schema5.

Stop all old and new effect workers before migration. Provision a trusted private
backup directory; this API is not an adversarial-filesystem sandbox. Existing
backup paths are never overwritten. A failed attempt may retain a backup artifact;
inspect it and use a distinct new path rather than deleting evidence. Migration
is not production activation, a coordinated multi-store backup, or a restore API.
Do not downgrade by changing user_version or copy an older central database into
a running system. The previous committed store implementation rejects schema5 without mutation and
opens the schema4 backup in an isolated probe. Full D05 domain consistency and
whole-deployment rollback checks are pending.

Startup inventory requires explicit schema5 configuration, otherwise it refuses
the new schema. Its bounded metadata scan includes intent, attempt and observation
custody. Reconciliation treats begun/unknown effects as blockers. Structural
inventory cannot establish provider success or permission. It requires exclusion
of concurrent writers across its separate domain reads.

## Retention and outstanding validation

Local retirement scrubs raw intent and permit JSON, marks attempts unknown and
preserves resource hashes, fences and evidence digests. It cannot cancel an
external operation. An in-flight response may still append a digest after full
scrubbing when the original permit digest and retained binding metadata match;
this observation-only path cannot authorize another dispatch. Evidence hashes
are not encryption and may be guessable for low-entropy responses. The approval ledger has a separate retention policy.

Current tests use synthetic providers through the actual ApprovalService and
central database. They cover lifetime/identity denial, atomic intent+begin,
response loss, same-resource new jobs, controlled older-SQLite restore, and
retirement, explicit schema5 startup custody and a real concurrent commit between
backup and migration. Three abrupt subprocess exits verify rollback before commit
and durable custody after commit and after entry to the synthetic provider.
Multi-process contention, full D05 restore protocol and actual Opus review
acceptance must still be completed. The journal is not ready for production
enablement.
