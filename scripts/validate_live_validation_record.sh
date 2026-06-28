#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

platform="${1:-feishu}"
record="${2:-release/live-validation-${platform}.md}"

if [[ ! "$platform" =~ ^(feishu|lark)$ ]]; then
  echo "usage: $0 [feishu|lark] [record-path]" >&2
  exit 2
fi

if [[ ! -f "$record" ]]; then
  echo "missing live validation record: $record" >&2
  exit 2
fi

if rg -q 'Status: `blocked_missing_credentials`' "$record"; then
  echo "blocked: live credentials are missing in $record" >&2
  exit 3
fi

if ! rg -q 'Status: `(preflight_passed_manual_group_test_required|manual_group_validation_passed)`' "$record"; then
  echo "invalid record status; run scripts/live_validation_record.sh first" >&2
  exit 2
fi

required_labels=(
  "Test chat id"
  "Incoming message id"
  "Backend"
  "Audit log path"
  "Audit timestamp"
  "Result"
)

for label in "${required_labels[@]}"; do
  if ! awk -v label="$label" '
    $0 ~ "^- " label ":" {
      value=$0
      sub("^- " label ":[[:space:]]*", "", value)
      if (value != "") found=1
    }
    END { exit found ? 0 : 1 }
  ' "$record"; then
    echo "missing manual validation field: $label" >&2
    exit 2
  fi
done

if ! awk '
  /^- Result:/ {
    value=$0
    sub(/^- Result:[[:space:]]*/, "", value)
    if (tolower(value) ~ /^(pass|passed|ok)$/) found=1
  }
  END { exit found ? 0 : 1 }
' "$record"; then
  echo "manual validation result must be pass/passed/ok" >&2
  exit 2
fi

audit_path="$(
  awk '
    /^- Audit log path:/ {
      value=$0
      sub(/^- Audit log path:[[:space:]]*/, "", value)
      print value
      exit
    }
  ' "$record"
)"

if [[ "$audit_path" != /* ]]; then
  audit_path="$ROOT/$audit_path"
fi

if [[ ! -f "$audit_path" ]]; then
  echo "audit log path does not exist: $audit_path" >&2
  exit 2
fi

if ! rg -q '"status": "ok"|status=ok' "$audit_path"; then
  echo "audit log does not contain a successful ok event: $audit_path" >&2
  exit 2
fi

evidence_path="$(
  awk '
    /^- Screenshot\/transcript:/ {
      value=$0
      sub(/^- Screenshot\/transcript:[[:space:]]*/, "", value)
      print value
      exit
    }
  ' "$record"
)"

if [[ -n "$evidence_path" ]]; then
  if [[ "$evidence_path" != /* ]]; then
    evidence_path="$ROOT/$evidence_path"
  fi
  if [[ ! -e "$evidence_path" ]]; then
    echo "screenshot/transcript evidence does not exist: $evidence_path" >&2
    exit 2
  fi
fi

echo "live validation record passed: $record"
