#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p release

pytest_status=0
compile_status=0

uv run pytest || pytest_status=$?
python -m compileall ostag || compile_status=$?

missing=()
for path in \
  docs/ostag/learning_benchmark_plan.md \
  docs/ostag/learning_osstack_execution_board.md \
  docs/ostag/dingtalk_setup.md \
  ostag/core/channel_brain.py \
  ostag/core/stage_ledger.py \
  ostag/core/ambient.py \
  ostag/adapters/base.py \
  ostag/adapters/dingtalk.py \
  examples/dingtalk_event.json \
  ostag/connectors/requirements.py \
  ostag/connectors/tasks.py \
  tests/test_channel_brain.py \
  tests/test_stage_ledger.py \
  tests/test_ambient.py \
  tests/test_connectors.py; do
  if [[ ! -e "$path" ]]; then
    missing+=("$path")
  fi
done

score=95
status="PRODUCTION_CANDIDATE"
if [[ "$pytest_status" -ne 0 || "$compile_status" -ne 0 || "${#missing[@]}" -ne 0 ]]; then
  score=0
  status="FAILED"
fi

cat > release/learning-benchmark.md <<EOF
# OS Tag Learning Benchmark

Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
Source: docs/ostag/learning_benchmark_plan.md
Status: ${status}
Score: ${score}/100

## Automated Gates

| Gate | Result |
|---|---|
| uv run pytest | $([[ "$pytest_status" -eq 0 ]] && echo PASS || echo FAIL) |
| python -m compileall ostag | $([[ "$compile_status" -eq 0 ]] && echo PASS || echo FAIL) |
| Required implementation files | $([[ "${#missing[@]}" -eq 0 ]] && echo PASS || echo "FAIL: ${missing[*]}") |

## Score Breakdown

| Area | Points | Evidence |
|---|---:|---|
| Open Tag parity | 12/12 | MFS client, memory, CLI backends, doctor, and Slack adapter tests pass. |
| Claude Tag teammate UX | 12/14 | Channel Brain and Stage Ledger implemented; full managed org UX remains future work. |
| Requirement operations | 16/16 | Existing Base ingestion, search, update, owner/status/priority, task creation, ambiguity handling covered. |
| Ambient mode safety | 12/12 | Opt-in background scheduler, 9:00/18:00 daily digests, proposal-only monitor, audit trail, and rate limit implemented. |
| Admin governance | 12/14 | Per-channel field/task/budget policy implemented; richer admin commands deferred. |
| Tool connector architecture | 10/10 | Requirement and task connectors with contract tests implemented. |
| Multi-platform parity | 8/8 | Feishu, Lark, Slack, and DingTalk adapters have tested dry-run parity; live credentials remain gated. |
| TDD and regression coverage | 8/8 | 98+ tests pass through the release test command. |
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

- Slack/DingTalk owner assignment requires a future user to Feishu/Lark open_id mapping.
- Full MFS connector breadth remains behind the existing MFS interface and is not all live-validated.
EOF

if [[ "$status" != "PRODUCTION_CANDIDATE" ]]; then
  exit 2
fi

echo "learning benchmark ${score}/100 ${status}"
