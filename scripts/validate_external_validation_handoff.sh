#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

test -f release/external-validation-handoff.md
test -f release/external-validation-handoff.json

rg -q "scripts/launch_status.sh" release/external-validation-handoff.md
rg -q "scripts/validate_live_validation_record.sh feishu" release/external-validation-handoff.md
rg -q "scripts/validate_mfs_validation_record.sh" release/external-validation-handoff.md
rg -q "MFS production decision" release/external-validation-handoff.md
rg -q '"required_operator_commands"' release/external-validation-handoff.json
rg -q '"launch_exit_code": (0|3)' release/external-validation-handoff.json

echo "external validation handoff validation passed"
