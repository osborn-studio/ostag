# Copilot Relay production evidence

Date: 2026-07-14 (Asia/Shanghai)

## Immutable inputs

- OStag commit: `01a2c738140289b5cd47f868a130e30a5f4e8ab4`
- osstack commit: `eaa170bcaa41415aac86475e8ccfedbc352cf6cf`
- Production image: `ostag-work-monitor:copilot-01a2c73`
- Image digest: `sha256:5b24ffdabf93cd733f796f7948286ca09115d835fb1416d246c17d785607071d`
- Region and endpoint: Tencent Lighthouse Singapore behind
  `https://ostag-monitor.ihiking.me`

The final image was built from `git archive HEAD`, not from a dirty working
tree or a diagnostic layer. The release uses only the base Compose file. Its
protected `.env` is linked from the existing production deployment and is not
reproduced here.

## Backup and storage identity

- Correct live storage: Docker named volume `work-monitor_work-monitor-data`,
  mounted at `/var/lib/ostag`.
- Pre-release SQLite backup inside that volume:
  `/var/lib/ostag/backups/work_monitor-before-01a2c73-20260714T131656Z.sqlite3`.
- Backup size: 2,363,392 bytes; mode `0600`.
- Backup and live database both returned `PRAGMA integrity_check = ok`.
- Final container inspection confirms a named-volume mount, not the obsolete
  host bind path.

## Real same-session canary

- Public task: `task_230cdcc6ac9c6a539ff61300`
- Public run: `run_230cdcc6ac9c6a539ff61300`
- Public session: `sess_230cdcc6ac9c6a539ff61300`
- Decision: `decision_canary_live_1`
- The owner clicked “继续单会话灰度” in Feishu. The signed, encrypted callback
  passed signature, app/token, replay, owner, private-binding, TTL, and exact
  session checks and returned HTTP 200; Feishu displayed “回答已排队”.
- The `decision.answer` was acknowledged as delivered, and the local osstack
  daemon returned an `applied` delivery receipt.
- The exact original session advanced from version 5 to version 7 and became
  `idle`; its bounded summary is “收到，继续单会话灰度。”.
- The version-7 assistant snapshot notification was delivered to the bound
  Feishu private chat at `2026-07-14T13:11:32.018771+00:00`.
- The decision projection is now `answered` at
  `2026-07-14T13:11:01Z`.

Native agent-session identifiers and message contents beyond the bounded
acceptance summary are deliberately excluded from this artifact.

## Release gates

- `scripts/release_check.sh`: passed with 671 Python tests.
- Ruff: passed.
- osstack CLI: 416 tests passed; typecheck and production build passed.
- Cross-repository same-session integration smoke: passed.
- Secret hygiene: passed with zero findings.
- External health: `GET /healthz` returned HTTP 200.
- Callback route posture: unsupported `GET` returned HTTP 405.
- Both launchd services remained running after the canary.

## Incidents found and resolved during rollout

1. Release-local `.env` omission caused the callback route to fail closed.
   Immutable releases now link the protected production environment.
2. A production override selected an obsolete host bind database. Final and
   rollback commands use only base Compose and verify the named-volume mount.
3. Feishu sends an opaque signed request timestamp. Integer parsing rejected
   otherwise valid callbacks as 200671; signature verification now accepts the
   exact opaque value and replay protection uses server receive time.
4. Feishu private and group chat IDs both use an `oc_` prefix. Chat type is now
   resolved from the server-owned binding; unknown bindings default to the
   stricter group policy.
5. The answer reached osstack but the central decision projection remained
   pending. Claiming the decision and enqueueing its answer are now one atomic
   transaction; the accepted historical canary was reconciled only after its
   delivered answer and applied receipt were both verified.

## Acceptance result

PASS. The owner can use Feishu to inspect the current osstack session, answer
its decision, continue that exact session, and receive the resulting assistant
reply. The canary remains restricted to the approved owner/private binding and
single public session.
