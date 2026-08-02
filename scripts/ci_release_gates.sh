#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

scripts/deploy_preflight.sh

uv run pytest -q tests/test_connector_diagnostics.py tests/test_connector_live_smoke.py
uv run pytest -q tests/test_provider_evidence.py tests/test_provider_evidence_live_smoke.py

set +e
scripts/validate_live_validation_record.sh feishu >/tmp/ostag-ci-live-gate-blocked.out 2>&1
blocked_code=$?
set -e
if [[ "$blocked_code" -ne 0 && "$blocked_code" -ne 3 ]]; then
  cat /tmp/ostag-ci-live-gate-blocked.out >&2
  echo "expected live validation gate to pass or report missing credentials" >&2
  exit 1
fi

fixture_dir="release/live-validation-gate-fixture"
mkdir -p "$fixture_dir"
audit_path="$fixture_dir/audit.log"
transcript_path="$fixture_dir/transcript.txt"
record_fixture="$fixture_dir/live-validation-feishu-pass.md"

printf '{"status": "ok", "event_id": "evt_ci_live_validation"}\n' > "$audit_path"
printf 'OS Tag replied in the Feishu test group.\n' > "$transcript_path"

cat > "$record_fixture" <<EOF
# OS Tag Live Validation Record

- Platform: \`feishu\`
- Generated at: \`2026-06-28T00:00:00Z\`
- App id prefix: \`ci_test...\`
- Doctor exit code: \`0\`
- Status: \`manual_group_validation_passed\`

## Manual Group Test

- Test chat id: oc_ci_live_validation
- Incoming message id: om_ci_live_validation
- Backend: mock
- Audit log path: $audit_path
- Audit timestamp: 2026-06-28T00:00:01Z
- Result: pass
- Screenshot/transcript: $transcript_path
EOF

scripts/validate_live_validation_record.sh feishu "$record_fixture" \
  >/tmp/ostag-ci-live-gate-pass.out
rg -q "live validation record passed" /tmp/ostag-ci-live-gate-pass.out

handoff="release/manual-live-validation-handoff.md"
test -f "$handoff"
rg -q "scripts/deploy_preflight.sh" "$handoff"
rg -q "scripts/validate_live_validation_record.sh feishu" "$handoff"
rg -q "OSTAG_BACKEND=mock scripts/live_smoke.sh feishu" "$handoff"
rg -q "manual_group_validation_passed" "$handoff"

echo "ci release gates passed"
