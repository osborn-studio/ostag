# Cross-Platform Daily Task Boards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Operations Minimalist 09:00 and 18:00 task boards to the configured Feishu and DingTalk groups from one reliable Singapore scheduler.

**Architecture:** A platform-neutral `DailyDigestView` computes statistics and priority lists from each platform's existing requirement table. Feishu and DingTalk renderers map that view to native cards, while a lifespan-owned dual-profile scheduler persists delivery windows and retryable outbox rows in the central SQLite database.

**Tech Stack:** Python 3.11, dataclasses, SQLite, FastAPI lifespan, Feishu interactive cards, DingTalk robot Markdown/ActionCard, Lark CLI, DWS CLI, Docker Compose, pytest.

---

### Task 1: Create the platform-neutral daily board model

**Files:**
- Create: `ostag/core/daily_digest.py`
- Test: `tests/test_daily_digest.py`

- [ ] **Step 1: Write failing model tests**

Add fixtures covering overdue, due-today, high-priority, unowned, completed, and
open requirements. Assert the exact morning and evening KPIs and order:

```python
morning = DailyDigestView.build("morning", tasks, today=date(2026, 7, 13))
assert morning.kpis == {
    "focus": 4, "overdue": 2, "high_priority": 2, "unowned": 1,
}
assert [item.record_id for item in morning.priority_items][:2] == ["p0-overdue", "overdue"]

evening = DailyDigestView.build("evening", tasks, today=date(2026, 7, 13))
assert evening.kpis == {
    "completed_today": 2, "completion_rate": 50,
    "open": 2, "escalation": 2,
}
```

Also assert an empty view uses `completion_rate=None`, item lists are limited to
six, `overflow_count` is correct, and source freshness/table URL survive serialization.

- [ ] **Step 2: Run tests and confirm RED**

Run:

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q tests/test_daily_digest.py
```

Expected: import failure because `ostag.core.daily_digest` does not exist.

- [ ] **Step 3: Implement the immutable model and builder**

Create:

```python
@dataclass(frozen=True)
class DailyDigestItem:
    record_id: str
    title: str
    owner: str
    owner_ids: tuple[str, ...]
    status: str
    priority: str
    due_date: date | None
    days_overdue: int
    unowned: bool

@dataclass(frozen=True)
class DailyDigestView:
    kind: Literal["morning", "evening"]
    today: date
    generated_at: datetime
    source_updated_at: datetime | None
    table_url: str
    kpis: dict[str, int | None]
    priority_items: tuple[DailyDigestItem, ...]
    completed_items: tuple[DailyDigestItem, ...]
    open_items: tuple[DailyDigestItem, ...]
    overflow_count: int

    @classmethod
    def build(cls, kind, requirements, *, today, generated_at=None,
              source_updated_at=None, table_url="", item_limit=6): ...
```

Use explicit urgency tuple keys matching the approved specification. Clamp
`item_limit` to `1..10`, truncate titles to 96 Unicode code points, and never
mutate `RequirementSnapshot` inputs.

- [ ] **Step 4: Verify and commit**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q tests/test_daily_digest.py tests/test_ambient.py
git add ostag/core/daily_digest.py tests/test_daily_digest.py
git commit -m "feat(digest): add daily task board model"
```

### Task 2: Build safe native Feishu and DingTalk renderers

**Files:**
- Create: `ostag/core/daily_digest_renderers.py`
- Modify: `ostag/adapters/dingtalk.py`
- Test: `tests/test_daily_digest_renderers.py`
- Test: `tests/test_event_normalization.py`

- [ ] **Step 1: Write failing renderer tests**

Require the morning Feishu card to have a blue header, KPI `column_set`,
`需要优先处理`, `今日推进`, a task-table link, no control callbacks, no more
than 20 top-level elements, and an encoded size below 24 KB. Require the evening
card to contain `今日完成`, `仍未完成`, and `明日关注`.

Require DingTalk output to have:

```python
payload = render_daily_dingtalk_payload(view)
assert payload["msgtype"] in {"markdown", "actionCard"}
assert "今日作战看板" in json.dumps(payload, ensure_ascii=False)
assert payload.get("at", {}).get("atUserIds") == ["manager", "owner"]
assert "callback_id" not in json.dumps(payload)
```

