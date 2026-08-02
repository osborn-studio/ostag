#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

template="deploy/launchd/ai.ostag.feishu.plist.template"
output="${1:-release/ai.ostag.feishu.plist}"
uv_bin="${OSTAG_UV_BIN:-$(command -v uv || true)}"

if [[ ! -f "$template" ]]; then
  echo "missing launchd template: $template" >&2
  exit 1
fi

if [[ "$uv_bin" != /* || ! -x "$uv_bin" ]]; then
  echo "uv executable must be an absolute executable path: ${uv_bin:-not found}" >&2
  exit 1
fi

mkdir -p "$(dirname "$output")" logs

escaped_root="${ROOT//\\/\\\\}"
escaped_root="${escaped_root//&/\\&}"
escaped_uv_bin="${uv_bin//\\/\\\\}"
escaped_uv_bin="${escaped_uv_bin//&/\\&}"
sed \
  -e "s#__OSTAG_ROOT__#${escaped_root}#g" \
  -e "s#__UV_BIN__#${escaped_uv_bin}#g" \
  "$template" > "$output"

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$output" >/dev/null
fi

echo "$output"
