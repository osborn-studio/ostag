# CLI Reference

The `ostag` CLI serves both humans and agents. Every inspection command
supports `--json` for compact single-line output with sorted keys and pruned
null/empty fields. Errors are structured JSON with `code`, `message`, and
`recovery_hint`, and exit with code 1.

## Service / runtime

```bash
ostag --platform feishu --dry-run --event examples/feishu_event.json  # local fixture run
ostag --platform dingtalk --live                                      # live transport
ostag --doctor --platform feishu --dry-run --event examples/feishu_event.json
```

## Submitting work

```bash
ostag run --platform feishu --chat oc_xxx --text "请总结项目下一步" --json
# {"reply": "...", "run": {"run_id": "run_...", "status": "succeeded",
#  "context_packet_id": "ctx_...", ...}}
```

`ostag run` pushes a task through the full pipeline (safety checks, receipt
commands, channel brain, auto_base workflow, backend) exactly as if the bot
was mentioned in chat — but the reply is returned to the caller; **nothing is
posted to any platform**. Table writes still go through governed-writes
policy. Options: `--actor` (default `local-admin`), `--thread`, `--backend`.
Exit code is 0 only if the run succeeded.

## Run inspection

```bash
ostag status --run run_9f8e7d6c5b4a --json
# {"event_count": 4, "run_id": "run_9f8e7d6c5b4a", "status": "succeeded", ...}

ostag ledger export --run run_9f8e7d6c5b4a --json
# [ {run_created}, {run_status_changed}, ..., {run_finished} ]

ostag artifacts list --run run_9f8e7d6c5b4a --json
# [ {"artifact_id": "art_...", "name": "report.md", "path": "/tmp/report.md", ...} ]

ostag runs list [--limit 20] --json
# recent run snapshots, newest first

ostag context export --run run_9f8e7d6c5b4a --json
# {"context_packet": {"source_thread_summary": "...", "channel_brain_facts": [...],
#   "policy_constraints": {...}, "citations": ["feishu://oc_xxx/om_xxx"]},
#  "hash": "fe58..."}  # sha256/16 audit hash of the packet
```

Options: `--config <path>` to point at an alternative `.env`; the ledger path
comes from `OSTAG_RUN_LEDGER_PATH` (or is derived from the audit log
directory).

## Connector inspection

```bash
ostag connector list --json
ostag connector list --platform dingtalk --json
```

## Receipt verbs

```bash
ostag approve <n|action_id> [--actor U1] [--chat C1] --json
ostag reject  <n|action_id> [--actor U1] [--chat C1] --json
ostag apply   <n|action_id> [--actor U1] [--chat C1] --json
ostag continue <run_id|action_id> [--actor U1] --json
```

- `--actor` defaults to `local-admin`, which resolves to `admin` unless
  explicitly demoted via `OSTAG_ACTOR_PERMISSIONS` — the CLI runs on the
  operator's own machine. Pass `--actor <id>` to act as (and be permission-
  checked as) a specific actor.
- Numeric references resolve against pending receipts (scoped by `--chat`
  when given).
- `apply` requires `OSTAG_APPLY_ENABLED=true` and a registered executor;
  otherwise it returns `apply_disabled` / `no_apply_executor` errors and the
  approval is preserved.

## Receipt management

```bash
ostag receipts list [--chat C1] [--pending] --json
ostag receipts propose --file receipt.json [--chat C1] [--source-platform feishu] --json
```

`receipts propose` reads an `ActionReceipt` JSON object (see
`docs/action_receipt_protocol.md`) — this is the agent-facing entry point for
putting a mutation under governance from outside a chat thread.

## Follow-ups (scheduled continuations)

```bash
ostag followups list [--all] --json   # pending continuations, soonest first
ostag followups run-once --json       # fire due continuations now
```

`continue <run_id>` (chat or CLI) schedules a durable follow-up; governed
proposals auto-schedule a pending-approval reminder. See
`docs/async_runs.md`.

## Ambient

```bash
ostag ambient run-once --json          # dry sweep: prints would-be messages
ostag ambient run-once --send --json   # deliver via platform adapter
```

See `docs/ambient_v2.md` for monitors and noise controls.

## Budget

```bash
ostag budget status --platform feishu --chat oc_xxx --json
# {"daily_budget": 50, "spent_today": 10, "remaining": 40, "unit": "credits", ...}
```

Set caps via `ostag channel policy set --daily-cost-budget N|inherit`. See
`docs/cost_budget.md`.

## Audit export

```bash
ostag audit export --since 2026-07-01 --platform feishu --json
ostag audit export --chat oc_xxx --output audit.json --json
```

Consolidated bundle of runs, receipt states, approvals, costs, and security
events, filterable by date range / platform / channel. See
`docs/audit_export.md`.

## Channel policy

```bash
ostag channel policy show --platform feishu --chat oc_xxx --json
ostag channel policy set --platform feishu --chat oc_xxx \
  --governed-writes true|false|inherit --apply-enabled true|false|inherit \
  --ambient true|false --grant ou_alice:write --revoke ou_bob --json
```

Channel values override globals; `inherit` resets to the global config. See
`docs/channel_policy.md`.

## Channel memory (existing)

```bash
ostag memory show --platform feishu --chat-id oc_xxx
ostag memory path --platform feishu --chat-id oc_xxx
ostag memory remember --platform feishu --chat-id oc_xxx --text "..." [--sync]
ostag memory sync
```

## Error contract

```bash
ostag status --run run_nope --json
# {"error": {"code": "run_not_found",
#            "message": "No ledger events found for run_id='run_nope'",
#            "recovery_hint": "Check the run id with 'ostag ledger export' or
#             confirm the ledger path (OSTAG_RUN_LEDGER_PATH)."}}
# exit code 1
```

Agent-facing guarantees:

- `--json` output is one line, keys sorted, no null/empty noise;
- artifact locations are absolute paths or URLs;
- every error includes a recovery hint;
- all inspection commands are read-only.

## Planned next commands

`ostag connector test <platform>`, `ostag ambient run-once`,
`ostag artifacts get`.
