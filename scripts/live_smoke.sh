#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

platform="${1:-feishu}"

uv run python -m ostag.main --platform "$platform" --live --doctor
uv run python -m ostag.main --platform "$platform" --live
