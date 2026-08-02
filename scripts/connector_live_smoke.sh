#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

platform="${1:-}"
case "$platform" in
  feishu|lark|dingtalk|github|gitlab) ;;
  *)
    echo "usage: scripts/connector_live_smoke.sh <feishu|lark|dingtalk|github|gitlab> [output.json]" >&2
    exit 2
    ;;
esac

output="${2:-release/connector-live-${platform}.json}"
mkdir -p "$(dirname "$output")"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
raw="$tmpdir/raw.json"
candidate="$tmpdir/evidence.json"

set +e
if [[ -n "${OSTAG_CONNECTOR_CLI_BIN:-}" ]]; then
  "$OSTAG_CONNECTOR_CLI_BIN" connector test "$platform" --live --json \
    >"$raw" 2>"$tmpdir/stderr.log"
else
  uv run ostag connector test "$platform" --live --json >"$raw" 2>"$tmpdir/stderr.log"
fi
command_code=$?
set -e

if ! uv run python - "$platform" "$raw" "$candidate" <<'PY'
import json
from pathlib import Path
import sys

platform, source_name, output_name = sys.argv[1:]
try:
    payload = json.loads(Path(source_name).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)

if not isinstance(payload, dict):
    raise SystemExit(1)
required = {
    "schema_version": int,
    "platform": str,
    "mode": str,
    "status": str,
    "read_ready": bool,
    "checks": list,
}
if any(not isinstance(payload.get(key), kind) for key, kind in required.items()):
    raise SystemExit(1)
if payload["schema_version"] != 1 or payload["platform"] != platform:
    raise SystemExit(1)
if payload["mode"] != "live" or payload["status"] not in {"ready", "blocked", "degraded"}:
    raise SystemExit(1)
if any(not isinstance(item, dict) for item in payload["checks"]):
    raise SystemExit(1)

sensitive = {"access_token", "authorization", "app_secret", "client_secret", "password"}
def contains_sensitive_key(value):
    if isinstance(value, dict):
        return any(key.casefold() in sensitive or contains_sensitive_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_sensitive_key(item) for item in value)
    return False

if contains_sensitive_key(payload):
    raise SystemExit(1)
Path(output_name).write_text(
    json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    encoding="utf-8",
)
PY
then
  echo "connector live evidence is malformed" >&2
  exit 2
fi

read_ready="$(uv run python -c 'import json,sys; print(str(json.load(open(sys.argv[1]))["read_ready"]).lower())' "$candidate")"
if [[ "$read_ready" == "true" && "$command_code" -eq 0 ]]; then
  mv "$candidate" "$output"
  echo "$output"
  exit 0
fi
if [[ "$read_ready" == "false" && ("$command_code" -eq 0 || "$command_code" -eq 1) ]]; then
  mv "$candidate" "$output"
  echo "$output"
  exit 3
fi

echo "connector live command and evidence status disagree" >&2
exit 2
