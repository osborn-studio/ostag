#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Resolve from the shared repository root so this also works from a Git worktree.
SOURCE_REPO_ROOT="$(cd "$(git -C "$ROOT" rev-parse --git-common-dir)/.." && pwd)"
SOURCE_PARENT="$(cd "$SOURCE_REPO_ROOT/.." && pwd)"
if [[ -n "${OSSTACK_COPILOT_REPO:-}" ]]; then
  OSSTACK_ROOT="$OSSTACK_COPILOT_REPO"
elif [[ -d "$SOURCE_PARENT/osstack-copilot-session/packages/cli" ]]; then
  OSSTACK_ROOT="$SOURCE_PARENT/osstack-copilot-session"
elif [[ -d "$SOURCE_PARENT/agentsteam/osstack-copilot-session/packages/cli" ]]; then
  OSSTACK_ROOT="$SOURCE_PARENT/agentsteam/osstack-copilot-session"
else
  OSSTACK_ROOT="$SOURCE_PARENT/osstack-copilot-session"
fi
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ostag-copilot-relay.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

cd "$ROOT"
export PYTHON_DOTENV_DISABLED=1
export OSTAG_SMOKE_RELAY_SECRET="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run pytest -q tests/test_copilot_relay_integration.py --basetemp "$TMP_ROOT/pytest"
uv run python scripts/multi_device_copilot_acceptance.py \
  --fixtures tests/fixtures/multi-device

if [[ "${OSTAG_SKIP_OSSTACK_CROSS_REPO_SMOKE:-0}" == "1" ]]; then
  echo "Skipping external osstack CLI smoke by explicit CI configuration."
else
  if [[ ! -d "$OSSTACK_ROOT/packages/cli" ]]; then
    echo "osstack Copilot worktree not found: $OSSTACK_ROOT" >&2
    exit 1
  fi
  cd "$OSSTACK_ROOT"
  pnpm --filter @osstack/cli exec vitest run src/copilot-relay.integration.test.ts
fi

if find "$TMP_ROOT" -type f -print0 | xargs -0 grep -F -a -l \
  "$OSTAG_SMOKE_RELAY_SECRET" >/dev/null 2>&1; then
  echo "temporary relay credential remained in smoke artifacts" >&2
  exit 1
fi

echo "Copilot Relay smoke passed: central/local restart, same-session resume, dual-device isolation."
