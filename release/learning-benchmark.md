# OS Tag Learning Benchmark

Generated: 2026-07-01T00:39:52Z
Source: docs/ostag/learning_benchmark_plan.md
Status: PRODUCTION_CANDIDATE
Score: 95/100

## Automated Gates

| Gate | Result |
|---|---|
| uv run pytest | PASS |
| python -m compileall ostag | PASS |
| Required implementation files | PASS |

## Score Breakdown

| Area | Points | Evidence |
|---|---:|---|
| Open Tag parity | 12/12 | MFS client, memory, CLI backends, doctor, and Slack adapter tests pass. |
| Claude Tag teammate UX | 12/14 | Channel Brain and Stage Ledger implemented; full managed org UX remains future work. |
| Requirement operations | 16/16 | Existing Base ingestion, search, update, owner/status/priority, task creation, ambiguity handling covered. |
| Ambient mode safety | 12/12 | Opt-in background scheduler, 9:00/18:00 daily digests, proposal-only monitor, audit trail, and rate limit implemented. |
| Admin governance | 12/14 | Per-channel field/task/budget policy implemented; richer admin commands deferred. |
| Tool connector architecture | 10/10 | Requirement and task connectors with contract tests implemented. |
| Multi-platform parity | 7/8 | Slack Channel Brain and governed Base operations covered; Slack-to-Feishu identity mapping deferred. |
| TDD and regression coverage | 8/8 | 94+ tests pass through the release test command. |
| Security and audit | 4/4 | Default ambient writes disabled; audit and secret-redaction tests remain passing. |
| Documentation and osstack readiness | 2/2 | Benchmark plan, execution board, task queue, and handoff docs exist. |

## Hard Blocker Audit

| Blocker | Status |
|---|---|
| Default ambient write without opt-in | PASS: ambient mode defaults off and emits proposals only. |
| Cross-channel memory leakage | PASS: Channel Brain tests enforce platform/chat isolation. |
| Wrong Base/table writes | PASS: default Base/table resolves through channel policy or env; tests cover routing. |
| Duplicate task creation without idempotency | PASS: task connector uses idempotency keys. |
| Secret or raw private log exposure | PASS: existing audit/safety tests remain passing. |

## Remaining Non-Blocking Gaps

- Slack owner assignment requires a future Slack user to Feishu/Lark open_id mapping.
- Full MFS connector breadth remains behind the existing MFS interface and is not all live-validated.
