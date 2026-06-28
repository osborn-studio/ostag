#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== release check =="
scripts/release_check.sh

echo "== docker compose config =="
docker compose config >/dev/null

echo "== launchd plist render =="
plist_path="$(scripts/render_launchd_plist.sh)"
test -f "$plist_path"
rg -q "$ROOT" "$plist_path"
rg -q "ostag.main" "$plist_path"
rg -q -- "--live" "$plist_path"

echo "== credential-gated live validation record =="
record_path="$(scripts/live_validation_record.sh feishu)"
test -f "$record_path"
rg -q "Status:" "$record_path"
if [[ -z "${FEISHU_APP_ID:-}" || -z "${FEISHU_APP_SECRET:-}" ]] \
  && ! rg -q "manual_group_validation_passed" "$record_path"; then
  rg -q "blocked_missing_credentials" "$record_path"
fi

echo "== mfs validation record =="
mfs_record_path="$(scripts/mfs_validation_record.sh)"
test -f "$mfs_record_path"
rg -q "Status:" "$mfs_record_path"

echo "deploy preflight passed"