Prove `<script>`, Markdown metacharacters, 300-character Chinese titles, and
table-provided links cannot break either payload.

- [ ] **Step 2: Run tests and confirm RED**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_daily_digest_renderers.py tests/test_event_normalization.py
```

Expected: renderer imports fail and `DingTalkAdapter` lacks native card delivery.

- [ ] **Step 3: Implement renderers and adapter delivery**

Expose exactly:

```python
def render_daily_feishu_card(view: DailyDigestView) -> dict[str, Any]: ...
def render_daily_dingtalk_payload(view: DailyDigestView,
                                  manager_ids: tuple[str, ...] = ()) -> dict[str, Any]: ...
def render_daily_digest_text(view: DailyDigestView) -> str: ...
```

Use the approved module order and semantic blue/red/green labels. Add
`DingTalkAdapter.send_card(destination_id, payload, fallback_text, *, delivery_id)`.
Webhook delivery posts the native payload; DWS bot delivery uses
`chat message send-by-bot --title ... --text ... --format json`. On a native
validation or platform rejection, call the existing text/Markdown fallback once
with the same stable delivery ID.

- [ ] **Step 4: Verify and commit**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_daily_digest_renderers.py tests/test_event_normalization.py \
  tests/test_feishu_work_monitor.py
git add ostag/core/daily_digest_renderers.py ostag/adapters/dingtalk.py \
  tests/test_daily_digest_renderers.py tests/test_event_normalization.py
git commit -m "feat(digest): render native Feishu and DingTalk boards"
```

### Task 3: Add a durable isolated dual-profile scheduler

**Files:**
- Create: `ostag/work_monitor/daily_digest_scheduler.py`
- Modify: `ostag/work_monitor/store.py`
- Test: `tests/test_daily_digest_scheduler.py`

- [ ] **Step 1: Write failing scheduler tests**

Use fake connectors and transports to prove:

```python
await scheduler.tick(drain=True)
assert feishu.sent[0].kind == "morning"
assert dingtalk.sent[0].kind == "morning"
await scheduler.tick(drain=True)
assert len(feishu.sent) == len(dingtalk.sent) == 1
```

Add tests for 18:00, Beijing conversion from UTC, bounded same-day catch-up,
stable delivery IDs, restart deduplication, one connector failing while the other
sends, 1/2/4/8-minute retry timestamps, fallback delivery, and revocation by
disabling a profile before drain.

