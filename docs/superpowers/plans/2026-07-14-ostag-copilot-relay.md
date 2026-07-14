# OStag Copilot Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner use OStag in Feishu as a durable remote Copilot console for osstack-managed Codex CLI and Claude Code sessions: see what each session is doing, receive decisions and latest replies, answer them from Feishu, redirect work, and create a new governed task without exposing shell access or repository contents.

**Architecture:** Add a resumable-session contract to osstack first, backed by its SQLite event store and agent-native session IDs. Add an OStag Copilot Relay beside the existing Work Monitor: a local relay agent exchanges signed, idempotent envelopes with the Singapore central service; the central service owns session registry, current-task binding, decisions, policy and Feishu cards. Communication is store-and-forward polling, not a live terminal tunnel. The system fails closed on stale versions, unknown sessions, unsupported adapters, replayed callbacks and high-risk instructions.

**Tech Stack:** TypeScript, Node.js, Zod, SQLite, osstack daemon RPC, Codex CLI JSONL, Anthropic Claude Agent SDK, Python 3.11, dataclasses, FastAPI, Feishu interactive cards, HMAC-SHA256, Docker Compose, launchd, Vitest, pytest.

**Approved design:** `docs/superpowers/specs/2026-07-14-ostag-copilot-relay-design.md`

**Repositories and branches:**

- OStag implementation worktree: `/Users/gilbertchen/Documents/CODE/agentsteam/ostag-copilot-relay`, branch `codex/copilot-relay`, based on `codex/daily-task-boards` commit `7f662cd`.
- osstack implementation must use a new clean worktree at `/Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session`, branch `codex/copilot-session`, based on fetched `origin/main` commit `1595f2e` (or its reviewed fast-forward successor). Do not modify or clean `/Users/gilbertchen/Documents/CODE/agentsteam/osstack`; its local `main` is behind the remote and contains unrelated user changes.
- osstack management card: `card_227f12f3`. Its legal progression is `triage -> ready -> running -> blocked|done`. Card comments record every task commit and verification gate.

**Non-goals for v1:** arbitrary terminal control, Codex Desktop control, repository browsing from Feishu, source/diff/log/secret upload, ungoverned shell commands, group-chat task selection, or automatic execution of high-risk actions.

---

## Delivery contract and invariants

The implementation must preserve these invariants in every task:

1. The complete conversation, source tree, command output and credentials remain on the worker host. Only a bounded user-facing assistant reply, decision prompt, task summary and opaque evidence reference may leave it.
2. A reply is applied only to the exact `(instance_id, task_id, run_id, session_id, turn_id, session_version)` tuple that produced the prompt.
3. Every cross-host message has a stable `message_id`; enqueue is at-least-once and application is exactly-once through a unique constraint.
4. New-task proposals require confirmation. High-risk requests never become adapter prompts until the existing approval policy records approval.
5. Safe independent work may continue while a decision is pending; a direction-changing branch moves to `awaiting_decision` and does not guess.
6. An adapter that cannot prove native resume support reports `resume_supported=false`; OStag renders it read-only instead of starting a fresh AI session.
7. Production is not complete until a real Feishu private-chat round trip resumes the same native agent session after process and network interruptions.

---

### Task 0: Establish clean worktrees, baselines and osstack tracking

**Files:**
- Modify: `docs/superpowers/plans/2026-07-14-ostag-copilot-relay.md`
- Reference: `/Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/SKILL.md`

- [ ] **Step 1: Move the management card to ready and attach the plan**

Run from the OStag worktree:

```bash
osstack kanban comment card_227f12f3 \
  "Approved plan: docs/superpowers/plans/2026-07-14-ostag-copilot-relay.md; design: docs/superpowers/specs/2026-07-14-ostag-copilot-relay-design.md" codex
osstack kanban move card_227f12f3 ready
```

Expected: the card appears once under `READY`, with no implementation task marked complete.

- [ ] **Step 2: Create the isolated osstack worktree without touching the dirty source worktree**

```bash
git -C /Users/gilbertchen/Documents/CODE/agentsteam/osstack status --short
git -C /Users/gilbertchen/Documents/CODE/agentsteam/osstack fetch origin --prune
git -C /Users/gilbertchen/Documents/CODE/agentsteam/osstack worktree add \
  -b codex/copilot-session \
  /Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session origin/main
git -C /Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session status --short --branch
```

Expected: the original status is recorded but unchanged; the new worktree is clean on `codex/copilot-session`.

- [ ] **Step 3: Record both baseline test results**

```bash
cd /Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session
pnpm test
pnpm typecheck

cd /Users/gilbertchen/Documents/CODE/agentsteam/ostag-copilot-relay
PYTHON_DOTENV_DISABLED=1 uv run pytest -q
uv run ruff check ostag tests
```

Expected: all committed baseline checks pass. A pre-existing failure is copied verbatim into a card comment and fixed or explicitly isolated before feature work.

- [ ] **Step 4: Start execution tracking only when implementation begins**

```bash
osstack kanban move card_227f12f3 running
osstack kanban comment card_227f12f3 \
  "Task 0 complete: clean OStag and osstack worktrees; baseline checks recorded." codex
```

---

### Task 1: Define the osstack resumable-session domain contract

