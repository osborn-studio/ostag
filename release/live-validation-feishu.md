# OS Tag Live Validation Record

- Platform: `feishu`
- Generated at: `2026-06-28T14:13:49Z`
- App id prefix: `cli_aab1...`
- Doctor exit code: `0`
- Status: `manual_group_validation_passed`

## Doctor Output

```text
[ok] python: Python 3.11; requires >=3.11
[ok] safety: dangerous shell and unsafe CLI flags are disabled
[ok] audit_log: audit log path is writable: logs/audit.log
[ok] backend: mock backend is ready
[ok] mfs: MFS disabled; context lookup will be skipped
[ok] adapter: feishu live adapter dependencies found
```

## Manual Group Test

- Test chat id: oc_1ae82ede7eb6fe9a2cf644b43b97611b
- Incoming message id: om_x100b6b26f5f0e0b8c39fb2c5768098e
- Backend: mock
- Audit log path: logs/audit.log
- Audit timestamp: 2026-06-28T14:12:04.331616+00:00
- Result: pass
- Screenshot/transcript: release/live-validation-feishu-transcript-20260628.txt

## Reply Evidence

- Bot reply message id: om_x100b6b26f5ad90a0c2b2918012c73c9
- Reply-to message id: om_x100b6b26f5f0e0b8c39fb2c5768098e
- Chat message position: 6

## Launchd Production Evidence

- LaunchAgent: ai.ostag.feishu
- LaunchAgent state: running
- Incoming message id: om_x100b6b26971d54a0c31106829e5085f
- Bot reply message id: om_x100b6b26973e08a4c03bc167d84d75b
- Reply-to message id: om_x100b6b26971d54a0c31106829e5085f
- Audit timestamp: 2026-06-28T14:20:13.538566+00:00
- Transcript: release/launchd-production-validation-20260628.txt

## Notes

Do not record app secrets, tenant access tokens, or user tokens here.
