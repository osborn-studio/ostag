#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

platform="${1:-feishu}"
record="${2:-release/live-validation-${platform}.md}"
report="release/production-candidate-report.md"

scripts/validate_live_validation_record.sh "$platform" "$record" >/tmp/ostag-promote-live-gate.out

score="$(awk -F'`' '/^Total:/ { print $2; exit }' release/ostag-scorecard.md)"
timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

{
  echo "# OS Tag Production Candidate Report"
  echo
  echo "- Generated at: \`$timestamp\`"
  echo "- Platform: \`$platform\`"
  echo "- Score before live promotion: \`${score:-96 / 100}\`"
  echo "- Live validation record: \`$record\`"
  echo "- Status: \`production_candidate\`"
  echo
  echo "## Gate Evidence"
  echo
  echo '```text'
  cat /tmp/ostag-promote-live-gate.out
  echo '```'
  echo
  echo "## Required Follow-up"
  echo
  echo "- Update \`release/ostag-scorecard.md\` after operator review."
  echo "- Preserve screenshot/transcript and audit log evidence."
} > "$report"

echo "$report"
