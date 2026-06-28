# OS Tag Launch Evidence

Generated for the deep-fork effort managed from
`/Users/gilbertchen/Documents/CODE/ostag`.

## Fork Provenance

- Upstream: `https://github.com/zilliztech/mfs`
- Baseline commit: `16574f9b365bc67f3716c552dad416da730088b8`
- Fork workspace: `/Users/gilbertchen/Documents/CODE/ostag-deep-fork`
- Branch: `ostag/deep-fork`
- Evidence: `FORK_ORIGIN.md`
- Migration report: `docs/fork_migration_report.md`

## Automated Verification

Release check:

```bash
scripts/release_check.sh
```

Result:

```text
31 passed
Feishu dry-run printed OS Tag mock reply
Lark dry-run printed OS Tag mock reply
Doctor reported safe dry-run defaults
```

Compose config:

```bash
docker compose config
```

Result: passed.

Deployment preflight:

```bash
scripts/deploy_preflight.sh
```

Result: passed. It ran release check, Docker Compose static validation,
launchd plist rendering/linting, and credential-gated live validation record
generation.

Generated deployment artifacts:

- `deploy/launchd/ai.ostag.feishu.plist.template`
- `release/ai.ostag.feishu.plist`

CI release gates:

```bash
scripts/ci_release_gates.sh
```

Result: passed locally and wired into `.github/workflows/ostag-ci.yml`.

Docker image build:

```bash
scripts/docker_image_verify.sh ostag:deep-fork
```

Result: passed. The image builds and runs Feishu dry-run doctor plus Feishu
dry-run reply inside the container.

Structured release manifest:

```bash
uv run python scripts/generate_release_manifest.py
uv run python scripts/validate_release_manifest.py
```

Result: passed.

Release bundle:

```bash
scripts/package_release_bundle.sh
scripts/validate_release_bundle.sh
```

Result: passed via `task_b607da9d`. The generated bundle excludes local secrets,
logs, synthetic live-validation fixtures, and production-candidate fixtures.
Generated artifacts:

- `release/ostag-release-bundle.tar.gz`
- `release/ostag-release-bundle.sha256`

MFS live smoke gate:

```bash
scripts/mfs_validation_record.sh
scripts/validate_mfs_validation_record.sh
```

Current result without live MFS configuration: blocked with exit code `3` and a
structured record at `release/mfs-validation.md`. The gate passes when
`MFS_ENABLED=true`, `MFS_BIN` is executable, and a live allowlisted MFS search
returns exit code `0`. osstack verification passed via `task_3f9f86a2`.

Completion audit:

```bash
uv run python scripts/generate_completion_audit.py
uv run python scripts/validate_completion_audit.py
```

Current result: `PASS_WITH_EXTERNAL_BLOCKER` with 10 checks passed, 2 external
blockers, 0 pending checks, and 0 failed checks. The audit is the authoritative
matrix for deciding whether the full `docs/fork_deep_rebuild_plan.md` objective
can be marked complete.

Launch status:

```bash
scripts/launch_status.sh
```

Current result: `blocked_external` with exit code `3`. The script is the
single-command launch裁决 gate: exit `0` means production-candidate gates are
complete, exit `2` means automated release failure, and exit `3` means automated
gates pass but external live validation is still blocked.

Production scope:

```bash
scripts/production_scope_record.sh
scripts/validate_production_scope.sh
```

Current result: MFS is `required` by default. MFS can only be excluded from the
production-candidate gate through an explicit `release/production-scope.json`
decision with owner and rationale.

External validation handoff:

```bash
scripts/external_validation_handoff.sh
scripts/validate_external_validation_handoff.sh
```

Current result: generated. The handoff consolidates launch status, blocking
conditions, operator commands, evidence fields, and the final `launch_status.sh`
completion rule for the human validator.

Secret hygiene:

```bash
scripts/secret_hygiene_check.sh
scripts/validate_secret_hygiene.sh
```

Current result: passed. The release bundle and formal handoff artifacts contain
zero detected secret-like values.

Production-candidate promotion gate:

```bash
scripts/promote_production_candidate.sh feishu
```

