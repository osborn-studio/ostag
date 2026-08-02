#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANAGEMENT_ROOT="${OSTAG_MANAGEMENT_ROOT:-$ROOT}"
cd "$ROOT"

report="${OSTAG_READINESS_REPORT:-release/readiness-audit.md}"
mkdir -p "$(dirname "$report")"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

pass_count=0
fail_count=0
warn_count=0
rows=()

add_row() {
  local status="$1"
  local requirement="$2"
  local evidence="$3"
  rows+=("| $status | $requirement | $evidence |")
  case "$status" in
    PASS) pass_count=$((pass_count + 1)) ;;
    FAIL) fail_count=$((fail_count + 1)) ;;
    WARN) warn_count=$((warn_count + 1)) ;;
  esac
}

check_cmd() {
  local requirement="$1"
  local evidence="$2"
  shift 2
  if "$@" >"$tmpdir/check.log" 2>&1; then
    add_row "PASS" "$requirement" "$evidence"
  else
    add_row "FAIL" "$requirement" "$evidence; see command output in terminal"
  fi
}

check_cmd "Fork-derived git history exists" \
  "\`FORK_ORIGIN.md\` preserves the upstream URL and pinned baseline in product history" \
  bash -c 'test -s FORK_ORIGIN.md && rg -q "https://github.com/zilliztech/mfs" FORK_ORIGIN.md && rg -q "16574f9b365bc67f3716c552dad416da730088b8" FORK_ORIGIN.md && git log -1 --format=%H -- FORK_ORIGIN.md | rg -q .'

check_cmd "Open Tag migration decisions are recorded" \
  "\`docs/fork_migration_report.md\` maps upstream files to ported/rewritten/deferred decisions" \
  bash -c 'test -s docs/fork_migration_report.md && rg -q "\| .* \| (ported|rewritten|deferred|deleted) \|" docs/fork_migration_report.md'

check_cmd "OS Tag product structure exists" \
  "\`ostag/\`, adapters, backends, MFS helpers, docs, examples, deploy, release, and tests exist" \
  bash -c 'test -d ostag/adapters && test -d ostag/backends && test -d ostag/mfs && test -d docs/ostag && test -d examples && test -d deploy && test -d release && test -d tests'

check_cmd "Required product files exist" \
  "\`README.md\`, \`pyproject.toml\`, \`.env.example\`, \`LICENSE\`, \`NOTICE\`, Docker and Compose files exist" \
  bash -c 'test -f README.md && test -f pyproject.toml && test -f .env.example && test -f LICENSE && test -f NOTICE && test -f Dockerfile && test -f docker-compose.yml'

check_cmd "Automated release checks pass" \
  "\`scripts/release_check.sh\` passes compile, pytest, doctor, and Feishu/Lark dry-runs" \
  scripts/release_check.sh

check_cmd "Prompt contract is migrated" \
  "\`ostag/core/prompt.py\` exists and its focused test module passes" \
  bash -c 'test -f ostag/core/prompt.py && test -f tests/test_prompt.py && uv run pytest tests/test_prompt.py'

check_cmd "Memory utility is migrated" \
  "\`ostag/mfs/memory.py\` exists and its focused test module passes" \
  bash -c 'test -f ostag/mfs/memory.py && test -f tests/test_memory.py && uv run pytest tests/test_memory.py'

check_cmd "MFS client subprocess integration is covered" \
  "\`tests/test_mfs_client.py\` exercises enabled MFS search through a fake CLI subprocess" \
  bash -c 'test -f tests/test_mfs_client.py && uv run pytest tests/test_mfs_client.py'

check_cmd "MFS validation record exists" \
  "\`release/mfs-validation.md\` records either a real MFS smoke pass or a structured external blocker" \
  bash -c 'scripts/mfs_validation_record.sh >/dev/null && test -f release/mfs-validation.md && rg -q "Status: `(mfs_smoke_passed|blocked_mfs_disabled|blocked_missing_mfs_bin|blocked_mfs_search_failed)`" release/mfs-validation.md'

set +e
scripts/validate_mfs_validation_record.sh >"$tmpdir/mfs-validation-gate.log" 2>&1
mfs_gate_code=$?
set -e
mfs_mode="$(uv run python - <<'PY'
import json
from pathlib import Path
record = Path("release/production-scope.json")
if record.exists():
    print(json.loads(record.read_text(encoding="utf-8")).get("mfs_mode", "required"))
else:
    print("required")
PY
)"
if [[ "$mfs_mode" == "excluded" ]]; then
  add_row "PASS" "Live MFS smoke validation gate" "MFS is explicitly excluded from the current production-candidate scope"
elif [[ "$mfs_gate_code" -eq 0 ]]; then
  add_row "PASS" "Live MFS smoke validation gate" "\`scripts/validate_mfs_validation_record.sh\` passed"
elif [[ "$mfs_gate_code" -eq 3 ]]; then
  add_row "WARN" "Live MFS smoke validation gate" "Externally gated: live MFS command/config is not available in this environment"
else
  add_row "FAIL" "Live MFS smoke validation gate" "\`scripts/validate_mfs_validation_record.sh\` failed"
fi

check_cmd "Deployment preflight passes" \
  "\`scripts/deploy_preflight.sh\` validates release, Compose, launchd, and live-validation record generation" \
  scripts/deploy_preflight.sh

