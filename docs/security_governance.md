# Security And Governance

OS Tag is safe by default: read-only/propose unless policy grants write, no
destructive action without approval, no secret logging, full audit trail.

## Enforced today

| Rule | Enforcement |
|---|---|
| No secret logging | `redact_secrets()` applied to audit log previews/errors and recursively to every string in run ledger payloads (`ostag/core/safety.py`, `ostag/core/run_ledger.py`) |
| Default propose mode | Every `ConnectorCapability` declares `default_mode: propose` and `destructive_requires_approval: true` |
| No apply without approval | Receipt state machine: `apply` only from `ready_to_apply`, which is only reachable via `approve` (`ostag/core/receipts.py`) |
| Stale approvals rejected | `stale` receipts refuse all verbs with code `stale_receipt`; TTL check via `is_stale()` |
| No delete on tables | Table connectors declare `can_delete_records: false` |
| No shell by default | `OSTAG_ALLOW_SHELL=false` default; CLI backends require explicit `*_UNSAFE_MODE` flags |
| Scoped context | MFS access restricted to `MFS_ALLOWED_SCOPES` (`validate_scope`) |
| Audit trail | Audit log (JSONL) + run ledger (JSONL) written for every handled event; prod mode hashes input instead of storing previews |
| Input hygiene | `sanitize_user_text` strips control/bidi characters and redacts secrets before processing; `OSTAG_MAX_INPUT_CHARS` bounds input |
| Actor permission model | `PermissionGrant` with ordered levels `read < propose < write < admin` and `allows()` check (schema level; enforcement wiring is a next phase) |

## Governance data model

- `Run` — who asked for what, where, with what outcome.
- `LedgerEvent` — immutable audit records including `security_event`.
- `ActionReceipt` — target, mutation, risk, required permissions, state.
- `ApprovalDecision` — who approved/rejected which action and why.
- `ApplyPlan` — ordered steps plus rollback hint; `requires_approval` defaults
  to true.
- `PermissionGrant` — per actor/platform/channel permission level with
  expiry field.
- `ConnectorCapability` — what each platform may do, with policy defaults.

## Error contract

All schema/protocol violations raise `SchemaError` carrying `code`,
`message`, and `recovery_hint`. The CLI surfaces them as structured JSON and
exit code 1 — agents never need to parse prose.

## Automatic failure conditions (release gate)

- secret leaked to logs or repo;
- destructive write without explicit approval;
- applied action without ledger evidence;
- existing Slack/Feishu/Lark/DingTalk flows broken;
- tests fail.

`scripts/release_check.sh` and `scripts/secret_hygiene_check.sh` must pass
before release.

## Next hardening phase

- Wire `PermissionGrant` checks into the runner/workflows (read-only actor
  cannot approve or apply).
- Approval recording (`ApprovalDecision`) written to the run ledger when
  receipt verbs execute in live flows.
- Sandbox/worktree execution mode for code-writing executors.
- Environment scrub before executor invocation.
- Repo write-access checks before any VCS-facing apply.
