# Work monitor operations

The work monitor is one persistent control-plane service plus outbound-only
reporters. Two signed event streams (native OStag and an osstack bridge) project
into one SQLite board. Feishu and Telegram render that projection; remote control
actions remain receipt-governed and are executed only by the originating reporter.

Production uses one Docker container and one uvicorn worker on Tencent CVM or
Lighthouse. `/var/lib/ostag` is a host directory, not an EdgeOne store or an
anonymous Docker volume. One process owns the scheduler and notification outbox.

Required protected environment values include `OSTAG_MONITOR_ADMIN_TOKEN`,
`OSTAG_MONITOR_INSTANCE_SECRETS`, and platform credentials. Keep the env file
mode `0600`; never put values in Compose, Git, screenshots, cards, callbacks, or
validation transcripts. Each instance has a distinct revocable secret.

## Preflight

```bash
uv sync --all-extras
uv run pytest
scripts/work_monitor_smoke.sh
scripts/release_check.sh
scripts/secret_hygiene_check.sh
```

For a reporter, set `OSTAG_MONITOR_ENABLED=true`, its instance ID/secret, and the
EdgeOne central URL; `ostag --doctor` checks credentials and `/healthz`. For the
central container set `OSTAG_ENV=production` and `OSTAG_MONITOR_ROLE=central`;
the running scheduler publishes a short DB lease and doctor requires exactly one
unexpired runtime owner. Doctor also performs a temporary SQLite online
backup/restore and checks platform callback state and stale instances.
Doctor calls Telegram `getWebhookInfo` with a five-second timeout and requires its
returned URL to equal `TELEGRAM_WEBHOOK_URL`. For Feishu it sends a deliberately
invalid callback request and requires the authenticated route's HTTP 401 response,
proving the public TLS/WAF/origin route is reachable without a valid action. Probe
errors, tokens, URLs, and bodies are never printed. Tests inject the transport and
never call the network.

## Platform setup

- Feishu: follow [the project setup guide](feishu_setup.md), publish the app, and
  configure the encrypted card-action callback through EdgeOne.
- Telegram: create the bot with [BotFather](https://t.me/BotFather), then follow
  [the project Telegram guide](telegram_setup.md) to set the HTTPS webhook and
  secret header. The repository does not contain or synthesize a token.

Local smoke proves only deterministic application behavior. Production acceptance
also requires the external and real-device evidence listed in
`release/work-monitor-live-validation-template.md`.
