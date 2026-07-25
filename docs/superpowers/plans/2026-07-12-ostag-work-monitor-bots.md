# OStag Work Monitor Bots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an OStag-native, governed Feishu and Telegram mobile work monitor for remote OStag/osstack instances, including Kanban, critical alerts, twice-daily digests, subscriptions, and safe controls.

**Architecture:** A platform-neutral `ostag.work_monitor` domain folds sanitized, signed instance events into a persistent SQLite projection and reuses OStag runner, ledger, permissions, receipts, and ambient semantics. One Dockerized FastAPI service on Tencent CVM/Lighthouse owns APIs, webhooks, scheduler, and outbox drain; EdgeOne only terminates HTTPS/WAF and reverse-proxies to that origin. Local reporters tail RunLedger automatically, accept an osstack event bridge, and only make outbound HTTPS calls.

**Tech Stack:** Python 3.11+, uv, pytest/pytest-asyncio, FastAPI, uvicorn, stdlib dataclasses/sqlite3/hmac/urllib, lark-oapi, Telegram Bot HTTP API, Docker Compose, Tencent CVM/Lighthouse, EdgeOne reverse proxy/WAF, lark-cli.

---

## File map

- `ostag/work_monitor/models.py`: strict versioned monitor domain models and parsing.
- `ostag/work_monitor/identity.py`: canonical principals and private/group bindings.
- `ostag/work_monitor/redaction.py`: cloud allowlist and secret filtering.
- `ostag/work_monitor/store.py`: SQLite event, projection, subscription, outbox, command repository.
- `ostag/work_monitor/projection.py`: ordered idempotent Kanban projection.
- `ostag/work_monitor/service.py`: queries, subscriptions, critical classification, governed controls.
- `ostag/work_monitor/reporter.py`: durable local upload/poll loop and HMAC client.
- `ostag/work_monitor/executor.py`: validated local OStag/osstack control execution.
- `ostag/work_monitor/rendering.py`: shared mobile board/detail view models.
- `ostag/work_monitor/scheduler.py`: 09:00/19:00 and health notification scheduling.
- `ostag/work_monitor/server.py`: FastAPI API/webhook application and lifespan owner.
- `ostag/adapters/feishu.py`: interactive cards and action normalization.
- `ostag/adapters/telegram.py`: Bot API transport and InlineKeyboard callbacks.
- `ostag/core/runner.py`: route monitor intents through the shared service.
- `ostag/core/permissions.py`, `ostag/core/receipts.py`: extend existing governance verbs only.
- `ostag/core/run_ledger.py`, `ostag/core/ambient.py`: monitor audit events and scheduler integration.
- `ostag/main.py`, `ostag/config.py`: platform, reporter, central-service CLI and settings.
- `deploy/work-monitor/docker-compose.yml`, `Dockerfile`: persistent central service.
- `deploy/work-monitor/edgeone-origin.md`: EdgeOne HTTPS/WAF/reverse-proxy settings.
- `docs/ostag/work_monitor.md`, `docs/ostag/telegram_setup.md`: operations and setup.

### Task 1: Strict event contracts and outbound redaction

**Files:** Create `ostag/work_monitor/__init__.py`, `models.py`, `redaction.py`; create `tests/test_work_monitor_models.py`, `tests/test_work_monitor_redaction.py`; modify `pyproject.toml` testpaths to the `tests` directory.

