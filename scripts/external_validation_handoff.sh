#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p release

set +e
bash scripts/launch_status.sh >/tmp/ostag-external-handoff-launch.out 2>&1
launch_code=$?
set -e

if [[ "$launch_code" -ne 0 && "$launch_code" -ne 3 ]]; then
  cat /tmp/ostag-external-handoff-launch.out >&2
  exit "$launch_code"
fi

json_out="release/external-validation-handoff.json"
md_out="release/external-validation-handoff.md"
timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

uv run python - <<PY
import json
from pathlib import Path

launch = json.loads(Path("release/launch-status.json").read_text(encoding="utf-8"))
scope = json.loads(Path("release/production-scope.json").read_text(encoding="utf-8"))
data = {
    "generated_at": "$timestamp",
    "launch_status": launch["status"],
    "launch_exit_code": launch["exit_code"],
    "mfs_mode": scope["mfs_mode"],
    "blocking_conditions": launch.get("blocking_conditions", []),
    "required_operator_commands": [
        "scripts/deploy_preflight.sh",
        "scripts/live_validation_record.sh feishu",
        "scripts/validate_live_validation_record.sh feishu",
        "scripts/mfs_validation_record.sh",
        "scripts/validate_mfs_validation_record.sh",
        "scripts/launch_status.sh",
    ],
}
Path("$json_out").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY

{
  echo "# OS Tag External Validation Handoff"
  echo
  echo "- Generated at: \`$timestamp\`"
  echo "- Launch status: \`$(uv run python - <<'PY'
import json
from pathlib import Path
print(json.loads(Path("release/launch-status.json").read_text(encoding="utf-8"))["status"])
PY
)\`"
  echo "- Launch exit code: \`$launch_code\`"
  echo "- MFS mode: \`$(uv run python - <<'PY'
import json
from pathlib import Path
print(json.loads(Path("release/production-scope.json").read_text(encoding="utf-8"))["mfs_mode"])
PY
)\`"
  echo
  echo "## Blocking Conditions"
  echo
  uv run python - <<'PY'
import json
from pathlib import Path
launch = json.loads(Path("release/launch-status.json").read_text(encoding="utf-8"))
for item in launch.get("blocking_conditions", []):
    print(f"- {item}")
if not launch.get("blocking_conditions"):
    print("- None.")
PY
  echo
  echo "## Operator Commands"
  echo
  echo '```bash'
  echo "scripts/deploy_preflight.sh"
  echo "scripts/live_validation_record.sh feishu"
  echo "scripts/validate_live_validation_record.sh feishu"
  echo "scripts/mfs_validation_record.sh"
  echo "scripts/validate_mfs_validation_record.sh"
  echo "scripts/launch_status.sh"
  echo '```'
  echo
  echo "## Evidence To Fill"
  echo
  echo "- Feishu/Lark app id prefix, never the secret."
  echo "- Test chat id."
  echo "- Incoming message id."
  echo "- Backend used, starting with \`mock\`."
  echo "- Audit log path and matching timestamp."
  echo "- Screenshot or transcript path."
  echo "- MFS production decision: keep \`required\` and pass live smoke, or explicitly record \`excluded\` with owner and rationale."
  echo
  echo "## Completion Rule"
  echo
  echo "\`scripts/launch_status.sh\` must exit \`0\` before OS Tag can be called production-candidate."
} > "$md_out"

echo "$md_out"
echo "$json_out"
