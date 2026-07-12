#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export OSTAG_BACKEND="${OSTAG_BACKEND:-mock}"

python -m compileall ostag
uv run ruff check ostag tests
collection_output="$(mktemp)"
trap 'rm -f "$collection_output"' EXIT
uv run pytest --collect-only -q >"$collection_output"
missing_tests="$(comm -23 \
  <(find tests -maxdepth 1 -type f -name 'test_*.py' -print | sort) \
  <(sed -n 's/^\(tests\/test_[^:]*\.py\)::.*$/\1/p' "$collection_output" | sort -u))"
if [[ -n "$missing_tests" ]]; then
  echo "pytest did not collect test modules:" >&2
  echo "$missing_tests" >&2
  exit 1
fi
uv run pytest
scripts/work_monitor_smoke.sh
scripts/secret_hygiene_check.sh
docker compose -f deploy/work-monitor/docker-compose.yml config >/dev/null
uv run python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json --doctor
uv run python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
uv run python -m ostag.main --platform lark --dry-run --event examples/lark_event.json
uv run python -m ostag.main --platform slack --dry-run --event examples/slack_event.json
uv run python -m ostag.main --platform dingtalk --dry-run --event examples/dingtalk_event.json