- [ ] Write failing tests that parse a valid `task.blocked` event, reject an unknown `raw_log` field, reject commands without `expires_at`, and prove strings containing `Bearer`, `TOKEN=`, source, prompt and full log content never appear in serialized output.
- [ ] Run `uv run pytest tests/test_work_monitor_models.py tests/test_work_monitor_redaction.py -q`; expect import failures.
- [ ] Implement frozen dataclasses `TaskSnapshot`, `MonitorEvent`, `RemoteCommand` with explicit `from_dict` key-set validation. Use stages `triage,todo,ready,running,blocked,done`, event kinds from the design, 500-character summary and positive sequence.
- [ ] Implement `sanitize_task_input(input: Mapping[str, object]) -> TaskSnapshot` by projecting only `task_id,title,project,status,agent,progress,duration_seconds,summary,error_class,evidence_ref`; replace token/password/authorization patterns with `[REDACTED]` and truncate before model construction.
- [ ] Replace the current explicit pytest file whitelist with `testpaths = ["tests"]`. Run `uv run pytest --collect-only -q` and assert the output contains every new `test_work_monitor_*`, `test_feishu_work_monitor`, `test_feishu_card_security`, and `test_telegram_work_monitor` module; add this collection check to `scripts/release_check.sh` so a new test file cannot be silently skipped.
- [ ] Re-run focused tests; expect all pass. Commit `feat(work-monitor): add strict remote event contracts`.

### Task 2: SQLite projection with ordering and idempotency

**Files:** Create `ostag/work_monitor/store.py`, `projection.py`; create `tests/test_work_monitor_projection.py`.

- [ ] Write failing tests using `tmp_path / "monitor.db"`: duplicate `(instance_id, sequence)` applies once; sequence 3 arriving before 2 remains pending; applying 2 folds 2 then 3; an event cannot mutate another instance's task; six-stage counts match task rows.
- [ ] Run `uv run pytest tests/test_work_monitor_projection.py -q`; expect missing repository/projector.
- [ ] Implement migrations for `instances`, `events`, `pending_events`, `tasks`, `subscriptions`, `notification_outbox`, `commands`, and unique keys. All writes use one SQLite transaction and `INSERT ... ON CONFLICT` only where version checks pass.
- [ ] Implement `Projector.apply(event) -> ApplyResult` using the stored last sequence, buffering gaps and draining consecutive pending events. Never let an older task version overwrite a newer row.
- [ ] Run focused tests and `uv run pytest tests/test_run_ledger.py -q`; expect pass. Commit `feat(work-monitor): project ordered instance events`.

### Task 3: Signed reporter, reliable spool, and osstack bridge

**Files:** Create `ostag/work_monitor/reporter.py`, `ostag/work_monitor/executor.py`; modify `ostag/main.py`, `ostag/config.py`, `.env.example`; create `tests/test_work_monitor_reporter.py`, `tests/test_work_monitor_executor.py`, `examples/work_monitor_event.json`.

- [ ] Write failing tests with a fake HTTP transport and append-only ledger: persisted inode/offset resumes automatic RunLedger tailing after restart without duplicates; run/stage/receipt/terminal records map to sanitized events; enqueue survives restart; upload signs `timestamp + newline + body` with HMAC-SHA256; acknowledgements delete only confirmed sequences; 500 retains rows.
- [ ] Write failing executor tests with fake OStag and osstack control ports: capability negotiation exposes only declared actions; cockpit retry accepts only failed task, requeues through the existing path and verifies new/reordered attempt state; unsupported pause/resume returns `UNSUPPORTED`; status/version mismatch never invokes execution; cancel polls until authoritative `cancelled` terminal state.
- [ ] Run `uv run pytest tests/test_work_monitor_reporter.py -q`; expect import/CLI failures.
- [ ] Implement `Reporter.tail_ledger_once`, `enqueue`, `flush_once`, `heartbeat`, `poll_commands_once` with injectable clock/transport and exponential backoff metadata. Persist ledger identity, byte offset and last event ID only after local spool commit. Add `ostag monitor ingest --file|--stdin`, `reporter run-once`, `reporter start`, and `reporter status`; the osstack bridge emits task ID/version/status through this CLI without importing internals.
- [ ] Implement `CommandExecutor` with `OStagControlPort` and `OsstackControlPort` protocols. Persist capabilities and recheck at render/apply. Adapt osstack 0.8.1 cancel/loop.stop and existing cockpit retry/requeue; retry requires failed state and acknowledges only verified new/reordered attempt state. Never map `kanban.move`/`daemon.resume` to pause/resume. Compare instance/status/version and poll cancel terminal state.
- [ ] Add settings for instance ID/secret, central-service URL, spool DB, upload interval and command polling; keep reporter disabled by default.
- [ ] Verify `uv run pytest tests/test_work_monitor_reporter.py tests/test_work_monitor_executor.py tests/test_cli_run.py -q` and dry-run `uv run ostag monitor ingest --file examples/work_monitor_event.json --json`; expect sanitized queued event. Commit `feat(work-monitor): add durable reporter and command executor`.

