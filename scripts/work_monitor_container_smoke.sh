#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

project="${OSTAG_CONTAINER_SMOKE_PROJECT:-ostag-wm-smoke-$$}"
compose=(docker compose -p "$project" -f deploy/work-monitor/docker-compose.yml)
volume="${project}_work-monitor-data"

cleanup() {
  "${compose[@]}" down >/dev/null 2>&1 || true
  if [[ "${OSTAG_CONTAINER_SMOKE_CLEANUP_VOLUME:-0}" == "1" ]]; then
    docker volume rm "$volume" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

wait_healthy() {
  local container_id status
  container_id="$("${compose[@]}" ps -q work-monitor)"
  for _ in $(seq 1 60); do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$container_id")"
    if [[ "$status" == "healthy" ]]; then
      return 0
    fi
    if [[ "$status" == "unhealthy" ]]; then
      "${compose[@]}" logs work-monitor >&2
      return 1
    fi
    sleep 1
  done
  "${compose[@]}" logs work-monitor >&2
  return 1
}

export OSTAG_MONITOR_PORT="${OSTAG_MONITOR_PORT:-18081}"
"${compose[@]}" build work-monitor
"${compose[@]}" up -d work-monitor
wait_healthy
"${compose[@]}" exec -T work-monitor python - <<'PY'
import sqlite3
import lark_oapi
import Crypto

with sqlite3.connect("/var/lib/ostag/work_monitor.sqlite3") as database:
    database.execute("CREATE TABLE IF NOT EXISTS container_smoke(value TEXT PRIMARY KEY)")
    database.execute("INSERT OR REPLACE INTO container_smoke VALUES ('persisted')")
PY
"${compose[@]}" restart work-monitor
wait_healthy
"${compose[@]}" exec -T work-monitor python - <<'PY'
import sqlite3

with sqlite3.connect("/var/lib/ostag/work_monitor.sqlite3") as database:
    assert database.execute("SELECT value FROM container_smoke").fetchone() == ("persisted",)
PY

echo "container smoke passed; named volume retained by default: $volume"
