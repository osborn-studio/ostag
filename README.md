# OS Tag

> OS Tag turns any work thread into a governed, auditable, multi-agent work loop.

OS Tag is an open, self-hostable control plane for human-agent collaboration.
Teams delegate work from Feishu/Lark, DingTalk, Slack group threads — or any
agent delegates via the CLI — to the right backend (mock, Codex CLI, Claude
CLI, OpenAI API), with scoped context, policy checks, action receipts,
approvals, artifacts, durable follow-ups, and a full audit ledger. It
maintains systems of record such as Feishu Base / DingTalk AI Table
requirement tables while keeping humans in control.

OS Tag is not a Claude Tag clone and not a Zilliz official product. It
competes on being open, neutral, inspectable, and system-of-record aware —
see [docs/claude_tag_strategy.md](docs/claude_tag_strategy.md) for the full
strategic direction.

## What it does

- **Source-thread work loop** — mention `@OS Tag` in a group; the adapter
  normalizes the event, the runner opens an auditable run, applies safety
  checks, retrieves allowlisted MFS context, routes to a workflow or backend,
  and replies in the thread.
- **Governed writes** — with `OSTAG_GOVERNED_WRITES=true`, table mutations
  become action receipts: propose → `approve 1` → `apply 1`. The exact
  approved payload is what executes. No apply without approval, ever.
- **Actor permissions** — `read < propose < write < admin`, global or scoped
  per channel; approving or applying requires `write`; denials are ledgered
  as security events.
- **Run ledger** — every handled event creates a run with source, actor,
  intent, context packet (hashed), status, and terminal outcome, in an
  append-only JSONL ledger with secret redaction.
- **Durable follow-ups** — proposals auto-schedule pending-approval
  reminders; `continue run_x` schedules a follow-up; continuations survive
  restarts, fire once, and cancel when resolved.
- **Ambient monitors** (opt-in) — unowned / overdue / stale / missing-status
  requirement detection with per-record suppression, rate limits, cited
  evidence, and ledger audit. Proposal-only: ambient never writes.
- **Agent-facing CLI** — every command supports compact `--json`; errors
  carry `code`, `message`, `recovery_hint`.
- **Remote work monitor** — signed OStag/osstack event streams feed a persistent
  mobile Feishu/Telegram board; pause/resume/retry/cancel stay receipt-governed.

## Architecture

```text
Feishu/Lark/Slack/DingTalk thread @OS Tag ──┐            ┌── ostag run (CLI/agents)
                                            ▼            ▼
                         Adapter → NormalizedEvent → OSTagRunner
                                                        │  run ledger (JSONL, redacted)
                        safety checks → receipt commands → channel brain
                                                        │  context packet (hashed)
                              AutoBase workflow ────────┤
                              (governed writes → receipts → approve/apply
                               via connector executors)  │
                              Backend (mock/Codex/Claude/OpenAI) 
                                                        ▼
                                        reply in source thread + audit
```

Boundaries: adapters own transport/rendering; core owns events, runs,
receipts, ledger, policy; connectors own system-of-record operations;
executors/backends own agent invocation; workflows coordinate domain
operations; the CLI exposes stable human/agent operations. Details:
[docs/ostag/architecture.md](docs/ostag/architecture.md).

## Quick start

```bash
cp .env.example .env
uv sync --extra dev
python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
uv run pytest                 # 217 tests
scripts/release_check.sh      # full release gate
```

Submit work through the full pipeline from the CLI (nothing is posted to any
platform; the reply returns to you):

```bash
ostag run --platform feishu --chat oc_demo --text "请总结项目下一步" --json
```

## CLI surface

```bash
ostag run --chat <id> --text "..." --json      # submit a task
ostag status --run run_x --json                # run snapshot
ostag runs list --json                         # recent runs
ostag ledger export --run run_x --json         # full audit trail
ostag context export --run run_x --json        # what the run was given (hashed)
ostag artifacts list --run run_x --json

ostag receipts list --pending --json           # pending approvals
ostag approve|reject|apply|continue <ref> --json
ostag followups list --json                    # scheduled continuations
ostag followups run-once --json

ostag connector list --json                    # platform capability declarations
ostag channel policy show|set --platform p --chat c ...
ostag ambient run-once [--send] --json
ostag memory show|remember|sync ...
```

Full reference: [docs/cli_reference.md](docs/cli_reference.md).

Work-monitor architecture, deterministic smoke, production deployment, backup,
rollback, and the explicitly incomplete external evidence checklist are in
[docs/ostag/work_monitor.md](docs/ostag/work_monitor.md) and
[docs/ostag/deployment.md](docs/ostag/deployment.md).

## Governance model

