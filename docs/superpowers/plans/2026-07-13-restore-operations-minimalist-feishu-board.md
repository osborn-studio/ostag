# Restore Operations Minimalist Feishu Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the user-approved Operations Minimalist Feishu monitor experience and make the Singapore central service the single authority for Feishu messages, monitor data, and permissions.

**Architecture:** The central FastAPI process on the Singapore Lighthouse owns the Feishu WebSocket, `WorkMonitorService`, SQLite projection, identity bindings, scheduler, and card callbacks. Platform-neutral view objects expose six-stage counts, operational KPIs, prioritized attention items, and running tasks; the Feishu renderer maps those views to native vertical card elements without trusting card payloads for authorization.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, Feishu/Lark WebSocket and interactive cards, pytest, Docker Compose, Tencent Lighthouse, EdgeOne.

---

### Task 1: Restore the Operations Minimalist board view contract

**Files:**
- Modify: `ostag/work_monitor/rendering.py`
- Modify: `ostag/work_monitor/service.py`
- Test: `tests/test_work_monitor_rendering.py`
- Test: `tests/test_work_monitor_service.py`

- [ ] **Step 1: Write failing view tests**

Add tests proving `MobileBoardView.from_tasks()` exposes stable six-stage counts, `running`, `blocked`, `attention`, and `done` KPIs while preserving critical-first pagination:

```python
view = MobileBoardView.from_tasks(tasks, page=1, page_size=6)
assert view.stage_counts == {
    "triage": 1, "todo": 1, "ready": 1,
    "running": 2, "blocked": 1, "done": 3,
}
assert view.kpis == {"running": 2, "blocked": 1, "attention": 1, "done": 3}
assert view.attention[0]["status"] == "blocked"
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_work_monitor_rendering.py \
  tests/test_work_monitor_service.py
```

Expected: failure because `stage_counts`, `kpis`, and `attention` do not exist.

- [ ] **Step 3: Implement the view contract**

Extend `MobileBoardView` with immutable summary fields and compute them from the full unpaginated task set. Keep `cards` paginated and ordered by the existing critical-first status priority. Add the new keys to `to_dict()` without removing existing `type`, `cards`, or `pagination` keys.

- [ ] **Step 4: Verify and commit**

Run the focused tests and commit:

```bash
git add ostag/work_monitor/rendering.py ostag/work_monitor/service.py \
  tests/test_work_monitor_rendering.py tests/test_work_monitor_service.py
git commit -m "feat(work-monitor): restore operational board summaries"
```

### Task 2: Rebuild the native Feishu Operations Minimalist renderer

**Files:**
- Modify: `ostag/work_monitor/feishu_cards.py`
- Modify: `ostag/adapters/feishu.py`
- Test: `tests/test_feishu_work_monitor.py`
- Test: `tests/test_work_monitor_smoke.py`

- [ ] **Step 1: Write failing structure and safety tests**

Add tests requiring this element order and semantics:

```python
card = render_feishu_card(view)
assert card["header"]["template"] == "blue"
assert card["header"]["title"]["content"] == "OStag Control · 今日看板"
assert any(item["tag"] == "column_set" for item in card["elements"])
assert "需要你处理" in json.dumps(card, ensure_ascii=False)
assert "运行中的任务" in json.dumps(card, ensure_ascii=False)
assert "六阶段概览" in json.dumps(card, ensure_ascii=False)
assert len(json.dumps(card, ensure_ascii=False).encode()) <= MAX_CARD_BYTES
```

Also prove blocked/failed content uses red semantics, done uses green semantics, long Chinese titles wrap/truncate safely, untrusted Markdown is escaped, and controls appear only when `callback_factory` supplies an opaque callback ID.

- [ ] **Step 2: Run renderer tests and confirm RED**

Run:

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_feishu_work_monitor.py \
  tests/test_work_monitor_smoke.py
```

Expected: failure because the current renderer is only Markdown rows separated by rules.

- [ ] **Step 3: Implement native vertical modules**

Map the approved Stitch design to supported Feishu JSON elements:

```python
elements = [
    _health_summary(view),
    _kpi_columns(view),
    *_attention_modules(view),
    _running_heading(view),
    *_running_modules(view),
    _stage_overview(view),
]
```

Use Feishu blue only for primary actions/running, red only for blocked/danger, and green only for healthy/done. Use native `column_set`, `div`, `markdown`, `hr`, `note`, and `action` elements; keep nesting under five levels and total JSON below 24 KB.

- [ ] **Step 4: Verify and commit**

Run focused tests, then:

```bash
git add ostag/work_monitor/feishu_cards.py ostag/adapters/feishu.py \
  tests/test_feishu_work_monitor.py tests/test_work_monitor_smoke.py
