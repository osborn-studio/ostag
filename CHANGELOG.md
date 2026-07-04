# Changelog

## Unreleased

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
