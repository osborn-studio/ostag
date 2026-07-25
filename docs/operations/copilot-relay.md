# OStag multi-device Copilot operations

This is the production operator entry point for the device-first Copilot relay.
The central service runs behind `https://ostag-monitor.ihiking.me`; every Mac
connects outbound over HTTPS. A device has its own Ed25519 key, durable spool,
heartbeat, runtime inventory, and session namespace. Never share an identity
file or spool between devices.

## Safety defaults

- Keep `OSTAG_COPILOT_RELAY_WRITE_ENABLED=false` for discovery-only rollout.
- Enable writes only with `OSTAG_COPILOT_RELAY_CANARY_SESSION_IDS` containing
  the exact approved public session IDs. Device authentication, owner scope,
  runtime ID, session version, and turn ID remain mandatory.
- Store private keys in macOS Keychain. Metadata and environment files must be
  mode `0600`; the Application Support directory must be mode `0700`.
- Do not copy native Agent IDs, prompts, source, terminal history, or secrets
  into central evidence.

## Pair, list, and revoke

Generate a ten-minute, once-only code inside the authenticated central
container:

```bash
python -m ostag.main copilot device pairing-code --json
```

On the target Mac, use the exact tested release checkout:

```bash
deploy/copilot-relay/install-launchd.sh \
  --pairing-code '<one-time-code>' \
  --display-name 'Mac mini' \
  --central-url 'https://ostag-monitor.ihiking.me' \
  '/absolute/path/to/ostag-release'
```

The installer writes no secret to stdout. It creates a device-specific spool,
copies the runner outside protected source directories, and retries transient
launchd bootstrap failures. List and revoke centrally:

```bash
python -m ostag.main copilot device list --json
python -m ostag.main copilot device revoke --device '<public-device-id>' --json
```

Revocation is immediate. Stop the corresponding LaunchAgent after central
revocation, but retain its spool for audit and reconciliation.

## Governed osstack runtime

Build the exact approved osstack commit in dependency order:

```bash
pnpm --filter @osstack/core build
pnpm --filter @osstack/cli build
```

Install both the CLI `dist/bin.js` and `@osstack/core/dist` into the OStag
Application Support runtime. Start a dedicated daemon with its own
`OSSTACK_HOME` and `OSSTACK_SOCKET`; do not reuse the developer's global daemon
socket. `session list --json` must show `runtime_id: codex-cli` on MacBook Pro
or `runtime_id: claude-code` on Mac mini before pairing is accepted as healthy.
Native session identifiers must not appear in logs or evidence.

## Claude Code headless authentication

An interactive Claude login in Terminal does not prove that a macOS
LaunchAgent can authenticate. Terminal may read the user's Keychain credential
while an SSH or background launch context reports `loggedIn: false`. Treat that
difference as an authentication-context problem, not as a missing session or a
reason to copy the Keychain item.

For a dedicated Mac mini daemon, obtain explicit operator approval before
creating a long-lived credential. Run `claude setup-token` only from the
already logged-in GUI Terminal, store the result in a mode `0600` file owned by
the user, and expose it as `CLAUDE_CODE_OAUTH_TOKEN` only to
`ai.osstack.copilot-daemon`. Never place the token in the Gateway environment,
shell history, command output, osstack state, logs, or central evidence. Do not
export or duplicate `Claude Code-credentials` from Keychain.

After restarting the dedicated daemon, run one bounded read-only Claude probe
through the same LaunchAgent environment. The gate passes only when the probe
returns non-empty result text and the original governed session remains
addressable. If Claude returns a terminal authentication or adapter failure,
the inbound relay reservation must stay accepted and retryable: do not write an
empty applied turn and do not mark the message stale merely because the local
session version advanced while it was reserved.

Claude native sessions are scoped to their original working directory. The
governed osstack session must persist that absolute `repo_path` when it starts
and pass the same path back to the SDK on every resume. For a pre-migration
session, backfill only after matching its protected native ID to exactly one
local Claude JSONL transcript, extracting exactly one existing absolute `cwd`,
and completing a compare-and-swap update. Never infer the path from a central
payload or expose it on the status board.

Only the SDK `system:init` event establishes native session identity. Current
Claude versions may emit `hook_started` and `hook_response` events before
`init`; their transient IDs must be ignored. A different ID on `system:init`
still fails closed as `native_session_mismatch`.

## Health and logs

Heartbeat interpretation uses server receipt time:

- online: under 30 seconds;
- degraded: from 30 seconds to under 2 minutes;
- offline: 2 minutes or more.

Check the worker and dedicated daemon:

```bash
launchctl print "gui/$(id -u)/ai.ostag.copilot-relay"
launchctl print "gui/$(id -u)/ai.osstack.copilot-daemon"
```

Worker stdout, stderr, the device spool, metadata, and runner live under
`~/Library/Application Support/OStag/`. Logs must contain status and bounded
error codes only, never payload bodies. Operational checks are, in order:
external and origin `/healthz`, container health/restart count, SQLite
`PRAGMA integrity_check`, fresh independent device heartbeats, runtime
inventory, session projection, pending spool count, and delivery receipts.

## Dual-device acceptance

1. Confirm MacBook Pro/Codex and Mac mini/Claude Code have different device
   identities, spools, runtime IDs, and fresh heartbeats.
2. Put one real session on each device into `awaiting_decision` using the exact
   bounded `ostag_decision` directive.
3. Answer both fresh cards in Feishu. Each answer must be applied once to its
   exact device/runtime/session/turn/version tuple; acceptance reports
   `crossed_routes=0`.
4. Stop only the Mac mini Gateway. Verify degraded, then offline, restart it,
   and prove ordered once-only replay.
5. Restart the central container. Both devices, sessions, decisions, and
   receipts must persist.
6. On mobile Feishu test overview, device detail, continue session, decision,
   refresh, and expired-card recovery. Times must say Beijing time and no
   `200671` or `200672` error may occur.

## Legacy migration

Migrate `ostag-local` only after a signed replacement device has a projected
session and a real applied canary:

```bash
deploy/copilot-relay/migrate-legacy-instance.sh \
  --database '/var/lib/ostag/work_monitor.sqlite3' \
  --replacement '<public-device-id>'
```

The migration is once-only. It revokes legacy control callbacks and removes the
legacy instance from normal counts while preserving sessions, snapshots,
messages, receipts, and audit rows. Never delete legacy rows to make a board
look clean.

## Keychain recovery

If Keychain lookup fails, stop the Gateway and verify the identity metadata
still names the expected public device. Re-pair with a new once-only code only
after centrally revoking the unusable device. Do not export, copy, print, or
hex-encode the private key as a recovery shortcut. If the metadata is corrupt,
preserve it and the spool with mode `0600` for reconciliation before replacing
the identity.

## Rollback

Central rollback:

1. Set write enabled to `false` and recreate only the central service.
2. Verify the pre-deploy SQLite backup with `PRAGMA integrity_check`.
3. Select the retained immutable image tag and recreate the service without
   deleting the volume or relay tables.
4. Verify Work Monitor, Feishu, DingTalk, and daily boards, then reconcile
   durable cursors before re-enabling a canary.

Per-device rollback:

1. Revoke the affected device centrally if its key or routing is suspect.
2. Boot out only `ai.ostag.copilot-relay`; leave unrelated Agents untouched.
3. Restore the previous runner/release and the preserved device spool.
4. Bootstrap the LaunchAgent, keep central writes disabled, and prove heartbeat,
   runtime inventory, cursor continuity, and zero duplicate receipts.
5. Re-enable only a single-session canary after a fresh backup.
