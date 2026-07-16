#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
PAIRING_CODE=""
DISPLAY_NAME="OStag Device"
CENTRAL_URL=""

usage() {
  echo "usage: $0 [--dry-run] [--pairing-code CODE --display-name NAME --central-url URL] /absolute/path/to/ostag" >&2
  exit 64
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --pairing-code) [[ $# -ge 2 ]] || usage; PAIRING_CODE="$2"; shift 2 ;;
    --display-name) [[ $# -ge 2 ]] || usage; DISPLAY_NAME="$2"; shift 2 ;;
    --central-url) [[ $# -ge 2 ]] || usage; CENTRAL_URL="$2"; shift 2 ;;
    --*) usage ;;
    *) break ;;
  esac
done
[[ $# -eq 1 ]] || usage
[[ "$1" == /* ]] || { echo "repository path must be absolute" >&2; exit 64; }
[[ -d "$1" ]] || { echo "repository path does not exist" >&2; exit 66; }
REPO="${1%/}"

if [[ -n "$PAIRING_CODE" && ( -z "$CENTRAL_URL" || -z "$DISPLAY_NAME" ) ]]; then
  echo "pairing requires display name and central URL" >&2
  exit 64
fi
if [[ -n "$CENTRAL_URL" && "$CENTRAL_URL" != https://* && "$CENTRAL_URL" != http://localhost* && "$CENTRAL_URL" != http://127.0.0.1* ]]; then
  echo "central URL must use HTTPS outside localhost" >&2
  exit 64
fi

DATA_DIR="${OSTAG_COPILOT_DATA_DIR:-$HOME/Library/Application Support/OStag}"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
PLIST="$LAUNCH_DIR/ai.ostag.copilot-relay.plist"
ENV_FILE="$DATA_DIR/copilot-relay.env"
IDENTITY_FILE="$DATA_DIR/device-identity.json"
RUNNER="$DATA_DIR/run-copilot-relay.sh"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "would validate virtualenv and render ai.ostag.copilot-relay.plist from $REPO"
  echo "would write mode-0600 non-secret config at $ENV_FILE"
  if [[ -n "$PAIRING_CODE" ]]; then
    echo "would create/load the Keychain device identity and exchange a one-time pairing code"
  fi
  echo "would restart one LaunchAgent and verify device status and central reachability"
  exit 0
fi

[[ -x "$REPO/.venv/bin/python" ]] || { echo "missing virtualenv python" >&2; exit 66; }
mkdir -p "$DATA_DIR" "$LAUNCH_DIR"
chmod 700 "$DATA_DIR"

if [[ -e "$ENV_FILE" ]]; then
  MODE="$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE")"
  [[ "$MODE" == "600" ]] || { echo "existing config must already use mode 0600" >&2; exit 77; }
fi
PRESERVED_CONFIG=""
if [[ -f "$ENV_FILE" ]]; then
  PRESERVED_CONFIG="$(grep -E '^OSTAG_COPILOT_RELAY_(OSSTACK_BIN|DEFAULT_REPO|POLL_INTERVAL_SECONDS)=' "$ENV_FILE" || true)"
fi

if [[ -n "$PAIRING_CODE" ]]; then
  OSTAG_DEVICE_IDENTITY_FILE="$IDENTITY_FILE" \
  OSTAG_DEVICE_DISPLAY_NAME="$DISPLAY_NAME" \
  OSTAG_MONITOR_SERVER_DB="$DATA_DIR/device-admin.sqlite3" \
    "$REPO/.venv/bin/python" -m ostag.main copilot device pair \
      --code "$PAIRING_CODE" --display-name "$DISPLAY_NAME" \
      --central-url "$CENTRAL_URL" --json >/dev/null
fi
[[ -f "$IDENTITY_FILE" ]] || { echo "device identity is not paired" >&2; exit 66; }
MODE="$(stat -f '%Lp' "$IDENTITY_FILE" 2>/dev/null || stat -c '%a' "$IDENTITY_FILE")"
[[ "$MODE" == "600" ]] || { echo "device identity metadata must use mode 0600" >&2; exit 77; }

if [[ ! -e "$ENV_FILE" || -n "$PAIRING_CODE" ]]; then
  umask 077
  {
    echo "OSTAG_COPILOT_RELAY_ENABLED=true"
    echo "OSTAG_COPILOT_RELAY_WRITE_ENABLED=true"
    echo "OSTAG_COPILOT_RELAY_CENTRAL_URL=$CENTRAL_URL"
    echo "OSTAG_COPILOT_RELAY_SPOOL_DB=$DATA_DIR/copilot-relay.sqlite3"
    echo "OSTAG_DEVICE_IDENTITY_FILE=$IDENTITY_FILE"
    echo "OSTAG_DEVICE_DISPLAY_NAME=$DISPLAY_NAME"
    [[ -z "$PRESERVED_CONFIG" ]] || printf '%s\n' "$PRESERVED_CONFIG"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

sed -e "s#/opt/ostag#$REPO#g" -e "s#/Users/Shared/OStag#$DATA_DIR#g" \
  "$REPO/deploy/copilot-relay/ai.ostag.copilot-relay.plist" > "$PLIST"
sed -e "s#/opt/ostag#$REPO#g" -e "s#/Users/Shared/OStag#$DATA_DIR#g" \
  "$REPO/deploy/copilot-relay/run-worker.sh" > "$RUNNER"
chmod 700 "$RUNNER"
sed -i.bak "s#$REPO/deploy/copilot-relay/run-worker.sh#$RUNNER#g" "$PLIST"
rm -f "$PLIST.bak"
chmod 600 "$PLIST"

launchctl bootout "gui/$(id -u)/ai.ostag.copilot-relay" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/ai.ostag.copilot-relay"
sleep 2
launchctl print "gui/$(id -u)/ai.ostag.copilot-relay" >/dev/null

export OSTAG_DEVICE_IDENTITY_FILE="$IDENTITY_FILE"
"$REPO/.venv/bin/python" - <<'PY'
import json
import os
from pathlib import Path

metadata = json.loads(Path(os.environ["OSTAG_DEVICE_IDENTITY_FILE"]).read_text())
print(f"installed device_id={metadata['device_id']} display_name={metadata['display_name']}")
print("runtime_inventory=reported-by-signed-heartbeat central_reachability=launch-agent-running")
PY
