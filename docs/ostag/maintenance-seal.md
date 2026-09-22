# Persistent maintenance seal

Status: development host protocol. This is not installed in production and does
not implement data migration, verified recovery, or unsealing.

The deployment launcher requires `/var/lib/ostag-maintenance/work-monitor.json`
after taking the global deployment lock, before any runtime access. A trusted
host may pin `OSTAG_MAINTENANCE_STATE_PATH`; every managed launcher must share
both state and lock identities. Keep state outside application data volumes and
ordinary backup restores. Missing, sealed, malformed or unsafe state denies
deployment and preflight with a fixed diagnostic.

On a fresh offline host only, trusted provisioning supplies a protected parent
directory and a single-link regular file owned by the effective deploy UID,
mode `0600`, with this initial record:

```json
{"schema_version":1,"generation":0,"state":"open","maintenance_id":null,"request_sha256":null}
```

This is not a recovery recipe. Never delete a seal, restore an older state, or
write an open record to bypass interrupted maintenance. Production provisioning
and verified installation are outstanding gates before using the new script.

`sealed_maintenance` acquires the same exclusive kernel lock, validates the
expected generation and trusted authorizer, and atomically replaces open state
with a seal bound to a maintenance ID and request digest. It synchronizes the
file and parent directory before yielding. Re-entry requires the exact binding
and re-synchronizes state before yielding. A different request cannot take over.
Checkpoints revalidate authorization, lock identity and state; the guard expires
when the context exits. The seal remains on success, exception and process crash.

An existing deployment holds the lock through rollback, excluding maintenance.
A later deployment is denied by the persistent seal even after the maintenance
process exits. The shell reader is self-contained so the installed launcher does
not import code from an uploaded release. Tests exercise this contract with real
subprocess locks and crash exits.

`seal_and_stop_container` persists the seal before issuing Docker commands and
retains it on failure. Its returned stop observation is still not maintenance
authorization or proof that arbitrary host writers have stopped. No automatic
retry, restart or unseal is provided.

The complete controller still needs closed-world operator scope, all managed
start paths, ledger retirement and generation verification, backup receipts and
verified recovery before it can safely unseal and start a new image. Persistent
denial alone is not completion of U02 or live acceptance.

## Diagnostics and host contract

The launcher distinguishes validated `maintenance sealed` from `maintenance
state unavailable` using fixed path-free messages. Neither grants recovery
permission. `inspect_maintenance(lock_path, state_path)` provides a strictly
validated read-only snapshot under a shared existing deployment lock. It uses
the module's exact state validator, creates nothing, and refuses busy, missing
or unsafe locks/state. Its returned generation and seal binding are diagnostic
snapshots, not grants; a later operation must acquire and revalidate them again.

Deploy and maintenance must run as the same effective UID. Both paths must come
from one pinned trusted host configuration shared by every managed invocation.
`OSTAG_MAINTENANCE_STATE_PATH` has the same restrictions as
`OSTAG_DEPLOY_LOCK_PATH`: isolated tests/trusted host administration only, never
release artifacts, SSH command arguments or untrusted forwarded environment.
The library arguments are trusted adapter inputs, not per-request overrides.
No production controller that resolves this configuration is provided yet;
implementing and verifying that binding is an installation prerequisite.

Schema 1 is a deliberately minimal, versioned denial record. Unknown versions
and extra fields fail closed. It binds the exact authorized request by digest;
timestamps and operator display names are not authority or a generation anchor.
Before production, the controller must retain its operator/host/time audit
receipt under that request digest. This audit record must not contain the
private effect payload. Those diagnostics belong in the controller receipt,
not in a freely editable annotation on the denial gate. No v1 host has been
provisioned by this work. A future v2 requires offline coordinated migration;
updated readers must continue recognizing valid v1 seals as denial throughout
the transition, and neither old records nor unreadable versions may be reset
to open. Additive schema fields must never be rolled out ad hoc.

## Ordered rollout and recovery limits

1. Before provisioning, establish trusted offline host provenance: no prior
   maintenance state/history, no running managed writers, one pinned pair of
   paths and one effective UID. An absent file alone does not prove a fresh
   host. Existing hosts require the separately verified migration controller.
2. Only on that verified fresh host, provision generation zero as above, then
   validate permissions, ownership and the record while still offline.
3. Install the reviewed launcher and its matching immutable hash. Validate the
   pinned configuration and rejection tests, then run preflight. Provisioning
   must precede installation; installation alone does not authorize deployment.
4. If a fresh-host rollout installs the launcher too early, keep the host
   offline and back out the launcher to its verified prior version only when
   provenance proves no maintenance ever began. Do not manufacture state to
   make preflight pass. If any seal/history may exist or provenance is unknown,
   retain denial and use the future verified recovery procedure. Never revert
   to a seal-unaware launcher as recovery from active/interrupted maintenance.

Re-entry permits repeated execution of the guarded body; it does not make
external effects idempotent. The stop adapter revalidates its exact request.
Orphan `.maintenance-*` files after a crash are ignored; cleanup may remove only
verified obsolete candidates under the same exclusive host lock. Never use
them as an automatic replacement for missing state. Process-crash tests run on
Mac and Linux; storage durability relies on the Linux filesystem's fsync
contract, not a claim of Darwin device-cache power-loss protection.

The default lock lives in `/run`, normally tmpfs and cleared on reboot. The
launcher's preflight diagnostic is the first-line check: it recreates a missing
boot lock, but never recreates or clears persistent state. Only after this
invocation can the non-creating inspector read a snapshot. A valid seal still
denies preflight before Docker; an open state permits the ordinary read-only
runtime checks. A busy inspector/launcher should be retried after the existing
owner exits; never remove its lock. The inspector's shared lock can briefly
make a concurrent deployment report busy.

Newly created lock and candidate descriptors receive explicit mode `0600`,
independent of umask; existing unsafe files are rejected without chmod repair.
The candidate mode is verified before replacement, so restrictive umask cannot
install a state file that the same readers reject.
