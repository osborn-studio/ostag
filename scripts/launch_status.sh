#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p release
json_report="release/launch-status.json"
md_report="release/launch-status.md"

bash scripts/release_readiness_audit.sh >/dev/null
bash scripts/production_scope_record.sh >/dev/null
bash scripts/validate_production_scope.sh >/tmp/ostag-launch-scope-gate.out
uv run python scripts/generate_release_manifest.py >/dev/null
uv run python scripts/generate_completion_audit.py >/dev/null

readiness_gate="$(uv run python -c '
import re
from pathlib import Path
text = Path("release/readiness-audit.md").read_text(encoding="utf-8")
print(re.search(r"Gate status: `([^`]+)`", text).group(1))
')"
readiness_checks="$(uv run python -c '
import re
from pathlib import Path
text = Path("release/readiness-audit.md").read_text(encoding="utf-8")
print(re.search(r"Checks: `([^`]+)`", text).group(1))
')"
completion_gate="$(uv run python -c '
import json
from pathlib import Path
data = json.loads(Path("release/completion-audit.json").read_text(encoding="utf-8"))
print(data["gate"])
')"

set +e
bash scripts/validate_live_validation_record.sh feishu >/tmp/ostag-launch-live-gate.out 2>&1
live_code=$?
bash scripts/validate_mfs_validation_record.sh >/tmp/ostag-launch-mfs-gate.out 2>&1
mfs_code=$?
set -e

mfs_mode="$(uv run python -c '
import json
from pathlib import Path
data = json.loads(Path("release/production-scope.json").read_text(encoding="utf-8"))
print(data.get("mfs_mode", "required"))
')"
effective_mfs_code="$mfs_code"
if [[ "$mfs_mode" == "excluded" ]]; then
  effective_mfs_code=0
fi

if [[ "$readiness_gate" == "FAIL" || "$completion_gate" == "FAIL" ]]; then
  launch_status="failed"
  exit_code=2
elif [[ "$live_code" -eq 0 && "$effective_mfs_code" -eq 0 && "$completion_gate" == "PASS" ]]; then
  launch_status="production_candidate"
  exit_code=0
else
  launch_status="blocked_external"
  exit_code=3
fi

timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

uv run python - <<PY
import json
from pathlib import Path

data = {
    "generated_at": "$timestamp",
    "status": "$launch_status",
    "readiness_gate": "$readiness_gate",
    "readiness_checks": "$readiness_checks",
    "completion_gate": "$completion_gate",
    "mfs_mode": "$mfs_mode",
    "live_validation_exit_code": $live_code,
    "mfs_validation_exit_code": $mfs_code,
    "effective_mfs_exit_code": $effective_mfs_code,
    "exit_code": $exit_code,
    "blocking_conditions": [],
}
if data["status"] == "blocked_external":
    if data["live_validation_exit_code"] != 0:
        data["blocking_conditions"].append("Feishu/Lark manual live validation is not complete.")
    if data["effective_mfs_exit_code"] != 0:
        data["blocking_conditions"].append("Live MFS smoke is not complete or is intentionally out of production scope.")
Path("$json_report").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY

{
  echo "# OS Tag Launch Status"
  echo
  echo "- Generated at: \`$timestamp\`"
  echo "- Status: \`$launch_status\`"
  echo "- Exit code: \`$exit_code\`"
  echo "- Readiness gate: \`$readiness_gate\`"
  echo "- Readiness checks: \`$readiness_checks\`"
  echo "- Completion gate: \`$completion_gate\`"
  echo "- MFS mode: \`$mfs_mode\`"
  echo "- Live validation exit code: \`$live_code\`"
  echo "- MFS validation exit code: \`$mfs_code\`"
  echo "- Effective MFS exit code: \`$effective_mfs_code\`"
  echo
  echo "## Blocking Conditions"
  echo
  if [[ "$launch_status" == "blocked_external" ]]; then
    if [[ "$live_code" -ne 0 ]]; then
      echo "- Feishu/Lark manual live validation is not complete."
    fi
    if [[ "$effective_mfs_code" -ne 0 ]]; then
      echo "- Live MFS smoke is not complete or is intentionally out of production scope."
    fi
  elif [[ "$launch_status" == "production_candidate" ]]; then
    echo "- None."
  else
    echo "- Release readiness or completion audit failed."
  fi
  echo
  echo "## Exit Code Contract"
  echo
  echo "- \`0\`: production-candidate gates are complete."
  echo "- \`2\`: release failed an automated gate."
  echo "- \`3\`: automated gates pass but external live validation is blocked."
} > "$md_report"

echo "$md_report"
echo "$json_report"
exit "$exit_code"
