#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

record="${1:-release/mfs-validation.md}"
test -f "$record"

rg -q "Status: \`(mfs_smoke_passed|blocked_mfs_disabled|blocked_missing_mfs_bin|blocked_mfs_search_failed)\`" "$record"
rg -q "MFS scope: \`" "$record"
rg -q "Query: \`" "$record"

if rg -q "Status: \`mfs_smoke_passed\`" "$record"; then
  rg -q "Result: pass" "$record"
  echo "mfs validation passed"
  exit 0
fi

if rg -q "Status: \`blocked_mfs_disabled\`" "$record"; then
  echo "mfs validation blocked: MFS_ENABLED is not true"
  exit 3
fi

if rg -q "Status: \`blocked_missing_mfs_bin\`" "$record"; then
  echo "mfs validation blocked: MFS_BIN missing"
  exit 3
fi

if rg -q "Status: \`blocked_mfs_search_failed\`" "$record"; then
  echo "mfs validation blocked: search failed"
  exit 3
fi

echo "unrecognized mfs validation status" >&2
exit 2
