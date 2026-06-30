# OS Tag Learning Benchmark osstack Execution Board

Status: Active
Date: 2026-06-30
Source spec: `docs/ostag/learning_benchmark_plan.md`

## Operating Rule

Run one active task at a time. Keep future tasks in `tasks/paused/` while
osstack executes the current phase. This prevents the queue runner from
attempting dependent phases out of order.

## Task Board

| Phase | osstack task id | Priority | Capability | Status | Required gate |
|---|---:|---:|---|---|---|
| Phase 1: Channel Brain | `task_5a0d4f86` | 100 | `code.feature` | completed | Channel brain store/policy resolver exist; channel isolation tests pass; memory status reply works. |
| Phase 2: Stage Ledger | `task_c04abd42` | 95 | `code.feature` | active | Requirement operations emit staged success/failure ledger; tests pass. |
| Phase 3: Connector Layer | `task_49e1f4d9` | 90 | `code.refactor` | queued | Base/task CLI calls are behind connector interfaces; contract tests pass; behavior preserved. |
| Phase 4: Governance Policy | `task_07fd8203` | 85 | `code.feature` | queued | Per-channel readable/writable fields, task creation, confirmation, budget, and tool/backend policy tests pass. |
| Phase 5: Ambient Mode v1 | `task_6564e35f` | 80 | `code.feature` | queued | Ambient reminders are opt-in, rate-limited, proposal-only, and audited. |
| Phase 6: Slack Parity | `task_227a5da7` | 75 | `code.feature` | queued | Slack can use Channel Brain and governed requirement operations in tested fixtures. |
| Phase 7: Benchmark Gate | `task_93596cb6` | 70 | `code.test` | queued | `scripts/osstack_learning_benchmark_verify.sh` generates `release/learning-benchmark.md` with a score. |

## Global Verification

Every completed phase must pass:

```bash
uv run pytest
python -m compileall ostag
```

The final phase must additionally pass:

```bash
scripts/osstack_learning_benchmark_verify.sh
scripts/release_check.sh
```

## Scoring Gate

The final release candidate must score at least 90/100 in
`release/learning-benchmark.md` and must have zero hard blockers:

- no default ambient write without opt-in;
- no cross-channel memory leakage;
- no writes to the wrong Base/table;
- no duplicate task creation without idempotency;
- no secret or raw private log exposure in user replies.

## Live Deployment Gate

After implementation and automated verification:

1. Restart `ai.ostag.feishu` launchd service.
2. Verify Feishu group status reply.
3. Verify read-only Base search.
4. Run one controlled requirement operation only if the user explicitly asks
   for a live write validation.