Current result without credentials: blocked by the live validation gate. The
osstack verifier also proves a synthetic complete live record can produce
`release/production-candidate-report.md`.

Release readiness audit:

```bash
scripts/release_readiness_audit.sh
```

Result: `PASS_WITH_EXTERNAL_BLOCKER` with 14 checks passed, 3 warnings, and 0
failures. The warnings are the credential-gated manual group validation and
the live MFS smoke configuration gate.

Generated audit artifact:

- `release/readiness-audit.md`
- `release/manual-live-validation-handoff.md`
- `release/release-manifest.json`
- `release/mfs-validation.md`
- `release/completion-audit.md`
- `release/completion-audit.json`
- `release/launch-status.md`
- `release/launch-status.json`
- `release/production-scope.md`
- `release/production-scope.json`
- `release/external-validation-handoff.md`
- `release/external-validation-handoff.json`
- `release/secret-hygiene.md`
- `release/secret-hygiene.json`

Live preflight without credentials:

```bash
uv run python -m ostag.main --platform feishu --live --doctor
```

Result: failed correctly with missing `FEISHU_APP_ID` and
`FEISHU_APP_SECRET`.

Credential-gated live validation record:

```bash
scripts/live_validation_record.sh feishu
```

Result without credentials: writes a blocked validation record under `release/`.

Current generated record:

- `release/live-validation-feishu.md`

Manual live validation gate:

```bash
scripts/validate_live_validation_record.sh feishu
```

Current result without credentials: blocked with exit code `3`. After real
manual group validation, this gate must pass before production-candidate status.
The osstack verifier also creates a synthetic passing record under
`release/live-validation-gate-fixture/` to prove the pass path works when manual
evidence is complete.

Human handoff:

- `release/manual-live-validation-handoff.md`

## osstack Evidence

Completed phase tasks:

- Phase 0: `task_d57d82ef`
- Phase 1: `task_2b786dcd`
- Phase 2: `task_49df7528`
- Phase 3: `task_1edc63fd`
- Phase 4: `task_c0be8580`
- Phase 5: `task_657bb669`
- Phase 6: `task_8c98c03f`
- Post-phase hardening: `task_deff9f0d`
- Live validation kit: `task_938dc237`
- Deployment preflight hardening: `task_6a154f84`
- Release readiness audit: `task_9e75642a`
- Prompt contract migration: `task_e5ed23d9`
- Memory utility migration: `task_d488dfdc`
- Release readiness recertification: `task_b57ff6f9`
- MFS client subprocess verification: `task_4fe88e7f`
- Release readiness recertification after MFS client verification: `task_ed36600e`
- Live validation gate: `task_d6e536d7`
- Live validation gate pass-path verification: `task_cbcf6d63`
- Production-candidate promotion gate: `task_52cc9ad0`
- Manual live validation handoff: `task_29f2ca9f`
- CI release gates coverage: `task_1d181656`
- Structured release manifest: `task_0034b624`
- Docker image verification: `task_1d0171a0`
- Release bundle packaging: `task_b607da9d`
- MFS live smoke gate: `task_3f9f86a2`
- Completion audit gate: `task_c754fc3a`
- Launch status gate: `task_9db34f83`
- Production scope gate: `task_0cd7cac4`
- External validation handoff gate: `task_c657d330`
- Secret hygiene gate: `task_73b45f33`

Phase 7 evidence generation is tracked by `task_d2859192`.

Historical escalated tasks remain in `STATE.md` because an early osstack run
attempted every queued phase at once. The corrected queue isolation procedure is
documented in `docs/osstack_execution_board.md`.

## Known External Gaps

- Feishu/Lark manual group validation requires real app credentials and a test
  group. The evidence template and generator are present.
- Docker image build and in-container dry-run verification pass with
  `scripts/docker_image_verify.sh`.
- Live MFS server integration is now guarded by `release/mfs-validation.md`.
  The current environment records a structured external blocker because live
  MFS is not enabled; local scoped helpers and channel memory remain covered by
  automated tests.

## Launch Position

This build is pilot-ready by automated gates. It is not yet a
production-candidate until live Feishu or Lark group validation is recorded.
