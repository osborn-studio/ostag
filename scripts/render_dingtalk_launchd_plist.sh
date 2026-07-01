#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

template="deploy/launchd/ai.ostag.dingtalk.plist.template"
output="${1:-release/ai.ostag.dingtalk.plist}"

if [[ ! -f "$template" ]]; then
  echo "missing launchd template: $template" >&2
  exit 1
fi

if [[ ! -f ".env.dingtalk" ]]; then
  echo "missing .env.dingtalk; run DingTalk credential setup first" >&2
  exit 1
fi

mkdir -p "$(dirname "$output")" logs

escaped_root="${ROOT//\\/\\\\}"
escaped_root="${escaped_root//&/\\&}"
sed "s#__OSTAG_ROOT__#${escaped_root}#g" "$template" > "$output"

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$output" >/dev/null
fi

echo "$output"
