# Live Feishu/Lark Validation

Use this procedure to upgrade OS Tag from automated pilot-ready status to a
production-candidate score.

## Preconditions

- A Feishu or Lark self-built app exists.
- Bot capability is enabled.
- Message receive events are subscribed.
- The bot is visible to the target test group.
- The bot has been invited into the target group.
- `.env` contains the platform credentials.

## Preflight

Run:

```bash
scripts/live_validation_record.sh feishu
```

or:

```bash
scripts/live_validation_record.sh lark
```

The script writes `release/live-validation-<platform>.md`. If credentials are
missing, the record must show the missing variables and the validation remains
blocked.

## Manual Test

Start OS Tag in live mode:

```bash
scripts/live_smoke.sh feishu
```

In the Feishu group, send:

```text
@OS Tag 请总结这个项目下一步应该做什么
```

Expected result:

- The process receives a message event.
- The backend runs with the configured backend, starting with `mock`.
- The bot replies in the same group or message thread.
- `logs/audit.log` receives a JSONL row with `status=ok`.

## Evidence To Record

Add these fields to the generated validation record:

- App id prefix, not the full secret.
- Test chat id.
- Incoming message id.
- Backend used.
- Audit log path and matching timestamp.
- Result: pass/fail.
- Screenshot or transcript path if available.

Do not paste app secrets or full user tokens into the evidence file.

## Validation Gate

After filling the manual group test fields, run:

```bash
scripts/validate_live_validation_record.sh feishu
```

The gate passes only when:

- the record is not blocked by missing credentials;
- `Result:` is `pass`, `passed`, or `ok`;
- chat id, message id, backend, audit path, and audit timestamp are present;
- the audit log exists and contains a successful `status=ok` event;
- screenshot/transcript evidence exists when a path is provided.

## MFS Validation

Run the MFS gate before production-candidate promotion:

```bash
scripts/mfs_validation_record.sh
scripts/validate_mfs_validation_record.sh
```

The gate records a pass only when a live allowlisted `mfs search` succeeds. If
MFS is disabled or unavailable, the record remains a structured external
blocker rather than an implicit pass.
