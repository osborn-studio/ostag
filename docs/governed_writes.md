# Governed Writes

Governed writes route every auto_base table mutation through the action
receipt protocol: instead of writing immediately, OS Tag proposes a receipt in
the source thread, waits for `approve`, and only writes on `apply`.

## Compatibility and migration

**Default: off.** With `OSTAG_GOVERNED_WRITES=false` (the default), behavior
is unchanged — requirement patches and docx imports write directly, exactly as
before this feature. No migration is required to stay on direct writes.

To migrate a deployment to governed writes:

1. Set `OSTAG_GOVERNED_WRITES=true` — mutations become proposals; nothing is
   written until approved and applied.
2. Set `OSTAG_APPLY_ENABLED=true` — allows `apply` to execute approved
   receipts. Without it, approvals are recorded but applies are refused
   (`apply_disabled`), which is a valid dry-run rollout stage.
3. Grant write level to the humans who may approve/apply:
   `OSTAG_ACTOR_PERMISSIONS=ou_alice:write,ou_bob:admin`. Everyone else
   defaults to `OSTAG_DEFAULT_PERMISSION_LEVEL` (`propose`).

Recommended rollout: enable step 1 alone first (proposals visible, zero
writes), then steps 2–3 once the team is comfortable.

## What is governed

| Write path | Governed when enabled | Notes |
|---|---|---|
| Requirement patch (`base +record-batch-update`) | ✅ receipt (`mutation_type: update`, risk low) | includes owner/status/priority and task-link fields |
| Docx bulk import (`base +record-batch-create`) | ✅ receipt (`mutation_type: create`, risk medium) | the exact extracted rows are frozen in the payload |
| Feishu task creation | ➖ keeps its existing channel-policy gate (`task_create_requires_confirmation`) | receipt governance planned |

The receipt's `apply_payload` freezes the exact mutation at proposal time
(base token, table id, record id, patch / rows). Apply executes **exactly what
was approved** — nothing is recomputed between approval and apply.

## Flow

```text
用户: @OS Tag 请把 交易盯盘 分配给 @张三，状态改为 开发交付中
OS Tag: 已生成待审批操作（本群已开启写入审批）：
        [1] 更新需求「交易盯盘：行情异动提醒」
        目标：feishu_base / rec_001
        变更：update｜风险：低｜状态：等待审批
        回复：approve 1 / reject 1
写权限用户: @OS Tag approve 1
OS Tag: 已批准 action_xxx。…（状态：已批准待执行）
写权限用户: @OS Tag apply 1
OS Tag: 已执行 action_xxx。…（状态：已执行）
```

Every step leaves ledger evidence: `action_proposed`,
`action_state_changed`, `approval_recorded`, and apply evidence with the
updated record id.

## Executors

`ostag/connectors/apply.py` maps target systems to executors built from the
workflow's own connectors, so applies go through the same lark-cli/dws-cli
paths as direct writes:

- `feishu_base` / `lark_base` → `RequirementTableApplyExecutor` (lark-cli)
- `dingtalk_ai_table` → same executor over dws-cli

Unsupported or malformed payloads fail with `invalid_apply_payload` /
`unsupported_operation` and mark the receipt `failed` with ledger detail.
Deletes are not implemented anywhere in the executor — there is no governed
path to a table delete.
