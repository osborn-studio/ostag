#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

record="${1:-release/production-scope.json}"
test -f "$record"

mode="$(uv run python - <<PY
import json
from pathlib import Path
data = json.loads(Path("$record").read_text(encoding="utf-8"))
print(data.get("mfs_mode", ""))
PY
)"
owner="$(uv run python - <<PY
import json
from pathlib import Path
data = json.loads(Path("$record").read_text(encoding="utf-8"))
print(data.get("owner", ""))
PY
)"
rationale="$(uv run python - <<PY
import json
from pathlib import Path
data = json.loads(Path("$record").read_text(encoding="utf-8"))
print(data.get("mfs_rationale", ""))
PY
)"

case "$mode" in
  required)
    echo "production scope passed: MFS is required"
    exit 0
    ;;
  excluded)
    if [[ -z "$owner" || -z "$rationale" ]]; then
      echo "production scope invalid: MFS exclusion requires owner and rationale" >&2
      exit 2
    fi
    echo "production scope passed: MFS explicitly excluded"
    exit 0
    ;;
  *)
    echo "production scope invalid: unknown MFS mode '$mode'" >&2
    exit 2
    ;;
esac
