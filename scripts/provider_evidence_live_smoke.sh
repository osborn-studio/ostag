#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

platform="${1:-}"
case "$platform" in
  github|gitlab) ;;
  *) echo "usage: scripts/provider_evidence_live_smoke.sh <github|gitlab> [number] [output.json]" >&2; exit 2 ;;
esac

number="${2:-}"
output="${3:-release/provider-evidence-${platform}.json}"
mkdir -p "$(dirname "$output")"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
candidate="$tmpdir/evidence.json"

write_blocker() {
  local code="$1"
  uv run python - "$platform" "$code" "$candidate" <<'PY'
import json, sys
from pathlib import Path
platform, code, output = sys.argv[1:]
Path(output).write_text(json.dumps({
    "schema_version": 1,
    "platform": platform,
    "status": "blocked",
    "blocker_code": code,
}, sort_keys=True, indent=2) + "\n", encoding="utf-8")
PY
  mv "$candidate" "$output"
  echo "$output"
  exit 3
}

if [[ ! "$number" =~ ^[1-9][0-9]*$ ]]; then
  write_blocker "missing_change_number"
fi

raw="$tmpdir/raw.json"
set +e
if [[ -n "${OSTAG_CONNECTOR_CLI_BIN:-}" ]]; then
  "$OSTAG_CONNECTOR_CLI_BIN" connector evidence "$platform" --number "$number" --json \
    >"$raw" 2>"$tmpdir/stderr.log"
else
  uv run ostag connector evidence "$platform" --number "$number" --json \
    >"$raw" 2>"$tmpdir/stderr.log"
fi
command_code=$?
set -e

if [[ "$command_code" -ne 0 ]]; then
  blocker="$(uv run python - "$raw" <<'PY'
import json, sys
try:
    data=json.load(open(sys.argv[1]))
    code=data.get("error", {}).get("code", "provider_evidence_failed")
    print(code if isinstance(code, str) and code else "provider_evidence_failed")
except Exception:
    print("provider_evidence_failed")
PY
)"
  write_blocker "$blocker"
fi

if ! uv run python - "$platform" "$raw" "$candidate" <<'PY'
import json, re, sys
from pathlib import Path
platform, source, output = sys.argv[1:]
try:
    result=json.load(open(source))
except Exception:
    raise SystemExit(1)
evidence=result.get("evidence") if isinstance(result, dict) else None
digest=result.get("payloadDigest") if isinstance(result, dict) else None
if not isinstance(evidence, dict) or evidence.get("provider") != platform:
    raise SystemExit(1)
if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
    raise SystemExit(1)
payload={"schema_version": 1, "platform": platform, "status": "ready", "result": result}
sensitive={"access_token", "authorization", "app_secret", "client_secret", "password"}
def unsafe(value):
    if isinstance(value, dict):
        return any(str(key).casefold() in sensitive or unsafe(item) for key, item in value.items())
    if isinstance(value, list):
        return any(unsafe(item) for item in value)
    return False
if unsafe(payload):
    raise SystemExit(1)
Path(output).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
PY
then
  echo "provider evidence is malformed" >&2
  exit 2
fi

mv "$candidate" "$output"
echo "$output"
