#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

test -f release/secret-hygiene.md
test -f release/secret-hygiene.json
rg -q '"status": "pass"' release/secret-hygiene.json
rg -q "No secret-like values" release/secret-hygiene.md

echo "secret hygiene validation passed"
