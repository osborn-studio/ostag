# OStag Work Monitor Bots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an OStag-native, governed Feishu and Telegram mobile work monitor for remote OStag/osstack instances, including Kanban, critical alerts, twice-daily digests, subscriptions, and safe controls.

**Architecture:** A platform-neutral `ostag.work_monitor` domain folds sanitized, signed instance events into a persistent SQLite projection and reuses OStag runner, ledger, permissions, receipts, and ambient semantics. One Dockerized FastAPI service on Tencent CVM/Lighthouse owns APIs, webhooks, scheduler, and outbox drain; EdgeOne only terminates HTTPS/WAF and reverse-proxies to that origin. Local reporters tail RunLedger automatically, accept an osstack event bridge, and only make outbound HTTPS calls.

**Tech Stack:** Python 3.11+, uv, pytest/pytest-asyncio, FastAPI, uvicorn, stdlib dataclasses/sqlite3/hmac/urllib, lark-oapi, Telegram Bot HTTP API, Docker Compose, Tencent CVM/Lighthouse, EdgeOne reverse proxy/WAF, lark-cli.

---

## File map

- `ostag/work_monitor/models.py`: strict versioned monitor domain models and parsing.
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
- `ostag/main.py`, `ostag/config.py`: platform/reporter/relay CLI and settings.
- `deploy/work-monitor/docker-compose.yml`, `Dockerfile`: persistent central service.
- `deploy/work-monitor/edgeone-origin.md`: EdgeOne HTTPS/WAF/reverse-proxy settings.
- `docs/ostag/work_monitor.md`, `docs/ostag/telegram_setup.md`: operations and setup.

### Task 1: Strict event contracts and outbound redaction

**Files:** Create `ostag/work_monitor/__init__.py`, `models.py`, `redaction.py`; create `tests/test_work_monitor_models.py`, `tests/test_work_monitor_redaction.py`; modify `pyproject.toml` testpaths.

- [ ] Write failing tests that parse a valid `task.blocked` event, reject an unknown `raw_log` field, reject commands without `expires_at`, and prove strings containing `Bearer`, `TOKEN=`, source, prompt and full log content never appear in serialized output.
- [ ] Run `uv run pytest tests/test_work_monitor_models.py tests/test_work_monitor_redaction.py -q`; expect import failures.
- [ ] Implement frozen dataclasses `TaskSnapshot`, `MonitorEvent`, `RemoteCommand` with explicit `from_dict` key-set validation. Use stages `triage,todo,ready,running,blocked,done`, event kinds from the design, 500-character summary and positive sequence.
- [ ] Implement `sanitize_task_input(input: Mapping[str, object]) -> TaskSnapshot` by projecting only `task_id,title,project,status,agent,progress,duration_seconds,summary,error_class,evidence_ref`; replace token/password/authorization patterns with `[REDACTED]` and truncate before model construction.
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
- [ ] Write failing executor tests with fake OStag and osstack control ports: capability negotiation exposes only declared actions; unsupported action returns `UNSUPPORTED`; expired/wrong-instance and expected status/version mismatch never invoke execution; timeout/nonzero results fail; cancel polls until authoritative `cancelled` terminal state, because command acceptance is not success.
- [ ] Run `uv run pytest tests/test_work_monitor_reporter.py -q`; expect import/CLI failures.
- [ ] Implement `Reporter.tail_ledger_once`, `enqueue`, `flush_once`, `heartbeat`, `poll_commands_once` with injectable clock/transport and exponential backoff metadata. Persist ledger identity, byte offset and last event ID only after local spool commit. Add `ostag monitor ingest --file|--stdin`, `reporter run-once`, `reporter start`, and `reporter status`; the osstack bridge emits task ID/version/status through this CLI without importing internals.
- [ ] Implement `CommandExecutor` with `OStagControlPort` and `OsstackControlPort` protocols. Persist `supported_actions` from heartbeat and recheck at render/apply. For osstack 0.8.1 map only proven cancel/loop.stop; never map `kanban.move` or `daemon.resume` to task controls. Compare instance/status/version and acknowledge only observed post-state, with cancel terminal polling.
- [ ] Add settings for instance ID/secret, relay URL, spool DB, upload interval and command polling; keep reporter disabled by default.
- [ ] Verify `uv run pytest tests/test_work_monitor_reporter.py tests/test_work_monitor_executor.py tests/test_cli_run.py -q` and dry-run `uv run ostag monitor ingest --file examples/work_monitor_event.json --json`; expect sanitized queued event. Commit `feat(work-monitor): add durable reporter and command executor`.