### Task 3A: Complete osstack remote-control contract without duplicating retry

**Files (in `/Users/gilbertchen/Documents/CODE/agentsteam/osstack`):** Modify `packages/cli/src/bin.ts` and the existing loop/task runtime owning task lifecycle; create/modify adjacent `*.test.ts` CLI/runtime tests; update the CLI reference and capability/version declaration.

- [ ] Do not edit `/Users/gilbertchen/Documents/CODE/agentsteam/osstack` directly. Run its dirty-main guard and preserve user changes. Use global worktree path `~/.config/superpowers/worktrees/osstack/ostag-task-controls`: before creation verify the path does not exist and `codex/ostag-task-controls` is not already attached/ambiguous with `git worktree list --porcelain` and `git branch --list`; then create it from recorded source HEAD. Run baseline install/build/focused tests there before edits; no repository ignore change is needed.
- [ ] Locate authoritative task runtime and cockpit retry with `rg "retry|requeue|loop.stop|cancel|kanban.move|daemon.resume" packages`; record exact paths. First add characterization tests proving existing retry accepts only failed tasks and returns/creates the requeued attempt; do not reimplement it. Write failing tests for pause preserving resumable state, resume continuing the same task/run, and versioned remote command results.
- [ ] Run exact focused pnpm/Vitest tests; expect cockpit retry characterization to pass and pause/resume/versioned contract tests to fail.
- [ ] Adapt existing retry into the versioned remote command contract. Add only missing `task pause|resume` runtime/CLI capabilities with structured JSON containing task ID, prior/new status and monotonic version. Pause quiesces the active loop and resume continues that task; neither aliases `kanban.move` or `daemon.resume`.
- [ ] Add capability discovery declaring actions only when supported. Run focused and full osstack release gates in the isolated worktree, commit there, then integrate by reviewed cherry-pick/PR without touching unrelated user changes. Publish/install the resulting osstack version into the OStag integration environment and record its version/SHA; prove all four actions and cancel polling. Until this passes, UI shows `UNSUPPORTED` and launch remains incomplete.

### Task 4: Shared identity, queries, subscriptions, and mobile view model

**Files:** Create `ostag/work_monitor/identity.py`, `service.py`, `rendering.py`; modify `ostag/core/event.py`, `ostag/core/permissions.py`, `ostag/main.py`; create `tests/test_work_monitor_service.py`, `tests/test_work_monitor_rendering.py`, `tests/test_work_monitor_identity.py`; modify `tests/test_event_normalization.py`.

- [ ] Write failing tests for domain board/task/instances/watch methods and capability-aware views. Add identity tests for `Principal(platform, actor_id)`, owner/admin private binding, group enroll/revoke, revoked access denial, platform-scoped permissions, identical Feishu/Telegram actor strings never matching, and callback actor sourced only from trusted normalized payload.
- [ ] Run focused tests; expect missing service/view types.
- [ ] Implement canonical frozen `Principal(platform, actor_id)` and binding records keyed by platform plus destination. Add local-admin/enrollment-secret private owner bind/list/revoke and owner-authorized group enroll/list/revoke; store platform-scoped grants/subscriptions and never resolve a naked actor ID.
- [ ] Implement `WorkMonitorService` using repository queries and `Principal`-aware permission resolution; store subscriptions with delivery cursor. Implement `MobileBoardView` and `TaskDetailView` without platform markup.
- [ ] Extend `NormalizedEvent` with required trusted `chat_type: Literal["private","group"]` and destination identity, updating all adapters/fixtures. Extend `OSTagRunner` routing for Chinese/slash commands using only those normalized fields. Queries require `read`, watch requires `propose`; ambiguous matches return candidates.
- [ ] Run `uv run pytest tests/test_work_monitor_identity.py tests/test_work_monitor_service.py tests/test_work_monitor_rendering.py tests/test_permissions.py tests/test_cli_run.py -q`; expect pass. Commit `feat(work-monitor): add scoped principals and mobile queries`.

