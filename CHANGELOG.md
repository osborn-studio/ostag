# Changelog

## 0.4.0 — 2026-07-25

Selective back-port of OpenTag v0.7 completion governance (upstream
`amplifthq/opentag` pinned at `758fb5a6` / `cc1f4fb` / `de70cca`), executed
as seven independently reviewed, phase-gated increments (PR #2–#8). Every
intentional divergence from upstream is declared in the mapping notes under
`docs/upgrade/`.

- Added completion contracts and evidence-based completion assessment
  (`ostag.core.completion`, `completion_evaluate`): five gate kinds,
  assurance grading where unverifiable evidence never satisfies,
  resource-version-pinned evidence where stale never passes, gate-scoped
  expiring waivers requiring human attribution, append-only assessments
  with input digests and optimistic recorded/duplicate/conflict appends,
  and execution-compat vs governed modes. The runner mirrors a best-effort
  execution-compat assessment into the ledger.
- Added evidence-aware completion boards (`completion_board`): one
  identity-scoped WorkLoopView projection separating "agent reported done"
  from "evidence proves done", with contract-driven test/deploy/acceptance
  badges, sanitized rendering, and personal vs global-owner admin scoping.
- Added agent access-profile snapshots in shadow mode
  (`ostag.core.access_snapshot`, `work_monitor.access_snapshots`):
  immutable intake captures with content digests and redacted views (no
  broker refs, no grant reasons), a post-projection best-effort hook that
  provably cannot alter projection, startup reconciliation for captures
  lost to crashes, and no role in routing or authorization.
- Added structured, time-bounded human escalation
  (`ostag.core.escalation`, `escalation_store`): full upstream lifecycle
  (open/acknowledged/resolved/expired/superseded) with dedupe slots,
  attributed idempotent transitions, terminal immutability, optimistic
  concurrency, and best-effort expiry sweeps. A late acknowledgement or
  resolution is coerced to expired — an expired escalation never
  authorizes — and expired escalations are inert to the completion
  evaluator.
- Added read-only routing explanations (`ostag.core.routing_explanation`,
  `copilot_relay.route_explainer`): RoutingDecision records with
  eligible-only selection, derived strictly after the actual router
  decides (the upstream placement selector was deliberately not ported).
  Explanations never invoke the relay policy, are recorded best-effort
  with a non-throwing fallback ladder, and a corpus equivalence suite
  proves routing results and relay state are identical with and without
  the explanation layer. Production Feishu routing (text, decision
  answers, snapshots, proposal confirmations) records deterministic,
  principal-attributed, immutable decisions with sanitized views.
- Added the automatic-routing-fallback design document
  (`docs/upgrade/v0.3-auto-routing-design.md`) — design only: a narrow
  same-task-successor fallback gated behind a read-only instrumentation
  phase, a demand test allowed to conclude "do not build", transactional
  evidence, a DB-backed kill switch, and an explicit written owner
  approval gate. No fallback runtime exists in this release.
- Schema migrations v21–v24, all additive with migration fixtures; the
  dual-device acceptance invariant `crossed_routes=0` and the full test
  suite (955 tests) hold throughout.

## 0.3.0 — 2026-07-25

- Added consolidated audit export through `ostag audit export`, including
  filterable run status and cost, action receipts, approval decisions, spend
  totals, and security events. Exports can be scoped by UTC date range,
  platform, or chat and written as redacted JSON.
- Added identity-scoped, device-first osstack status dashboards in Feishu.
  Natural-language status questions are now read-only, show the active device,
  runtime, agent, session, progress, and pending decision, and do not enqueue a
  Copilot Relay instruction.
- Added secure multi-device Copilot Relay gateways for MacBook and Mac mini,
  including device pairing, signed identities, runtime-aware v2 envelopes,
  realtime heartbeats, exact device/runtime/session routing, critical event
  notifications, and recovery-safe launchd deployment.
- Added the OStag Copilot Relay: an owner-scoped Feishu console for discovering
  osstack sessions, viewing bounded assistant snapshots, answering explicit
  decisions, changing direction, and creating governed tasks without exposing
  native agent identifiers, source, terminal logs, local paths, or credentials.
- Added durable signed central/local mailboxes, idempotent callbacks and task
  creation, monotonic session versions, restart recovery, one-session canary
  allowlists, read-only rollout controls, and approval holds for high-risk work.
- Added Feishu current-task, latest-reply, decision, and receipt cards. Private
  proactive delivery now resolves the bound user's `open_id` while preserving
  the private chat as the authorization scope.
- Added durable daily task and requirement dashboards for Feishu and DingTalk,
  including central ownership, idempotent delivery windows, Beijing-time
  scheduling, native mobile cards, validation commands, and production-safe
  fallbacks.
- Added a persistent work-monitor control plane with scoped principals, live
  task projections, governed remote controls, rich Feishu cards, Telegram
  transport, replay protection, reliable acknowledgement, and operational
  alert scheduling.
- Added launchd and Compose deployment assets, a production runbook, rollback
  procedure, security/redaction gates, and cross-repository same-session smoke
  coverage with osstack.

## 0.2.0 — 2026-07-05

The governance-layer release: OS Tag becomes an open control plane for
human-agent collaboration. Open protocol schemas, a durable run ledger, the
action receipt protocol (approve/reject/apply/continue) with actor
permissions, high-risk approval quorum, and a four-eyes rule; governed writes
across five connector families (Feishu/Lark Base, DingTalk AI Table,
Feishu/Lark tasks, GitHub issues, GitLab issues); CLI-submittable runs with
hashed context packets; durable scheduled continuations; per-channel policy;
generalized ambient monitors; agent-subprocess environment scrubbing; and
per-channel daily cost budgets. 264 tests. See entries below.

- GitLab connector (`docs/gitlab_connector.md`): governed issue creation and
  commenting via the `glab` CLI (`OSTAG_GLAB_BIN`, `OSTAG_GITLAB_DEFAULT_REPO`),
  mirroring the GitHub connector. No direct-write path — reached only through
  propose → approve → apply receipts (target `gitlab_issues`); no close/delete.
  Capability declaration flipped from planned to available.

- Cost & budget governance (`docs/cost_budget.md`): per-run cost in credits
  (`OSTAG_RUN_COST_CREDITS`, default 0 = off) and a daily channel spend cap
  (`OSTAG_DAILY_COST_BUDGET`, channel override `daily_cost_budget`). A run
  that would exceed the channel's budget is blocked before the paid backend
  call — no cost incurred, run status `blocked`; spend is ledgered as
  `cost_recorded` and resets per UTC day.
- New CLI: `ostag budget status --platform --chat` and
  `ostag channel policy set --daily-cost-budget N|inherit`.

- GitHub connector (`docs/github_connector.md`): governed issue creation and
  commenting via the `gh` CLI (`OSTAG_GH_BIN`, `OSTAG_GITHUB_DEFAULT_REPO`).
  There is no direct-write path — GitHub is reached only through
  propose → approve → apply receipts (target `github_issues`); no close or
  delete operations exist. Capability declaration flipped from planned to
  available; GitLab remains a stub.
- CLI receipt verbs (`ostag approve/reject/apply/continue`, `receipts`) now
  run with the full apply-executor registry, so `ostag apply` can execute
  approved table, task, and GitHub receipts (previously no executors were
  registered in the CLI path).

- Four-eyes rule (`OSTAG_FOUR_EYES_ENABLED`, default off; channel override
  `--four-eyes true|false|inherit`): the recorded proposer of a receipt
  cannot approve it; violations raise `four_eyes_violation` and are ledgered
  as security events. Rejecting (withdrawing) your own proposal stays
  allowed; receipts proposed before proposer tracking are unrestricted.
- Receipts now record their proposer (`proposer_id` on `action_proposed`);
  `ostag receipts propose` gained `--actor`.

- Approval quorum for high-risk receipts: `risk: high` actions stay in
  `needs_approval` until `OSTAG_APPROVAL_QUORUM_HIGH_RISK` distinct
  write-level actors approve (default 1 = unchanged behavior); duplicate
  approvals don't advance the count; reject is always immediate. Channel
  override via `ostag channel policy set --quorum-high-risk N|inherit`;
  partial approvals reply with progress (1/2) in the source thread.

- Environment scrub: Codex/Claude CLI backends and the MFS client now spawn
  with chat-platform secrets (Feishu/Lark/Slack/DingTalk tokens and keys)
  removed from their environment; model-provider keys are preserved.
  `OSTAG_ENV_SCRUB_EXTRA` scrubs additional custom variables. Connector CLIs
  (lark-cli/dws-cli) are deliberately exempt as system-of-record transport.

- Governed task creation: under governed writes, Feishu/Lark task creation is
  proposed as a receipt (target `feishu_task`/`lark_task`) instead of
  executed; apply creates the task and backfills the task-link fields
  declared at proposal time. The receipt satisfies the
  `task_create_requires_confirmation` channel gate; direct mode is unchanged.
- Combined commands (patch + task) propose two independently approvable
  receipts.

- Ambient Mode V2 (`docs/ambient_v2.md`): new `overdue_requirement` and
  `missing_status` monitors alongside unowned/stale; every decision cites
  record id, rule, and threshold; done statuses stay quiet.
- Ambient noise controls: per-sweep decision cap
  (`OSTAG_AMBIENT_MAX_DECISIONS`), per-record suppression window
  (`OSTAG_AMBIENT_SUPPRESS_DAYS`), configurable staleness threshold
  (`OSTAG_AMBIENT_STALE_DAYS`); multiple findings join into one message.
- Ambient decisions are audited in the run ledger as `ambient_decision`
  events on an `executor_id: ambient` run.
- New CLI: `ostag ambient run-once [--send] --json` — dry by default,
  collecting would-be messages into the output.

- Added per-channel policy (`docs/channel_policy.md`): `governed_writes` and
  `apply_enabled` channel overrides with inherit-from-global semantics, and
  channel-scoped actor grants that outrank the global mapping inside their
  channel only; legacy brain.json files load as inherit.
- New CLI: `ostag channel policy show/set --platform --chat` with
  `--governed-writes/--apply-enabled true|false|inherit`, repeatable
  `--grant actor:level`, `--revoke actor`; output shows stored channel
  values and resolved effective values.
- Enforcement wired into the auto_base workflow (both write paths) and the
  approval service (apply gate + permission resolution).

- Added durable scheduled continuations (`docs/async_runs.md`): ledger-backed
  follow-ups that survive restarts, fire at most once, cancel when the
  subject is already resolved, and always cite why they triggered.
- Governed-write proposals auto-schedule a pending-approval reminder
  (`OSTAG_FOLLOWUP_ENABLED` default on, `OSTAG_FOLLOWUP_AFTER_SECONDS`
  default 4h); the `continue <run_id>` verb now schedules a real follow-up
  and reports its due time.
- Ambient scheduler runs the follow-up sweep each tick; new CLI:
  `ostag followups list [--all]` and `ostag followups run-once`.

- Added `ostag run`: submit a task through the full pipeline from the CLI
  (safety, receipts, channel brain, workflow, backend) with the reply
  returned to the caller — nothing is posted to platforms; exit code tracks
  run outcome.
- Runs now record a `ContextPacket` (`context_packet_built` ledger event)
  with source summary, MFS results, channel brain facts, policy constraints,
  citations, and a sha256/16 audit hash; run snapshots expose
  `context_packet_id`.
- Added `ostag runs list` (recent runs, newest first) and
  `ostag context export --run` (packet + hash).

- Added governed writes (`OSTAG_GOVERNED_WRITES`, default off): requirement
  patches and docx bulk imports become action receipts that require
  approve + apply instead of writing directly; the exact mutation is frozen
  in the receipt's `apply_payload` (`docs/governed_writes.md`).
- Added real apply executors (`ostag/connectors/apply.py`): approved receipts
  for `feishu_base`/`lark_base`/`dingtalk_ai_table` execute through the same
  connector paths as direct writes; wired into the runner's ApprovalService.
- Added `apply_payload` field to `ActionReceipt` (additive, round-trips).

- Wired receipt verbs into live flows: chat messages `approve/reject/apply/
  continue/status <ref>` are parsed by the runner and executed by the new
  `ApprovalService` (`ostag/core/approvals.py`), with replies in the source
  thread.
- Enforced actor permissions (`OSTAG_ACTOR_PERMISSIONS`,
  `OSTAG_DEFAULT_PERMISSION_LEVEL`): `approve`/`apply` require `write`;
  denials are ledgered as `security_event` (`ostag/core/permissions.py`).
- Receipts are now persisted in the run ledger (`action_proposed`,
  `action_state_changed`, `approval_recorded`) and reconstructed by folding
  events; every approve/reject records an `ApprovalDecision` with actor.
- Apply is double-gated: `OSTAG_APPLY_ENABLED` (default false) plus a
  registered `ApplyExecutor` per target system; executor failures mark the
  receipt `failed` with ledger evidence.
- TTL staleness enforced on access (`OSTAG_RECEIPT_TTL_SECONDS`, default
  24h): expired pending receipts are marked `stale` and refuse all verbs.
- New CLI: `ostag approve/reject/apply/continue <ref>` (with `--actor`,
  default `local-admin`) and `ostag receipts list/propose` for agents.

- Added open governance protocol schemas: `Run`, `ContextPacket`,
  `ActionReceipt`, `Artifact`, `PermissionGrant`, `ApprovalDecision`,
  `ApplyPlan`, `LedgerEvent`, `ConnectorCapability` (`ostag/core/schemas.py`).
- Added durable JSONL run ledger with recursive secret redaction; every
  handled inbound event now creates an auditable run with a terminal outcome
  (`ostag/core/run_ledger.py`, wired into `OSTagRunner`).
- Added action receipt protocol: text/JSON renderers, approve/reject/apply/
  continue/status command parser, and an enforced state machine — no apply
  without approval, stale receipts refuse all verbs (`ostag/core/receipts.py`).
- Added connector capability declarations for Slack/Feishu/Lark/DingTalk chat
  and table connectors, plus GitHub/GitLab planned stubs
  (`ostag/core/capabilities.py`).
- Added agent-facing inspection CLI: `ostag status --run`, `ostag ledger
  export --run`, `ostag artifacts list --run`, `ostag connector list`, all
  with compact `--json` output and structured errors with recovery hints.
- Added docs: `claude_tag_strategy.md`, `run_ledger.md`,
  `action_receipt_protocol.md`, `connector_capabilities.md`,
  `cli_reference.md`, `security_governance.md`, and an upstream research note.

## 0.1.0-deep-fork

- Fork-derived OS Tag product baseline from `zilliztech/mfs`.
- Added `FORK_ORIGIN.md` and migration report.
- Added Feishu/Lark adapter abstraction and dry-run fixtures.
- Added backend modules for mock, Codex CLI, Claude CLI, and OpenAI API.
- Added scoped MFS helper command construction.
- Added safety, audit, doctor, and live preflight checks.
- Added release check, Docker Compose, launchd deployment notes, and CI.
