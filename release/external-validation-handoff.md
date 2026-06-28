# OS Tag External Validation Handoff

- Generated at: `2026-06-28T13:43:42Z`
- Launch status: `blocked_external`
- Launch exit code: `3`
- MFS mode: `required`

## Blocking Conditions

- Feishu/Lark manual live validation is not complete.
- Live MFS smoke is not complete or is intentionally out of production scope.

## Operator Commands

```bash
scripts/deploy_preflight.sh
scripts/live_validation_record.sh feishu
scripts/validate_live_validation_record.sh feishu
scripts/mfs_validation_record.sh
scripts/validate_mfs_validation_record.sh
scripts/launch_status.sh
```

## Evidence To Fill

- Feishu/Lark app id prefix, never the secret.
- Test chat id.
- Incoming message id.
- Backend used, starting with `mock`.
- Audit log path and matching timestamp.
- Screenshot or transcript path.
- MFS production decision: keep `required` and pass live smoke, or explicitly record `excluded` with owner and rationale.

## Completion Rule

`scripts/launch_status.sh` must exit `0` before OS Tag can be called production-candidate.
