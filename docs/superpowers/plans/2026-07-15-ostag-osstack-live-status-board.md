# OStag osstack Live Status Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Use osstack Loop Engineering for task state and release gates; do not dispatch subagents for this plan.

**Goal:** Make natural-language Feishu status questions return an identity-scoped C-style osstack dashboard showing device and Agent information without creating a Copilot Relay instruction.

**Architecture:** Add a deterministic read-only intent classifier before ordinary Copilot Relay routing, a durable Feishu-principal-to-instance ownership model, and a platform-neutral status-board service over the existing Work Monitor and Relay projections. Render the view as a mobile Feishu card with identity-bound callbacks; retain the existing exact-session relay path for actual instructions and decisions.

**Tech Stack:** Python 3.11, SQLite, FastAPI, Feishu interactive cards, pytest, Ruff, Docker Compose, osstack Loop Engineering.

**Approved design:** `docs/superpowers/specs/2026-07-15-ostag-osstack-live-status-board-design.md`

---

## File map

- Create `ostag/copilot_relay/status_intent.py`: pure natural-language status/mixed-intent classification.
- Create `ostag/copilot_relay/status_board.py`: platform-neutral board data classes, authorization-aware query service, sorting, health, and text fallback.
- Modify `ostag/work_monitor/identity.py`: add member private bindings and durable instance ownership methods.
- Modify `ostag/copilot_relay/policy.py`: resolve non-owner instance scopes dynamically from `IdentityStore`.
- Modify `ostag/copilot_relay/feishu_cards.py`: render the C dashboard with bounded, opaque callbacks.
- Modify `ostag/copilot_relay/feishu_routes.py`: route status intent before relay writes and handle refresh/personal/team callbacks.
- Verify `ostag/adapters/feishu.py` unchanged: its existing structured-card branch must deliver the new controller result without a relay-message binding.
- Modify `ostag/work_monitor/server.py`: construct the dynamic policy and status-board service from the shared store.
- Modify `ostag/config.py` and `.env.example`: add the rollback feature flag.
- Modify `ostag/main.py`: add local-admin CLI commands for member private binding and instance assignment.
- Modify `docs/ostag/deployment.md`: document onboarding, permission boundaries, smoke tests, rollback, and production acceptance.
- Create `tests/test_copilot_status_intent.py`: classifier behavior and false-positive protection.
- Create `tests/test_copilot_status_board.py`: view aggregation, scoping, health, sorting, and text fallback.
- Modify `tests/test_work_monitor_identity.py`: member/private and instance ownership migration tests.
- Modify `tests/test_copilot_relay_feishu_cards.py`: C-card content, safety, callbacks, and owner/member differences.
- Modify `tests/test_copilot_relay_feishu_routes.py`: no-queue status routing, mixed intent, callbacks, and scope isolation.
- Modify `tests/test_feishu_work_monitor.py`: adapter-level structured-card delivery.
- Modify `tests/test_work_monitor_server.py`: production wiring and feature-flag behavior.
- Modify `tests/test_cli_run.py`: local-admin instance binding CLI.

## Spec coverage map

- Spec §§1–4（目标、根因、状态意图、混合意图）→ Tasks 2 and 6.
- Spec §5（共享机器人、member 私聊、个人/团队实例范围）→ Tasks 3, 6, and 8.
- Spec §6（Work Monitor + Relay 只读视图、北京时间、排序）→ Task 4.
- Spec §7（C 看板、按钮、数据最小化）→ Tasks 5 and 6.
- Spec §8（未绑定、空会话、离线、陈旧、错误回退）→ Tasks 4, 5, and 6.
- Spec §9（组件边界）→ File map and Tasks 2–6.
- Spec §10（单元、权限、集成和生产验收）→ Tasks 2–10.
- Spec §11（不可变部署与回滚）→ Tasks 7, 9, and 10.
- Spec §12（完成标准）→ Task 10 live acceptance and final osstack gate.

## Task 1: Start osstack-managed execution

**Files:**
- Runtime state only: `tasks/`, `LOOP-STATE.json`, `STATE.md` (task queue files should be archived; generated state must not be committed unless already tracked by project policy)

- [ ] **Step 1: Check the Loop Engineering runtime**

Run:

```bash
/Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/loop-doctor.sh
```

Expected: `Loop doctor: ok`, the workspace is this repository, and protected instruction files are unchanged.

- [ ] **Step 2: Enqueue the implementation task**

Run:

```bash
/Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/osstack-loop.sh enqueue \
  "Implement the approved identity-scoped OStag osstack live status board plan" \
  --capability code.feature --priority 100
```

Expected: one `task_<id>` and a JSON path under `tasks/`.

- [ ] **Step 3: Record the task ID in the working checklist**

Run:

```bash
/Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/osstack-loop.sh status
```

Expected: the new task appears under Pending and no unrelated task is running.

## Task 2: Classify status queries before relay instructions

**Files:**
- Create: `ostag/copilot_relay/status_intent.py`
- Create: `tests/test_copilot_status_intent.py`

- [ ] **Step 1: Write failing classifier tests**

Create `tests/test_copilot_status_intent.py`:

```python
import pytest

from ostag.copilot_relay.status_intent import classify_osstack_status_intent


@pytest.mark.parametrize(
    "text",
    (
        "现在 osstack 在干嘛？",
        "OS Stack 正在做什么",
        "目前开发进展怎么样",
        "有哪些设备和 Agent 正在运行？",
        "给我看一下工作状态",
        "用看板汇报目前进展",
    ),
)
def test_status_queries_are_read_only(text: str) -> None:
    assert classify_osstack_status_intent(text) == "status_query"


@pytest.mark.parametrize(
    "text",
    ("继续开发", "换成方案 B", "先补测试再部署", "修改 osstack 状态显示组件"),
)
def test_mutating_instructions_are_not_status_queries(text: str) -> None:
    assert classify_osstack_status_intent(text) == "none"


def test_mixed_query_and_mutation_is_explicit() -> None:
    assert classify_osstack_status_intent("告诉我现在进展，然后换成方案 B") == "mixed"
```

