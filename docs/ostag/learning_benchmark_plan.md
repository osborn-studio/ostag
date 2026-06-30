# OS Tag Learning Benchmark Plan

Status: Draft for osstack execution
Date: 2026-06-30
Owner: Loop Engineering / OS Tag

## 1. Objective

This document defines what OS Tag should learn from:

- Open Tag in `zilliztech/mfs/examples/open-tag-skill`
- Claude Tag and Claude Code in Slack

It converts those lessons into product requirements, technical architecture,
TDD verification standards, benchmark cases, and a 100-point scoring rubric.

The target product direction is:

> OS Tag becomes a multi-platform AI teammate for Feishu, Lark, and Slack
> groups. Each group has persistent memory, governed tool access, requirement
> table operations, staged task execution, and optional ambient proactive
> follow-up.

## 2. Source References

Primary references:

- Open Tag upstream example:
  https://github.com/zilliztech/mfs/tree/main/examples/open-tag-skill
- Local upstream snapshot:
  `examples/open-tag-skill/`
- OS Tag migration inventory:
  `docs/fork_migration_report.md`
- Anthropic Claude Tag announcement:
  https://www.anthropic.com/news/introducing-claude-tag
- Claude Code in Slack docs:
  https://code.claude.com/docs/en/slack

Reference interpretation:

- Open Tag is a self-hosted reference pattern: group mention, authorized MFS
  memory/tools, CLI agent backend, and channel memory.
- Claude Tag is a managed product pattern: shared channel AI teammate, staged
  work, ambient mode, organization governance, and admin-controlled tool/data
  access.

## 3. Current OS Tag Baseline

Already implemented:

- Feishu/Lark live adapter and Slack adapter foundation.
- Normalized event model across platforms.
- Mock, Codex CLI, Claude CLI, and OpenAI API backend support.
- MFS search interface with allowlisted scopes.
- Local chat memory commands.
- Safety checks, audit log, doctor checks, release gates.
- Feishu Base docx ingestion into an existing requirement table.
- Requirement operations: record search, owner/status/priority updates, and
  Feishu task creation.
- launchd production deployment on the current host.

Known gaps:

- Slack requirement operations are not production-equivalent to Feishu/Lark.
- MFS connector breadth is not fully productionized.
- Channel memory is not yet automatically summarized and injected as a durable
  group brain.
- Ambient proactive mode is not implemented.
- Tool connectors are still partly workflow-specific instead of a unified tool
  abstraction.
- Governance exists as docs and defaults, but not as per-channel runtime policy.

## 4. Capability Matrix

| Capability | Open Tag lesson | Claude Tag lesson | OS Tag target |
|---|---|---|---|
| Group mention as work trigger | Mention a Slack bot to run a CLI agent. | Tag `@Claude` in Slack as a teammate. | Mention OS Tag in Feishu/Lark/Slack to run governed workflows. |
| Thread/channel context | Use Slack context and MFS indexed memory. | Read thread context and recent channel context. | Pull recent chat context plus persistent channel memory. |
| Persistent memory | MFS and local memory scripts. | Learns channel/team context over time. | Channel Brain with facts, preferences, active work, and tool bindings. |
| CLI agent backend | Claude Code or Codex CLI. | Claude Code sessions for engineering tasks. | Pluggable backend router: chat, requirement ops, code, docs, research. |
| Tool access | MFS connectors expose external sources. | Admin-approved tools and data. | Unified tool registry with per-channel permissions. |
| Staged execution | Demo-level CLI work. | Breaks delegated work into stages and reports progress. | Requirement/task execution ledger with visible stages. |
| Ambient mode | Not included. | Proactively flags updates and follows up quiet tasks. | Optional ambient monitors with conservative defaults and rate limits. |
| Governance | Explicitly not production boundary. | Admin controls access, tools, cost, and org policy. | Per-channel policy: allowed tables/tools/actions, approval mode, budget. |
| Auditability | Minimal/demo. | Managed product expectation. | Structured audit, execution records, test evidence, rollback. |
| Multi-platform | Slack first. | Slack first. | Feishu/Lark first, Slack parity as a required benchmark. |

## 5. Product Requirements

### P0: Channel Brain

Each group has a durable brain with:

