# Cost And Budget Governance

OS Tag can cap how much a channel spends per day, matching the admin
spend-limit model that shared AI teammates need. Accounting lives in the run
ledger, so it is auditable and survives restarts.

## Model

- **Credits** are an abstract integer unit the operator defines. Map them to
  cents, tokens/1k, or plain call counts — OS Tag only sums and compares.
- **Per-run cost** (`OSTAG_RUN_COST_CREDITS`, default 0): credits charged for
  a run that reaches the LLM backend. `0` disables cost accounting entirely
  (fully backward compatible). Receipt commands, brain queries, ambient
  status, and governed writes are not LLM calls and are never charged.
- **Daily budget** (`OSTAG_DAILY_COST_BUDGET`, default 0 = unlimited; channel
  override `daily_cost_budget`): the maximum credits a channel may spend per
  UTC day.

## Enforcement

Budget is checked **before** the paid call:

1. A run reaches the backend stage with cost `C`.
2. If the channel's spent-today `+ C` would exceed its effective budget, the
   run is **blocked** — the LLM is never called, so no cost is incurred. The
   run's ledger status becomes `blocked`; the source thread gets a clear
   reply with a recovery hint.
3. Otherwise the backend runs and `C` credits are charged (recorded as a
   `cost_recorded` ledger event attributed to the channel and date) —
   regardless of whether the backend answered or errored, because the call
   itself incurs the cost.

Budgets reset naturally: `cost_today` only sums the current UTC date.

## CLI

```bash
ostag budget status --platform feishu --chat oc_xxx --json
# {"daily_budget": 50, "spent_today": 10, "remaining": 40,
#  "unit": "credits", "unlimited": false, "run_cost": 10, "date": "2026-07-04"}

ostag channel policy set --platform feishu --chat oc_xxx \
  --daily-cost-budget 50 --json     # 0 = unlimited, 'inherit' = use global
```

The channel policy `show`/`set` output reports both the stored channel
`daily_cost_budget` and the resolved effective value.

## Resolution

`effective_budget = channel.daily_cost_budget if set else OSTAG_DAILY_COST_BUDGET`.
`0` at either level means unlimited. A channel can therefore cap spend even
when the global default is unlimited, and vice versa.

## Acceptance criteria

- Zero cost (default) changes nothing. ✅
- A run that would exceed the channel budget is blocked before the paid call,
  with no charge recorded. ✅
- Spend accumulates per channel per UTC day and is queryable. ✅
- Channel override beats the global default in both directions. ✅
- Covered in `tests/test_budget.py`.
