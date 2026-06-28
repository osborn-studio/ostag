#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p release
json_record="release/production-scope.json"
md_record="release/production-scope.md"

mode="${OSTAG_PRODUCTION_MFS_MODE:-}"
owner="${OSTAG_PRODUCTION_SCOPE_OWNER:-}"
rationale="${OSTAG_PRODUCTION_MFS_RATIONALE:-}"

if [[ -z "$mode" && -f "$json_record" ]]; then
  mode="$(uv run python - <<'PY'
import json
from pathlib import Path
data = json.loads(Path("release/production-scope.json").read_text(encoding="utf-8"))
print(data.get("mfs_mode", "required"))
PY
)"
  owner="$(uv run python - <<'PY'
import json
from pathlib import Path
data = json.loads(Path("release/production-scope.json").read_text(encoding="utf-8"))
print(data.get("owner", ""))
PY
)"
  rationale="$(uv run python - <<'PY'
import json
from pathlib import Path
data = json.loads(Path("release/production-scope.json").read_text(encoding="utf-8"))
print(data.get("mfs_rationale", ""))
PY
)"
fi

mode="${mode:-required}"

case "$mode" in
  required|excluded) ;;
  *)
    echo "invalid OSTAG_PRODUCTION_MFS_MODE: $mode" >&2
    exit 2
    ;;
esac

if [[ "$mode" == "required" && -z "$rationale" ]]; then
  rationale="MFS is in production scope unless an operator explicitly excludes it."
fi

timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

uv run python - <<PY
import json
from pathlib import Path

data = {
    "generated_at": "$timestamp",
    "mfs_mode": "$mode",
    "owner": "$owner",
    "mfs_rationale": "$rationale",
}
Path("$json_record").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY

{
  echo "# OS Tag Production Scope"
  echo
  echo "- Generated at: \`$timestamp\`"
  echo "- MFS mode: \`$mode\`"
  echo "- Owner: \`${owner:-unset}\`"
  echo "- MFS rationale: ${rationale:-unset}"
  echo
  echo "## MFS Mode Contract"
  echo
  echo "- \`required\`: live MFS smoke must pass before production-candidate."
  echo "- \`excluded\`: MFS is explicitly out of the current production-candidate scope; owner and rationale are required."
} > "$md_record"

echo "$md_record"
echo "$json_record"
