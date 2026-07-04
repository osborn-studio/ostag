# Run Ledger

The run ledger is OS Tag's durable, append-only audit store. Every handled
inbound event creates a `Run` and appends immutable `LedgerEvent` records to a
local JSONL file. The ledger is the source of truth for run status, artifacts,
and audit export.

## Storage

- Format: JSONL, one `LedgerEvent` per line, keys sorted, UTF-8.
- Default path: `./logs/run_ledger.jsonl`.
- Override with `OSTAG_RUN_LEDGER_PATH`. When unset, the path is derived from
  the audit log directory (`OSTAG_AUDIT_LOG`'s parent), so audit log and run
  ledger always live together.
- All string payload values pass through `redact_secrets()` before they touch
  disk. Secrets never enter the ledger.

## Event types

`run_created`, `run_status_changed`, `context_packet_built`,
`executor_invoked`, `action_proposed`, `action_state_changed`,
`artifact_recorded`, `approval_recorded`, `apply_started`, `apply_finished`,
`run_finished`, `security_event`.

## Run lifecycle

Statuses: `queued → admitted → running → waiting_approval | blocked →
succeeded | failed | cancelled`.

Current wiring in `OSTagRunner.handle_event`:

1. A mention event opens a run (`run_created`, status `admitted`).
2. Status moves to `running`.
3. Every exit path records a terminal `run_finished` event with `succeeded`
   or `failed` (rejected input, workflow failure, and backend errors are
   `failed` with a redacted detail).

Non-mention events are intentionally **not** ledgered as runs: they are noise
in busy group chats and are already counted in the audit log with status
`ignored`.

## Example record

```json
{"created_at": "2026-07-04T08:00:00+00:00", "event_id": "evt_1a2b3c4d5e6f",
 "event_type": "run_created", "payload": {"run": {"run_id": "run_9f8e7d6c5b4a",
 "source": {"platform": "dingtalk", "channel_id": "cid_demo", "message_id": "m1"},
 "actor": {"actor_id": "user_1", "permission_level": "propose"},
 "intent": {"raw_text": "更新需求表", "normalized_type": "chat", "confidence": 1.0},
 "status": "admitted", "executor_id": "mock",
 "created_at": "2026-07-04T08:00:00+00:00", "updated_at": "2026-07-04T08:00:00+00:00"}},
 "run_id": "run_9f8e7d6c5b4a"}
```

## Inspection

```bash
ostag status --run run_9f8e7d6c5b4a --json     # latest reconstructed snapshot
ostag ledger export --run run_9f8e7d6c5b4a --json  # every event, append order
ostag artifacts list --run run_9f8e7d6c5b4a --json # artifact records only
```

Python API: `ostag.core.run_ledger.RunLedger` —
`open_run`, `append`, `record_status`, `record_artifact`, `export_run`,
`run_status`, `list_artifacts`, `list_runs`.

## Acceptance criteria

- Every handled (mention) inbound event creates a run id. ✅ tested in
  `tests/test_run_ledger.py`.
- Every run has source, actor, intent, status, and final outcome. ✅
- Secrets are redacted before write. ✅ tested with OpenAI/Slack token
  patterns.
- Ledger lines are stable sorted-key JSON suitable for agent parsing. ✅
