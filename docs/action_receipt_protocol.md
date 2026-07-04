# Action Receipt Protocol

An action receipt represents a proposed or applied mutation against a target
system (requirement table, task list, issue tracker, chat platform). Receipts
are platform-independent: rich platforms may later render them as cards or
buttons, but every platform gets the plain text command fallback.

Core rule:

> No receipt reaches `applied` without passing through an approval-bearing
> state. There is no transition from `proposed` directly to `applying`.

## Schema

`ostag.core.schemas.ActionReceipt`:

| Field | Notes |
|---|---|
| `action_id` | `action_` + 12 hex chars |
| `run_id` | owning run |
| `title` | one-line human summary |
| `target_system` | e.g. `dingtalk_ai_table`, `feishu_base` |
| `target_object` | e.g. record id `REQ-12` |
| `mutation_type` | `create / update / delete / send / schedule / merge / assign` |
| `summary`, `diff` | optional detail |
| `risk` | `low / medium / high` |
| `required_permissions` | subset of `read / propose / write / admin` |
| `state` | see state machine |
| `verification` | list of `{name, status: pass/fail/skipped, evidence}` |
| `commands` | derived: canonical approve/reject/apply/continue/status strings |

## State machine

States: `proposed`, `needs_approval`, `ready_to_apply`, `applying`,
`applied`, `approved_only`, `rejected`, `blocked`, `stale`, `failed`.

Enforced transitions (`ostag.core.receipts.transition_receipt`):

| Verb | Allowed from | Result |
|---|---|---|
| `approve` | `proposed`, `needs_approval` | `ready_to_apply` |
| `reject` | `proposed`, `needs_approval`, `ready_to_apply` | `rejected` |
| `apply` | `ready_to_apply` | `applying` |

- `mark_applied()` moves `applying → applied` only.
- `mark_stale()` moves any non-terminal receipt to `stale`; `applied`,
  `rejected`, and `failed` are preserved.
- Any verb against a `stale` receipt fails with code `stale_receipt` and the
  hint to re-propose.
- Invalid transitions raise `SchemaError` with `code=invalid_transition` and a
  recovery hint containing the `status <run_id>` command.
- `is_stale(receipt, ttl_seconds=...)` implements TTL-based staleness for
  pending receipts.

## Text commands

Parsed by `parse_receipt_command()` from source-thread replies:

```text
approve <n|action_id>
reject <n|action_id>
apply <n|action_id>
continue <n|action_id|run_id>
status <n|action_id|run_id>
```

`<n>` is the numbered index shown in the rendered receipt list. A leading
`@botname` mention is tolerated. Anything else returns `None` — normal chat is
never mistaken for a command.

## Renderers

- `render_receipt_text(receipt, index=1)` — universal plain text fallback,
  includes target, mutation, risk, state, required permissions, and the exact
  reply commands.
- `render_receipt_json(receipt)` — compact dict for agents; null/empty fields
  pruned.

Example text rendering:

```text
[1] 更新需求 REQ-12 owner
目标：dingtalk_ai_table / REQ-12
变更：update｜风险：低｜状态：等待审批
所需权限：propose
说明：把 owner 从空改为 @小王
回复：approve 1 / reject 1
查询：status run_9
```

## Acceptance criteria

- Platforms without buttons get the text command fallback. ✅
- All writes can be represented as receipts (schema covers create/update/
  delete/send/schedule/merge/assign). ✅
- No apply without approval — enforced at the state machine level and tested
  in `tests/test_receipts.py`. ✅
- Stale, rejected, and applied state handling covered by tests. ✅
