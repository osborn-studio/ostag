#!/usr/bin/env bash
set -euo pipefail

python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
