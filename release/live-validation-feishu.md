# OS Tag Live Validation Record

- Platform: `feishu`
- Generated at: `2026-08-02T07:36:59Z`
- Release commit: `a4eae2aaaef0cdfa0a4269467a36b083cfafc4d8`
- Release version: `0.5.0`
- App id prefix: `not set`
- Doctor exit code: `1`
- Status: `blocked_missing_credentials`
- Missing: `FEISHU_APP_ID FEISHU_APP_SECRET`

## Doctor Output

```text
[ok] python: Python 3.13; requires >=3.11
[ok] safety: dangerous shell and unsafe CLI flags are disabled
[ok] audit_log: audit log path is writable: logs/audit.log
[ok] backend: mock backend is ready
[ok] mfs: MFS disabled; context lookup will be skipped
[error] adapter: missing live credentials: FEISHU_APP_ID, FEISHU_APP_SECRET
```

## Manual Group Test

- Test chat id:
- Incoming message id:
- Backend:
- Audit log path:
- Audit timestamp:
- Result: pass|fail
- Screenshot/transcript:

## Notes

Do not record app secrets, tenant access tokens, or user tokens here.