### Task 5: Reuse receipts for safe remote controls

**Files:** Modify `ostag/core/schemas.py`, `ostag/core/permissions.py`, `ostag/core/receipts.py`, `ostag/core/run_ledger.py`, `ostag/work_monitor/service.py`; create `tests/test_work_monitor_controls.py`; modify `tests/test_schemas.py`, `tests/test_run_ledger.py`, `tests/test_receipts.py`.

- [ ] Write failing tests proving pause/resume/retry/cancel create `ActionReceipt`; no command exists before approval/apply; controls need `write`; group cancel is denied; confirmation expires/single-use; stale/cross-instance/replay reject. Add fixture tests reading old ledger receipts without serialized Principal, unambiguously migrating from legacy platform/channel context, rejecting ambiguous legacy actor identity, and round-tripping new platform-scoped Principal.
- [ ] Run `uv run pytest tests/test_work_monitor_controls.py -q`; expect unsupported work-control behavior.
- [ ] Extend `MUTATION_TYPES` and `ActionReceipt` serialization for work controls and optional canonical Principal without changing existing transitions. Implement backward-compatible `from_dict`: derive legacy principal only from explicit receipt platform/channel context; mark ambiguous records legacy/unbound and deny control. New receipts always serialize platform + actor ID. Approved apply writes exactly one versioned command.
- [ ] Implement command ack mapping back to receipt terminal state and a new monitor event; failure never masquerades as applied.
- [ ] Run focused tests plus `tests/test_receipts.py tests/test_permissions.py tests/test_cli_receipts.py`; expect pass. Commit `feat(work-monitor): govern remote task controls with receipts`.

### Task 6: Compose core FastAPI service and lifespan

**Files:** Create `ostag/work_monitor/server.py`; create `tests/test_work_monitor_server.py`; modify `pyproject.toml`, `ostag/config.py`; create `deploy/work-monitor/Dockerfile`, `deploy/work-monitor/docker-compose.yml`.

- [ ] Using only completed Task 1–5 domain interfaces, add `server = ["fastapi>=0.115", "uvicorn[standard]>=0.34"]`. Write failing tests for core APIs, scoped bindings/subscriptions, governed command single-consumer poll/ack, `/healthz`, Feishu WS lifespan supervision and graceful shutdown. Do not register either platform webhook in this task.
- [ ] Run `uv run --extra server pytest tests/test_work_monitor_server.py -q`; expect missing composition root/routes, not missing domain placeholders.
- [ ] Compose completed interfaces into `/v1/events`, `/v1/board`, `/v1/tasks/{id}`, `/v1/instances`, `/v1/subscriptions`, `/v1/commands/poll`, `/v1/commands/{id}/ack` and `/healthz` only. Authenticate before domain calls. Provide explicit router-registration hook used by later platform slices; no placeholder webhook route or fake handler.
- [ ] Implement lifespan ownership/supervision and Docker Compose single worker, persistent volume, restart/healthcheck and no secret literals.
- [ ] Run focused tests, Docker Compose config and compileall; expect pass. Commit `feat(work-monitor): compose persistent central service`.

### Task 7: Feishu mobile cards and callbacks

**Files:** Modify `ostag/adapters/base.py`, `ostag/adapters/feishu.py`, `ostag/work_monitor/server.py`; create `ostag/work_monitor/feishu_cards.py`, `ostag/work_monitor/feishu_card_security.py`, `ostag/work_monitor/feishu_routes.py`, `tests/test_feishu_work_monitor.py`, `tests/test_feishu_card_security.py`; modify `docs/ostag/feishu_setup.md`.

