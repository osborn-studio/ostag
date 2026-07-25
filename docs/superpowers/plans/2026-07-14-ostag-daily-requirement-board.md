# OStag Daily Requirement Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send one read-only Feishu requirement dashboard card to the FDE group every calendar day at 12:00 Asia/Shanghai time.

**Architecture:** Reuse `AmbientScheduler` for polling and persisted idempotency, add a focused board analyzer plus Feishu Card 2.0 renderer, and deliver through a structured-card adapter with text fallback. The scheduled path reads the original Base through the existing connector and never calls a Base mutation method.

**Tech Stack:** Python 3.11, `zoneinfo`, asyncio, Feishu OpenAPI interactive messages, lark-cli Base connector, pytest, Ruff, osstack Loop Engineering.

---

## File Structure

- Create `ostag/core/requirement_board.py`: models, metrics, focus ranking, recommendation, text fallback.
- Create `ostag/adapters/feishu_cards.py`: pure Feishu Card 2.0 renderer.
- Modify `ostag/core/ambient.py`: snapshot enrichment, schedule, delivery, idempotency, audit.
- Modify `ostag/adapters/base.py` and `ostag/adapters/feishu.py`: structured-card capability and transport.
- Modify `ostag/config.py` and `ostag/main.py`: settings and manual validation command.
- Create `tests/test_requirement_board.py` and `tests/test_feishu_cards.py`.
- Modify `tests/test_ambient.py`, `tests/test_event_normalization.py`, `pyproject.toml`.
- Modify `.env.example` and `docs/ostag/deployment.md`.

## Task 0: Worktree And osstack Tracking

**Files:**
- Verify: `docs/superpowers/specs/2026-07-14-ostag-daily-requirement-board-design.md`
- Verify: `docs/superpowers/plans/2026-07-14-ostag-daily-requirement-board.md`

- [ ] **Step 1: Create the isolated implementation worktree**

```bash
git worktree add /Users/gilbertchen/Documents/CODE/ostag-daily-board \
  -b codex/ostag-daily-requirement-board main
```

Expected: clean worktree with both approved documents.

- [ ] **Step 2: Run Loop Engineering doctor**

```bash
LOOP_WORKSPACE=/Users/gilbertchen/Documents/CODE/ostag-daily-board \
  bash /Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/loop-doctor.sh
```

Expected: exit `0`.

- [ ] **Step 3: Enqueue the bounded feature**

```bash
cd /Users/gilbertchen/Documents/CODE/ostag-daily-board
/Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/osstack-loop.sh \
  enqueue "Implement the approved OStag daily requirement board plan with TDD; never mutate Base records" \
  --capability code.feature --priority 90 \
  --context-json '{"files":["docs/superpowers/specs/2026-07-14-ostag-daily-requirement-board-design.md","docs/superpowers/plans/2026-07-14-ostag-daily-requirement-board.md"]}'
```

Expected: one task JSON under `tasks/`.

## Task 1: Board Model And Deterministic Analysis

**Files:**
- Create: `ostag/core/requirement_board.py`
- Modify: `ostag/core/ambient.py`
- Create: `tests/test_requirement_board.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Collect the new test file**

Add `"tests/test_requirement_board.py"` to `tool.pytest.ini_options.testpaths`.

- [ ] **Step 2: Write failing metric and focus-order tests**

```python
from datetime import date
from ostag.core.requirement_board import RequirementSnapshot, analyze_daily_requirement_board

TODAY = date(2026, 7, 15)

def item(record_id, title, *, status, priority="中 - P1", owner="", owner_ids=(), due=None):
    return RequirementSnapshot(record_id=record_id, title=title, status=status,
        priority=priority, owner=owner, owner_ids=owner_ids, due_date=due)

def test_daily_board_metrics_and_focus_order():
    board = analyze_daily_requirement_board([
        item("r1", "逾期 P0", status="验收中", priority="高 - P0",
             owner="葛德彬", owner_ids=("ou_ge",), due=date(2026, 6, 26)),
        item("r2", "合规", status="待评估", priority="高 - P0"),
        item("r3", "运营", status="待评估", priority="高 - P0"),
        item("r4", "今日到期", status="验收中", priority="高 - P0", due=TODAY),
        item("r5", "完成", status="已完成", priority="高 - P0"),
    ], today=TODAY)
    assert (board.open_count, board.p0_count, board.overdue_count, board.unowned_count) == (4, 4, 1, 3)
    assert [focus.kind for focus in board.focus_items] == [
        "p0_overdue", "p0_unowned_group", "due_today"
    ]
    assert board.focus_items[0].mention_ids == ("ou_ge",)
    assert board.focus_items[1].mention_ids == ()
