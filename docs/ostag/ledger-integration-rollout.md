# Journal and maintenance foundation integration

This delivery integrates the previously separate U02 foundation branch onto
production main after PR27. It does not complete U02, B02, B03, D02 or D05.
The original acceptance criteria in delivery-implementation-plan-2026-09-21.md
remain authoritative.

## Application delivery

The default managed ledger list is empty. Existing product constructors retain
legacy ledger admission until a trusted host explicitly configures
`OSTAG_MANAGED_LEDGER_PATHS` as a JSON array of unique absolute direct paths.
With managed domains configured, every declared domain must already exist with
its validated coordination lock. Startup refuses missing/unsafe domains before
starting the Monitor database owners; it does not recreate coordination files.
This only covers cooperating constructors, not old binaries or manual writers.

The effect journal is not registered as a production provider executor. An
explicit owned schema4 database migration and verified backup are required to
create schema5. Ordinary startup does not perform that migration. Schema5
startup inventory requires explicit matching configuration. Pending effects
remain unverified observations; only future independently verified settlement
may close them. No write capability, provider credential or schema migration is
enabled by this release.

## Host delivery is separate

The GitHub deployment workflow invokes the installed, hash-pinned SSH wrapper
and launcher. It checks their installed hashes against GitHub configuration;
it does not install the launcher contained in the source archive. Therefore an
application release does not establish that the new maintenance seal is enforced
on the production host. Record the installed launcher hash and its rejection
probes separately before claiming that protection.

The candidate launcher requires persistent maintenance state under its global
deployment lock. Missing, unsafe, unknown or sealed state denies deployment,
including preflight. Never copy it into service and then invent an open record
to make deployment succeed. Follow maintenance-seal.md: only a proven fresh,
offline host can provision generation zero. Existing hosts require a separately
verified migration/recovery controller. Old-writer exclusion, trusted operator
binding, retention witnesses, consistent domain backup/restore, unsealing and
restart verification remain outstanding. A seal-unaware launcher must never be
used to recover from maintenance that may already have begun.

## Verification and release evidence

Integration must pass current-main regression tests, real kernel/process
boundary tests on Linux amd64, exact production image cold boot, and independent
Claude CLI Opus5 integration review. Keep failures and skipped-platform evidence
separate from successful runs. Native osstack Loop acceptance records only this
integration scope; GraphSpec validation is not Graph execution or business
acceptance. Formal GitHub approval, merge, main CI, deployment and a matching
release SHA plus public health are separate release gates.