- Platform and chat id.
- Human-readable group name when available.
- Default requirement Base/table.
- Active project facts.
- Members and role hints.
- Accepted terminology and aliases.
- Active requirements and unresolved follow-ups.
- Memory freshness metadata.

Required behavior:

- OS Tag injects Channel Brain context into every relevant workflow.
- Users can ask `@OS Tag 这个群你记住了什么`.
- Users can correct memory: `@OS Tag 记住：张三负责交易相关需求`.
- OS Tag records memory updates with audit evidence.

### P0: Governed Requirement Operations

The requirement workflow must not be a one-off importer. It must maintain the
group's requirement table over time.

Required behavior:

- Search requirement records by code, title, description, and detail fields.
- Update owner, status, priority, due date, and task link when fields exist.
- Create Feishu/Lark tasks when requested.
- Refuse ambiguous matches and ask for a more specific target.
- Never create a new Base/table when a default governed table is configured,
  unless policy explicitly allows it.

### P1: Staged Execution Ledger

Every non-trivial workflow should report and store execution stages:

1. Received request.
2. Loaded channel policy.
3. Loaded context.
4. Located target record or artifact.
5. Validated permissions.
6. Planned actions.
7. Executed write operations.
8. Reported result.
9. Waiting for validation, if needed.

Required behavior:

- User-facing replies show high-signal stages, not raw JSON errors.
- Audit logs store machine-readable stage events.
- Failed stages are recoverable and explain the exact blocker.

### P1: Ambient Proactive Mode

Ambient mode must be opt-in per channel.

Allowed first use cases:

- Requirement has no owner for more than configured threshold.
- Requirement status remains unchanged for more than configured threshold.
- A created task has no visible follow-up after threshold.
- A docx or requirement request appeared but was not ingested.

Safety requirements:

- Ambient mode defaults to off.
- Ambient mode can only send reminders or draft proposed actions in v1.
- Writes require explicit user command unless channel policy enables auto-write.
- Max reminders per channel per day.
- All ambient decisions are auditable.

### P1: Admin Governance

Each channel policy controls:

- Default Base/table and views.
- Fields OS Tag may read.
- Fields OS Tag may write.
- Whether OS Tag may create tasks.
- Whether task creation requires confirmation.
- Whether ambient mode is enabled.
- Daily message/action budget.
- Allowed backends and tools.
- Slack/Feishu/Lark identity behavior.

Required behavior:

- Missing policy falls back to safe defaults.
- Policy changes require admin command or local config update.
- Policy is testable without live platform credentials.

### P2: Unified Tool Connector Layer

Move platform-specific and tool-specific operations behind stable tool
interfaces:

- Chat history connector.
- Requirement table connector.
- Task connector.
- Document connector.
- GitHub connector.
- MFS connector.
- Backend/agent connector.

Required behavior:

- Workflows call interfaces, not raw `lark-cli` commands directly.
- Each connector has fake implementations for TDD.
- Each connector reports required scopes and permission failures consistently.

### P2: Slack Parity

Slack should become a first-class runtime target, not only a normalized event
adapter.

Required behavior:

- Slack mention loads recent channel/thread context.
- Slack can use Channel Brain.
- Slack can trigger requirement operations against Feishu/Lark Base if policy
  allows cross-tool writes.
- Slack replies use staged progress updates.
- Slack-specific auth/setup is documented and tested.

## 6. Architecture Target

```text
Platform Event
  -> Adapter: Feishu / Lark / Slack
  -> NormalizedEvent
  -> ChannelPolicyResolver
  -> ChannelBrainStore
  -> IntentRouter
      -> RequirementWorkflow
      -> TaskWorkflow
      -> MemoryWorkflow
      -> AmbientWorkflow
      -> AgentWorkflow
  -> ToolRegistry
      -> ChatConnector
      -> RequirementTableConnector
      -> TaskConnector
      -> DocumentConnector
      -> MFSConnector
      -> GitHubConnector
  -> StageLedger + AuditLog
  -> Platform Reply
```

Design rules:

- Workflow logic must be platform-neutral where possible.
- Connector implementations may be platform-specific.
- Policy is checked before tool execution.
- Writes are idempotent when possible.
- User-facing errors are actionable and concise.

## 7. TDD Strategy

### Test Pyramid

