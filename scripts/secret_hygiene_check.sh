#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

report_dir="${OSTAG_SECRET_HYGIENE_REPORT_DIR:-release}"
mkdir -p "$report_dir"
json_report="$report_dir/secret-hygiene.json"
md_report="$report_dir/secret-hygiene.md"

scan_root="$(mktemp -d)"
matches="$(mktemp)"
json_tmp="$(mktemp "$report_dir/.secret-hygiene.json.tmp.XXXXXX")"
md_tmp="$(mktemp "$report_dir/.secret-hygiene.md.tmp.XXXXXX")"
trap 'rm -rf "$scan_root" "$matches" "$json_tmp" "$md_tmp"' EXIT

if [[ -f release/ostag-release-bundle.tar.gz ]]; then
  mkdir -p "$scan_root/bundle" "$scan_root/current"
  tar -xzf release/ostag-release-bundle.tar.gz -C "$scan_root/bundle"
  cp -R ostag scripts deploy release docs README.md .env.example "$scan_root/current/"
else
  mkdir -p "$scan_root/source"
  cp -R ostag scripts deploy release docs README.md .env.example "$scan_root/source/" \
    2>/dev/null || true
fi

if [[ -n "${OSTAG_SECRET_HYGIENE_EXTRA_SCAN_ROOT:-}" ]]; then
  cp -R "$OSTAG_SECRET_HYGIENE_EXTRA_SCAN_ROOT" "$scan_root/fixture-input"
fi

# Never scan a previous report: failure details are transient and reports contain
# only aggregate evidence, so a failed run cannot poison the next scan.
find "$scan_root" -type f \( -name 'secret-hygiene.json' -o -name 'secret-hygiene.md' \) -delete

set +e
rg -n --hidden --glob '!*.tar.gz' --glob '!*.sha256' \
  '([A-Z0-9_]*(SECRET|TOKEN|API_KEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*[[:space:]]*([=][[:space:]]*|:[[:space:]]+)[A-Za-z0-9_/-]{8,})|(app_secret[[:space:]]*([=][[:space:]]*|:[[:space:]]+)[A-Za-z0-9_/-]{8,})|(sk-[A-Za-z0-9_-]{16,})|(https://alidocs\.dingtalk\.com/i/nodes/[A-Za-z0-9_-]{24,})' \
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
findings_digest="$(shasum -a 256 "$matches" | awk '{print $1}')"

uv run python - "$json_tmp" "$timestamp" "$status" "$match_count" "$findings_digest" <<'PY'
import json
import sys
from pathlib import Path

data = {
    "generated_at": sys.argv[2],
    "status": sys.argv[3],
    "match_count": int(sys.argv[4]),
    "findings_digest_sha256": sys.argv[5],
    "scanned": "release bundle plus current release/docs" if Path("release/ostag-release-bundle.tar.gz").exists() else "release/docs fallback",
}
Path(sys.argv[1]).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
    echo "Potential secret-like values were found. Raw matches are intentionally not persisted."
    echo "Findings digest (SHA-256): \`$findings_digest\`"
  fi
} > "$md_tmp"

chmod 0600 "$json_tmp" "$md_tmp"
mv -f "$json_tmp" "$json_report"
mv -f "$md_tmp" "$md_report"

echo "$md_report"
echo "$json_report"

if [[ "$status" != "pass" ]]; then
  exit 2
fi