- [ ] **Step 2: Run tests and confirm RED**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q tests/test_daily_digest_scheduler.py
```

Expected: scheduler import fails.

- [ ] **Step 3: Add SQLite tables and scheduler**

Extend store initialization with:

```sql
CREATE TABLE IF NOT EXISTS daily_digest_windows (
  window_key TEXT PRIMARY KEY, created_at TEXT NOT NULL, delivered_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS daily_digest_outbox (
  delivery_id TEXT PRIMARY KEY, window_key TEXT NOT NULL, platform TEXT NOT NULL,
  destination_id TEXT NOT NULL, payload TEXT NOT NULL, fallback_text TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL, delivered_at TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT ''
);
```

Implement `DailyDigestProfile` and `DailyTaskBoardScheduler`. `tick()` computes
today's 09:00/18:00 due windows, fetches and normalizes records independently,
inserts one outbox row per profile/window, and drains due rows. Mark the window
delivered only after native or fallback delivery succeeds. `run(stop)` polls at
the configured interval and exits cleanly.

- [ ] **Step 4: Verify and commit**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_daily_digest_scheduler.py tests/test_work_monitor_scheduler.py \
  tests/test_work_monitor_server.py
git add ostag/work_monitor/daily_digest_scheduler.py ostag/work_monitor/store.py \
  tests/test_daily_digest_scheduler.py
git commit -m "feat(digest): schedule durable dual-platform boards"
```

### Task 4: Configure and own the scheduler in the central service

**Files:**
- Modify: `ostag/config.py`
- Modify: `.env.example`
- Modify: `ostag/work_monitor/server.py`
- Modify: `deploy/work-monitor/Dockerfile`
- Modify: `deploy/work-monitor/docker-compose.yml`
- Create: `tests/test_config.py`
- Test: `tests/test_work_monitor_server.py`
- Test: `tests/test_work_monitor_container_deploy.py`

- [ ] **Step 1: Write failing configuration and ownership tests**

Assert disabled-by-default validation, exact 09:00/18:00 hour bounds, complete
profile requirements, one lifespan owner, and the scheduler sharing the central
`MonitorStore`. Verify the container includes Node, `@larksuite/cli`, and a DWS
CLI command path without embedding credentials.

- [ ] **Step 2: Run tests and confirm RED**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_config.py tests/test_work_monitor_server.py \
  tests/test_work_monitor_container_deploy.py
```

- [ ] **Step 3: Implement configuration and lifespan composition**

Add named environment-backed fields for enabled flag, polling interval, hours,
and each platform's group ID/base token/table ID/table URL/manager IDs. Reject
enabled profiles with missing identifiers. In `create_app()` construct the two
connectors, adapters, and `DailyTaskBoardScheduler` from the same central store,
then include it in supervised lifespan owners. Do not create a second Feishu WS
or DingTalk Stream consumer.

Install pinned `@larksuite/cli@1.0.69` and
`dingtalk-workspace-cli@1.0.51` in the production image.
Keep all app credentials and table/group identifiers in `.env`, never the image
or Compose YAML.

- [ ] **Step 4: Verify and commit**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_config.py tests/test_work_monitor_server.py \
  tests/test_work_monitor_container_deploy.py
git add ostag/config.py .env.example ostag/work_monitor/server.py \
  deploy/work-monitor/Dockerfile deploy/work-monitor/docker-compose.yml \
  tests/test_config.py tests/test_work_monitor_server.py \
  tests/test_work_monitor_container_deploy.py
git commit -m "feat(server): own daily boards in central service"
```

### Task 5: Full verification and production cutover

**Files:**
- Modify: `docs/ostag/deployment.md`
- Modify: `release/work-monitor-live-validation-template.md`
- Modify: `release/secret-hygiene.json`
- Modify: `release/secret-hygiene.md`

- [ ] **Step 1: Run automated release gates through Loop Engineering**

```bash
LOOP_VERIFY="PYTHON_DOTENV_DISABLED=1 ./scripts/release_check.sh" \
  LOOP_WORKTREE=0 \
  /Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/loop-run.sh \
  --execute "PYTHON_DOTENV_DISABLED=1 ./scripts/release_check.sh" \
  --cycles 2 --stop-when-idle
```

Expected: `STATE.md` reports success, 529+ tests pass, ruff passes, and secret
match count is zero.

- [ ] **Step 2: Create rollback artifacts and deploy**

Create timestamped online SQLite, `.env`, and Compose backups. Sync committed
source without `.git`, local `.env`, logs, or visual companion files. Build the
production image, restart one worker, and wait for healthy status.

- [ ] **Step 3: Configure the two production profiles**

Populate the server `.env` from the existing secret-backed Feishu and DingTalk
profiles. Preserve current group IDs, table IDs, manager IDs, app credentials,
and signed DingTalk webhook. Run connector read-only preflights from inside the
container and print only record counts and profile names.

- [ ] **Step 4: Send controlled real previews**

Force one morning preview to each configured group using a non-windowed preview
delivery ID. Confirm Feishu history reports `interactive`; use DWS chat message
history or robot response evidence to confirm DingTalk ActionCard/Markdown.
Inspect module titles, KPI counts, owner mentions, link, and absence of controls.

- [ ] **Step 5: Enable windows and retire the local scheduler owner**

Enable the central digest scheduler, verify exactly one owner and an empty
outbox, then unload `ai.ostag.dingtalk`. Leave the already-disabled local Feishu
consumer disabled. Validate EdgeOne health and both 09:00/18:00 keys without
manually duplicating a production window.

- [ ] **Step 6: Record evidence and commit**

Record redacted image digest, backup timestamp, health, profile names, preview
message IDs, outbox state, and rollback command. Run secret hygiene again and
commit:

```bash
git add docs/ostag/deployment.md release/work-monitor-live-validation-template.md \
  release/secret-hygiene.json release/secret-hygiene.md
git commit -m "chore(release): launch cross-platform daily boards"
```