| Test level | Target share | Purpose |
|---|---:|---|
| Unit tests | 70% | Intent parsing, policy decisions, stage generation, memory merge logic. |
| Connector contract tests | 20% | Fake Feishu/Lark/Slack/MFS/GitHub tool behavior and error mapping. |
| Live gated smoke tests | 10% | Credential-gated verification for actual platform permissions and writes. |

### Required TDD Rules

- New behavior starts with failing tests.
- Every workflow has fake connector tests before live validation.
- Every write path has an idempotency or duplicate-prevention test.
- Every permission failure has a user-facing error test.
- Every ambient action has a rate-limit and policy-disable test.

### Required Test Commands

```bash
uv run pytest
python -m compileall ostag
scripts/release_check.sh
```

Credential-gated live checks:

```bash
lark-cli base +record-search --as user --base-token <base> --table-id <table> --keyword <known>
lark-cli task +create --summary "OS Tag smoke" --dry-run --json
python -m ostag.main --platform feishu --live --doctor
python -m ostag.main --platform slack --live --doctor
```

## 8. Benchmark Suite

### Benchmark A: Open Tag Parity

Goal: OS Tag preserves the useful Open Tag pattern.

Cases:

1. Mention bot with a question; it routes to backend.
2. Backend receives MFS context when enabled.
3. MFS scope allowlist prevents unauthorized search.
4. Channel memory can be written and read.
5. Claude CLI and Codex CLI commands are built safely.
6. Doctor detects missing backend or platform credentials.
7. Slack mention normalizes to the same event contract as Feishu/Lark.

Pass criteria:

- All cases have automated tests.
- No unsafe CLI flags are enabled by default.
- MFS disabled mode degrades gracefully.

### Benchmark B: Claude Tag Channel Teammate

Goal: OS Tag behaves like a shared channel teammate.

Cases:

1. Channel Brain stores default project facts.
2. A second user can ask OS Tag to recall the same channel facts.
3. Thread/recent-channel context is included in task routing.
4. Long-running work reports stages.
5. A failed tool call reports the exact stage and recovery action.

Pass criteria:

- Same channel id produces same memory context.
- Different channel id does not leak memory.
- Stage ledger exists for each non-trivial workflow.

### Benchmark C: Requirement Operations

Goal: OS Tag maintains the requirement table safely.

Cases:

1. Ingest docx into existing Base/table.
2. Search a known requirement by phrase.
3. Update status.
4. Update priority with alias mapping such as `Need`, `Nice`, `P0`, `高`.
5. Assign owner from a real platform mention.
6. Create task and attach/link task URL when the field exists.
7. Refuse ambiguous matches.
8. Refuse writes to fields disallowed by policy.

Pass criteria:

- No duplicate companion Base is created.
- All writes are against configured target table.
- User-facing reply includes what changed.

### Benchmark D: Ambient Mode

Goal: Ambient mode is useful and safe.

Cases:

1. Ambient disabled means no proactive reply.
2. Requirement without owner triggers one reminder after threshold.
3. Status stale triggers one reminder after threshold.
4. Reminder budget prevents repeated messages.
5. Auto-write disabled means OS Tag proposes, but does not write.
6. Audit log records why an ambient action was proposed.

Pass criteria:

- Zero ambient writes in default configuration.
- All ambient messages cite the triggering rule.
- Rate limits are deterministic in tests.

### Benchmark E: Governance

Goal: Admin policy controls behavior.

Cases:

1. Missing policy falls back to safe defaults.
2. Policy can allow read-only operation.
3. Policy can allow writes to selected fields only.
4. Policy can require confirmation before task creation.
5. Policy can disable a backend.
6. Budget limit blocks further actions and reports the blocker.

Pass criteria:

- Policy decisions are unit tested.
- User-facing response never exposes secrets.
- Audit log contains policy decision ids.

### Benchmark F: Multi-Platform Parity

Goal: The same product concept works in Feishu, Lark, and Slack.

Cases:

1. Feishu mention triggers requirement workflow.
2. Lark mention triggers requirement workflow.
3. Slack mention triggers memory workflow.
4. Slack can trigger a governed cross-tool Base operation.
5. Platform-specific mention parsing extracts assignee identity.

Pass criteria:

