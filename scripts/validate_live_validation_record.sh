#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

platform="${1:-feishu}"
default_record="release/live-validation-${platform}.md"
record="${2:-$default_record}"

if [[ ! "$platform" =~ ^(feishu|lark)$ ]]; then
  echo "usage: $0 [feishu|lark] [record-path]" >&2
  exit 2
fi

if [[ ! -f "$record" ]]; then
  echo "missing live validation record: $record" >&2
  exit 2
fi

if [[ "$record" == "$default_record" ]]; then
  expected_version="$(uv run python -c 'import ostag; print(ostag.__version__)')"
  recorded_commit="$({
    awk '
      /^- Release commit:/ {
        value=$0
        sub(/^- Release commit:[[:space:]]*`?/, "", value)
        sub(/`?[[:space:]]*$/, "", value)
        print value
        exit
      }
    ' "$record"
  } || true)"
  recorded_version="$({
    awk '
      /^- Release version:/ {
        value=$0
        sub(/^- Release version:[[:space:]]*`?/, "", value)
        sub(/`?[[:space:]]*$/, "", value)
        print value
        exit
      }
    ' "$record"
  } || true)"
  generated_at="$({
    awk '
      /^- Generated at:/ {
        value=$0
        sub(/^- Generated at:[[:space:]]*`?/, "", value)
        sub(/`?[[:space:]]*$/, "", value)
        print value
        exit
      }
    ' "$record"
  } || true)"
  if [[ -z "$recorded_commit" ]] \
    || ! git cat-file -e "$recorded_commit^{commit}" >/dev/null 2>&1 \
    || ! git merge-base --is-ancestor "$recorded_commit" HEAD \
    || [[ "$recorded_version" != "$expected_version" ]]; then
    echo "blocked: live validation evidence is not bound to the current release commit" >&2
    exit 3
  fi
  if ! uv run python -c '
from datetime import UTC, datetime, timedelta
import sys

generated = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
age = datetime.now(UTC) - generated
limit = timedelta(days=int(sys.argv[2]))
raise SystemExit(0 if -timedelta(minutes=5) <= age <= limit else 1)
' "$generated_at" "${OSTAG_LIVE_EVIDENCE_MAX_AGE_DAYS:-7}"; then
    echo "blocked: live validation evidence is stale for the current release" >&2
    exit 3
  fi
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