**Files (osstack worktree):**
- Modify: `packages/agents/src/base.ts`
- Create: `packages/agents/src/session.ts`
- Modify: `packages/agents/src/index.ts`
- Create: `packages/agents/src/session.test.ts`

- [ ] **Step 1: Write failing schema and adapter-contract tests**

Test exact parsing, rejected unknown fields, monotonic version and adapter capability:

```ts
const descriptor = AgentSessionDescriptorSchema.parse({
  session_id: "sess_01",
  task_id: "task_01",
  run_id: "run_01",
  agent_id: "codex-cli",
  native_session_id: "019f-thread",
  state: "idle",
  resume_supported: true,
  version: 1,
  updated_at: 1784000000,
});
expect(descriptor.version).toBe(1);
expect(() => AgentSessionDescriptorSchema.parse({...descriptor, version: 0})).toThrow();
```

Define a conformance fake and assert `openSession`, `resumeSession`, and `snapshotSession` preserve `session_id`, while an ordinary `AgentAdapter` remains valid with no session controller.

- [ ] **Step 2: Run the focused test and confirm RED**

```bash
pnpm --filter @osstack/agents test -- session.test.ts
```

Expected: imports for `AgentSessionDescriptorSchema` and `SessionCapableAdapter` fail.

- [ ] **Step 3: Implement strict exported types**

Create Zod-backed values with no passthrough keys:

```ts
export const AgentSessionStateSchema = z.enum([
  "starting", "running", "awaiting_decision", "idle", "completed", "failed", "cancelled",
]);

export const AgentSessionDescriptorSchema = z.object({
  session_id: z.string().min(1),
  task_id: z.string().min(1),
  run_id: z.string().min(1),
  agent_id: z.string().min(1),
  native_session_id: z.string().min(1),
  state: AgentSessionStateSchema,
  resume_supported: z.boolean(),
  version: z.number().int().positive(),
  updated_at: z.number().nonnegative(),
}).strict();

export interface SessionCapableAdapter extends AgentAdapter {
  sessionCapabilities(): { resume: boolean; snapshot: boolean };
  resumeSession(input: AgentSessionInput, signal: AbortSignal): AsyncIterable<AgentSessionEvent>;
}
```

`AgentSessionInput` includes `expected_version`, `message_id`, `prompt`, and the full osstack tuple. `AgentSessionEvent` includes `native_session.started`, `assistant.message`, `decision.requested`, `session.state`, and `session.terminal`; it never includes tool arguments or command output.

- [ ] **Step 4: Verify and commit**

```bash
pnpm --filter @osstack/agents test -- session.test.ts adapter-conformance.test.ts
pnpm typecheck
git add packages/agents/src/base.ts packages/agents/src/session.ts \
  packages/agents/src/index.ts packages/agents/src/session.test.ts
git commit -m "feat(agents): define resumable session contract"
osstack kanban comment card_227f12f3 \
  "Task 1 complete: $(git rev-parse --short HEAD)" codex
```

---

### Task 2: Persist native sessions, turns and idempotent inbound messages in osstack

**Files (osstack worktree):**
- Modify: `packages/core/src/state/migrations.ts`
- Modify: `packages/core/src/state/schema.ts`
- Modify: `packages/core/src/state/index.ts`
- Create: `packages/core/src/state/sessions.ts`
- Create: `packages/core/src/state/sessions.test.ts`
- Modify: `packages/core/src/state/db.test.ts`

- [ ] **Step 1: Write failing migration and repository tests**

Assert migration creates these tables and indexes:

```sql
agent_sessions(session_id PRIMARY KEY, task_id, run_id, agent_id,
  native_session_id, state, resume_supported, version, updated_at)
session_turns(turn_id PRIMARY KEY, session_id, ordinal, state,
  assistant_text, decision_json, created_at, updated_at)
session_messages(message_id PRIMARY KEY, session_id, turn_id,
  direction, kind, payload_json, state, created_at, applied_at)
```

Tests must prove:

```ts
expect(repo.applyInbound(message, 3).outcome).toBe("applied");
expect(repo.applyInbound(message, 3).outcome).toBe("duplicate");
expect(() => repo.applyInbound({...message, message_id: "m2"}, 2)).toThrow(SessionVersionConflict);
```

Also reopen the database and prove data and `PRAGMA user_version` survive.

- [ ] **Step 2: Run tests and confirm RED**

```bash
pnpm --filter @osstack/core test -- state/sessions.test.ts state/db.test.ts
```

Expected: the session repository and next migration do not exist.

- [ ] **Step 3: Implement migration and transactional repository**

Add migration `id: 6`, foreign-key and lookup indexes, plus `SessionRepository` methods:

```ts
createSession(row: AgentSessionDescriptor): void;
recordNativeSession(sessionId: string, nativeId: string, expectedVersion: number): AgentSessionDescriptor;
appendTurn(turn: SessionTurnRow): void;
applyInbound(message: SessionMessageRow, expectedVersion: number): ApplyMessageResult;
setState(sessionId: string, state: AgentSessionState, expectedVersion: number): AgentSessionDescriptor;
latestSnapshot(sessionId: string): SessionSnapshot;
listSessions(filter: SessionFilter): readonly AgentSessionDescriptor[];
```

Every state mutation uses `UPDATE ... WHERE session_id=? AND version=?`, increments exactly once, and distinguishes duplicate message IDs from stale versions.