- [ ] **Step 2: Run the tests and confirm the missing module failure**

Run: `uv run pytest tests/test_copilot_status_intent.py -q`

Expected: FAIL with `ModuleNotFoundError: ostag.copilot_relay.status_intent`.

- [ ] **Step 3: Implement the pure classifier**

Create `ostag/copilot_relay/status_intent.py`:

```python
"""Deterministic read-only osstack status intent classification."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal


StatusIntent = Literal["status_query", "mixed", "none"]

_OSSTACK = re.compile(r"\bos\s*stack\b", re.IGNORECASE)
_STATUS_TERMS = (
    "在干嘛", "做什么", "进展", "状态", "看板", "汇报", "报告", "运行",
    "哪些设备", "哪个设备", "哪些 agent", "哪个 agent", "当前任务",
)
_READ_TERMS = ("现在", "目前", "当前", "查看", "看看", "给我看", "告诉我", "汇报")
_WRITE_TERMS = (
    "继续", "改为", "换成", "采用", "先做", "开发", "部署", "上线", "发布",
    "删除", "停止", "暂停", "恢复", "重试", "回答", "同意", "批准",
)


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(value.replace("os-stack", "osstack").split())


def classify_osstack_status_intent(text: str) -> StatusIntent:
    normalized = _normalize(text)
    status_signal = (
        "看板" in normalized
        or (any(term in normalized for term in _STATUS_TERMS)
            and (bool(_OSSTACK.search(normalized))
                 or any(term in normalized for term in _READ_TERMS)))
    )
    if not status_signal:
        return "none"
    if any(term in normalized for term in _WRITE_TERMS):
        # “开发进展” is a query phrase, not a request to develop.
        remainder = normalized.replace("开发进展", "进展")
        if any(term in remainder for term in _WRITE_TERMS):
            return "mixed"
    return "status_query"
```

- [ ] **Step 4: Run classifier tests**

Run: `uv run pytest tests/test_copilot_status_intent.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit the classifier**

```bash
git add ostag/copilot_relay/status_intent.py tests/test_copilot_status_intent.py
git commit -m "feat(copilot): classify read-only osstack status queries"
```

## Task 3: Persist member bindings and instance ownership

**Files:**
- Modify: `ostag/work_monitor/identity.py`
- Modify: `tests/test_work_monitor_identity.py`

- [ ] **Step 1: Add failing identity and migration tests**

Append to `tests/test_work_monitor_identity.py`:

```python
def test_local_admin_binds_member_private_chat_and_exact_instances(tmp_path: Path) -> None:
    identities = IdentityStore(tmp_path / "monitor.sqlite3")
    member = Principal("feishu", "ou_employee")
    private = Destination("feishu", "oc_employee_private", "private")

    identities.bind_private(owner=member, destination=private, local_admin=True, role="member")
    identities.bind_instance(
        member, "employee-mac", display_name="员工 Mac mini",
        bound_at="2026-07-15T00:00:00Z",
    )

    assert identities.binding(private) == Binding(member, private, role="member")
    assert identities.personal_instances(member) == frozenset({"employee-mac"})
    assert identities.instance_display_names({"employee-mac"}) == {
        "employee-mac": "员工 Mac mini"
    }
    assert identities.allowed_instances(member) == frozenset({"employee-mac"})
    assert not identities.is_global_owner(member)


def test_instance_has_one_active_owner_and_revoke_is_immediate(tmp_path: Path) -> None:
    identities = IdentityStore(tmp_path / "monitor.sqlite3")
    first = Principal("feishu", "first")
    second = Principal("feishu", "second")
    identities.bind_instance(first, "shared-mac", display_name="Mac", bound_at="2026-07-15T00:00:00Z")
    with pytest.raises(IdentityError, match="already bound"):
        identities.bind_instance(second, "shared-mac", display_name="Mac", bound_at="2026-07-15T00:01:00Z")
    identities.revoke_instance("shared-mac", revoked_at="2026-07-15T00:02:00Z")
    assert identities.personal_instances(first) == frozenset()


def test_existing_identity_database_migrates_instance_ownership_table(tmp_path: Path) -> None:
    database = tmp_path / "monitor.sqlite3"
    sqlite3.connect(database).execute("CREATE TABLE monitor_bindings(platform TEXT, destination_id TEXT, chat_type TEXT, owner_platform TEXT, owner_actor_id TEXT, role TEXT, scope TEXT, revoked INTEGER, PRIMARY KEY(platform,destination_id))").connection.close()
    identities = IdentityStore(database)
    with identities._connect() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(monitor_instance_owners)")}
    assert {"instance_id", "principal_platform", "actor_id", "display_name", "revoked"} <= columns
```

- [ ] **Step 2: Run the identity tests and confirm missing APIs**

Run: `uv run pytest tests/test_work_monitor_identity.py -q`

Expected: FAIL because `role="member"`, `bind_instance`, and ownership queries do not exist.

- [ ] **Step 3: Add the schema and focused ownership API**

In `ostag/work_monitor/identity.py`, extend `Binding.__post_init__` to accept `member`, add this table to `IdentityStore.__init__`, and add the methods below:

```python
if self.role not in {"owner", "admin", "member"}:
    raise ValueError("binding role must be owner, admin, or member")