- [ ] Write failing renderer tests for mobile cards and proactive sends. Write platform-security spy tests for the exact pipeline: bad raw-body/ciphertext signature means decryptor and runner are both uncalled; tampered ciphertext is rejected before decrypt; valid signature then decrypt/token/schema; callback `Principal` actor comes from verified Feishu payload and ignores button actor fields; bad token/schema never reaches runner; encrypted payload without key is rejected.
- [ ] Run `uv run pytest tests/test_feishu_work_monitor.py -q`; expect missing rich-message APIs.
- [ ] Add rich sends/renderers. Implement `FeishuCardSecurity` in this non-reorderable sequence: capture raw body/ciphertext, timestamp and nonce; calculate platform signature with encrypt key and compare via `hmac.compare_digest`; enforce time window and persist replay key; decrypt only after those checks; validate plaintext verification token and strict schema; then answer challenge or normalize action. Reject encrypted input without key. This is separate from reporter HMAC.
- [ ] Create the real `/webhooks/feishu/card-action` APIRouter around completed security/adapter code and register it through Task 6's router hook. Tests prove the route does not exist before registration, then handles challenge/action and all failure paths without placeholders.
- [ ] Keep ordinary messages exclusively on the lifespan-supervised Feishu WebSocket path. The HTTPS endpoint accepts card actions/challenge only and rejects ordinary message events. Add deployment settings for WS enabled=true and card action URL; document that there is no message-webhook fallback.
- [ ] Document lark-cli app creation/update, minimum message/card/event scopes, `open_id` binding, callback URL/signature, publication and rollback commands with secret placeholders.
- [ ] Run `uv run pytest tests/test_feishu_work_monitor.py tests/test_feishu_card_security.py tests/test_event_normalization.py tests/test_work_monitor_server.py -q`; expect pass, including crash supervision and graceful shutdown. Commit `feat(feishu): secure cards and supervise websocket input`.

### Task 8: Telegram transport and InlineKeyboard parity

**Files:** Create `ostag/adapters/telegram.py`, `ostag/work_monitor/telegram_views.py`, `ostag/work_monitor/telegram_routes.py`, `tests/test_telegram_work_monitor.py`, `docs/ostag/telegram_setup.md`; modify `ostag/work_monitor/server.py`, `ostag/adapters/__init__.py`, `ostag/core/event.py`, `ostag/config.py`, `ostag/main.py`, `pyproject.toml`, `.env.example`.

- [ ] Write failing tests for webhook secret validation, message/callback normalization, HTML escaping, board pagination, InlineKeyboard callback payload under 64 bytes, `answerCallbackQuery`, proactive private/group send, and equivalent receipt confirmation behavior.
- [ ] Run `uv run pytest tests/test_telegram_work_monitor.py -q`; expect missing platform/adapter.
- [ ] Add `telegram` to Platform and adapter factory; implement stdlib HTTP Bot API client with injectable transport. Render shared view models as escaped HTML plus InlineKeyboard; callback data contains only compact opaque action/page IDs.
- [ ] Create and register the real `/webhooks/telegram` APIRouter only after Telegram secret validation, normalization and adapter are implemented. Tests prove no pre-Task-8 placeholder route and verify the registered route constructs Principal from trusted Telegram update data.
- [ ] Add Telegram token, webhook secret/base URL and allowed chat settings. Document BotFather token creation, webhook `secret_token`, private administrator binding, group privacy/allowlist and rollback.
- [ ] Run focused tests plus all normalization tests; expect pass. Commit `feat(telegram): add work monitor bot transport`.

### Task 9: Critical notifications, health, and Beijing digests

**Files:** Create `ostag/work_monitor/scheduler.py`; modify `ostag/core/ambient.py`, `ostag/main.py`, `ostag/config.py`; create `tests/test_work_monitor_scheduler.py`.