```

Also test empty input, malformed dates, unknown status, title truncation, grouped unowned items, and deterministic text fallback containing `只读巡检`.

- [ ] **Step 3: Verify RED**

```bash
uv run pytest tests/test_requirement_board.py -q
```

Expected: import failure for `ostag.core.requirement_board`.

- [ ] **Step 4: Implement public contracts and analyzer**

```python
@dataclass(frozen=True)
class RequirementSnapshot:
    record_id: str
    title: str
    owner: str = ""
    owner_ids: tuple[str, ...] = ()
    status: str = ""
    priority: str = ""
    due_date: date | None = None
    proposed_date: date | None = None
    days_stale: int = 0

@dataclass(frozen=True)
class BoardFocusItem:
    kind: str
    title: str
    detail: str
    tone: str
    mention_ids: tuple[str, ...] = ()

@dataclass(frozen=True)
class DailyRequirementBoard:
    today: date
    total_count: int
    open_count: int
    p0_count: int
    overdue_count: int
    unowned_count: int
    status_counts: dict[str, int]
    focus_items: tuple[BoardFocusItem, ...]
    recommendation: str
```

Implement `analyze_daily_requirement_board(requirements, today)` with exact ordering: P0 overdue by oldest due date, one grouped remaining P0-unowned item, due-today by priority, then other overdue. Deduplicate selected records and cap at three focus items. Implement `format_daily_requirement_board_text` without an LLM call.

- [ ] **Step 5: Preserve ambient imports and enrich snapshots**

Move `RequirementSnapshot` to the new module and import it from `ambient.py`, leaving it in that module namespace for backward compatibility. Add `需求提出日期` to `AMBIENT_PROJECTION_FIELDS` and populate:

```python
proposed_date=_first_date(record.fields, ("需求提出日期", "提出日期", "created_at")),
```

- [ ] **Step 6: Verify GREEN and commit**

```bash
uv run pytest tests/test_requirement_board.py tests/test_ambient.py -q
git add pyproject.toml ostag/core/requirement_board.py ostag/core/ambient.py tests/test_requirement_board.py
git commit -m "feat(ambient): analyze daily requirement board"
```

## Task 2: Feishu Card Rendering And Transport

**Files:**
- Create: `ostag/adapters/feishu_cards.py`
- Modify: `ostag/adapters/base.py`, `ostag/adapters/feishu.py`, `pyproject.toml`
- Create: `tests/test_feishu_cards.py`
- Modify: `tests/test_event_normalization.py`

- [ ] **Step 1: Write failing renderer tests and collect them**

Add `"tests/test_feishu_cards.py"` to pytest paths. Test a `DailyRequirementBoard` and assert:

```python
card = render_requirement_board_card(board, group_name="Dadao 投研交易 FDE", base_url=BASE_URL)
assert card["schema"] == "2.0"
assert card["header"]["template"] == "blue"
assert "<at id=ou_ge></at>" in str(card)
assert BASE_URL in str(card)
assert "只读巡检" in str(card)
assert "打开需求表" in str(card)
assert "查看全部风险" in str(card)
assert "负责人：普通成员" in str(card)
assert "<at id=ou_normal></at>" not in str(card)
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_feishu_cards.py -q
```

- [ ] **Step 3: Implement Card 2.0 payload**

Create `render_requirement_board_card(board, group_name, base_url)` returning:

```python
{
    "schema": "2.0",
    "config": {"update_multi": True},
    "header": {
        "template": "blue",
        "title": {"tag": "plain_text", "content": "📋 OStag 每日需求看板"},
        "subtitle": {"tag": "plain_text", "content": subtitle},
    },
    "body": {"elements": [
        {"tag": "markdown", "content": metrics},
        {"tag": "hr"},
        {"tag": "markdown", "content": flow},
        {"tag": "markdown", "content": focus},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": recommendation}]},
        {"tag": "button", "text": {"tag": "plain_text", "content": "打开需求表"},
         "type": "primary", "url": base_url},
        {"tag": "button", "text": {"tag": "plain_text", "content": "查看全部风险"},
         "type": "default", "url": base_url},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]},
    ]},
}
```

Build `flow` as a deterministic four-segment text bar in the approved order
(`待评估 / 方案设计中 / 开发交付中 / 验收中`) followed by numeric labels. Add a
concise data-quality note when unknown statuses or optional fields are missing.
Show every available owner name as plain text, but only `focus.mention_ids` from
overdue P0 records render as `<at id=...></at>`; missing owners never render
`@all`.

- [ ] **Step 4: Add structured-card capability and failing transport test**

Add to `adapters/base.py`:

```python
@runtime_checkable
class StructuredCardAdapter(Protocol):
    async def send_card(self, chat_id: str, card: dict[str, Any]) -> None: ...
