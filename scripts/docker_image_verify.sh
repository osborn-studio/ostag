#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

image="${1:-ostag:deep-fork}"

docker build -t "$image" .
docker run --rm "$image" uv run python -m ostag.main \
  --platform feishu \
  --dry-run \
  --event examples/feishu_event.json \
  --doctor
docker run --rm "$image" uv run python -m ostag.main \
  --platform feishu \
  --dry-run \
  --event examples/feishu_event.json | rg -q "OS Tag mock reply:"

echo "docker image verification passed: $image"
