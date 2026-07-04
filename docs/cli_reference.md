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

## Run inspection

```bash
ostag status --run run_9f8e7d6c5b4a --json
# {"event_count": 4, "run_id": "run_9f8e7d6c5b4a", "status": "succeeded", ...}

ostag ledger export --run run_9f8e7d6c5b4a --json
# [ {run_created}, {run_status_changed}, ..., {run_finished} ]

ostag artifacts list --run run_9f8e7d6c5b4a --json
# [ {"artifact_id": "art_...", "name": "report.md", "path": "/tmp/report.md", ...} ]
```

Options: `--config <path>` to point at an alternative `.env`; the ledger path
comes from `OSTAG_RUN_LEDGER_PATH` (or is derived from the audit log
directory).

## Connector inspection

```bash
ostag connector list --json
ostag connector list --platform dingtalk --json
```

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

`ostag run` (submit work from CLI), `ostag approve/reject/apply/continue`
(receipt verbs from CLI, sharing `parse_receipt_command` semantics),
`ostag context export --run`, `ostag channel policy show/set`,
`ostag connector test <platform>`, `ostag ambient run-once`.
