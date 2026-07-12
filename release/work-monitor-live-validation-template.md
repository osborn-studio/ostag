# Work monitor live validation

Status: **INCOMPLETE — external production deployment and real-device validation
have not been performed by this repository task.**

Security blocker: **credential rotation and a coordinated repository-history scrub
are required for the legacy value previously committed in
`docs/ostag/dingtalk_setup.md`.** Do not paste the value into this record. Treat
rotation as mandatory even if the corresponding integration is believed inactive.

Do not change status to complete until every item below has redacted evidence.

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
