#!/usr/bin/env bash
set -euo pipefail

DATABASE=""
LEGACY="ostag-local"
REPLACEMENT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --database) DATABASE="${2:-}"; shift 2 ;;
    --legacy) LEGACY="${2:-}"; shift 2 ;;
    --replacement) REPLACEMENT="${2:-}"; shift 2 ;;
    *) echo "usage: $0 --database /absolute/monitor.sqlite3 --replacement DEVICE [--legacy ostag-local]" >&2; exit 64 ;;
  esac
done
[[ "$DATABASE" == /* && -f "$DATABASE" ]] || { echo "database must be an existing absolute path" >&2; exit 64; }
[[ "$LEGACY" =~ ^[A-Za-z0-9._-]{1,128}$ && "$REPLACEMENT" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || { echo "invalid instance id" >&2; exit 64; }
[[ "$LEGACY" != "$REPLACEMENT" ]] || { echo "replacement must differ from legacy instance" >&2; exit 64; }

EXISTING="$(sqlite3 "$DATABASE" "SELECT replacement_device_id FROM relay_legacy_migrations WHERE legacy_instance_id='$LEGACY' LIMIT 1;" 2>/dev/null || true)"
if [[ -n "$EXISTING" && "$EXISTING" != "$REPLACEMENT" ]]; then
  echo "legacy instance already migrated to another device" >&2
  exit 65
fi
REPORTED="$(sqlite3 "$DATABASE" "SELECT COUNT(*) FROM relay_sessions WHERE instance_id='$REPLACEMENT';")"
[[ "$REPORTED" -gt 0 ]] || { echo "replacement device has not reported a session" >&2; exit 65; }

sqlite3 -bail "$DATABASE" <<SQL
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS relay_legacy_migrations (
  legacy_instance_id TEXT PRIMARY KEY,
  replacement_device_id TEXT NOT NULL UNIQUE,
  migrated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO relay_legacy_migrations VALUES ('$LEGACY','$REPLACEMENT',CURRENT_TIMESTAMP);
UPDATE monitor_instance_owners SET revoked=1,revoked_at=CURRENT_TIMESTAMP
WHERE instance_id='$LEGACY' AND revoked=0;
UPDATE relay_callbacks SET consumed_at=CAST(strftime('%s','now') AS INTEGER)
WHERE consumed_at IS NULL AND reference_id LIKE '%' || '$LEGACY' || '%';
COMMIT;
SQL
echo "legacy instance migrated read-only; audit rows preserved"
