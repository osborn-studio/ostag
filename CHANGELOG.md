# Changelog

## Unreleased

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
