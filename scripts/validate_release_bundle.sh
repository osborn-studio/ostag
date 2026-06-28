#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE="release/ostag-release-bundle.tar.gz"
CHECKSUM="release/ostag-release-bundle.sha256"

cd "$ROOT"

test -f "$BUNDLE"
test -f "$CHECKSUM"
shasum -a 256 -c "$CHECKSUM"

listing="$(mktemp)"
trap 'rm -f "$listing"' EXIT
tar -tzf "$BUNDLE" | sort > "$listing"

required_paths=(
  "FORK_ORIGIN.md"
  "README.md"
  "LICENSE"
  "NOTICE"
  ".env.example"
  "Dockerfile"
  "docker-compose.yml"
  ".github/workflows/ostag-ci.yml"
  "docs/fork_migration_report.md"
  "docs/ostag/acceptance_criteria.md"
  "docs/ostag/architecture.md"
  "docs/ostag/deployment.md"
  "docs/ostag/live_validation.md"
  "docs/ostag/osstack_execution_board.md"
  "release/ostag-scorecard.md"
  "release/readiness-audit.md"
  "release/ostag-launch-evidence.md"
  "release/release-manifest.json"
  "release/live-validation-feishu.md"
  "release/live-validation-feishu-transcript-20260628.txt"
  "release/launchd-production-validation-20260628.txt"
  "release/mfs-validation.md"
  "release/production-scope.md"
  "release/production-scope.json"
  "release/completion-audit.md"
  "release/completion-audit.json"
  "release/launch-status.md"
  "release/launch-status.json"
  "release/manual-live-validation-handoff.md"
  "release/external-validation-handoff.md"
  "release/external-validation-handoff.json"
  "release/secret-hygiene.md"
  "release/secret-hygiene.json"
  "release/ai.ostag.feishu.plist"
  "scripts/release_check.sh"
  "scripts/deploy_preflight.sh"
  "scripts/ci_release_gates.sh"
  "scripts/live_validation_record.sh"
  "scripts/validate_live_validation_record.sh"
  "scripts/mfs_validation_record.sh"
  "scripts/validate_mfs_validation_record.sh"
  "scripts/promote_production_candidate.sh"
  "scripts/docker_image_verify.sh"
  "scripts/generate_completion_audit.py"
  "scripts/validate_completion_audit.py"
  "scripts/launch_status.sh"
  "scripts/production_scope_record.sh"
  "scripts/validate_production_scope.sh"
  "scripts/external_validation_handoff.sh"
  "scripts/validate_external_validation_handoff.sh"
  "scripts/secret_hygiene_check.sh"
  "scripts/validate_secret_hygiene.sh"
)

for path in "${required_paths[@]}"; do
  if ! grep -Fxq "$path" "$listing"; then
    echo "bundle missing required path: $path" >&2
    exit 1
  fi
done

if grep -Fxq ".env" "$listing"; then
  echo "bundle contains forbidden path: .env" >&2
  exit 1
fi

for forbidden_prefix in "logs/" "release/live-validation-gate-fixture/"; do
  if grep -Fq "$forbidden_prefix" "$listing"; then
    echo "bundle contains forbidden path: $forbidden_prefix" >&2
    exit 1
  fi
done

if grep -Fxq "release/production-candidate-report.md" "$listing"; then
  echo "bundle contains forbidden path: release/production-candidate-report.md" >&2
  exit 1
fi

uv run python scripts/validate_release_manifest.py
uv run python scripts/validate_completion_audit.py
bash scripts/validate_production_scope.sh
bash scripts/validate_external_validation_handoff.sh
bash scripts/validate_secret_hygiene.sh

echo "release bundle validation passed"
