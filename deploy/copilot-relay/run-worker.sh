#!/bin/zsh
set -euo pipefail

ENV_FILE="${OSTAG_COPILOT_CONFIG_FILE:-/Users/Shared/OStag/copilot-relay.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  print -u2 "missing relay environment file: $ENV_FILE"
  exit 78
fi
set -a
source "$ENV_FILE"
set +a
exec "/opt/ostag/.venv/bin/python" -m ostag.copilot_relay.worker