```

In `test_event_normalization.py`, capture `FeishuAdapter.send_card` request and assert `msg_type == "interactive"` and decoded `content == card`.

- [ ] **Step 5: Implement proactive interactive transport**

```python
async def send_card(self, chat_id: str, card: dict[str, Any]) -> None:
    if self.config.dry_run:
        print(json.dumps(card, ensure_ascii=False))
        return
    token = await self._tenant_access_token()
    payload = json.dumps({"receive_id": chat_id, "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False)}).encode()
    req = urllib.request.Request(
        f"{self.api_base_url}/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    )
    await asyncio.to_thread(_urlopen_json, req)
```

- [ ] **Step 6: Verify and commit**

```bash
uv run pytest tests/test_feishu_cards.py tests/test_event_normalization.py -q
git add pyproject.toml ostag/adapters/base.py ostag/adapters/feishu.py ostag/adapters/feishu_cards.py tests/test_feishu_cards.py tests/test_event_normalization.py
git commit -m "feat(feishu): send daily requirement dashboard cards"
```

## Task 3: Shanghai Noon Schedule And Idempotency

**Files:**
- Modify: `ostag/config.py`, `ostag/core/ambient.py`, `tests/test_ambient.py`

- [ ] **Step 1: Write failing schedule tests**

Cover defaults, Saturday 12:05 send, 11:59 skip, same-day second run skip, service-restart persisted skip, and a first send failure retrying inside the window. `CapturingAmbientAdapter` must capture both text and cards.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_ambient.py -k daily_board -q
```

- [ ] **Step 3: Add validated settings**

```python
ambient_daily_board_enabled: bool = False
ambient_daily_board_hour: int = 12
ambient_daily_board_window_minutes: int = 60
ambient_timezone: str = "Asia/Shanghai"
ambient_daily_board_group_name: str = "Dadao 投研交易 FDE"
ambient_daily_board_base_url: str = ""
```

Load corresponding `OSTAG_*` variables. Reject hours outside `0..23`, non-positive windows, and invalid `ZoneInfo` names.

- [ ] **Step 4: Implement local-time due logic and post-success marker**

Use an aware UTC default clock and convert it to `ZoneInfo(config.ambient_timezone)`. Naive injected test clocks are interpreted as local. The due predicate is:

```python
start <= local.hour * 60 + local.minute < start + window
```

After snapshots load, analyze and deliver the card before legacy digests. Use `StructuredCardAdapter` when available, otherwise call `send_message` with the text fallback. Write `daily_board:YYYY-MM-DD = 1` only after successful delivery, audit without raw Base rows, then `continue` to avoid a second ambient message in the same sweep.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/test_ambient.py -k daily_board -q
uv run pytest tests/test_ambient.py -q
git add ostag/config.py ostag/core/ambient.py tests/test_ambient.py
git commit -m "feat(ambient): schedule idempotent noon requirement board"
```

## Task 4: Manual Board Validation Command

**Files:**
- Modify: `ostag/main.py`, `tests/test_ambient.py`

- [ ] **Step 1: Write a failing CLI test**

Invoke `main_async(["ambient", "board-once", "--json", "--config", path])` with fakes and assert dry-run delivery, one collected card, and no network call.

- [ ] **Step 2: Add parser and forced execution**

```python
ambient_board = ambient_sub.add_parser("board-once", help="Build the daily requirement board now")
ambient_board.add_argument("--send", action="store_true")
ambient_board.add_argument("--json", action="store_true")
ambient_board.add_argument("--config")
```

Add `AmbientScheduler.run_daily_board_once(force=True)`. Force bypasses the clock window only; it does not bypass ambient policy, Base configuration, delivery errors, or read-only boundaries. Extend `_CollectingAdapter.send_card` and return collected cards in JSON.

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/test_ambient.py -k 'board_once or daily_board' -q
git add ostag/main.py tests/test_ambient.py
git commit -m "feat(cli): add daily board validation command"
```

## Task 5: Configuration And Runbook

**Files:**
- Modify: `.env.example`, `docs/ostag/deployment.md`

- [ ] **Step 1: Add documented defaults**

```dotenv
OSTAG_AMBIENT_ENABLED=false
OSTAG_AMBIENT_CHAT_IDS=
OSTAG_AMBIENT_INTERVAL_SECONDS=300
OSTAG_AMBIENT_DAILY_BOARD_ENABLED=false
OSTAG_AMBIENT_DAILY_BOARD_HOUR=12
OSTAG_AMBIENT_DAILY_BOARD_WINDOW_MINUTES=60
OSTAG_AMBIENT_TIMEZONE=Asia/Shanghai
OSTAG_AMBIENT_DAILY_BOARD_GROUP_NAME="Dadao 投研交易 FDE"
OSTAG_AMBIENT_DAILY_BOARD_BASE_URL=
OSTAG_AMBIENT_RECORD_LIMIT=200
```

- [ ] **Step 2: Add exact production values and validation commands**

Document target chat `oc_ef8aba337000ae1c69aae11dfdfd8ef0`, Base `Q3FkbLGQ6axmTuspP0fc8fLInQm`, table `tblcj5dhYa9vQLd0`, original Base URL, `OSTAG_AUTO_BASE_IDENTITY=bot`, app scopes `base:field:read` and `base:record:read`, plus:

```bash
uv run python -m ostag.main ambient board-once --json
uv run python -m ostag.main ambient board-once --send --json
```

Rollback is `OSTAG_AMBIENT_DAILY_BOARD_ENABLED=false` plus service restart.

- [ ] **Step 3: Check and commit**

```bash
uv run python -m ostag.main --help >/dev/null
git diff --check
git add .env.example docs/ostag/deployment.md
git commit -m "docs: add daily requirement board runbook"
```

## Task 6: Automated And osstack Verification

**Files:**
- Verify all Task 1-5 files.
- Inspect `LOOP-STATE.json` and `STATE.md`.

- [ ] **Step 1: Run focused and full gates**

```bash
uv run pytest tests/test_requirement_board.py tests/test_feishu_cards.py tests/test_ambient.py tests/test_event_normalization.py -q
uv run ruff check ostag tests
uv run pytest -q
scripts/release_check.sh
git diff --check
```

Expected: all exit `0`.

- [ ] **Step 2: Run one osstack Loop verification cycle**

```bash
cd /Users/gilbertchen/Documents/CODE/ostag-daily-board
LOOP_WORKSPACE="$PWD" LOOP_WORKTREE=0 LOOP_ARCHIVE_COMPLETED=1 \
LOOP_EXECUTE="uv run pytest tests/test_requirement_board.py tests/test_feishu_cards.py tests/test_ambient.py tests/test_event_normalization.py -q" \
LOOP_VERIFY="uv run pytest -q && uv run ruff check ostag tests && scripts/release_check.sh" \
bash /Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/loop-run.sh --cycles 1 --stop-when-idle
```

Expected: exit `0`, task archived, verifier successful.

- [ ] **Step 3: Inspect durable state**

```bash
sed -n '1,240p' STATE.md
python -m json.tool LOOP-STATE.json >/dev/null
```

Do not claim completion if state is failed, escalated, or blocked. Do not stage Loop state or unrelated local files.

## Task 7: Feishu Scope, Deployment, And Live Acceptance

**Files:**
- Change production configuration only after Task 6 passes.
- Never mutate the original Base during validation.

- [ ] **Step 1: Apply and publish OStag app read scopes**

Apply `base:field:read` and `base:record:read`; retain existing group-message send scope. Verify one bot-identity record read before deployment. Do not add scheduled Base write scope.

- [ ] **Step 2: Deploy the verified branch**

Use the existing production mechanism, preserve secrets, configure the approved environment, and restart exactly one scheduler-owning OStag process.

- [ ] **Step 3: Run preflight and live send**

```bash
scripts/deploy_preflight.sh
scripts/launch_status.sh
uv run python -m ostag.main ambient board-once --send --json
```

Expected: automated gates pass; live command returns `errors=0`, `sent=1`; one interactive card appears in the FDE group.

- [ ] **Step 4: Independently verify acceptance**

Compare unfinished, P0, overdue, unowned, status distribution, and three focus items against a fresh Base read. Verify button URL, owner mention only for overdue P0, no Base record changes, Asia/Shanghai noon schedule, five-minute polling, one scheduler owner, and persisted daily success marker.

- [ ] **Step 5: Final handoff**

Report original Base link, target group, live card message ID, production process status, full gate results, and osstack state. Leave the unused Base copy untouched unless the user separately authorizes deletion.
