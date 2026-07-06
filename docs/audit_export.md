# Audit Export

`ostag audit export` folds the append-only run ledger into a single,
filterable bundle answering the admin question *"what did OS Tag do, for
whom, at what cost?"* — the "view logs by actor/channel/run" capability shared
AI teammates need. Because secrets are redacted at ledger-write time, the
export is safe to hand to auditors as-is.

## Command

```bash
ostag audit export [--since YYYY-MM-DD] [--until YYYY-MM-DD] \
  [--platform feishu|lark|slack|dingtalk|github|gitlab] [--chat <id>] \
  [--output path.json] [--json]
```

- `--since` / `--until` filter runs by their creation date (UTC, inclusive).
- `--platform` / `--chat` scope to one platform or channel.
- `--output` writes the bundle to a file (pretty JSON) and prints a short
  confirmation; without it the bundle is printed.

## Bundle shape

```json
{
  "generated_at": "2026-07-05T12:00:00+00:00",
  "filters": {"platform": "feishu", "since": "2026-07-01"},
  "summary": {
    "runs": 2,
    "runs_by_status": {"succeeded": 1, "failed": 1},
    "cost_credits": 5,
    "receipts": 1,
    "receipts_by_state": {"applied": 1},
    "approvals": {"approve": 1, "reject": 0},
    "security_events": 1,
    "ambient_decisions": 0
  },
  "runs": [ {run snapshot + "cost_credits": N} ... ],
  "security_events": [ {"run_id", "reason", "created_at"} ... ]
}
```

Each run snapshot carries its source (platform/channel/actor), intent,
status, context packet id, and the credits it cost. Receipt states, approval
decisions, costs, and security events are aggregated only over the runs that
pass the filters.

## Acceptance criteria

- Every run/receipt/approval/cost/security event in range is accounted for.
- Filtering by date range, platform, and channel is exact and composable.
- Secrets never appear (redacted upstream at ledger write).
- An empty ledger yields a well-formed, zeroed bundle.
- Covered in `tests/test_audit_export.py`.