- Common workflow tests run against platform-neutral events.
- Platform adapters have fixture tests.
- Slack parity gaps are explicitly marked if not complete.

## 9. 100-Point Scoring Rubric

| Area | Points | Acceptance standard |
|---|---:|---|
| Open Tag parity | 12 | Mention trigger, MFS context, memory, CLI backends, doctor, and Slack fixture pass. |
| Claude Tag teammate UX | 14 | Channel Brain, shared memory, staged progress, and recoverable errors work. |
| Requirement operations | 16 | Existing Base ingestion, search, update, owner assignment, task creation, ambiguity refusal pass. |
| Ambient mode safety | 12 | Opt-in, rate-limited, proposal-first ambient reminders with audit evidence. |
| Admin governance | 14 | Per-channel policy controls tools, fields, writes, confirmation, budget, and backend access. |
| Tool connector architecture | 10 | Workflows use connector interfaces with fake implementations and scope reporting. |
| Multi-platform parity | 8 | Feishu/Lark live paths remain stable and Slack has tested parity for core workflows. |
| TDD and regression coverage | 8 | Unit/contract/live-gated tests exist; `uv run pytest` and release gates pass. |
| Security and audit | 4 | Secrets are redacted, unsafe defaults remain disabled, writes are auditable. |
| Documentation and osstack readiness | 2 | Docs, task prompts, scorecard, and launch handoff are current. |

Release thresholds:

- 90-100: Production-candidate.
- 80-89: Pilot-ready with documented gaps.
- 70-79: Internal dogfood only.
- Below 70: Not releasable.

Hard blockers:

- Any default ambient write without explicit opt-in.
- Cross-channel memory leakage.
- Writing to the wrong Base/table.
- Creating duplicate tasks without idempotency.
- Exposing app secrets, tokens, or raw private chat logs in user replies.

## 10. osstack Execution Tasks

Use these as osstack task prompts.

```text
Phase 1: Channel Brain
Implement per-channel OS Tag memory/policy foundation. Add ChannelBrainStore,
ChannelPolicyResolver, tests for channel isolation, default table resolution,
and user-visible memory status replies.
```

```text
Phase 2: Stage Ledger
Add staged execution ledger for requirement operations. Every workflow must
record received, policy_loaded, context_loaded, target_found, permission_checked,
executed, and replied stages. Add tests for success and failure stages.
```

```text
Phase 3: Connector Layer
Extract Feishu Base and Task operations behind RequirementTableConnector and
TaskConnector interfaces. Keep existing behavior passing. Add fake connector
contract tests.
```

```text
Phase 4: Governance Policy
Implement per-channel policy controls for readable/writable fields, task
creation, confirmation mode, ambient enablement, and daily budget. Add policy
decision tests and user-facing blocker messages.
```

```text
Phase 5: Ambient Mode v1
Implement opt-in ambient reminders for unowned and stale requirements. No
auto-write in v1. Add deterministic clock tests, rate-limit tests, and audit
evidence.
```

```text
Phase 6: Slack Parity
Bring Slack runtime to parity for Channel Brain and governed requirement
operations. Add Slack fixture tests and live doctor docs.
```

```text
Phase 7: Benchmark Gate
Create scripts/osstack_learning_benchmark_verify.sh that runs pytest, selected
contract tests, release checks, and generates release/learning-benchmark.md with
the 100-point score.
```

## 11. Definition of Done

The learning program is complete when:

- `uv run pytest` passes.
- Release gates pass.
- `release/learning-benchmark.md` reports at least 90/100.
- Each hard blocker is explicitly tested.
- Feishu/Lark production bot still performs current requirement operations.
- Slack parity status is either passing or explicitly scored as a remaining gap.
- osstack archived all phases with verifier evidence.

## 12. Initial Recommended Scope

Build in this order:

1. Channel Brain.
2. Stage Ledger.
3. Connector Layer.
4. Governance Policy.
5. Ambient Mode v1.
6. Slack Parity.
7. Benchmark Gate.

Rationale:

- Channel Brain and policy are the base of Claude Tag-style behavior.
- Stage Ledger improves user trust immediately.
- Connector extraction reduces risk before adding ambient actions.
- Ambient mode should come after governance, not before.
- Slack parity is easier after workflows are connector-based.
