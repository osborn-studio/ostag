#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT="$(dirname "$ROOT")"
FORK_DIR="${OSTAG_FORK_DIR:-$PARENT/ostag-deep-fork}"
UPSTREAM_URL="https://github.com/zilliztech/mfs.git"
BASELINE_COMMIT="16574f9b365bc67f3716c552dad416da730088b8"
BRANCH="ostag/deep-fork"

if [[ ! -d "$FORK_DIR/.git" ]]; then
  git clone "$UPSTREAM_URL" "$FORK_DIR"
fi

if git -C "$FORK_DIR" remote get-url upstream >/dev/null 2>&1; then
  FETCH_REMOTE="upstream"
else
  FETCH_REMOTE="origin"
fi

git -C "$FORK_DIR" fetch --tags "$FETCH_REMOTE"
git -C "$FORK_DIR" checkout -B "$BRANCH" "$BASELINE_COMMIT"

if git -C "$FORK_DIR" remote get-url origin >/dev/null 2>&1; then
  if ! git -C "$FORK_DIR" remote get-url upstream >/dev/null 2>&1; then
    git -C "$FORK_DIR" remote rename origin upstream
  fi
fi

mkdir -p "$FORK_DIR/docs"

cat > "$FORK_DIR/FORK_ORIGIN.md" <<'EOF'
# OS Tag Fork Origin

OS Tag is a fork-derived product based on the Open Tag example in `zilliztech/mfs`.

## Upstream

- Repository: https://github.com/zilliztech/mfs
- Baseline commit: 16574f9b365bc67f3716c552dad416da730088b8
- Upstream area: `examples/open-tag-skill`
- License: Apache License 2.0

## Product Direction

- Product name: OS Tag
- Internal code name: `ostag`
- Target: deeply rebuild Open Tag into a Feishu/Lark AI group-chat context assistant.

## Migration Policy

Every upstream Open Tag file must be classified as one of:

- `ported`
- `rewritten`
- `deleted`
- `deferred`

The migration report lives at `docs/fork_migration_report.md`.

## Product Remote

The local fork currently preserves the upstream remote as `upstream`. Add the
product remote as `origin` when the destination repository exists.
EOF

{
  echo "# OS Tag Fork Migration Report"
  echo
  echo "Baseline: \`$BASELINE_COMMIT\`"
  echo
  echo "## Upstream Open Tag Inventory"
  echo
  echo "| Upstream file | Decision | Notes |"
  echo "|---|---|---|"
  git -C "$FORK_DIR" ls-tree -r --name-only "$BASELINE_COMMIT" examples/open-tag-skill \
    | while IFS= read -r file; do
      echo "| \`$file\` | pending | To be classified during Phase 1-3. |"
    done
  echo
  echo "## Phase 0 Verification"
  echo
  echo "- Fork directory: \`$FORK_DIR\`"
  echo "- Branch: \`$BRANCH\`"
  echo "- Upstream remote preserved as \`upstream\`."
} > "$FORK_DIR/docs/fork_migration_report.md"

git -C "$FORK_DIR" status --short
git -C "$FORK_DIR" remote -v
git -C "$FORK_DIR" rev-parse HEAD
test "$(git -C "$FORK_DIR" rev-parse HEAD)" = "$BASELINE_COMMIT"
test -f "$FORK_DIR/FORK_ORIGIN.md"
test -s "$FORK_DIR/docs/fork_migration_report.md"
