#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE="release/ostag-release-bundle.tar.gz"
CHECKSUM="release/ostag-release-bundle.sha256"

cd "$ROOT"

rm -f "$BUNDLE" "$CHECKSUM"

bash scripts/production_scope_record.sh >/dev/null
uv run python scripts/generate_release_manifest.py
uv run python scripts/validate_release_manifest.py
OSTAG_COMPLETION_AUDIT_ALLOW_MISSING_BUNDLE=1 uv run python scripts/generate_completion_audit.py

bundle_paths=(
  "FORK_ORIGIN.md"
  "README.md"
  "LICENSE"
  "NOTICE"
  "pyproject.toml"
  "uv.lock"
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
  "scripts/generate_release_manifest.py"
  "scripts/validate_release_manifest.py"
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

for path in "${bundle_paths[@]}"; do
  if [[ ! -e "$path" ]]; then
    echo "missing bundle input: $path" >&2
    exit 1
  fi
done

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

tar -czf "$tmp_dir/ostag-release-bundle.tar.gz" "${bundle_paths[@]}"
mv "$tmp_dir/ostag-release-bundle.tar.gz" "$BUNDLE"
shasum -a 256 "$BUNDLE" > "$CHECKSUM"

uv run python scripts/generate_release_manifest.py
uv run python scripts/validate_release_manifest.py
bash scripts/validate_production_scope.sh
uv run python scripts/generate_completion_audit.py
uv run python scripts/validate_completion_audit.py
bash scripts/secret_hygiene_check.sh

tar -czf "$tmp_dir/ostag-release-bundle.tar.gz" "${bundle_paths[@]}"
mv "$tmp_dir/ostag-release-bundle.tar.gz" "$BUNDLE"
shasum -a 256 "$BUNDLE" > "$CHECKSUM"

echo "$BUNDLE"
echo "$CHECKSUM"