check_cmd "Live validation is credential-gated" \
  "\`release/live-validation-feishu.md\` exists and records pass or blocked_missing_credentials status" \
  bash -c 'test -f release/live-validation-feishu.md && rg -q "Status: `(blocked_missing_credentials|preflight_passed_manual_group_test_required|manual_group_validation_passed)`" release/live-validation-feishu.md'

check_cmd "Connector live smoke evidence is structured" \
  "Feishu, DingTalk, GitHub, and GitLab evidence records are present and schema-valid" \
  bash -c 'for platform in feishu dingtalk github gitlab; do file="release/connector-live-${platform}.json"; test -s "$file" && uv run python -c '\''import json,sys; data=json.load(open(sys.argv[1])); assert data["schema_version"] == 1 and data["platform"] == sys.argv[2] and data["mode"] == "live" and isinstance(data["read_ready"], bool)'\'' "$file" "$platform" || exit 1; done'

check_cmd "Provider completion evidence is structured" \
  "GitHub and GitLab PR/MR evidence records contain ready evidence or an explicit blocker" \
  bash -c 'for platform in github gitlab; do file="release/provider-evidence-${platform}.json"; test -s "$file" && uv run python -c '\''import json,sys; data=json.load(open(sys.argv[1])); assert data["schema_version"] == 1 and data["platform"] == sys.argv[2] and data["status"] in {"ready", "blocked"}'\'' "$file" "$platform" || exit 1; done'

set +e
scripts/validate_live_validation_record.sh feishu >"$tmpdir/live-validation-gate.log" 2>&1
live_gate_code=$?
set -e
if [[ "$live_gate_code" -eq 0 ]]; then
  add_row "PASS" "Manual live validation gate" "\`scripts/validate_live_validation_record.sh feishu\` passed"
elif [[ "$live_gate_code" -eq 3 ]]; then
  add_row "WARN" "Manual live validation gate" "Credential-gated: validation record is structurally blocked until live credentials are present"
else
  add_row "FAIL" "Manual live validation gate" "\`scripts/validate_live_validation_record.sh feishu\` failed"
fi

if rg -q 'Status: `blocked_missing_credentials`' release/live-validation-feishu.md; then
  add_row "WARN" "Manual live group validation" "Blocked by missing Feishu/Lark credentials; preflight documents missing variables"
else
  check_cmd "Manual live group validation" \
    "\`release/live-validation-feishu.md\` includes a non-empty manual result" \
    bash -c 'rg -q "Result: *(pass|PASS|passed|Passed)" release/live-validation-feishu.md'
fi

check_cmd "osstack state records phase execution" \
  "\`LOOP-STATE.json\` is present and structurally valid at the configured management root" \
  bash -c 'test -s "$0/LOOP-STATE.json" && uv run python -m json.tool "$0/LOOP-STATE.json" >/dev/null' "$MANAGEMENT_ROOT"

check_cmd "osstack task evidence is retained" \
  "P0, P1, and split P2 review tasks exist and have successful outcomes in \`LOOP-STATE.json\`" \
  bash -c 'for id in task_37dce3d6 task_acaade5e task_df75534b task_a54c03d5; do test -s "$0/tasks/$id.json" || exit 1; done; uv run python -c '\''import json,sys; data=json.load(open(sys.argv[1], encoding="utf-8")); completed={item.get("task_id") for item in data.get("completed_tasks", []) if item.get("outcome") == "success"}; required={"task_37dce3d6", "task_acaade5e", "task_df75534b", "task_a54c03d5"}; assert required <= completed'\'' "$0/LOOP-STATE.json"' "$MANAGEMENT_ROOT"

check_cmd "Scorecard is current" \
  "\`release/ostag-scorecard.md\` reports 96/100 pilot-ready status" \
  bash -c 'rg -q "Total: `96 / 100`" release/ostag-scorecard.md && rg -q "Pilot-ready: pass" release/ostag-scorecard.md'

gate_status="PASS"
if [[ "$fail_count" -gt 0 ]]; then
  gate_status="FAIL"
elif [[ "$warn_count" -gt 0 ]]; then
  gate_status="PASS_WITH_EXTERNAL_BLOCKER"
fi

{
  echo "# OS Tag Release Readiness Audit"
  echo
  echo "- Generated at: \`$(date -u +"%Y-%m-%dT%H:%M:%SZ")\`"
  echo "- Fork root: \`$ROOT\`"
  echo "- Management root: \`$MANAGEMENT_ROOT\`"
  echo "- Gate status: \`$gate_status\`"
  echo "- Checks: \`$pass_count passed / $warn_count warnings / $fail_count failed\`"
  echo
  echo "## Requirement Results"
  echo
  echo "| Status | Requirement | Evidence |"
  echo "|---|---|---|"
  printf '%s\n' "${rows[@]}"
  echo
  echo "## Launch Decision"
  echo
  if [[ "$gate_status" == "PASS" ]]; then
    echo "Automated and manual launch evidence are complete."
  elif [[ "$gate_status" == "PASS_WITH_EXTERNAL_BLOCKER" ]]; then
    echo "Automated gates pass. Production-candidate status is externally blocked by missing live Feishu/Lark credentials, manual group validation evidence, or live MFS smoke configuration."
  else
    echo "Release is not ready; fix failed checks before launch."
  fi
} > "$report"

echo "$report"
if [[ "$fail_count" -gt 0 ]]; then
  exit 2
fi
