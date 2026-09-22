# Existing-host rollout: observed prerequisites and execution order

This runbook starts from main `96fc142666528f514d8c98fe67e1bc1d3fd6b17c`.
It is not a migration or activation receipt. Preserve the original acceptance
criteria in `delivery-implementation-plan-2026-09-21.md`.

## Verified host facts

The installed launcher hash `de656d47992b53e2a8546bf4a156396f7a542618f74cc2037c68fc4d9d3da4dc`
exactly matches `scripts/deploy_work_monitor_release.sh` at repository commit
`3c77b418a2f9910401cc78cfe2e268e800eb1fb1`. The installed wrapper matches repository
hash `28a92f53f3e4e2a18a564cb3e7aea6d9975564b5df7c122879cd05ae8819b5f6`.
The installed launcher has neither the new deployment mutex nor the persistent
maintenance gate. It accepts normal deployments through the restricted wrapper;
that interface cannot install host tools or perform operator activation.

An authorized Tencent Lighthouse console session reached the existing Linux
x86_64 host through TAT with sudo UID 0. Direct read-only inspection found the
current release receipt reports `deployed` at the SHA above. The expected new
maintenance state and deployment lock paths were absent. Absence does not prove
that no past maintenance or current writer exists.

The production data mount is a **named Docker volume**
`work-monitor_work-monitor-data` mounted at `/var/lib/ostag` in the container.
Do not apply the bind-mount examples in deployment.md as if they described this
host. Re-observe the mount before any migration and include its actual data root
in the backup, exclusion and restore protocol. Six Brief environment variables
were unset in Docker configuration; effective settings must be inspected
separately. No host file, feature flag or database was changed by this inventory.

## Repeatable diagnostic

`scripts/inspect_production_host.py --sha <full-current-main-sha>` is a standalone
standard-library Python diagnostic for a **trusted host administrator console**.
It requires root when run as a CLI. It does not add a command to the SSH wrapper.
It runs only Docker ps/inspect, reads bounded non-symlink files and prints hashes,
metadata, container identity, mounts and six configured flag enums. Other
configuration values and raw command errors are omitted. It never executes code
inside the container, opens application databases, changes flags or creates
locks/state files. Its subprocess output and duration are bounded.

Outputs are observations, not admission decisions: `activation_ready` and
`migration_authorized` remain false; container samples are non-atomic; file
observations are single samples. Configured flag values use the uniform enum
`true`, `false`, `unset`, `invalid`. Unset is not proof of an effective value.
Missing lock files prove neither idleness nor writer exclusion. A receipt hash
is not validation of receipt contents. The utility refuses container/release
mismatch and container/environment drift, with fixed diagnostic reasons.

Before using this candidate on the host, verify the exact reviewed source hash.
Preserve the diagnostic receipt in protected operational storage. Never pipe an
unverified downloaded candidate into a privileged interpreter.

## Ordered work remaining

1. Retain the verified host facts and re-read current main/deploy status before
   installation. Establish an explicit release freeze for the host-tool/hash-pin
   transition; do not let an in-flight old launcher cross the transition.
2. Build and verify an existing-host adoption/recovery controller. The old
   launcher has **no shared lock**, so taking a new flock cannot exclude it.
   Account for all managed start paths, restart policies, writers and operator
   custody before touching state or data. Persist the pre-seal generation and
   request identity for recovery. Do not write an open record merely to satisfy
   the candidate launcher, and do not treat absence as fresh-host provenance.
3. Back up and restore the actual complete data domain offline in isolation;
   record identity/schema/hash and restore results. Keep Monitor data and Brief
   stores distinct; rollback must not overwrite newer Monitor data. Retained
   effects and JSONL recovery witnesses remain part of the write-capability gate.
4. Install the reviewed host tool only after its prerequisites pass, then update
   the matching GitHub hash pin, verify both through `version`, run rejection
   probes and exact-image preflight. Retain the wrapper's restricted command set.
5. Independently wire the authenticated production `TrustedBriefComposition`,
   verified Feishu ingress, owner scope, real read-only model transport and
   operator control. Environment flags alone cannot supply these. Read-only A1
   must not wait for unrelated external-effect execution/retention development;
   leave managed-ledger mode and write-effect flags off for that path.
6. Run real-model A1 and isolated staging acceptance before owner-only activation.
   Check the fresh boot, image/config/persistence identities, admission OFF drain,
   explicit operator activation, real private draft and return-to-OFF recovery.
   A health check is not a substitute for any of these.
7. Advance history and meeting pilots with the genuine source/identity matrix and
   independent human evaluation. D04 and D07 require their own seven-day,
   ten-distinct-request observations under the frozen dependency order. Simulated
   tests, repetitions and an agent-generated approval cannot close those gates.

Claude Opus 5's design challenge helped separate A1 from write-effect migration.
Its suggestion of bootstrapping under an already-installed shared lock does not
apply here: source inspection proves the installed launcher has no such lock.
No proposed bootstrap was executed.