git commit -m "feat(feishu): restore Operations Minimalist cards"
```

### Task 3: Make the Singapore central service the only Feishu monitor ingress

**Files:**
- Modify: `tests/test_work_monitor_server.py`
- Modify: `release/ai.ostag.feishu.plist`
- Modify: `docs/superpowers/specs/2026-07-12-ostag-work-monitor-bots-design.md`
- Modify: production `/opt/ostag-work-monitor/deploy/work-monitor/.env`

- [ ] **Step 1: Add ownership regression tests**

Prove production server lifespan starts one Feishu WebSocket owner bound to the same `WorkMonitorService` used by `/v1/board`, while the standalone local adapter is documented as development-only. Verify a signed private Feishu event reads the central store and central binding.

- [ ] **Step 2: Run server tests and confirm the ownership gap**

Run:

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q tests/test_work_monitor_server.py
```

- [ ] **Step 3: Prepare reversible deployment**

Create a SQLite online backup and copy the current server `.env` and Compose config to timestamped rollback files. Deploy the committed source to `/opt/ostag-work-monitor`, rebuild the image, and run the full production doctor before switching ingress.

- [ ] **Step 4: Switch ingress without overlap**

Set `FEISHU_USE_WEBSOCKET=true` in the Singapore container, restart the single Compose worker, verify the WebSocket connects, then unload `ai.ostag.feishu` locally. Never run both owners after the cutover verification window.

- [ ] **Step 5: Verify and commit deployment documentation**

Record the central owner, rollback paths, health output, and local launchd shutdown. Commit only repository documentation/config changes; never commit production secrets.

### Task 4: Correct and verify the owner permission model

**Files:**
- Modify: `tests/test_work_monitor_identity.py`
- Modify: `tests/test_feishu_work_monitor.py`
- Modify: production SQLite binding through `IdentityStore`

- [ ] **Step 1: Add permission matrix tests**

Prove:

```python
assert identities.allows(owner, private, "read")
assert identities.allows(owner, private, "propose")
assert identities.allows(owner, private, "write")
assert not identities.allows(group_reader, group, "write")
```

Also prove cancel controls are absent outside an owner/admin private destination and callback identity comes only from verified Feishu operator data.

- [ ] **Step 2: Verify production binding through application APIs**

Use `IdentityStore.bind_private(..., local_admin=True)` only against the backed-up central database. Confirm `/v1/board` returns 200 for the Feishu human credential and 403 for an unbound credential. Do not duplicate the binding into the reporter spool database.

- [ ] **Step 3: Verify buttons and governed controls**

Request a real task detail card as the owner, confirm only task-supported actions appear, and exercise one non-destructive governed control through callback → receipt → command poll → acknowledgement. Keep cancel behind private five-minute confirmation.

### Task 5: Production validation and release gate

**Files:**
- Modify: `release/work-monitor-live-validation-template.md`
- Modify: `release/secret-hygiene.json`
- Modify: `release/secret-hygiene.md`

- [ ] **Step 1: Run automated gates**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q
./scripts/secret_hygiene_check.sh
git diff --check
```

Expected: all tests pass, secret match count is zero, and no whitespace errors.

- [ ] **Step 2: Run live production checks**

Verify central `/healthz`, authenticated `/v1/board`, one central scheduler/WebSocket owner, reporter pending queue zero, and Telegram webhook pending count zero.

- [ ] **Step 3: Send and inspect real cards**

Send one board request and one task-detail request from the real Feishu private chat. Confirm the returned messages are `interactive`, match the approved information hierarchy on mobile, and do not produce permission fallback text.

- [ ] **Step 4: Validate rollback artifacts**

Confirm the database backup opens successfully and the previous image/config paths exist. Record exact timestamps and message IDs without secrets.

- [ ] **Step 5: Final commit**

```bash
git add release/work-monitor-live-validation-template.md \
  release/secret-hygiene.json release/secret-hygiene.md
git commit -m "chore(release): validate restored Feishu monitor"
```
