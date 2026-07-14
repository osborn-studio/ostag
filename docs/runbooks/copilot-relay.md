# Copilot Relay production runbook

## Topology and safety defaults

The central API remains the existing Singapore `work-monitor` service behind
`https://ostag-monitor.ihiking.me`; it does not expose another port or database.
Every worker Mac initiates outbound HTTPS only. Start each rollout with
`OSTAG_COPILOT_RELAY_WRITE_ENABLED=false`. Never copy source, diffs, terminal
history, native agent IDs, or credentials into central evidence.

## Central rollout

1. Record the OStag and osstack commit SHAs and immutable image digest.
2. Perform a database backup of `/var/lib/ostag/work_monitor.sqlite3`, then open
   the copy read-only with SQLite and run `PRAGMA integrity_check`.
3. Render Compose and inspect the redacted output. Instance credentials belong
   in the protected `.env`, never Compose YAML or release evidence.
4. Deploy with `OSTAG_COPILOT_RELAY_SERVER_ENABLED=true` and write disabled.
5. Run the health check at `/healthz`, then verify signed event ingestion,
   owner-scoped session listing, existing Work Monitor boards, and daily boards.
6. Enable writes for one owner/private-chat/session canary only after discovery
   has remained stable for at least one reporter polling interval.

## Worker installation

Create `~/Library/Application Support/OStag/copilot-relay.env` with mode `600`:

```dotenv
OSTAG_COPILOT_RELAY_ENABLED=true
OSTAG_COPILOT_RELAY_INSTANCE_ID=macmini
OSTAG_COPILOT_RELAY_SECRET=<credential>
OSTAG_COPILOT_RELAY_CENTRAL_URL=https://ostag-monitor.ihiking.me
OSTAG_COPILOT_RELAY_OSSTACK_BIN=/absolute/path/to/osstack
OSTAG_COPILOT_RELAY_POLL_INTERVAL_SECONDS=5
```

Then run `deploy/copilot-relay/install-launchd.sh /absolute/path/to/ostag`.
Inspect with `launchctl print gui/$(id -u)/ai.ostag.copilot-relay`. The spool and
separate stdout/stderr logs live outside the repository under Application
Support. Log payload bodies are forbidden.

## Codex binary repair

If discovery reports Codex unavailable, run the no-model resume preflight
`codex exec resume --help` as the launchd user. Repair the binary path or login,
update `OSTAG_COPILOT_RELAY_OSSTACK_BIN` if necessary, and restart the agent.
Do not create a replacement AI session to hide a broken resume binary.

## Key rotation and instance revocation

For key rotation, add the replacement instance credential centrally, update the
worker environment file atomically, restart launchd, prove one signed poll, then
remove the old credential. Do not overlap keys longer than the verification
window. For instance revocation, remove its central credential first, disable
writes, stop its launch agent, and retain its spool/database for audit.

## Operational health check

Verify, in order: central `/healthz`; Compose health; one owner-scoped session
query; reporter process; spool pending counts; last central cursor; osstack
`session list --json`; and one safe snapshot. A successful HTTP request is not
proof that the original native agent session resumed.

## Evidence collection

Evidence includes timestamps, redacted command names, commit/image digests,
session/task public IDs, monotonic versions, delivery states, test summaries,
backup integrity output, and callback/receipt hashes. Exclude message bodies when
they contain user content. Never record the instance credential or native ID.

## Rollback

1. Set `OSTAG_COPILOT_RELAY_WRITE_ENABLED=false` centrally.
2. Stop local launch agents with `launchctl bootout`.
3. Restore the previous central image without deleting relay tables or queues.
4. Verify Work Monitor and daily Feishu/DingTalk boards still deliver.
5. Preserve spools for reconciliation. Restore the new image and resume from
   durable cursors only after the cause is understood.

If migration rollback requires restoring data, stop the service, retain the
failed database, restore the verified backup to a new path, run integrity check,
and switch the bind mount atomically. Never delete queues to clear duplicates.
