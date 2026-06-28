#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python -m compileall ostag
uv run pytest
uv run python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json --doctor
uv run python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
uv run python -m ostag.main --platform lark --dry-run --event examples/lark_event.json
uv run python -m ostag.main --platform slack --dry-run --event examples/slack_event.json