- [ ] Write failing clock-driven tests: blocked/failed/approval/offline enqueue immediately 24h; normal completion waits; degraded at 2 minutes, offline at 10; 09:00 and 19:00 `Asia/Shanghai` windows each enqueue once across restarts; one subscription cursor per target; 429/5xx backoff then dead-letter and delayed recovery label.
- [ ] Run focused tests; expect missing scheduler.
- [ ] Implement `WorkMonitorScheduler.run_once` and `drain_notifications_once` using `zoneinfo.ZoneInfo("Asia/Shanghai")`, persisted window keys and outbox. It runs only from the central FastAPI lifespan scheduler owner; request handlers and ambient evaluation may enqueue/wake it but never drain concurrently. EdgeOne never runs domain code.
- [ ] Add configurable hours defaulting to 9 and 19, heartbeat thresholds, retry cap and allowlisted destinations. Record enqueue/deliver/fail/dead-letter in `RunLedger` with redacted payloads.
- [ ] Run scheduler, ambient and ledger tests; expect pass. Commit `feat(work-monitor): schedule critical alerts and daily boards`.

### Task 10: Doctor, deployment, full verification, and real launch

**Files:** Modify `ostag/core/doctor.py`, `README.md`, `docs/ostag/architecture.md`, `docs/ostag/deployment.md`, `docs/cli_reference.md`, `scripts/release_check.sh`; create `docs/ostag/work_monitor.md`, `deploy/work-monitor/edgeone-origin.md`, `scripts/work_monitor_smoke.sh`, `scripts/backup_work_monitor.sh`, `release/work-monitor-live-validation-template.md`.

- [ ] Write failing doctor/smoke tests for missing central credential, unreachable origin, unwritable/unrecoverable persistent volume, duplicate scheduler owner, Feishu credentials/card callback, Telegram webhook status and stale instance; add secret-hygiene fixture containing fake tokens that must never reach artifacts.
- [ ] Implement doctor checks and a deterministic local smoke: two instances → signed ingestion → board projection → Feishu/Telegram render → receipt approve/apply → command poll/ack → notification update.
- [ ] Run `uv sync --all-extras`, `uv run pytest`, `scripts/work_monitor_smoke.sh`, `scripts/release_check.sh`, and `scripts/secret_hygiene_check.sh`; all must exit 0. Fix failures before deployment.
- [ ] Build/push the central image; deploy Docker Compose to Tencent CVM/Lighthouse with `/var/lib/ostag` persistent host volume, restricted origin firewall, healthcheck, restart policy and verified backup/restore. Configure EdgeOne domain, TLS, WAF, rate/body limits and reverse proxy to the origin; prove `/healthz` via origin and EdgeOne. Do not deploy Pages Functions or store SQLite in EdgeOne.
- [ ] Use lark-cli to create/update/publish the Feishu app and save redacted scope/event/card evidence; configure callbacks to EdgeOne. Telegram token is currently absent: the administrator must create it in BotFather and inject it through protected server env before configuring the webhook secret and saving redacted `getWebhookInfo`; never synthesize credentials or mark this complete without external evidence. Register one OStag and one osstack-bridge instance and start reporters.
- [ ] On real Feishu iOS/Android and Telegram iOS/Android verify mobile board, long Chinese title, dark mode, critical alert, forced 09:00/19:00 preview, subscribe, pause/resume, cancel second confirmation, offline/recovery, callback retry, and delayed delivery. Store timestamps/message IDs/screenshots with secrets removed in the live validation record.
- [ ] Re-run `uv run pytest && scripts/release_check.sh`; expect all pass. Update README/architecture/CLI/deployment docs with exact production commands and rollback. Commit `docs(work-monitor): record production launch evidence`.

## Completion gate

Do not call the feature complete until every checkbox is done, all automated gates pass, EdgeOne/Feishu/Telegram production state is inspected, and the real-device evidence proves both notification and governed control loops. A local mock, dry-run card, configured secret name, or successful deployment command alone is not production acceptance.
