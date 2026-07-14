#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi
if [[ $# -ne 1 ]]; then
  echo "usage: $0 [--dry-run] /absolute/path/to/ostag" >&2
  exit 64
fi
REPO="$(cd "$1" && pwd)"
if [[ "$REPO" != /* ]]; then
  echo "repository path must be absolute" >&2
  exit 64
fi

DATA_DIR="${OSTAG_COPILOT_DATA_DIR:-$HOME/Library/Application Support/OStag}"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
PLIST="$LAUNCH_DIR/ai.ostag.copilot-relay.plist"
ENV_FILE="$DATA_DIR/copilot-relay.env"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "would render ai.ostag.copilot-relay.plist from $REPO"
  echo "would read credentials from $ENV_FILE"
  exit 0
fi
if [[ ! -x "$REPO/.venv/bin/python" ]]; then
  echo "missing virtualenv python: $REPO/.venv/bin/python" >&2
  exit 66
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "create $ENV_FILE with mode 600 before installation" >&2
  exit 66
fi
chmod 600 "$ENV_FILE"
mkdir -p "$DATA_DIR" "$LAUNCH_DIR"

sed -e "s#/opt/ostag#$REPO#g" -e "s#/Users/Shared/OStag#$DATA_DIR#g" \
  "$REPO/deploy/copilot-relay/ai.ostag.copilot-relay.plist" > "$PLIST"
sed -e "s#/opt/ostag#$REPO#g" -e "s#/Users/Shared/OStag#$DATA_DIR#g" \
  "$REPO/deploy/copilot-relay/run-worker.sh" > "$DATA_DIR/run-copilot-relay.sh"
chmod 700 "$DATA_DIR/run-copilot-relay.sh"
sed -i.bak "s#$REPO/deploy/copilot-relay/run-worker.sh#$DATA_DIR/run-copilot-relay.sh#g" "$PLIST"
rm -f "$PLIST.bak"

launchctl bootout "gui/$(id -u)/ai.ostag.copilot-relay" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/ai.ostag.copilot-relay"
echo "installed $PLIST; credentials remain in $ENV_FILE"
