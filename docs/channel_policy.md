# Per-Channel Policy

Governance policy can be scoped to a single channel/chat, layered over the
global config. Channel policy lives in the channel's brain file
(`<memory_root>/<platform>/<chat_id>/brain.json`) — the same scoped,
inspectable, editable store as channel facts.

## Resolution model

Three-valued override with inherit-by-default:

| Field | Channel values | Effective value |
|---|---|---|
| `governed_writes` | `true` / `false` / `null` (inherit) | channel value, else `OSTAG_GOVERNED_WRITES` |
| `apply_enabled` | `true` / `false` / `null` (inherit) | channel value, else `OSTAG_APPLY_ENABLED` |
| `actor_permissions` | `["actor:level", ...]` | channel grant wins over global `OSTAG_ACTOR_PERMISSIONS`, else global, else default level |
| `ambient_enabled` | `true` / `false` | channel value (ambient is opt-in per channel, as before) |
| `approval_quorum_high_risk` | integer / `null` (inherit) | channel value, else `OSTAG_APPROVAL_QUORUM_HIGH_RISK` (default 1) — distinct approvers required for `risk: high` receipts |

This lets one workspace run mixed modes: e.g. the finance channel requires
approvals with applies off (`governed_writes=true, apply_enabled=false`)
while an engineering sandbox channel keeps direct writes.

Channel grants are scoped: `ou_carol:write` in channel A gives Carol approve/
apply rights **only in channel A** — in other channels she falls back to the
global mapping or default level.

## CLI

```bash
ostag channel policy show --platform feishu --chat oc_xxx --json
# {"channel": {"governed_writes": true, "apply_enabled": false,
#   "actor_permissions": ["ou_alice:write"], ...},
#  "effective": {"governed_writes": true, "apply_enabled": false, ...}}

ostag channel policy set --platform feishu --chat oc_xxx \
  --governed-writes true --apply-enabled false \
  --grant ou_alice:write --json

ostag channel policy set --platform feishu --chat oc_xxx \
  --governed-writes inherit --revoke ou_alice --json   # back to global
```

`--grant` is repeatable; `--revoke <actor>` removes a channel grant. Output
always shows both the stored channel values and the resolved effective
values, so an admin can see exactly what will happen in that channel.

## Enforcement points

- `AutoBaseWorkflow` — governed-writes decision per event channel (both the
  requirement-patch path and the docx bulk-import path).
- `ApprovalService.execute` — apply gate and actor permission resolution use
  the channel policy of the receipt's chat when platform+chat are known
  (chat commands always are; CLI verbs pass `--chat` for channel scoping).
- Existing channel policy fields (task creation, writable fields, budgets,
  ambient) are unchanged.

## Compatibility

Legacy `brain.json` files without the new fields load as inherit (`null`) —
behavior is identical to before until an admin sets an override.
