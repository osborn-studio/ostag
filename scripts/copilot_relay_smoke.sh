#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OSSTACK_ROOT="${OSSTACK_COPILOT_REPO:-$(cd "$ROOT/.." && pwd)/osstack-copilot-session}"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ostag-copilot-relay.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

cd "$ROOT"
export PYTHON_DOTENV_DISABLED=1
export OSTAG_SMOKE_RELAY_SECRET="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run pytest -q tests/test_copilot_relay_integration.py --basetemp "$TMP_ROOT/pytest"

if [[ ! -d "$OSSTACK_ROOT/packages/cli" ]]; then
  echo "osstack Copilot worktree not found: $OSSTACK_ROOT" >&2
  exit 1
fi
cd "$OSSTACK_ROOT"
pnpm --filter @osstack/cli exec vitest run src/copilot-relay.integration.test.ts

if find "$TMP_ROOT" -type f -print0 | xargs -0 grep -F -a -l \
  "$OSTAG_SMOKE_RELAY_SECRET" >/dev/null 2>&1; then
  echo "temporary relay credential remained in smoke artifacts" >&2
  exit 1
fi

echo "Copilot Relay smoke passed: central/local restart, same-session resume, stable IDs."