```

```sql
CREATE TABLE IF NOT EXISTS monitor_instance_owners (
    instance_id TEXT PRIMARY KEY,
    principal_platform TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    bound_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    revoked_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS monitor_instance_owner_principal
ON monitor_instance_owners(principal_platform, actor_id, revoked);
```

Change `bind_private` to accept `role: str = ""`; resolve it with:

```python
resolved_role = role or ("admin" if local_admin else "owner")
if resolved_role == "member" and not local_admin:
    raise IdentityError("member private binding requires local-admin")
if resolved_role not in {"owner", "admin", "member"}:
    raise IdentityError("invalid private binding role")
```

Pass `resolved_role` into `_upsert_binding`. Add:

```python
def bind_instance(
    self, principal: Principal, instance_id: str, *, display_name: str, bound_at: str
) -> None:
    if not instance_id.strip() or len(instance_id) > 128:
        raise IdentityError("invalid instance id")
    with self._connect() as connection:
        row = connection.execute(
            "SELECT principal_platform,actor_id,revoked FROM monitor_instance_owners WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        if row is not None and not int(row["revoked"]) and (
            row["principal_platform"], row["actor_id"]
        ) != (principal.platform, principal.actor_id):
            raise IdentityError("instance is already bound")
        connection.execute(
            "INSERT INTO monitor_instance_owners(instance_id,principal_platform,actor_id,display_name,bound_at,revoked,revoked_at) "
            "VALUES (?,?,?,?,?,0,'') ON CONFLICT(instance_id) DO UPDATE SET "
            "principal_platform=excluded.principal_platform,actor_id=excluded.actor_id,"
            "display_name=excluded.display_name,bound_at=excluded.bound_at,revoked=0,revoked_at=''",
            (instance_id, principal.platform, principal.actor_id, display_name[:80], bound_at),
        )

def revoke_instance(self, instance_id: str, *, revoked_at: str) -> None:
    with self._connect() as connection:
        changed = connection.execute(
            "UPDATE monitor_instance_owners SET revoked=1,revoked_at=? WHERE instance_id=? AND revoked=0",
            (revoked_at, instance_id),
        ).rowcount
    if not changed:
        raise IdentityError("active instance binding not found")

def personal_instances(self, principal: Principal) -> frozenset[str]:
    with self._connect() as connection:
        rows = connection.execute(
            "SELECT instance_id FROM monitor_instance_owners WHERE principal_platform=? AND actor_id=? AND revoked=0",
            (principal.platform, principal.actor_id),
        ).fetchall()
    return frozenset(str(row["instance_id"]) for row in rows)

def team_instances(self) -> frozenset[str]:
    with self._connect() as connection:
        rows = connection.execute(
            "SELECT instance_id FROM monitor_instance_owners WHERE revoked=0"
        ).fetchall()
    return frozenset(str(row["instance_id"]) for row in rows)

def instance_display_names(self, instance_ids: set[str] | frozenset[str]) -> dict[str, str]:
    if not instance_ids:
        return {}
    placeholders = ",".join("?" for _ in instance_ids)
    with self._connect() as connection:
        rows = connection.execute(
            f"SELECT instance_id,display_name FROM monitor_instance_owners WHERE revoked=0 AND instance_id IN ({placeholders})",  # noqa: S608
            tuple(sorted(instance_ids)),
        ).fetchall()
    return {str(row["instance_id"]): str(row["display_name"] or row["instance_id"]) for row in rows}
```

Change `allowed_instances` so non-global users receive `personal_instances(principal)` union existing granted instance scopes. Do not change the `None` global-owner meaning.

- [ ] **Step 4: Run identity and legacy migration tests**

Run: `uv run pytest tests/test_work_monitor_identity.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit identity ownership**

```bash
git add ostag/work_monitor/identity.py tests/test_work_monitor_identity.py
git commit -m "feat(identity): bind Feishu members to osstack instances"
```

## Task 4: Build the platform-neutral status board

**Files:**
- Create: `ostag/copilot_relay/status_board.py`
- Create: `tests/test_copilot_status_board.py`

- [ ] **Step 1: Write failing aggregation and scope tests**

Create `tests/test_copilot_status_board.py`. Seed the real SQLite schema with two instances, three sessions, one pending decision, and tasks using the existing `RelayStore.upsert_session()` test-envelope helper and direct parameterized inserts for monitor projections. The central assertions are:

```python
view = service.build(OWNER, scope="personal")
assert view.scope == "personal"
assert view.device_count == 1
assert view.online_count == 1
assert view.running_count == 1
assert view.waiting_decision_count == 1
assert view.current_session is not None
assert view.current_session.device_name == "我的 Mac mini"
assert view.current_session.agent_id == "codex-cli"
assert all(item.instance_id == "owner-mac" for item in view.sessions)

team = service.build(OWNER, scope="team")
assert {item.instance_id for item in team.sessions} == {"owner-mac", "employee-mac"}

with pytest.raises(PermissionError):
    service.build(EMPLOYEE, scope="team")
```

Add these concrete assertions to the same test file:

```python
assert [item.state for item in team.sessions] == [
    "awaiting_decision", "blocked", "running",
]
assert team.attention == team.sessions[:2]
assert team.stale is False
assert "北京时间" in format_osstack_status_text(team)
with relay.monitor_store.connect() as connection:
    assert connection.execute("SELECT COUNT(*) FROM relay_messages").fetchone()[0] == 0

clock[0] += timedelta(seconds=601)
offline = service.build(OWNER, scope="personal")
assert offline.online_count == 0
assert offline.stale is True
```

- [ ] **Step 2: Run the new tests and confirm the missing module failure**

Run: `uv run pytest tests/test_copilot_status_board.py -q`

Expected: FAIL with missing `status_board` module.

- [ ] **Step 3: Add immutable view types**

Create `ostag/copilot_relay/status_board.py` with these public types:

```python
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ostag.core.identity import Principal
from ostag.work_monitor.identity import IdentityStore

from .store import RelaySession, RelayStore


BoardScope = Literal["personal", "team"]


@dataclass(frozen=True)
class StatusSessionItem:
    instance_id: str
    session_id: str
    device_name: str
    agent_id: str
    title: str
    state: str
    latest_summary: str
    updated_at: str
    online: bool
    waiting_decision: bool


@dataclass(frozen=True)
class OsstackStatusBoardView:
    scope: BoardScope
    generated_at: str
    device_count: int
    online_count: int
    running_count: int
    waiting_decision_count: int
    blocked_count: int
    completed_today_count: int
    current_session: StatusSessionItem | None
    sessions: tuple[StatusSessionItem, ...]
    attention: tuple[StatusSessionItem, ...]
    stale: bool
```

- [ ] **Step 4: Implement the read-only service**

Add `OsstackStatusBoardService(relay_store, identities, owner, clock, offline_seconds)` with:

```python
def _allowed(self, principal: Principal, scope: BoardScope) -> frozenset[str]:
    if scope == "team":
        if principal != self.owner or not self.identities.is_global_owner(principal):
            raise PermissionError("team status requires global owner")
        return self.identities.team_instances()
    return self.identities.personal_instances(principal)
```

Implement `build()` using parameterized `IN` queries against `relay_sessions`, `relay_decisions`, `instances`, and `tasks`. Do not query or mutate `relay_messages`. The method follows this complete shape (extract `_iso()` and `_beijing()` helpers rather than duplicating timestamp parsing):

```python
def build(self, principal: Principal, *, scope: BoardScope) -> OsstackStatusBoardView:
    allowed = self._allowed(principal, scope)
    now = self.clock()
    if not allowed:
        return OsstackStatusBoardView(
            scope, now.isoformat(), 0, 0, 0, 0, 0, 0, None, (), (), False
        )
    ids = tuple(sorted(allowed))
    placeholders = ",".join("?" for _ in ids)
    with self.store.monitor_store.connect() as db:
        session_rows = db.execute(
            f"SELECT * FROM relay_sessions WHERE instance_id IN ({placeholders})",  # noqa: S608
            ids,
        ).fetchall()
        decision_rows = db.execute(
            f"SELECT instance_id,session_id FROM relay_decisions WHERE state='pending' "
            f"AND instance_id IN ({placeholders})",  # noqa: S608
            ids,
        ).fetchall()
        instance_rows = db.execute(
            f"SELECT instance_id,last_seen_at FROM instances WHERE instance_id IN ({placeholders})",  # noqa: S608
            ids,
        ).fetchall()
        task_rows = db.execute(
            f"SELECT status,occurred_at FROM tasks WHERE instance_id IN ({placeholders})",  # noqa: S608
            ids,
        ).fetchall()
    pending = {(str(row["instance_id"]), str(row["session_id"])) for row in decision_rows}
    seen = {str(row["instance_id"]): str(row["last_seen_at"] or "") for row in instance_rows}
    names = self.identities.instance_display_names(frozenset(ids))

    def online(instance_id: str) -> bool:
        value = seen.get(instance_id, "")
        return bool(value) and (now - _iso(value)).total_seconds() < self.offline_seconds

    items = tuple(
        StatusSessionItem(
            instance_id=str(row["instance_id"]),
            session_id=str(row["session_id"]),
            device_name=names.get(str(row["instance_id"]), str(row["instance_id"])),
            agent_id=str(row["agent_id"]),
            title=str(row["title"]),
            state=str(row["state"]),
            latest_summary=str(row["latest_summary"]),
            updated_at=str(row["updated_at"]),
            online=online(str(row["instance_id"])),
            waiting_decision=(str(row["instance_id"]), str(row["session_id"])) in pending,
        )
        for row in session_rows
    )
    ordered = tuple(sorted(items, key=_rank))
    selected = self.store.current_session(principal.platform, principal.actor_id)
    current = next(
        (
            item for item in ordered
            if selected is not None
            and item.instance_id == selected.instance_id
            and item.session_id == selected.session_id
        ),
        ordered[0] if ordered else None,
    )
    today = now.astimezone(BEIJING).date()
    completed = sum(
        str(row["status"]) == "done" and _iso(str(row["occurred_at"])).astimezone(BEIJING).date() == today
        for row in task_rows
    )
    return OsstackStatusBoardView(
        scope=scope,
        generated_at=now.isoformat(),
        device_count=len(ids),
        online_count=sum(online(instance_id) for instance_id in ids),
        running_count=sum(item.state == "running" for item in ordered),
        waiting_decision_count=sum(item.waiting_decision for item in ordered),
        blocked_count=sum(item.state == "blocked" for item in ordered),
        completed_today_count=completed,
        current_session=current,
        sessions=ordered[:4],
        attention=tuple(item for item in ordered if item.waiting_decision or item.state == "blocked")[:3],
        stale=not all(online(instance_id) for instance_id in ids),
    )
```

Build `StatusSessionItem` values with the identity display-name map, calculate online state from `last_seen_at`, and select the actor's current session only when it belongs to the allowed set. Store `session_id` as an internal field on `StatusSessionItem` so current-session matching uses `(instance_id, session_id)`; omit it from rendered card content. Sort with:

```python
def _rank(item: StatusSessionItem) -> tuple[int, float]:
    priority = 0 if item.waiting_decision else {
        "blocked": 1, "running": 2, "idle": 3, "completed": 4,
    }.get(item.state, 5)
    return priority, -_iso(item.updated_at).timestamp()
```

Return no instance/session information when the allowed set is empty. Provide `format_osstack_status_text(view)` with device, Agent, state, summary, Beijing time, and `只读状态查询`.

- [ ] **Step 5: Run aggregation tests**

Run: `uv run pytest tests/test_copilot_status_board.py -q`

Expected: all tests PASS.

- [ ] **Step 6: Commit the status board service**

```bash
git add ostag/copilot_relay/status_board.py tests/test_copilot_status_board.py
git commit -m "feat(copilot): aggregate scoped osstack status boards"
```

## Task 5: Render the C-style Feishu card

**Files:**
- Modify: `ostag/copilot_relay/feishu_cards.py`
- Modify: `tests/test_copilot_relay_feishu_cards.py`

- [ ] **Step 1: Add failing C-card tests**

Add tests that build a personal view and assert:

```python
card = render_osstack_status_card(
    view,
    can_view_team=True,
    callback_factory=lambda action, ref: f"opaque-{action}",
)
encoded = _encoded(card)
assert card["header"]["title"]["content"] == "OStag · osstack 实时进度看板"
assert "我的设备" in encoded
assert "Mac mini" in encoded and "ostag-local" in encoded
assert "codex-cli" in encoded
assert "运行中" in encoded and "等你决策" in encoded and "今日完成" in encoded
assert "北京时间" in encoded and "只读状态查询" in encoded
assert "团队总览" in encoded
assert "session-secret" not in encoded and "run-secret" not in encoded
```

Add these explicit member, empty, offline, and safety assertions:

```python
member_card = render_osstack_status_card(
    member_view, can_view_team=False,
    callback_factory=lambda action, ref: f"opaque-{action}",
)
member_encoded = _encoded(member_card)
assert "团队总览" not in member_encoded
assert "其他员工 Mac" not in member_encoded

empty_encoded = _encoded(render_osstack_status_card(empty_view, can_view_team=False))
assert "尚未绑定 osstack" in empty_encoded

offline_encoded = _encoded(render_osstack_status_card(offline_view, can_view_team=False))
assert "数据可能已过期" in offline_encoded

unsafe_encoded = _encoded(render_osstack_status_card(unsafe_view, can_view_team=False))
assert "<script>" not in unsafe_encoded
assert "&lt;script&gt;" in unsafe_encoded
assert len(unsafe_encoded.encode()) < MAX_CARD_BYTES
assert len(render_osstack_status_card(unsafe_view, can_view_team=False)["elements"]) <= 20
```

- [ ] **Step 2: Run the renderer tests and confirm the missing function failure**

Run: `uv run pytest tests/test_copilot_relay_feishu_cards.py -q`

Expected: FAIL importing `render_osstack_status_card`.

- [ ] **Step 3: Implement the renderer with existing safety helpers**

In `ostag/copilot_relay/feishu_cards.py`, import the new view and add:

```python
def render_osstack_status_card(
    view: OsstackStatusBoardView,
    *,
    can_view_team: bool,
    callback_factory: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    scope = "团队总览" if view.scope == "team" else "我的设备"
    health = "当前空闲" if not view.running_count and not view.blocked_count else "正在运行"
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**{scope}**　在线 **{view.online_count}/{view.device_count}**　{health}"},
        {
            "tag": "column_set", "flex_mode": "none", "background_style": "grey",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "markdown", "content": f"{label}\n**{value}**"}
                ]}
                for label, value in (
                    ("运行中", view.running_count),
                    ("等你决策", view.waiting_decision_count),
                    ("阻塞", view.blocked_count),
                    ("今日完成", view.completed_today_count),
                )
            ],
        },
    ]
    current = view.current_session
    elements.append({"tag": "markdown", "content": "**当前协作会话**"})
    if current is None:
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": (
                "尚未绑定 osstack，请联系管理员绑定设备。"
                if view.device_count == 0 else "当前没有 osstack 会话。"
            )}],
        })
    else:
        state = "等待决策" if current.waiting_decision else current.state
        elements.append({
            "tag": "markdown",
            "content": (
                f"**{_safe(current.title, 240)}**　`{_safe(state, 32)}`\n"
                f"**设备**　{_safe(current.device_name, 80)} · `{_safe(current.instance_id, 128)}`\n"
                f"**Agent**　{_safe(current.agent_id, 64)}\n"
                f"**最近进展**　{_safe(current.latest_summary, 600)}\n"
                f"最近更新 {_beijing_time(current.updated_at)}"
            ),
        })
    elements.append({"tag": "markdown", "content": "**需要你处理**"})
    if view.attention:
        elements.extend({
            "tag": "markdown",
            "content": (
                f"• **{_safe(item.title, 160)}**\n"
                f"{_safe(item.device_name, 80)} · {_safe(item.agent_id, 64)} · "
                f"{_safe('等待决策' if item.waiting_decision else item.state, 32)}"
            ),
        } for item in view.attention[:3])
    else:
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "当前没有等待决策或阻塞事项。"}],
        })
    actions: list[tuple[str, str, str]] = [
        ("status_refresh", "刷新看板", view.scope),
        ("current", "当前会话", "current"),
        ("switch_picker", "切换任务", "current"),
    ]
    if can_view_team:
        actions.append((
            "status_personal" if view.scope == "team" else "status_team",
            "我的设备" if view.scope == "team" else "团队总览",
            "personal" if view.scope == "team" else "team",
        ))
    elements.append(_action(callback_factory, tuple(actions)))
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": (
            f"只读状态查询 · {_beijing_time(view.generated_at)}"
            + (" · 数据可能已过期" if view.stale else "")
        )}],
    })
    return _card("OStag · osstack 实时进度看板", elements)
```

For each displayed session include only title, device display name, public `instance_id`, `agent_id`, localized state, sanitized summary, and Beijing update time. Use action names `status_refresh`, `status_personal`, `status_team`, `current`, and `switch_picker`. The reference for scope-only actions is the literal `personal` or `team`; all callback values remain opaque IDs generated by the controller.

Handle zero bound instances with a single stable note: `尚未绑定 osstack，请联系管理员绑定设备。` Do not render team counts for members.

- [ ] **Step 4: Run card tests**

Run: `uv run pytest tests/test_copilot_relay_feishu_cards.py -q`

Expected: all tests PASS and every encoded card is below `MAX_CARD_BYTES`.

- [ ] **Step 5: Commit the card renderer**

```bash
git add ostag/copilot_relay/feishu_cards.py tests/test_copilot_relay_feishu_cards.py
git commit -m "feat(feishu): render scoped osstack status dashboards"
```

## Task 6: Route queries and callbacks without queueing

**Files:**
- Modify: `ostag/copilot_relay/policy.py`
- Modify: `ostag/copilot_relay/feishu_routes.py`
- Modify: `tests/test_copilot_relay_feishu_routes.py`
- Modify: `tests/test_copilot_relay_service.py`

- [ ] **Step 1: Add failing routing and isolation tests**

Extend the controller fixture with `IdentityStore`, personal/team instance assignments, and `OsstackStatusBoardService`. Assert:

```python
before = outbound_count(store)
response = controller.handle_event(_event("现在 osstack 在干嘛", event_id="status"))
assert response is not None and response.card is not None
assert response.text.startswith("osstack 实时状态")
assert outbound_count(store) == before
assert store.envelope_for_message("relay_status") is None
assert "Mac mini" in json.dumps(response.card, ensure_ascii=False)
```

Add these helpers and concrete assertions in separate tests, reusing the controller fixture:

```python
def outbound_count(store: RelayStore) -> int:
    with store.monitor_store.connect() as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM relay_messages WHERE direction='outbound'"
        ).fetchone()[0])


def next_callback(card: dict, *, label: str) -> str:
    return next(
        action["value"]["callback_id"]
        for element in card["elements"] if element["tag"] == "action"
        for action in element["actions"]
        if action["text"]["content"] == label
    )


mixed = controller.handle_event(
    _event("告诉我现在进展，然后换成方案 B", event_id="mixed")
)
assert mixed.card is not None
assert "请把修改指令单独发送" in mixed.text
assert store.envelope_for_message("relay_mixed") is None

instruction = controller.handle_event(_event("继续开发", event_id="write"))
assert instruction.text.startswith("指令已排队")
assert store.envelope_for_message("relay_write").kind == "instruction.send"

member = member_controller.handle_event(
    _event("现在 osstack 在干嘛", event_id="member", sender="member")
)
member_encoded = json.dumps(member.card, ensure_ascii=False)
assert "employee-mac" in member_encoded
assert "owner-mac" not in member_encoded
assert "团队总览" not in member_encoded

personal = controller.handle_event(_event("现在 osstack 在干嘛", event_id="personal"))
assert "employee-mac" not in json.dumps(personal.card, ensure_ascii=False)

team_callback = next_callback(personal.card, label="团队总览")
team = controller.handle_callback(team_callback, principal=OWNER, destination=PRIVATE)
team_encoded = json.dumps(team.card, ensure_ascii=False)
assert "owner-mac" in team_encoded and "employee-mac" in team_encoded

replayed = member_controller.handle_callback(
    team_callback, principal=MEMBER, destination=MEMBER_PRIVATE
)
assert replayed.text == "没有权限或操作目标不匹配。"

before_refresh = outbound_count(store)
refresh_callback = next_callback(personal.card, label="刷新看板")
refreshed = controller.handle_callback(refresh_callback, principal=OWNER, destination=PRIVATE)
assert refreshed.card is not None
assert outbound_count(store) == before_refresh

disabled = CopilotFeishuController(controller.service, status_board_enabled=False)
old = disabled.handle_event(_event("现在 osstack 在干嘛", event_id="old"))
assert old.text.startswith("指令已排队")
```

- [ ] **Step 2: Run routing tests and confirm failure**

Run: `uv run pytest tests/test_copilot_relay_feishu_routes.py tests/test_copilot_relay_service.py -q`

Expected: FAIL because the controller has no status service or callbacks.

- [ ] **Step 3: Make policy scopes dynamic**

Change `RelayPolicy` to accept:

```python
scope_provider: Callable[[Principal], frozenset[str] | None] | None = None
```

and implement:

```python
def can_access(self, principal: Principal, instance_id: str) -> bool:
    if principal == self.owner:
        return True
    if self.scope_provider is not None:
        scopes = self.scope_provider(principal)
        return scopes is None or instance_id in scopes
    return instance_id in self.instance_scopes.get(principal, ())
```

Retain `instance_scopes` for compatibility tests.

- [ ] **Step 4: Route status intent before write gating**

Add optional controller constructor arguments:

```python
status_service: OsstackStatusBoardService | None = None,
status_board_enabled: bool = False,
```

At the start of private-chat handling, after deriving verified Principal/Destination but before `write_enabled`, canary, or ordinary relay routing:

```python
intent = classify_osstack_status_intent(text)
if self.status_board_enabled and intent in {"status_query", "mixed"}:
    return self._status_board(
        principal,
        destination,
        scope="personal",
        mixed=intent == "mixed",
    )
```

`_status_board` must build the view, create only opaque callbacks, render the C card, and return a text fallback from `format_osstack_status_text`. For mixed intent append `请把修改指令单独发送；本次未下发任何指令。`

Add callback actions `status_refresh`, `status_personal`, `status_team`, `current`, and `switch_picker`. Re-check team authorization inside `status_service.build`; never trust the callback reference as authority. Continue using `_picker` filtering through dynamic `policy.can_access`.

- [ ] **Step 5: Run routing and service tests**

Run: `uv run pytest tests/test_copilot_relay_feishu_routes.py tests/test_copilot_relay_service.py -q`

Expected: all tests PASS; status tests show zero outbound relay messages.

- [ ] **Step 6: Commit query routing**

```bash
git add ostag/copilot_relay/policy.py ostag/copilot_relay/feishu_routes.py \
  tests/test_copilot_relay_feishu_routes.py tests/test_copilot_relay_service.py
git commit -m "feat(copilot): return status boards without queueing instructions"
```

## Task 7: Wire server, adapter, and rollback flag

**Files:**
- Modify: `ostag/config.py`
- Modify: `.env.example`
- Modify: `ostag/work_monitor/server.py`
- Verify: `ostag/adapters/feishu.py` (no production edit expected)
- Modify: `tests/test_work_monitor_server.py`
- Modify: `tests/test_feishu_work_monitor.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Add failing config and integration tests**

Assert `Settings().copilot_status_board_enabled is False`, the environment variable `OSTAG_COPILOT_STATUS_BOARD_ENABLED=true` loads as true, and the production app constructs a controller whose status service uses the same monitor database and identity store.

At adapter level, send a normalized private message `现在 osstack 在干嘛` and assert one interactive card goes to the sender `open_id`, no text “指令已排队” is sent, and no relay envelope is created.

- [ ] **Step 2: Run the integration tests and confirm failure**

Run: `uv run pytest tests/test_config.py tests/test_work_monitor_server.py tests/test_feishu_work_monitor.py -q`

Expected: FAIL because the setting and server wiring do not exist.

- [ ] **Step 3: Add the rollback setting**

Add to `Settings` and `load_settings`:

```python
copilot_status_board_enabled: bool = False
```

```python
copilot_status_board_enabled=_bool("OSTAG_COPILOT_STATUS_BOARD_ENABLED", False),
```

Add to `.env.example`:

```dotenv
# Read-only natural-language osstack status dashboard in Feishu private chats.
OSTAG_COPILOT_STATUS_BOARD_ENABLED=false
```

- [ ] **Step 4: Construct one shared status service**

In `ostag/work_monitor/server.py`, build:

```python
policy = RelayPolicy(owner, scope_provider=runtime.service.identities.allowed_instances)
relay_store = RelayStore(runtime.service.store)
status_service = OsstackStatusBoardService(
    relay_store,
    runtime.service.identities,
    owner=owner,
    offline_seconds=configured.monitor_offline_seconds,
)
runtime.copilot = CopilotFeishuController(
    CopilotRelayService(relay_store, policy),
    status_service=status_service,
    status_board_enabled=configured.copilot_status_board_enabled,
    write_enabled=configured.copilot_relay_write_enabled,
    canary_session_ids=frozenset(configured.copilot_relay_canary_session_ids),
)
```

Do not add a second SQLite connection owner or background task. The adapter's existing `_handle_copilot` structured-card branch sends the new card and, because the status response has `relay_message_id=None`, skips `record_delivery`. Keep that production code unchanged; the adapter integration test is the regression proof.

- [ ] **Step 5: Run config/server/adapter tests**

Run: `uv run pytest tests/test_config.py tests/test_work_monitor_server.py tests/test_feishu_work_monitor.py -q`

Expected: all tests PASS.

- [ ] **Step 6: Commit production wiring**

```bash
git add .env.example ostag/config.py ostag/work_monitor/server.py \
  tests/test_config.py tests/test_work_monitor_server.py tests/test_feishu_work_monitor.py
git commit -m "feat(server): wire read-only osstack status dashboards"
```

## Task 8: Add local-admin member and instance binding CLI

**Files:**
- Modify: `ostag/main.py`
- Modify: `tests/test_cli_run.py`

- [ ] **Step 1: Add failing CLI tests**

Use a temporary `OSTAG_MONITOR_SERVER_DB` and call:

```python
exit_code = await main_async([
    "monitor", "access", "bind-instance",
    "--platform", "feishu",
    "--actor", "ou_employee",
    "--private-chat", "oc_employee",
    "--instance", "employee-mac",
    "--display-name", "员工 Mac mini",
    "--role", "member",
    "--json",
])
assert exit_code == 0
assert json.loads(capsys.readouterr().out) == {
    "bound": True,
    "platform": "feishu",
    "actor_id": "ou_employee",
    "instance_id": "employee-mac",
    "display_name": "员工 Mac mini",
    "role": "member",
}
```

Add `list` and `revoke-instance` tests, a duplicate-active-owner error, and validation that `member` binding requires this local CLI path rather than chat text.

- [ ] **Step 2: Run CLI tests and confirm parser failure**

Run: `uv run pytest tests/test_cli_run.py -q`

Expected: FAIL with argparse invalid choice `access`.

- [ ] **Step 3: Add the nested CLI parser**

Under `monitor_sub` add `access` with `bind-instance`, `revoke-instance`, and `list`. Required bind arguments are `--platform feishu`, `--actor`, `--private-chat`, `--instance`, `--display-name`, `--role member|owner|admin`, `--json`, and `--config`. Revoke requires `--instance`; list accepts optional `--actor`.

Handle `monitor access` before constructing the local reporter:

```python
config = load_settings(getattr(args, "config", None))
if args.monitor_command == "access":
    identities = IdentityStore(config.monitor_server_db)
    if args.access_command == "bind-instance":
        principal = Principal(args.platform, args.actor)
        destination = Destination(args.platform, args.private_chat, "private")
        identities.bind_private(
            owner=principal, destination=destination, local_admin=True, role=args.role
        )
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        identities.bind_instance(
            principal, args.instance, display_name=args.display_name, bound_at=now
        )
        _emit({
            "bound": True, "platform": args.platform, "actor_id": args.actor,
            "instance_id": args.instance, "display_name": args.display_name,
            "role": args.role,
        }, as_json=args.json)
        return 0
```

For `list`, return only non-revoked ownership rows and never emit credentials. For errors, emit a stable `identity_binding_failed` code and return 1.

- [ ] **Step 4: Run CLI tests**

Run: `uv run pytest tests/test_cli_run.py tests/test_work_monitor_identity.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit the onboarding CLI**

```bash
git add ostag/main.py tests/test_cli_run.py
git commit -m "feat(cli): manage Feishu osstack instance ownership"
```

## Task 9: Document operations and run local release gates

**Files:**
- Modify: `docs/ostag/deployment.md`
- Generated evidence: `release/secret-hygiene.json`, `release/secret-hygiene.md` (restore timestamp-only changes to their pre-check values; never commit a time-only diff)

- [ ] **Step 1: Document exact owner and employee onboarding**

Add commands to `docs/ostag/deployment.md`:

```bash
uv run ostag monitor access bind-instance \
  --platform feishu \
  --actor '<verified-open-id>' \
  --private-chat '<verified-private-chat-id>' \
  --instance 'ostag-local' \
  --display-name 'Mac mini' \
  --role admin \
  --json
```

Document the same command with `--role member` for employees, `list`, `revoke-instance`, `OSTAG_COPILOT_STATUS_BOARD_ENABLED`, the personal/team distinction, no-queue guarantee, SQLite backup, and rollback to the previous immutable image.

- [ ] **Step 2: Run focused tests**

Run:

```bash
uv run pytest -q \
  tests/test_copilot_status_intent.py \
  tests/test_copilot_status_board.py \
  tests/test_work_monitor_identity.py \
  tests/test_copilot_relay_feishu_cards.py \
  tests/test_copilot_relay_feishu_routes.py \
  tests/test_feishu_work_monitor.py \
  tests/test_work_monitor_server.py \
  tests/test_cli_run.py
```

Expected: all focused tests PASS.

- [ ] **Step 3: Run the complete test and lint suites**

Run:

```bash
uv run pytest -q
uv run ruff check .
```

Expected: all tests PASS and Ruff prints `All checks passed!`.

- [ ] **Step 4: Run the repository release check**

Run:

```bash
OSSTACK_COPILOT_REPO=/Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session \
  scripts/release_check.sh
```

Expected: release check PASS, secret match count 0, and no credentials in artifacts. Restore timestamp-only secret-hygiene changes with `apply_patch` before committing if the command only refreshed generation time.

- [ ] **Step 5: Commit documentation**

```bash
git add docs/ostag/deployment.md
git commit -m "docs: operate scoped osstack status dashboards"
```

## Task 10: osstack final loop, production deployment, and live acceptance

**Files:**
- Production release: `/opt/ostag-releases/<commit>/`
- Protected environment: `/opt/ostag-work-monitor/deploy/work-monitor/.env`
- Persistent database: named volume mounted at `/var/lib/ostag`

- [ ] **Step 1: Run the osstack Loop Engineering release gate**

Run the queued task with isolated state paths so generated status files do not dirty the branch:

```bash
/Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/osstack-loop.sh run \
  --workspace "$PWD" \
  --source task_queue \
  --execute 'true' \
  --verify 'uv run pytest -q' \
  --verify 'uv run ruff check .' \
  --verify 'OSSTACK_COPILOT_REPO=/Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session scripts/release_check.sh' \
  --archive-completed \
  --cycles 1 \
  --state /tmp/ostag-status-board-loop-state.json \
  --markdown /tmp/ostag-status-board-loop-state.md \
  --json
```

Expected: outcome `success`, verifier score 1.00, and the task queue file archived.

- [ ] **Step 2: Back up production state**

Create a timestamped SQLite backup through the running container and verify `PRAGMA integrity_check=ok`. Back up the protected `.env` with root-only permissions. Record the prior image tag and retain it for rollback. Do not print credentials.

- [ ] **Step 3: Build an immutable release image**

Create `/opt/ostag-releases/<commit>`, verify the uploaded archive SHA-256, point its `.env` symlink to the protected environment, set `OSTAG_WORK_MONITOR_IMAGE=ostag-work-monitor:status-<commit>`, and build with the base Compose file only.

Expected: image tag exactly matches the source commit and contains one Uvicorn worker.

- [ ] **Step 4: Bind the existing owner and device before enabling the flag**

Run the new CLI inside the candidate container using the verified production owner `open_id`, verified private chat ID, `instance_id=ostag-local`, display name `Mac mini`, and `role=admin`. Query `monitor_instance_owners` without displaying unrelated identity data and confirm one active row.

- [ ] **Step 5: Enable and deploy**

Set `OSTAG_COPILOT_STATUS_BOARD_ENABLED=true`, recreate only the `work-monitor` service with `docker compose up -d --no-build work-monitor`, and wait for health `healthy`.

Verify:

```text
image = ostag-work-monitor:status-<commit>
mount = work-monitor_work-monitor-data -> /var/lib/ostag
origin health = {"status":"ok"}
external health = {"status":"ok"}
SQLite integrity = ok
```

- [ ] **Step 6: Perform owner live acceptance**

From the owner's real Feishu private chat send `现在 osstack 在干嘛` and verify:

- one `OStag · osstack 实时进度看板` card arrives;
- device is `Mac mini / ostag-local`;
- Agent matches the selected session reporter;
- timestamps say `北京时间`;
- relay outbound count and current session version do not change;
- no “指令已排队” reply appears;
- refresh, current session, switch task, and team overview work.

- [ ] **Step 7: Perform member isolation acceptance**

Bind one controlled member account to a dedicated test instance. In that member's Feishu private chat verify the C card contains only the member instance, has no team-overview button, and cannot replay the owner's team callback. Revoke the binding after acceptance if the test account should not remain active.

- [ ] **Step 8: Verify the write path was not regressed**

Send an explicit low-risk canary instruction to the exact current session and verify it still transitions through queued/delivered/accepted/applied. Do not use a deployment, deletion, credential, purchase, or external-message instruction for this canary.

- [ ] **Step 9: Run post-deploy osstack verification**

Run one final Loop Engineering task with verifiers for the full test suite, Ruff, external health, exact deployed image, and healthy container. Expected: all verifier commands pass and success rate 100%.

- [ ] **Step 10: Fast-forward and push the production branch**

After all acceptance evidence passes:

```bash
git status --short
git push origin codex/copilot-relay
```

Expected: clean worktree and remote `codex/copilot-relay` at the deployed commit.