- [ ] **Step 4: Verify and commit**

```bash
pnpm --filter @osstack/core test -- state/sessions.test.ts state/db.test.ts
pnpm typecheck
git add packages/core/src/state
git commit -m "feat(core): persist resumable agent sessions"
osstack kanban comment card_227f12f3 \
  "Task 2 complete: $(git rev-parse --short HEAD)" codex
```

---

### Task 3: Make Codex CLI and Claude Code resume the same native session

**Files (osstack worktree):**
- Modify: `packages/agents/src/adapters/codex-cli.ts`
- Modify: `packages/agents/src/adapters/codex-cli.test.ts`
- Modify: `packages/agents/src/adapters/claude-code.ts`
- Modify: `packages/agents/src/adapters/claude-code.test.ts`
- Modify: `packages/agents/src/adapter-conformance.test.ts`

- [ ] **Step 1: Write failing adapter tests around spawned arguments and SDK options**

For Codex, feed `thread.started` and assert the native ID is emitted structurally, then resume:

```ts
expect(events).toContainEqual(expect.objectContaining({
  type: "native_session.started",
  native_session_id: "019f-codex-thread",
}));
expect(spawnArgs).toEqual([
  "exec", "resume", "--json", "--skip-git-repo-check",
  "-s", "workspace-write", "-C", repo, "019f-codex-thread", "继续并回答这个决策",
]);
```

Before locking the argv order, run the installed binary from its resolved `OSSTACK_CODEX_BIN` with `exec resume --help`. The currently selected global wrapper returns `ENOENT`; the test fixture must use a functioning explicit binary and production preflight must reject the broken wrapper.

For Claude, capture the first SDK result's `session_id`, then assert the next call is:

```ts
query({prompt: "采用方案 B", options: expect.objectContaining({
  resume: "claude-session-uuid",
  cwd: repo,
})});
```

The installed `@anthropic-ai/claude-agent-sdk@0.3.143` declares `options.resume?: string`; no fallback conversation reconstruction is allowed.

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
pnpm --filter @osstack/agents test -- \
  adapters/codex-cli.test.ts adapters/claude-code.test.ts adapter-conformance.test.ts
```

Expected: adapters do not implement session capabilities and native IDs are not structural events.

- [ ] **Step 3: Implement native resume and bounded snapshots**

Refactor both adapters through one internal session runner. Capture native IDs without placing them in human-readable progress. For outbound snapshots, accumulate only completed `assistant.message` text, normalize control characters, cap at 4,000 Unicode code points, and exclude reasoning, tool calls, arguments, command output and stderr.

Codex uses the verified local CLI syntax. Claude sets `options.resume`. A missing native ID, nonzero resume, or SDK session mismatch emits a typed terminal error and leaves the session retriable; it never opens a new implicit session.

- [ ] **Step 4: Add availability preflight**

`isAvailable()` must distinguish `installed_but_broken`, `not_authenticated`, and `available`. Add a no-model-call binary probe for Codex resume help; keep model health checks outside routine polling.

- [ ] **Step 5: Verify and commit**

```bash
pnpm --filter @osstack/agents test -- \
  adapters/codex-cli.test.ts adapters/claude-code.test.ts adapter-conformance.test.ts
pnpm typecheck
git add packages/agents/src/adapters packages/agents/src/adapter-conformance.test.ts
git commit -m "feat(agents): resume Codex and Claude sessions"
osstack kanban comment card_227f12f3 \
  "Task 3 complete: $(git rev-parse --short HEAD)" codex
```

---

### Task 4: Add osstack daemon RPC and CLI for governed session control

**Files (osstack worktree):**
- Modify: `packages/cli/src/daemon.ts`
- Modify: `packages/cli/src/store.ts`
- Modify: `packages/cli/src/bin.ts`
- Modify: `packages/cli/src/daemon.test.ts`
- Create: `packages/cli/src/session-rpc.test.ts`

- [ ] **Step 1: Write failing RPC tests**

Cover `session.list`, `session.status`, `session.send`, and `session.snapshot`. Example:

```ts
const first = await rpc("session.send", {
  session_id: "sess_01", turn_id: "turn_07", message_id: "msg_feishu_01",
  expected_version: 4, text: "采用方案 B，继续测试", requested_by: "feishu:ou_owner",
});
expect(first.result).toMatchObject({state: "accepted", session_version: 5});
expect((await rpc("session.send", first.request)).result.state).toBe("duplicate");
```

Assert stale version, unknown session, unsupported resume, terminal session, oversized text, and concurrent sends return stable error codes and do not invoke an adapter.

- [ ] **Step 2: Run tests and confirm RED**

```bash
pnpm --filter @osstack/cli test -- session-rpc.test.ts daemon.test.ts
```

Expected: unknown RPC methods.

- [ ] **Step 3: Implement daemon handlers and CLI JSON output**

Add single-response list/status/snapshot handlers and a streaming send handler. `session.send` transactionally reserves the message, invokes `resumeSession`, persists projected events, and marks the message `applied` only after the native adapter accepts the turn. Add:

```bash
osstack session list --json
osstack session status sess_01 --json
osstack session snapshot sess_01 --json
osstack session send sess_01 --turn turn_07 \
  --message-id msg_feishu_01 --expected-version 4 \
  --text "采用方案 B，继续测试" --json