### Task 3A: Add real task controls to osstack upstream

**Files (in `/Users/gilbertchen/Documents/CODE/agentsteam/osstack`):** Modify `packages/cli/src/bin.ts` and the existing loop/task runtime owning task lifecycle; create/modify adjacent `*.test.ts` CLI/runtime tests; update the CLI reference and capability/version declaration.

- [ ] Locate the authoritative runtime with `rg "loop.stop|cancel|kanban.move|daemon.resume" packages` and record exact paths before editing. Write failing tests for task pause preserving resumable state, resume continuing the same task/run, retry creating an auditable new attempt, and invalid/terminal transitions refusing mutation.
- [ ] Run the exact focused pnpm/Vitest tests; expect failure because osstack 0.8.1 lacks task pause/resume/retry.
- [ ] Add explicit `task pause|resume|retry` runtime/CLI capabilities with structured JSON containing task ID, previous/new status and monotonically increasing version. Pause quiesces the active loop, resume continues the resumable task, and retry follows the documented failed/cancelled state machine; none may alias `kanban.move` or `daemon.resume`.
- [ ] Add capability discovery declaring actions only when supported. Run focused and full osstack release gates; publish/install that version, then prove OStag integration for all four actions and cancel terminal polling. Until this passes, UI shows `UNSUPPORTED` and production launch remains incomplete. Commit upstream and OStag changes separately.

### Task 4: Persistent FastAPI central service

**Files:** Create `ostag/work_monitor/server.py`; create `tests/test_work_monitor_server.py`; modify `pyproject.toml`, `ostag/config.py`; create `deploy/work-monitor/Dockerfile`, `deploy/work-monitor/docker-compose.yml`.

- [ ] Add `server = ["fastapi>=0.115", "uvicorn[standard]>=0.34"]` optional dependencies. Write failing `TestClient` tests for valid event batch, invalid signature, timestamp older than 300 seconds, replayed nonce, unknown/oversize payload, scoped board/task/instances queries, single-consumer command poll/ack, both webhook routes and `/healthz` storage/scheduler status.
- [ ] Run `uv run --extra server pytest tests/test_work_monitor_server.py -q`; expect missing app/routes.
- [ ] Implement FastAPI routes `/v1/events`, `/v1/board`, `/v1/tasks/{id}`, `/v1/instances`, `/v1/subscriptions`, `/v1/commands/poll`, `/v1/commands/{id}/ack`, `/webhooks/feishu/card-action`, `/webhooks/telegram`, `/healthz`. Authenticate/limit before domain calls, use `compare_digest`, persist replay nonce, and scope query tokens.
- [ ] Implement FastAPI lifespan so exactly one configured process owns `WorkMonitorScheduler` and notification drain; fail startup if scheduler-owner lock cannot be acquired. Docker Compose runs one uvicorn worker, mounts `${OSTAG_DATA_DIR:-./data}:/var/lib/ostag`, sets restart/healthcheck, and contains no secret literals.
- [ ] Run focused tests, `docker compose -f deploy/work-monitor/docker-compose.yml config`, and `python -m compileall ostag`; expect pass. Commit `feat(work-monitor): add persistent central FastAPI service`.

### Task 5: Shared queries, subscriptions, and mobile view model

**Files:** Create `ostag/work_monitor/service.py`, `rendering.py`; modify `ostag/core/event.py`; create `tests/test_work_monitor_service.py`, `tests/test_work_monitor_rendering.py`, modify `tests/test_event_normalization.py`.

