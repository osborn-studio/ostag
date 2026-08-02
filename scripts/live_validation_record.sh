#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

platform="${1:-feishu}"
case "$platform" in
  feishu|lark) ;;
  *)
    echo "usage: $0 [feishu|lark]" >&2
    exit 2
    ;;
esac

mkdir -p release
record="release/live-validation-${platform}.md"
doctor_log="$(mktemp)"
release_commit="$(git rev-parse HEAD)"
release_version="$(uv run python -c 'import ostag; print(ostag.__version__)')"

if [[ -f "$record" ]] \
  && rg -q 'Status: `manual_group_validation_passed`' "$record" \
  && scripts/validate_live_validation_record.sh "$platform" "$record" >/dev/null 2>&1; then
  echo "$record"
  rm -f "$doctor_log"
  exit 0
fi

set +e
uv run python -m ostag.main --platform "$platform" --live --doctor >"$doctor_log" 2>&1
doctor_code=$?
set -e

if [[ "$platform" == "feishu" ]]; then
  app_id="${FEISHU_APP_ID:-}"
  required_missing=()
  [[ -n "${FEISHU_APP_ID:-}" ]] || required_missing+=("FEISHU_APP_ID")
  [[ -n "${FEISHU_APP_SECRET:-}" ]] || required_missing+=("FEISHU_APP_SECRET")
else
  app_id="${LARK_APP_ID:-}"
  required_missing=()
  [[ -n "${LARK_APP_ID:-}" ]] || required_missing+=("LARK_APP_ID")
  [[ -n "${LARK_APP_SECRET:-}" ]] || required_missing+=("LARK_APP_SECRET")
fi

app_prefix=""
if [[ -n "$app_id" ]]; then
  app_prefix="${app_id:0:8}..."
fi

{
  echo "# OS Tag Live Validation Record"
  echo
  echo "- Platform: \`$platform\`"
  echo "- Generated at: \`$(date -u +"%Y-%m-%dT%H:%M:%SZ")\`"
  echo "- Release commit: \`$release_commit\`"
  echo "- Release version: \`$release_version\`"
  echo "- App id prefix: \`${app_prefix:-not set}\`"
  echo "- Doctor exit code: \`$doctor_code\`"
  if [[ "${#required_missing[@]}" -gt 0 ]]; then
    echo "- Status: \`blocked_missing_credentials\`"
    echo "- Missing: \`${required_missing[*]}\`"
  elif [[ "$doctor_code" -eq 0 ]]; then
    echo "- Status: \`preflight_passed_manual_group_test_required\`"
  else
    echo "- Status: \`preflight_failed\`"
  fi
  echo
  echo "## Doctor Output"
  echo
  echo '```text'
  cat "$doctor_log"
  echo '```'
  echo
  echo "## Manual Group Test"
  echo
  echo "- Test chat id:"
  echo "- Incoming message id:"
  echo "- Backend:"
  echo "- Audit log path:"
  echo "- Audit timestamp:"
  echo "- Result: pass|fail"
  echo "- Screenshot/transcript:"
  echo
  echo "## Notes"
  echo
  echo "Do not record app secrets, tenant access tokens, or user tokens here."
} > "$record"

rm -f "$doctor_log"
echo "$record"