```

The CLI prints only schema-approved fields. It never prints native session IDs unless `--local-debug` is explicitly set on the local host.

- [ ] **Step 4: Verify and commit**

```bash
pnpm --filter @osstack/cli test -- session-rpc.test.ts daemon.test.ts
pnpm test
pnpm typecheck
git add packages/cli/src
git commit -m "feat(cli): expose governed session relay RPC"
osstack kanban comment card_227f12f3 \
  "Task 4 complete: $(git rev-parse --short HEAD)" codex
```

---

### Task 5: Define strict OStag Copilot Relay envelopes and redaction

**Files (OStag worktree):**
- Create: `ostag/copilot_relay/__init__.py`
- Create: `ostag/copilot_relay/models.py`
- Create: `ostag/copilot_relay/redaction.py`
- Create: `tests/test_copilot_relay_models.py`
- Create: `tests/test_copilot_relay_redaction.py`

- [ ] **Step 1: Write failing contract tests**

Create fixtures for `session.upsert`, `assistant.snapshot`, `decision.request`, `decision.answer`, `instruction.send`, `task.create.proposal`, `task.create.confirm`, and `delivery.receipt`. Assert exact key sets, UTC timestamps, version positivity, tuple consistency, payload limits and rejection of booleans as integers.

```python
envelope = RelayEnvelope.from_dict(payload)
assert envelope.binding == SessionBinding(
    instance_id="macmini", task_id="task_1", run_id="run_1",
    session_id="sess_1", turn_id="turn_3", session_version=7,
)
with pytest.raises(ValueError, match="unknown keys"):
    RelayEnvelope.from_dict(payload | {"shell": "rm -rf /"})
```

Redaction tests include API keys, bot tokens, private keys, cookies, absolute local paths, fenced diffs, command transcripts and Unicode obfuscation.

- [ ] **Step 2: Run tests and confirm RED**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_models.py tests/test_copilot_relay_redaction.py
```

Expected: `ostag.copilot_relay` does not exist.

- [ ] **Step 3: Implement immutable contracts and fail-closed sanitizer**

Use frozen dataclasses with explicit `_KEYS`, following `ostag/work_monitor/models.py`. The sanitizer returns:

```python
@dataclass(frozen=True)
class SanitizedText:
    text: str
    redaction_count: int
    blocked_categories: tuple[str, ...]
    truncated: bool
```

Block a payload instead of sending it when it contains a private key, raw token, diff hunk, shell transcript, or local secret path. Ordinary assistant text is capped at 4,000 code points; decision choices at 8 items and 160 code points each.

- [ ] **Step 4: Verify and commit**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_models.py tests/test_copilot_relay_redaction.py
uv run ruff check ostag/copilot_relay tests/test_copilot_relay_*.py
git add ostag/copilot_relay tests/test_copilot_relay_models.py \
  tests/test_copilot_relay_redaction.py
git commit -m "feat(relay): define strict copilot envelopes"
osstack kanban comment card_227f12f3 \
  "Task 5 complete: $(git rev-parse --short HEAD)" codex
```

---

### Task 6: Build the central session registry, mailbox and decision store

**Files (OStag worktree):**
- Create: `ostag/copilot_relay/store.py`
- Create: `tests/test_copilot_relay_store.py`
- Modify: `ostag/work_monitor/store.py`
- Modify: `tests/test_work_monitor_projection.py`

- [ ] **Step 1: Write failing persistence and concurrency tests**

Assert migrations are restart-safe and create:

```sql
relay_sessions(instance_id, session_id, task_id, run_id, agent_id, title,
  state, session_version, last_turn_id, latest_summary, updated_at,
  PRIMARY KEY(instance_id, session_id))
relay_messages(message_id PRIMARY KEY, direction, kind, binding_json,
  payload_json, state, attempts, next_attempt_at, lease_owner, lease_until,
  created_at, delivered_at, applied_at, last_error)
relay_decisions(decision_id PRIMARY KEY, instance_id, session_id, turn_id,
  session_version, question, choices_json, state, expires_at, answered_at)
relay_current_tasks(platform, actor_id, instance_id, session_id, selected_at,
  PRIMARY KEY(platform, actor_id))
relay_task_proposals(proposal_id PRIMARY KEY, platform, actor_id, payload_json,
  state, expires_at, confirmed_at)
```

Prove duplicate ingest, two drainers, expired lease reclaim, stale session upsert, quote-to-turn lookup, current-task switch and restart recovery.

- [ ] **Step 2: Run tests and confirm RED**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_store.py tests/test_work_monitor_projection.py
```

- [ ] **Step 3: Implement one database owner and transactional operations**

Add the relay migration to `MonitorStore` so the central FastAPI process has one SQLite schema owner. `RelayStore` accepts an existing `MonitorStore` and exposes typed methods for registry, decisions, proposals, current-task binding and leased mailbox delivery. Use `INSERT OR IGNORE` only for stable IDs; use compare-and-swap on versions everywhere else.

- [ ] **Step 4: Verify and commit**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_store.py tests/test_work_monitor_projection.py
git add ostag/copilot_relay/store.py ostag/work_monitor/store.py \
  tests/test_copilot_relay_store.py tests/test_work_monitor_projection.py