- [ ] Write failing tests for `/board`, `/task`, `/instances`, `/watch`; capability-aware views hide/disable unsupported actions; `NormalizedEvent.chat_type` derives from signed Feishu/Telegram payloads; forged text/button private claims cannot elevate groups; ambiguous task selection; first-screen ordering and pagination.
- [ ] Run focused tests; expect missing service/view types.
- [ ] Implement `WorkMonitorService` using repository queries and existing `resolve_permission`/`level_allows`; store subscriptions by platform, destination, actor and scope with a delivery cursor. Implement `MobileBoardView` and `TaskDetailView` without platform markup.
- [ ] Extend `NormalizedEvent` with required trusted `chat_type: Literal["private","group"]` and destination identity, updating all adapters/fixtures. Extend `OSTagRunner` routing for Chinese/slash commands using only those normalized fields. Queries require `read`, watch requires `propose`; ambiguous matches return candidates.
- [ ] Run `uv run pytest tests/test_work_monitor_service.py tests/test_work_monitor_rendering.py tests/test_permissions.py tests/test_cli_run.py -q`; expect pass. Commit `feat(work-monitor): add governed mobile board queries`.

### Task 6: Reuse receipts for safe remote controls

**Files:** Modify `ostag/core/permissions.py`, `ostag/core/receipts.py`, `ostag/core/run_ledger.py`, `ostag/work_monitor/service.py`; create `tests/test_work_monitor_controls.py`.

- [ ] Write failing tests proving pause/resume/retry/cancel create `ActionReceipt`; no command exists before approval/apply; controls need `write`; group cancel is denied; cancel confirmation expires after 300 seconds and is single-use; stale task version, cross-instance target and replay are rejected and ledgered.
- [ ] Run `uv run pytest tests/test_work_monitor_controls.py -q`; expect unsupported work-control behavior.
- [ ] Extend receipt mutation types/verb mapping without changing existing transitions. `apply` of an approved work-control receipt writes exactly one `RemoteCommand` containing expected version/status, opaque nonce, requester, receipt ID and expiry. Use current permission resolution and `RunLedger.record_receipt*`; add monitor audit event payloads only.
- [ ] Implement command ack mapping back to receipt terminal state and a new monitor event; failure never masquerades as applied.
- [ ] Run focused tests plus `tests/test_receipts.py tests/test_permissions.py tests/test_cli_receipts.py`; expect pass. Commit `feat(work-monitor): govern remote task controls with receipts`.

### Task 7: Feishu mobile cards and callbacks

**Files:** Modify `ostag/adapters/base.py`, `ostag/adapters/feishu.py`; create `ostag/work_monitor/feishu_cards.py`, `tests/test_feishu_work_monitor.py`; modify `docs/ostag/feishu_setup.md`.

- [ ] Write failing tests for 390px-oriented vertical card JSON, critical-first ordering, long Chinese title, opaque action IDs, text fallback, callback normalization, operator/chat_type preservation, `interactive` payload, proactive private send with `receive_id_type=open_id`, and group send with `receive_id_type=chat_id`.
- [ ] Run `uv run pytest tests/test_feishu_work_monitor.py -q`; expect missing rich-message APIs.
- [ ] Add optional `send_view`/`reply_view` while retaining text send. Implement renderers and callback normalization; `send_view(destination_type="user")` calls Feishu messages API with `receive_id_type=open_id`, while group uses `chat_id`. Never trust actor/chat type/target fields from button data.
- [ ] Document lark-cli app creation/update, minimum message/card/event scopes, `open_id` binding, callback URL/signature, publication and rollback commands with secret placeholders.
- [ ] Run focused tests and existing event normalization tests; expect pass. Commit `feat(feishu): render governed work monitor cards`.

### Task 8: Telegram transport and InlineKeyboard parity

**Files:** Create `ostag/adapters/telegram.py`, `ostag/work_monitor/telegram_views.py`, `tests/test_telegram_work_monitor.py`, `docs/ostag/telegram_setup.md`; modify `ostag/adapters/__init__.py`, `ostag/core/event.py`, `ostag/config.py`, `ostag/main.py`, `pyproject.toml`, `.env.example`.

- [ ] Write failing tests for webhook secret validation, message/callback normalization, HTML escaping, board pagination, InlineKeyboard callback payload under 64 bytes, `answerCallbackQuery`, proactive private/group send, and equivalent receipt confirmation behavior.
- [ ] Run `uv run pytest tests/test_telegram_work_monitor.py -q`; expect missing platform/adapter.
- [ ] Add `telegram` to Platform and adapter factory; implement stdlib HTTP Bot API client with injectable transport. Render shared view models as escaped HTML plus InlineKeyboard; callback data contains only compact opaque action/page IDs.
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