| Layer | Mechanism |
|---|---|
| Receipts | state machine: `apply` only from approved state; stale receipts refuse all verbs ([docs/action_receipt_protocol.md](docs/action_receipt_protocol.md)) |
| Permissions | `OSTAG_ACTOR_PERMISSIONS` + per-channel grants ([docs/channel_policy.md](docs/channel_policy.md)) |
| Governed writes | opt-in per deployment or per channel; frozen `apply_payload`; connector executors; no delete path ([docs/governed_writes.md](docs/governed_writes.md)) |
| Audit | run ledger + audit log, secrets redacted ([docs/run_ledger.md](docs/run_ledger.md)) |
| Async | durable continuations ([docs/async_runs.md](docs/async_runs.md)) |
| Ambient | opt-in, cited, suppressed, proposal-only ([docs/ambient_v2.md](docs/ambient_v2.md)) |
| Security | defaults and hardening roadmap ([docs/security_governance.md](docs/security_governance.md)) |

Safe defaults: dry-run on, mock backend, MFS off, shell off, applies off,
governed writes off (fully backward compatible), unsafe CLI flags require
explicit env vars, audit and ledger redact secrets.

## Platforms and backends

- Platform setup: [Feishu](docs/ostag/feishu_setup.md) ·
  [Lark](docs/ostag/lark_setup.md) · [Slack](docs/ostag/slack_setup.md) ·
  [DingTalk](docs/ostag/dingtalk_setup.md) ·
  [deployment](docs/ostag/deployment.md) (launchd, Docker Compose)
- Backends: `OSTAG_BACKEND=mock|codex_cli|claude_cli|openai_api`
  (`uv sync --extra openai` for the API backend; unsafe CLI modes require
  explicit `OSTAG_*_UNSAFE_MODE` flags)
- Capability declarations per connector: `ostag connector list --json`
  ([docs/connector_capabilities.md](docs/connector_capabilities.md))

## Platform operating model

Feishu and DingTalk should present the same OS Tag operating model to users:

- **Feishu** — Feishu Base requirement pools, Feishu multidimensional-table
  tasks, internal approval flows, and customer/business group ambient
  monitoring with status follow-through.
- **DingTalk** — DingTalk AI Table requirement management, AI Table task
  objects, internal approval flows, and customer/business group ambient
  monitoring with status follow-through.

The platform-specific connector owns the system-of-record details, while the
governance layer stays consistent: receipts, actor permissions, approval
quorum, four-eyes rules, budgets, follow-ups, and ledger audit.

## Requirement table operations

With a default Feishu Base / DingTalk AI Table configured
(`OSTAG_AUTO_BASE_DEFAULT_BASE_TOKEN` / `..._TABLE_ID`):

```text
@OS Tag 请把 交易-001 分配给 @张三，状态改为 开发交付中
@OS Tag 请将 市场周报 优先级改为 高
@OS Tag 请为 交易盯盘 创建飞书任务并分配给 @李四
```

The workflow updates only existing fields, locates exactly one matching
record (refuses ambiguity), respects channel policy (writable fields, task
confirmation, daily budgets), and — with governed writes on — proposes a
receipt instead of writing:

```text
OS Tag: 已生成待审批操作：[1] 更新需求「交易-001」… 回复：approve 1 / reject 1
你:     @OS Tag approve 1
你:     @OS Tag apply 1
```

Docx bulk import into the requirement pool works the same way (`自动入表`).

## Context: MFS and channel memory

MFS retrieval is disabled by default and scope-allowlisted
(`MFS_ALLOWED_SCOPES`). Channel memory is local markdown plus a per-channel
brain (facts, preferences, policy) — scoped, inspectable, editable:

```bash
python -m ostag.main memory remember --platform feishu --chat-id oc_demo --text "Use mock backend for pilot."
ostag channel policy show --platform feishu --chat oc_demo --json
```

Every run that reaches the workflow/backend records a hashed context packet
(`ostag context export --run run_x --json`).

## Provenance

Forked from and inspired by `zilliztech/mfs` (Apache-2.0) open-tag-skill;
adapted and renamed as OS Tag with multi-platform adapters and the governance
layer. See [FORK_ORIGIN.md](FORK_ORIGIN.md) and upstream research notes in
[docs/research/](docs/research/).

## Roadmap

Done: run ledger · action receipts · actor permissions · governed writes with
connector executors · CLI-submittable runs · context packets · durable
continuations · per-channel policy · ambient monitors V2.

Also done since: governed task creation · environment scrub for agent
subprocesses · high-risk approval quorum · four-eyes rule · GitHub and GitLab
issues connectors (governed create/comment via gh/glab CLI) · per-channel
daily cost budgets.

Next: MFS-deepened context packets · sandbox hardening for code-writing
executors · Slack Block Kit / Feishu card renderers · GitHub/GitLab thread
reading · web admin console.

## Acceptance

Scoring standard: [docs/ostag/acceptance_criteria.md](docs/ostag/acceptance_criteria.md)
and the benchmark scorecard in
[docs/claude_tag_strategy.md](docs/claude_tag_strategy.md) §16. Release
requires `uv run pytest` green and `scripts/release_check.sh` exit 0.
