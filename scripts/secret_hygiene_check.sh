#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

report_dir="${OSTAG_SECRET_HYGIENE_REPORT_DIR:-release}"
mkdir -p "$report_dir"
json_report="$report_dir/secret-hygiene.json"
md_report="$report_dir/secret-hygiene.md"

scan_root="$(mktemp -d)"
json_tmp="$(mktemp "$report_dir/.secret-hygiene.json.tmp.XXXXXX")"
md_tmp="$(mktemp "$report_dir/.secret-hygiene.md.tmp.XXXXXX")"
trap 'rm -rf "$scan_root" "$json_tmp" "$md_tmp"' EXIT

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

scan_json="$(uv run --locked python scripts/secret_hygiene_scan.py "$scan_root")"

timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
read -r match_count findings_digest category_counts < <(
  uv run --locked python - "$scan_json" <<'PY'
import json, sys
result = json.loads(sys.argv[1])
print(result["match_count"], result["findings_digest_sha256"], json.dumps(result["category_counts"], separators=(",", ":")))
PY
)
status="pass"
if [[ "$match_count" -ne 0 ]]; then
  status="fail"
fi

uv run --locked python - "$json_tmp" "$timestamp" "$status" "$match_count" "$findings_digest" "$category_counts" <<'PY'
import json
import sys
from pathlib import Path

data = {
    "generated_at": sys.argv[2],
    "status": sys.argv[3],
    "match_count": int(sys.argv[4]),
    "findings_digest_sha256": sys.argv[5],
    "category_counts": json.loads(sys.argv[6]),
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
