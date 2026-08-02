# OS Tag Launch Status

- Generated at: `2026-08-02T07:37:18Z`
- Status: `blocked_external`
- Exit code: `3`
- Readiness gate: `PASS_WITH_EXTERNAL_BLOCKER`
- Readiness checks: `17 passed / 2 warnings / 0 failed`
- Completion gate: `PASS_WITH_EXTERNAL_BLOCKER`
- MFS mode: `excluded`
- Live validation exit code: `3`
- MFS validation exit code: `3`
- Effective MFS exit code: `0`

## Blocking Conditions

- Feishu/Lark manual live validation is not complete.

## Exit Code Contract

- `0`: production-candidate gates are complete.
- `2`: release failed an automated gate.
- `3`: automated gates pass but external live validation is blocked.