git commit -m "feat(relay): persist sessions decisions and mailbox"
osstack kanban comment card_227f12f3 \
  "Task 6 complete: $(git rev-parse --short HEAD)" codex
```

---

### Task 7: Implement governed routing, current-task semantics and task creation

**Files (OStag worktree):**
- Create: `ostag/copilot_relay/service.py`
- Create: `ostag/copilot_relay/policy.py`
- Create: `tests/test_copilot_relay_service.py`
- Modify: `ostag/work_monitor/identity.py`
- Modify: `tests/test_work_monitor_identity.py`

- [ ] **Step 1: Write failing service tests**

Cover owner-only access, ordinary message to current task, quoted reply to original turn, explicit task switch, ambiguous/no-current task, stale decision, duplicate Feishu event, new-task proposal/confirm/expiry and high-risk instruction classification.

```python
result = service.handle_text(owner, "采用 B，继续", quote_message_id="om_decision_7")
assert result.outbound.kind == "decision.answer"
assert result.outbound.binding.turn_id == "turn_7"

proposal = service.handle_text(owner, "新任务：修复登录回归")
assert proposal.kind == "task.create.proposal"
assert store.pending_messages() == ()
service.confirm_proposal(owner, proposal.proposal_id)
assert store.pending_messages()[0].kind == "task.create.confirm"
```

Role tests prove the configured owner can see every registered employee instance; non-owner grants remain scoped and cannot switch into another instance.

- [ ] **Step 2: Run tests and confirm RED**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_service.py tests/test_work_monitor_identity.py
```

- [ ] **Step 3: Implement deterministic routing and policy**

Expose `CopilotRelayService.handle_text`, `select_session`, `answer_decision`, `confirm_task_proposal`, and `request_snapshot`. Classification order is: authenticated callback -> quoted relay message -> explicit relay command -> current session text -> new-task proposal. Never use a language model to choose the target session.

`RelayPolicy` returns `allow`, `requires_confirmation`, or `requires_approval`. Destructive file, deployment, credential, billing, external-message and permission changes must enter the existing governed approval path and never be inserted directly into the local mailbox.

- [ ] **Step 4: Verify and commit**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_service.py tests/test_work_monitor_identity.py
git add ostag/copilot_relay/service.py ostag/copilot_relay/policy.py \
  ostag/work_monitor/identity.py tests/test_copilot_relay_service.py \
  tests/test_work_monitor_identity.py
git commit -m "feat(relay): route governed copilot instructions"
osstack kanban comment card_227f12f3 \
  "Task 7 complete: $(git rev-parse --short HEAD)" codex
```

---

### Task 8: Add the local osstack relay agent and durable spool

**Files (OStag worktree):**
- Create: `ostag/copilot_relay/osstack_port.py`
- Create: `ostag/copilot_relay/reporter.py`
- Create: `tests/test_copilot_relay_osstack_port.py`
- Create: `tests/test_copilot_relay_reporter.py`
- Modify: `ostag/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Write failing osstack-port tests**

Use a fake subprocess runner to validate only structured JSON commands:

```python
port.list_sessions()
assert argv == ("osstack", "session", "list", "--json")
port.send(message)
assert "--expected-version" in argv
assert "--message-id" in argv
```

Reject non-JSON output, extra fields, native session ID leakage, timeouts, oversized snapshots and a broken Codex availability result. Test `task.create.confirm` maps to existing `osstack task submit` with a stable external proposal ID in context, not to shell interpolation.

- [ ] **Step 2: Write failing store-and-forward tests**

Prove spool persistence, stable signatures, sequence ordering, retry/backoff, server 409 reconciliation, expired instructions, duplicate delivery, ack after local apply, crash between apply/ack, and offline accumulation followed by ordered recovery.

- [ ] **Step 3: Run tests and confirm RED**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_osstack_port.py tests/test_copilot_relay_reporter.py
```

- [ ] **Step 4: Implement the bounded local bridge**

`OsstackSessionPort` owns subprocess calls and validates all output through relay models. `CopilotReporter` uses its own SQLite spool, the existing monitor HMAC convention `timestamp + "\n" + body`, per-instance nonce replay protection, and injectable transport/clock. The loop order is:

```python
await reporter.discover_sessions_once()
await reporter.capture_changed_snapshots_once()
await reporter.flush_outbound_once()
await reporter.poll_inbound_once()
await reporter.apply_inbound_once()
await reporter.flush_receipts_once()
```

No central outage blocks an active local agent. Direction-changing inbound instructions remain pending until the exact session version is available.

- [ ] **Step 5: Add configuration validation**

Add `OSTAG_COPILOT_RELAY_ENABLED`, instance ID/secret, central URL, spool path, osstack binary, poll interval and text limits. Disabled is the default. Reject HTTP central URLs outside localhost; production requires HTTPS.

- [ ] **Step 6: Verify and commit**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_osstack_port.py tests/test_copilot_relay_reporter.py \
  tests/test_config.py
git add ostag/copilot_relay/osstack_port.py ostag/copilot_relay/reporter.py \
  ostag/config.py .env.example tests/test_copilot_relay_osstack_port.py \
  tests/test_copilot_relay_reporter.py tests/test_config.py
git commit -m "feat(relay): bridge local osstack sessions durably"
osstack kanban comment card_227f12f3 \
  "Task 8 complete: $(git rev-parse --short HEAD)" codex
```

