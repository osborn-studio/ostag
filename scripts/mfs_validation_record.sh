#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p release
record="release/mfs-validation.md"
query="${MFS_VALIDATION_QUERY:-ostag}"
scopes="${MFS_ALLOWED_SCOPES:-file://./examples/context}"
scope="${scopes%%,*}"
mfs_bin="${MFS_BIN:-mfs}"
enabled="${MFS_ENABLED:-false}"

status="blocked_mfs_disabled"
result="blocked"
details=()
stdout_file="$(mktemp)"
stderr_file="$(mktemp)"
trap 'rm -f "$stdout_file" "$stderr_file"' EXIT

enabled_normalized="$(printf '%s' "$enabled" | tr '[:upper:]' '[:lower:]')"
if [[ "$enabled_normalized" != "true" ]]; then
  details+=("MFS_ENABLED is not true.")
elif ! command -v "$mfs_bin" >/dev/null 2>&1; then
  status="blocked_missing_mfs_bin"
  details+=("MFS_BIN is not executable on PATH: $mfs_bin")
else
  set +e
  "$mfs_bin" search "$query" "$scope" --limit "${MFS_MAX_RESULTS:-1}" >"$stdout_file" 2>"$stderr_file"
  code=$?
  set -e
  if [[ "$code" -eq 0 ]]; then
    status="mfs_smoke_passed"
    result="pass"
    details+=("MFS search command exited 0.")
  else
    status="blocked_mfs_search_failed"
    details+=("MFS search command exited $code.")
  fi
fi

{
  echo "# OS Tag MFS Validation Record"
  echo
  echo "- Generated at: \`$(date -u +"%Y-%m-%dT%H:%M:%SZ")\`"
  echo "- Status: \`$status\`"
  echo "- Result: $result"
  echo "- MFS enabled: \`$enabled\`"
  echo "- MFS bin: \`$mfs_bin\`"
  echo "- MFS scope: \`$scope\`"
  echo "- Query: \`$query\`"
  echo "- Stdout chars: \`$(wc -c <"$stdout_file" | tr -d ' ')\`"
  echo "- Stderr chars: \`$(wc -c <"$stderr_file" | tr -d ' ')\`"
  echo
  echo "## Details"
  echo
  for item in "${details[@]}"; do
    echo "- $item"
  done
  echo
  echo "## Redacted Output Preview"
  echo
  echo '```text'
  head -c 1000 "$stdout_file" || true
  echo
  echo '```'
} > "$record"

echo "$record"
