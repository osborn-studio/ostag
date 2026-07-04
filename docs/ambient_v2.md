# Ambient Mode V2 — Generalized Monitors

Ambient mode watches the channel's requirement table and proposes follow-ups.
It never writes; every message cites why it triggered; noise is bounded by
rate limits and per-record suppression.

## Monitors

One decision per requirement, highest-priority issue first:

| Kind | Rule | Message cites |
|---|---|---|
| `unowned_requirement` | owner is empty | requirement title |
| `overdue_requirement` | due date < today and status not done | record id, due date, current status |
| `stale_requirement` | days stale ≥ `OSTAG_AMBIENT_STALE_DAYS` (7) and not done | days stale + threshold |
| `missing_status` | status is empty (owner present) | record id |

Done statuses (`已完成/完成/done/completed`) are always quiet.

## Noise controls

- **Opt-in**: channel policy `ambient_enabled` must be true (unchanged).
- **Daily rate limit**: at most `daily_limit` proposal sweeps per channel per
  day (unchanged, default 3).
- **Per-sweep cap**: at most `OSTAG_AMBIENT_MAX_DECISIONS` (3) findings per
  sweep; multiple findings are joined into one message.
- **Suppression**: a (kind, record) pair that was already flagged is not
  re-flagged for `OSTAG_AMBIENT_SUPPRESS_DAYS` (3) days — the same unowned
  requirement does not get nagged every sweep.
- **Proposal-only**: decisions never mutate the system of record.

## Audit

Every sweep that produces decisions is recorded in the run ledger as a run
(`executor_id: ambient`, actor `ambient_scheduler`) with one
`ambient_decision` event per finding carrying `kind`, `record_id`, `rule`,
`message`, and `proposal_only: true`. Inspect with:

```bash
ostag runs list --json                     # ambient runs appear here
ostag ledger export --run run_xxx --json  # per-decision evidence
```

## CLI

```bash
ostag ambient run-once --json          # dry: evaluates and prints messages
ostag ambient run-once --send --json   # delivers via the platform adapter
```

Dry mode (default) collects the messages that would be sent into the JSON
output (`delivery: "dry_run"`) — nothing reaches any chat. `--send` uses the
configured platform adapter and requires live credentials.

## Acceptance criteria (strategy §15 Phase 5)

- No ambient messages when disabled. ✅ (opt-in per channel + scheduler gate)
- Daily digest sends once per window. ✅ (unchanged)
- Every ambient message cites source data. ✅ (record id / rule / threshold)
- No automatic destructive write. ✅ (proposal-only, enforced and audited)
