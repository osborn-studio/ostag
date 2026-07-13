# Work monitor live validation

Status: **PARTIAL — the Operations Minimalist Feishu restoration is live and
validated; the broader multi-device/control matrix below remains open.**

Security blocker: **credential rotation and a coordinated repository-history scrub
are required for the legacy value previously committed in
`docs/ostag/dingtalk_setup.md`.** Do not paste the value into this record. Treat
rotation as mandatory even if the corresponding integration is believed inactive.

Do not change status to complete until every item below has redacted evidence.

## Operations Minimalist restoration — 2026-07-13

- [x] Singapore central FastAPI is the sole Feishu WebSocket ingress; the local
  `ai.ostag.feishu` launchd owner was unloaded.
- [x] Production container `f129437deae4` is healthy on image digest
  `sha256:034877aeb4b8a38424ab2dce5881acd16277e3eb33eb1fe7952feb3577bd328f`.
- [x] Origin and EdgeOne `/healthz` returned `{"status":"ok"}`.
- [x] Owner binding authorizes `read`, `propose`, and `write`; the owner board
  sees all enrolled instances (`scope=all`). Group readers remain read-only and
  group cards never expose cancel.
- [x] A real native Feishu board was accepted and delivered as interactive
  message `om_x100b6a7dc437ecb4b305b082ff26adc` at 2026-07-13 07:50 UTC.
- [x] Online SQLite rollback backup
  `work_monitor.pre-operations-minimalist-20260713T074730Z.sqlite3` passed
  `PRAGMA integrity_check` with one active binding.
- [x] Local reporter launchd owner is active; spool, pending event, and pending
  notification counts are zero.
- [x] Telegram webhook is configured with zero pending updates. Its retained
  last-error timestamp predates this deployment and is tracked separately.

Employee osstack instances must each use a unique `instance_id` and credential.
The owner's private destination uses `scope=all`, while employee/group
destinations receive explicit `project:`, `instance:`, or `task:` scopes. No
employee receives the owner credential.

## Deployment prerequisites and evidence

- [ ] Image digest built and pushed to the approved registry: `<digest>`
- [ ] Tencent CVM/Lighthouse host, restricted origin firewall rule IDs: `<ids>`
- [ ] `/var/lib/ostag` ownership/mode and successful backup/restore timestamp: `<evidence>`
- [ ] Exactly one running worker/scheduler owner: `<container id / timestamp>`
- [ ] EdgeOne TLS/WAF/body/rate/reverse-proxy rule export: `<redacted artifact>`
- [ ] Origin `/healthz` HTTP 200 and EdgeOne `/healthz` HTTP 200: `<timestamps>`
- [ ] Feishu app published; scopes/events/encrypted card callback inspected: `<ids>`
- [ ] Telegram BotFather token created and protected (never paste it here): `<operator/time>`
- [ ] Redacted Telegram `getWebhookInfo` confirms URL and no pending error: `<artifact>`
- [ ] Native OStag and osstack bridge reporters registered and fresh: `<instance ids>`

## Real-device matrix

Record UTC timestamp, platform message/callback ID, device/OS, expected/actual
result, and a secret-redacted screenshot for Feishu iOS/Android and Telegram
iOS/Android:

- [ ] Board and long Chinese title
- [ ] Dark mode
- [ ] Critical alert
- [ ] Forced 09:00 and 19:00 digest previews
- [ ] Subscribe
- [ ] Pause/resume receipt approval and application
- [ ] Cancel second confirmation
- [ ] Reporter offline and recovery
- [ ] Callback retry/idempotency
- [ ] Delayed notification delivery and eventual update

## Final gates

- [ ] `uv run pytest`
- [ ] `scripts/release_check.sh`
- [ ] `scripts/secret_hygiene_check.sh`
- [ ] Operator and reviewer sign-off: `<names / timestamps>`