---

### Task 9: Expose authenticated central relay endpoints

**Files (OStag worktree):**
- Modify: `ostag/work_monitor/server.py`
- Create: `ostag/copilot_relay/routes.py`
- Create: `tests/test_copilot_relay_routes.py`
- Modify: `tests/test_work_monitor_server.py`

- [ ] **Step 1: Write failing FastAPI tests**

Test:

```text
POST /v1/copilot/events
POST /v1/copilot/messages/poll
POST /v1/copilot/messages/ack
GET  /v1/copilot/sessions
GET  /v1/copilot/sessions/{instance_id}/{session_id}/snapshot
```

Cover valid HMAC, invalid signature, timestamp skew, nonce replay, wrong instance key, body limit, malformed strict envelope, duplicate event, long polling timeout, lease redelivery and ack-hash mismatch. Human read endpoints use the existing credential/role system and never accept an instance secret.

- [ ] **Step 2: Run tests and confirm RED**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_routes.py tests/test_work_monitor_server.py
```

- [ ] **Step 3: Implement routes and one composition root**

Mount a relay router from the existing central server lifespan. Reuse `MonitorStore`, instance secret lookup, body-size limit and timestamp verification. Poll returns a bounded batch ordered by creation time; ack requires each `message_id` and payload hash. Do not create a second scheduler or database connection owner.

- [ ] **Step 4: Verify and commit**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_routes.py tests/test_work_monitor_server.py
git add ostag/copilot_relay/routes.py ostag/work_monitor/server.py \
  tests/test_copilot_relay_routes.py tests/test_work_monitor_server.py
git commit -m "feat(relay): serve authenticated copilot mailbox"
osstack kanban comment card_227f12f3 \
  "Task 9 complete: $(git rev-parse --short HEAD)" codex
```

---

### Task 10: Build the Feishu Copilot console and bind replies safely

**Files (OStag worktree):**
- Create: `ostag/copilot_relay/feishu_cards.py`
- Create: `ostag/copilot_relay/feishu_routes.py`
- Create: `tests/test_copilot_relay_feishu_cards.py`
- Create: `tests/test_copilot_relay_feishu_routes.py`
- Modify: `ostag/adapters/feishu.py`
- Modify: `ostag/runner.py`
- Modify: `tests/test_feishu_work_monitor.py`

- [ ] **Step 1: Write failing card tests**

Require three mobile-first cards:

1. Current task: status, agent, instance, phase, last update, bounded summary, buttons `查看最新回复`, `切换任务`.
2. Decision: question, choices, `回复此消息也可作答`, exact task/turn label, expiry.
3. New-task confirmation: title, target instance, agent preference, risk, buttons `确认创建`, `取消`.

Assert card payloads stay below 24 KB, all untrusted strings are escaped, callback values contain only opaque signed IDs, source paths/native IDs are absent, and non-owner users cannot receive action buttons.

- [ ] **Step 2: Write failing route tests**

Test private-chat ordinary text, quoted decision reply, callback replay, callback expiry, wrong actor, wrong chat, current-task switch, `/最新回复`, `/任务`, ambiguous session and group-chat refusal. A Feishu `message_id` maps durably to the relay turn so a quote survives process restart.

- [ ] **Step 3: Run tests and confirm RED**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_feishu_cards.py tests/test_copilot_relay_feishu_routes.py
```

- [ ] **Step 4: Implement cards and integration routing**

Add relay handling before generic backend dispatch but after event normalization/authentication. Existing Work Monitor board commands retain precedence for their explicit command syntax. Unquoted private text uses the current-task binding; quoted text resolves its original relay turn; no heuristic title matching is allowed.

Push policy is hybrid: decisions immediately, stage summaries on existing monitor transitions, latest assistant reply only on demand or after a remote instruction completes.

- [ ] **Step 5: Verify and commit**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_feishu_cards.py tests/test_copilot_relay_feishu_routes.py \
  tests/test_feishu_work_monitor.py tests/test_event_normalization.py
git add ostag/copilot_relay/feishu_cards.py ostag/copilot_relay/feishu_routes.py \
  ostag/adapters/feishu.py ostag/runner.py tests/test_copilot_relay_feishu_cards.py \
  tests/test_copilot_relay_feishu_routes.py tests/test_feishu_work_monitor.py
git commit -m "feat(feishu): add remote copilot console"
osstack kanban comment card_227f12f3 \
  "Task 10 complete: $(git rev-parse --short HEAD)" codex
```

---

### Task 11: Integrate decisions, progress notifications and failure recovery

**Files (OStag worktree):**
- Create: `ostag/copilot_relay/scheduler.py`
- Create: `tests/test_copilot_relay_scheduler.py`
- Modify: `ostag/work_monitor/server.py`
- Modify: `ostag/work_monitor/scheduler.py`
- Modify: `tests/test_work_monitor_scheduler.py`

- [ ] **Step 1: Write failing clock-driven tests**

Prove immediate decision delivery, one message per decision, stage-summary coalescing, retry at 1/2/4/8 minutes, dead-letter after configured cap, process restart recovery, one scheduler lease owner, and independence from daily board delivery. Test expired unanswered decisions become `timed_out` and the local session receives a receipt, not a guessed answer.

