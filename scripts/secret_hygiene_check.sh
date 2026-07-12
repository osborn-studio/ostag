#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p release
json_report="release/secret-hygiene.json"
md_report="release/secret-hygiene.md"

scan_root="$(mktemp -d)"
matches="$(mktemp)"
trap 'rm -rf "$scan_root" "$matches"' EXIT

if [[ -f release/ostag-release-bundle.tar.gz ]]; then
  mkdir -p "$scan_root/bundle" "$scan_root/current"
  tar -xzf release/ostag-release-bundle.tar.gz -C "$scan_root/bundle"
  cp -R release docs README.md .env.example "$scan_root/current/"
else
  mkdir -p "$scan_root/source"
  cp -R release docs README.md .env.example "$scan_root/source/" 2>/dev/null || true
fi

set +e
rg -n --hidden --glob '!*.tar.gz' --glob '!*.sha256' \
  '([A-Z0-9_]*(SECRET|TOKEN|API_KEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*[[:space:]]*[:=][[:space:]]*[A-Za-z0-9_/-]{8,})|(app_secret[[:space:]]*[:=][[:space:]]*[A-Za-z0-9_/-]{8,})|(sk-[A-Za-z0-9_-]{16,})' \
  "$scan_root" > "$matches"
code=$?
set -e

status="pass"
if [[ "$code" -eq 0 ]]; then
  status="fail"
elif [[ "$code" -ne 1 ]]; then
  status="error"
fi

timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
match_count="$(wc -l < "$matches" | tr -d ' ')"

uv run python - <<PY
import json
from pathlib import Path

data = {
    "generated_at": "$timestamp",
    "status": "$status",
    "match_count": int("$match_count"),
    "scanned": "release bundle plus current release/docs" if Path("release/ostag-release-bundle.tar.gz").exists() else "release/docs fallback",
}
Path("$json_report").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY

{
  echo "# OS Tag Secret Hygiene"
  echo
  echo "- Generated at: \`$timestamp\`"
  echo "- Status: \`$status\`"
  echo "- Matches: \`$match_count\`"
  echo "- Scope: release bundle and formal handoff artifacts"
  echo
  echo "## Result"
  echo
  if [[ "$status" == "pass" ]]; then
    echo "No secret-like values were found in the release bundle or current formal artifacts."
  else
    echo "Potential secret-like values were found:"
    echo
    echo '```text'
    sed "s|$scan_root/||g" "$matches"
    echo '```'
  fi
} > "$md_report"

echo "$md_report"
echo "$json_report"

if [[ "$status" != "pass" ]]; then
  exit 2
fi
