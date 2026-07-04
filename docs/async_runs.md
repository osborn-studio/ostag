# Async Runs And Scheduled Continuations

OS Tag runs are durable: state, receipts, context packets, and scheduled
continuations all live in the run ledger, so they survive process restarts.
This document covers the continuation (follow-up) layer.

## Model

A continuation is a promise to come back to a run later. It is recorded as
ledger events:

- `continuation_scheduled` — `{continuation_id, due_at, kind, reason,
  chat_id, platform, action_id?}` on the run;
- `continuation_fired` — the follow-up message was delivered (or rendered);
- `continuation_cancelled` — the subject resolved before the due time, or the
  target vanished.

Kinds:

| Kind | Scheduled by | Fires as |
|---|---|---|
| `receipt_reminder` | automatically when a governed-write receipt is proposed (`OSTAG_FOLLOWUP_ENABLED`, default on) | reminder in the source chat with the receipt rendered and the reason cited |
| `run_follow_up` | explicitly, via `continue <run_id>` (chat or CLI) | run status summary in the source chat |

Default delay: `OSTAG_FOLLOWUP_AFTER_SECONDS` (14400 = 4h).

## Quietness guarantees

- A continuation fires **at most once**. Re-follow-up requires another
  explicit `continue <run_id>` — no automatic reminder loops.
- If the subject is already resolved when the sweep runs (receipt
  approved/rejected/applied/stale, run succeeded/failed/cancelled), the
  continuation is **cancelled**, not sent.
- A pending receipt that outlived `OSTAG_RECEIPT_TTL_SECONDS` is marked
  `stale` at sweep time and the reminder is cancelled.
- Every reminder message cites why it triggered and when it was scheduled.

## Execution

The sweep runs in two places:

1. **Ambient scheduler** — every ambient tick calls the follow-up sweep first
   (guarded so a sweep failure never kills the scheduler). Reminders are sent
   to the source chat through the platform adapter.
2. **CLI** — `ostag followups run-once --json` fires due continuations
   without a daemon; reminders are rendered in the output (`outcome:
   "rendered"`) since there is no chat transport.

```bash
ostag followups list --json           # pending continuations, soonest first
ostag followups list --all --json     # include fired/cancelled
ostag followups run-once --json       # fire what's due now
```

## Acceptance criteria

- Continuations survive restarts (ledger-backed). ✅
- `continue run_x` schedules a durable follow-up and reports the due time. ✅
- Resolved subjects never produce reminders. ✅
- One firing per continuation; no reminder loops. ✅
- All covered in `tests/test_followups.py`.