- [ ] **Step 2: Run tests and confirm RED**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_scheduler.py tests/test_work_monitor_scheduler.py
```

- [ ] **Step 3: Implement a lifespan-owned relay scheduler**

Use the existing central scheduler ownership lease. A wake event may request an early drain, but request handlers never deliver directly. Stable delivery IDs use `relay:{message_id}:{destination}`. Record delivered platform message IDs for quote binding before marking outbox delivery complete.

- [ ] **Step 4: Verify and commit**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_scheduler.py tests/test_work_monitor_scheduler.py \
  tests/test_daily_digest_scheduler.py
git add ostag/copilot_relay/scheduler.py ostag/work_monitor/server.py \
  ostag/work_monitor/scheduler.py tests/test_copilot_relay_scheduler.py \
  tests/test_work_monitor_scheduler.py
git commit -m "feat(relay): deliver durable copilot notifications"
osstack kanban comment card_227f12f3 \
  "Task 11 complete: $(git rev-parse --short HEAD)" codex
```

---

### Task 12: Add adversarial integration and end-to-end contract tests

**Files (both worktrees):**
- Create: `/Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session/packages/cli/src/copilot-relay.integration.test.ts`
- Create: `tests/test_copilot_relay_integration.py`
- Create: `scripts/copilot_relay_smoke.sh`
- Modify: `scripts/release_check.sh`

- [ ] **Step 1: Add osstack end-to-end adapter tests**

With fake Codex JSONL and fake Claude SDK streams, prove: create -> native ID captured -> daemon restart -> Feishu-shaped message ID applied -> same native ID resumed -> assistant snapshot returned. Kill the process after message reservation and prove replay does not run a second turn.

- [ ] **Step 2: Add OStag two-host simulation**

Run a temporary central FastAPI app and local reporter with separate SQLite files. Simulate:

- central down for 15 minutes, then ordered recovery;
- local restart after apply before ack;
- central restart while message is leased;
- duplicated Feishu webhook and callback;
- stale quote after a newer turn;
- session completes while an answer is in flight;
- owner switches between two employees' sessions;
- secret/diff/path injection blocked locally;
- high-risk deployment instruction held for approval.

- [ ] **Step 3: Implement a no-secret smoke script**

`scripts/copilot_relay_smoke.sh` starts fake osstack and central services, registers one session, requests a snapshot, sends an instruction, validates the receipt, then checks both databases for stable IDs. It must use generated temporary credentials and delete its temp directory on exit.

- [ ] **Step 4: Run full gates and commit**

```bash
cd /Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session
pnpm test
pnpm typecheck

cd /Users/gilbertchen/Documents/CODE/agentsteam/ostag-copilot-relay
PYTHON_DOTENV_DISABLED=1 uv run pytest -q
uv run ruff check ostag tests
scripts/copilot_relay_smoke.sh
scripts/release_check.sh
```

Expected: all commands exit 0; secret hygiene and existing Work Monitor/daily board tests remain green.

```bash
git add tests/test_copilot_relay_integration.py scripts/copilot_relay_smoke.sh \
  scripts/release_check.sh
git commit -m "test(relay): verify remote copilot recovery loop"
osstack kanban comment card_227f12f3 \
  "Task 12 complete: full local gates pass; $(git rev-parse --short HEAD)" codex
```

---

### Task 13: Package the local reporter and central service for deployment

**Files (OStag worktree):**
- Modify: `deploy/work-monitor/Dockerfile`
- Modify: `deploy/work-monitor/docker-compose.yml`
- Modify: `deploy/work-monitor/docker-compose.production.yml`
- Create: `deploy/copilot-relay/ai.ostag.copilot-relay.plist`
- Create: `deploy/copilot-relay/install-launchd.sh`
- Create: `docs/runbooks/copilot-relay.md`
- Modify: `tests/test_work_monitor_container_deploy.py`
- Create: `tests/test_copilot_relay_deploy.py`

- [ ] **Step 1: Write failing packaging tests**

Assert the central container mounts the existing database, exposes no new public port, includes relay configuration names but no secret values, and passes Compose rendering. Assert the launchd plist uses absolute paths, `KeepAlive`, bounded restart throttling, separate stdout/stderr logs and a local spool outside the repository.

- [ ] **Step 2: Run tests and confirm RED**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_deploy.py tests/test_work_monitor_container_deploy.py
```

- [ ] **Step 3: Implement deployment artifacts and runbook**

The central service remains the existing Singapore `work-monitor` Compose service behind `ostag-monitor.ihiking.me`. The local reporter runs on each worker Mac through launchd and reaches only the HTTPS central origin. The runbook includes setup, key rotation, instance revocation, Codex binary repair, health checks, database backup, rollout, rollback and evidence collection.

- [ ] **Step 4: Verify and commit**

```bash
PYTHON_DOTENV_DISABLED=1 uv run pytest -q \
  tests/test_copilot_relay_deploy.py tests/test_work_monitor_container_deploy.py
docker compose -f deploy/work-monitor/docker-compose.yml config >/dev/null
OSTAG_MONITOR_DATA_DIR=/var/lib/ostag docker compose \
  -f deploy/work-monitor/docker-compose.yml \
  -f deploy/work-monitor/docker-compose.production.yml config >/dev/null
