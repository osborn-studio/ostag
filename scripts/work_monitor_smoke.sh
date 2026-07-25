#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# The smoke is fixture-only. TestClient handles localhost ASGI calls in process;
# no external Feishu, Telegram, EdgeOne, or cloud endpoint is contacted.
uv run pytest tests/test_work_monitor_smoke.py -q