git add deploy docs/runbooks/copilot-relay.md tests/test_copilot_relay_deploy.py \
  tests/test_work_monitor_container_deploy.py
git commit -m "chore(relay): package central and local services"
osstack kanban comment card_227f12f3 \
  "Task 13 complete: deploy artifacts verified; $(git rev-parse --short HEAD)" codex
```

---

### Task 14: Review, deploy progressively and prove the live same-session loop

**Files (OStag worktree):**
- Create: `release/copilot-relay-release-evidence.md`
- Create: `release/copilot-relay-rollback-evidence.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Perform code, security and migration review**

Review the two branches independently, then the cross-repository contract. Block release for any native ID loss, implicit new-session fallback, missing unique constraint, unsafe callback, source/log leakage, migration rollback gap or test bypass. Record findings and resolutions on `card_227f12f3`.

- [ ] **Step 2: Build immutable artifacts and back up production**

Record osstack and OStag commit SHAs and image digest. Back up the Singapore SQLite database and verify it can be opened read-only. Export current Compose config with secrets redacted. Do not deploy directly from a dirty worktree.

- [ ] **Step 3: Deploy read-only discovery first**

Deploy central schema/routes with Feishu write controls disabled. Install the local reporter for one Mac mini with `session.send` disabled. Validate for at least one polling/backoff window:

- sessions appear with correct owner scope;
- Work Monitor and daily boards are unchanged;
- outbound snapshots contain no source, logs, paths, secrets or native IDs;
- restart and temporary network loss recover without duplicate sessions.

- [ ] **Step 4: Enable one-session canary writes**

Allow only the owner's Feishu private chat and one canary session. Execute this exact acceptance flow:

1. Start an osstack-managed Codex CLI or Claude Code task locally.
2. Receive its current-task card in Feishu.
3. Ask `最新回复` and match it to the bounded local assistant response.
4. Trigger a real decision request; answer it by quoting the Feishu message.
5. Verify the same osstack `session_id`, native session ID and incremented version continue locally.
6. Send a direction change and verify safe work pauses at `awaiting_decision` before the answer.
7. Restart the local reporter and central container, then send another answer successfully.
8. Submit `新任务：检查登录回归`, confirm once, and verify exactly one osstack task/card is created.
9. Attempt a high-risk request and verify it is held for approval.

- [ ] **Step 5: Expand to all registered osstack workers**

After the canary evidence passes, enable discovery and governed writes for the remaining registered instances. Verify the owner can switch and view every employee session, while scoped employees cannot view one another.

- [ ] **Step 6: Exercise rollback**

Disable write controls, stop local reporters, deploy the prior central image, and verify Work Monitor plus daily boards still operate. Restore the new image and confirm relay resumes from durable cursors without double application. Record timings and commands in `release/copilot-relay-rollback-evidence.md`.

- [ ] **Step 7: Run final gates, document evidence and close the card**

```bash
scripts/release_check.sh
git add release/copilot-relay-release-evidence.md \
  release/copilot-relay-rollback-evidence.md CHANGELOG.md
git commit -m "docs(release): record copilot relay production evidence"
osstack kanban comment card_227f12f3 \
  "Production accepted: same native session resumed from Feishu; recovery, authorization, redaction and rollback evidence attached." codex
osstack kanban move card_227f12f3 done
osstack kanban board default
```

Expected: `card_227f12f3` is under `DONE` only after all live evidence exists. A local mock, configured environment variable, successful image build, or visually correct Feishu card is not sufficient.

---

## Stop/rollback conditions

Immediately disable relay writes and move the card to `blocked` if any of these occur:

- an answer resumes a different native agent session;
- the same `message_id` invokes an adapter twice;
- a non-owner sees another employee's session;
- source code, diffs, raw logs, local paths, credentials or native IDs reach Feishu or central storage;
- a stale quote is accepted for a newer turn;
- a destructive action bypasses approval;
- the relay degrades existing monitor boards, scheduled Feishu/DingTalk delivery or Telegram controls;
- database migration cannot be restored from the verified backup.

Rollback order is: disable central write flag -> stop local launchd reporters -> restore previous central image -> verify existing Work Monitor/daily boards -> retain relay tables and spools for audit -> investigate from redacted evidence. Never delete queues or databases to make a duplicate-delivery symptom disappear.

## Final acceptance matrix

| Area | Required evidence |
|---|---|
| Same-session continuity | Native Codex/Claude session ID is unchanged across two Feishu turns and a restart |
| Routing | Current-task text and quoted reply reach the exact intended task/turn |
| New tasks | One confirmation creates exactly one osstack task/card |
| Authorization | Owner sees all registered workers; scoped users cannot cross instance boundaries |
| Safety | High-risk actions require approval; no shell endpoint exists |
| Privacy | Automated corpus and live capture show no source/diff/log/path/secret/native ID leakage |
| Reliability | Offline, crash-before-ack, lease expiry and duplicate webhook tests recover without double apply |
| Compatibility | Existing Work Monitor, Feishu/DingTalk daily boards and Telegram tests remain green |
| Operations | Canary, rollout, backup restore and rollback evidence are committed |
| osstack management | `card_227f12f3` contains every task commit/gate and reaches `done` only after production acceptance |
