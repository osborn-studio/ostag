# OS Tag osstack Execution Board

This board binds `docs/fork_deep_rebuild_plan.md` to concrete osstack task
queue files. It is the active management surface for the deep-fork effort.

Important rule: keep only the current phase task in `tasks/` when running
`osstack-loop.sh run`. Move all other phase task files to `tasks/paused/`.
osstack's task queue runner discovers every JSON task in `tasks/`, so a generic
run against a full queue can incorrectly attempt all phases.

## Active Goal

Use osstack to manage development, testing, verification, and launch for the OS
Tag deep fork described in `docs/fork_deep_rebuild_plan.md`.

## Current Queue

| Phase | osstack task id | Priority | Capability | Status | Required gate |
|---|---|---:|---|---|---|
| Phase 0: Fork baseline and inventory | `task_d57d82ef` | 100 | `code.feature` | completed | `FORK_ORIGIN.md` exists and upstream `examples/open-tag-skill` inventory is complete. |
| Phase 1: Product extraction and rename | `task_2b786dcd` | 95 | `code.feature` | completed | Open Tag runtime names are migrated to OS Tag; attribution remains. |
| Phase 2: Adapter abstraction | `task_49df7528` | 90 | `code.feature` | completed | Feishu/Lark dry-run fixtures pass through `NormalizedEvent`. |
| Phase 3: Backend and MFS migration | `task_1edc63fd` | 88 | `code.feature` | completed | Backends and MFS helpers are migrated/refactored with tests. |
| Phase 4: Safety, audit, doctor, governance | `task_c0be8580` | 86 | `code.feature` | completed | Safety/audit/doctor tests pass; no unsafe defaults. |
| Phase 5: Live Feishu/Lark adapter | `task_657bb669` | 84 | `code.feature` | completed | Live adapter preflight passes; manual validation recorded if credentials exist. |
| Phase 6: CI, packaging, deployment | `task_8c98c03f` | 78 | `code.feature` | completed | Release check, CI, Compose, Docker/launchd deployment paths pass or have documented external blocker. |
| Phase 7: Launch evidence and scorecard | `task_d2859192` | 70 | `code.test` | completed | `release/ostag-launch-evidence.md` and `release/ostag-scorecard.md` exist. |
| Deployment preflight hardening | `task_6a154f84` | 64 | `code.test` | completed | `scripts/deploy_preflight.sh` passes and launchd/live-validation artifacts are generated. |
| Release readiness audit | `task_9e75642a` | 58 | `code.test` | completed | `release/readiness-audit.md` reports `PASS_WITH_EXTERNAL_BLOCKER` with zero failed checks. |
| Prompt contract migration | `task_e5ed23d9` | 56 | `code.feature` | completed | `ostag/core/prompt.py` is migrated, tested, and included in release checks. |
| Memory utility migration | `task_d488dfdc` | 54 | `code.feature` | completed | `ostag/mfs/memory.py` is ported, tested, and exposed through the OS Tag CLI. |
| Release readiness recertification | `task_b57ff6f9` | 52 | `code.test` | completed | `release/readiness-audit.md` reports `95/100`, 12 passing checks, and only the credential-gated live warning. |
| MFS client subprocess verification | `task_4fe88e7f` | 50 | `code.test` | completed | `tests/test_mfs_client.py` covers enabled MFS search through a fake CLI subprocess and release check passes. |
| Release readiness recertification after MFS client verification | `task_ed36600e` | 48 | `code.test` | completed | `release/readiness-audit.md` reports `96/100`, 13 passing checks, and only the credential-gated live warning. |
| Live validation evidence gate | `task_d6e536d7` | 46 | `code.test` | completed | `scripts/validate_live_validation_record.sh` rejects incomplete manual records and reports credential-gated blocked status when secrets are absent. |
| Live validation gate pass-path verification | `task_cbcf6d63` | 44 | `code.test` | completed | Synthetic manual pass record, audit log, and transcript evidence pass `scripts/validate_live_validation_record.sh`. |
| Manual live validation handoff package | `task_29f2ca9f` | 42 | `code.test` | completed | `release/manual-live-validation-handoff.md` gives the human operator a single runnable validation path. |
| CI release gates coverage | `task_1d181656` | 40 | `code.test` | completed | GitHub Actions now runs `scripts/ci_release_gates.sh`, covering deploy preflight, live evidence gates, and handoff checks. |
| Structured release manifest | `task_0034b624` | 38 | `code.test` | completed | `release/release-manifest.json` records artifacts, gates, score, blockers, and archived osstack tasks. |
| Production-candidate promotion gate | `task_52cc9ad0` | 36 | `code.test` | completed | `scripts/promote_production_candidate.sh` refuses incomplete live records and generates a production-candidate report when evidence is complete. |
| Docker image verification | `task_1d0171a0` | 34 | `code.test` | completed | Docker image builds and runs Feishu dry-run doctor plus reply inside the container. |
| Release bundle packaging | `task_b607da9d` | 32 | `code.test` | completed | `release/ostag-release-bundle.tar.gz` and `.sha256` are generated and verified without secrets or fixture artifacts. |
| MFS live smoke gate | `task_3f9f86a2` | 30 | `code.test` | completed | `release/mfs-validation.md` records a live MFS smoke pass or a structured external blocker. |
| Completion audit gate | `task_c754fc3a` | 28 | `code.test` | completed | `release/completion-audit.md` maps `docs/fork_deep_rebuild_plan.md` Definition of Done to current evidence. |
| Launch status gate | `task_9db34f83` | 26 | `code.test` | completed | `scripts/launch_status.sh` produces a single exit-code contract for production-candidate, failed, or externally blocked status. |
| Production scope gate | `task_0cd7cac4` | 24 | `code.test` | completed | `release/production-scope.json` records whether MFS is required or explicitly excluded from production-candidate scope. |
| External validation handoff gate | `task_c657d330` | 22 | `code.test` | completed | `release/external-validation-handoff.md` gives the human validator a single executable checklist for remaining live blockers. |
| Secret hygiene gate | `task_73b45f33` | 20 | `code.test` | completed | `release/secret-hygiene.md` verifies the release bundle and formal handoff artifacts contain no detected secret-like values. |

## Execution Order

Run tasks in priority order. Phase 0 is mandatory before code migration because
the plan requires real fork provenance.

Recommended command pattern:

```bash
/Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/osstack-loop.sh run \
  --source task_queue \
  --execute "<phase-specific implementation command>" \
  --verify "<phase-specific verifier>" \
  --archive-completed \
  --max-iter 1 \
  --cycles 1 \
  --stop-when-idle
```

Use a phase-specific command. Do not use `scripts/release_check.sh` alone as the
execute step for Phase 0-7, because that would mark planning tasks complete
without doing the actual fork/migration work.

Queue isolation pattern:

```bash
mkdir -p tasks/paused
mv tasks/task_*.json tasks/paused/
mv tasks/paused/<current-phase-task>.json tasks/
```

After the phase is archived, move the next phase task back into `tasks/`.

## Phase Verifiers

| Phase | Minimum verifier |
|---|---|
| Phase 0 | `test -f FORK_ORIGIN.md && git remote -v && git log --oneline -- examples/open-tag-skill | head` |
| Phase 1 | `python -m compileall ostag && rg "OPENTAG|OpenClaude|OpenCodex" ostag` must return no runtime hits. |
| Phase 2 | `uv run pytest tests/test_event_normalization.py` plus Feishu/Lark dry-runs. |
| Phase 3 | `uv run pytest tests/test_mock_backend.py` plus backend doctor checks. |
| Phase 4 | `uv run pytest tests/test_safety.py tests/test_doctor.py` plus secret-redaction checks. |
| Phase 5 | `uv sync --extra feishu && uv run python -m ostag.main --platform feishu --doctor && uv run pytest`; live manual note if credentials exist. |
| Phase 6 | `scripts/release_check.sh && docker compose config`; `docker build` when Docker daemon is running. |
| Phase 7 | `test -f release/ostag-launch-evidence.md && test -f release/ostag-scorecard.md`. |
| Deployment preflight hardening | `scripts/osstack_deploy_preflight_verify.sh`. |
| Release readiness audit | `scripts/osstack_release_readiness_verify.sh`. |
| Prompt contract migration | `scripts/osstack_prompt_contract_verify.sh`. |
| Memory utility migration | `scripts/osstack_memory_utility_verify.sh`. |
| Release readiness recertification | `scripts/osstack_release_readiness_verify.sh`. |
| MFS client subprocess verification | `scripts/osstack_mfs_client_verify.sh`. |
| Live validation evidence gate | `scripts/osstack_live_validation_gate_verify.sh`. |
| Live validation gate pass-path verification | `scripts/osstack_live_validation_gate_verify.sh`. |
| Manual live validation handoff package | `scripts/osstack_manual_live_handoff_verify.sh`. |
| CI release gates coverage | `scripts/osstack_ci_release_gates_verify.sh`. |
| Structured release manifest | `scripts/osstack_release_manifest_verify.sh`. |
| Production-candidate promotion gate | `scripts/osstack_production_promotion_gate_verify.sh`. |
| Docker image verification | `scripts/osstack_docker_image_verify.sh`. |
| Release bundle packaging | `scripts/osstack_release_bundle_verify.sh`. |
| MFS live smoke gate | `scripts/osstack_mfs_live_gate_verify.sh`. |
| Completion audit gate | `scripts/osstack_completion_audit_verify.sh`. |
| Launch status gate | `scripts/osstack_launch_status_verify.sh`. |
| Production scope gate | `scripts/osstack_production_scope_verify.sh`. |
| External validation handoff gate | `scripts/osstack_external_validation_handoff_verify.sh`. |
| Secret hygiene gate | `scripts/osstack_secret_hygiene_verify.sh`. |

## Current Score

Current deep-fork score against the rubric: `production candidate passed for the
current Feishu/Lark OS Tag scope`.

Blocking gaps:

- None for the current production candidate scope.
- MFS is explicitly excluded from this production candidate because the Rust
  `mfs` CLI / `cargo` toolchain is not available on this host. The MFS adapter
  remains implemented and covered by fake CLI subprocess tests; live MFS can be
  promoted in a follow-up candidate once the binary is provisioned.

## Incident Log

- A first osstack run attempted all queued phase tasks because the queue runner
  discovers every task JSON in `tasks/`. Those original task ids are preserved in
  `tasks/paused/` and `STATE.md` as escalated audit history.
- The corrected operating model is queue isolation: only one active phase task
  remains in `tasks/` at a time.
- Phase 0 was retried as `task_d57d82ef` and completed successfully.
- Phase 1 was retried as `task_2b786dcd` with a stable verifier script and
  completed successfully. The original `task_8f3de655` remains in
  `tasks/paused/` as escalated audit history.
- Phase 2 completed successfully as `task_49df7528`.
- Phase 3 completed successfully as `task_1edc63fd`.
- Phase 4 completed successfully as `task_c0be8580`.
- Phase 5 completed successfully as `task_657bb669`; live manual group
  validation remains credential-gated.
- Phase 6 completed successfully as `task_8c98c03f`; Docker Compose config and
  release check pass, while local Docker build was canceled during base image
  metadata wait.
- Phase 7 completed successfully as `task_d2859192`; evidence and scorecard are
  available in `/Users/gilbertchen/Documents/CODE/ostag-deep-fork/release/`.
- Post-phase hardening completed as `task_deff9f0d`; fork release scripts are
  cwd-independent and release evidence was re-run.
- Live validation kit completed as `task_938dc237`; credential-gated validation
  records can be generated with `scripts/live_validation_record.sh`.
- Deployment preflight hardening completed as `task_6a154f84`; osstack archived
  the task after `scripts/osstack_deploy_preflight_verify.sh` passed. The loop
  process returned exit code 2 only because historical escalated tasks remain in
  durable state, not because this task failed.
- Release readiness audit completed as `task_9e75642a`; osstack archived the
  task after `scripts/osstack_release_readiness_verify.sh` passed. The generated
  audit report is `release/readiness-audit.md` in the fork repo.
- Prompt contract migration completed as `task_e5ed23d9`; `runtime-agent.md`
  is now represented by `ostag/core/prompt.py` and `tests/test_prompt.py`.
- Memory utility migration completed as `task_d488dfdc`; `opentag_memory.py`
  is now represented by `ostag/mfs/memory.py`, `tests/test_memory.py`, and the
  `python -m ostag.main memory ...` CLI.
- Release readiness recertification completed as `task_b57ff6f9`; readiness
  audit now reports `95/100`, 12 passing checks, and only the credential-gated
  manual live validation warning.
- MFS client subprocess verification completed as `task_4fe88e7f`; enabled MFS
  search is now covered by fake CLI subprocess tests for success, failed-scope
  tolerance, and result limiting.
- Release readiness recertification after MFS client verification completed as
  `task_ed36600e`; readiness audit now reports `96/100`, 13 passing checks, and
  only the credential-gated manual live validation warning.
- Live validation evidence gate completed as `task_d6e536d7`; current
  credential-free validation correctly returns blocked exit code 3, while filled
  manual records must satisfy chat/message/backend/audit/evidence checks.
- Live validation gate pass-path verification completed as `task_cbcf6d63`;
  the verifier creates a synthetic complete manual record with audit log and
  transcript evidence and confirms the gate passes.
- Manual live validation handoff package completed as `task_29f2ca9f`; the
  handoff file consolidates credential setup, live startup, group message,
  validation record fields, final gate, and rollback commands.
- CI release gates coverage completed as `task_1d181656`; GitHub Actions now
  runs `scripts/ci_release_gates.sh` to cover deploy preflight, blocked/pass live
  validation gates, and handoff checks.
- Structured release manifest completed as `task_0034b624`; `release/release-manifest.json`
  now provides a machine-readable launch bundle index.
- Production-candidate promotion gate completed as `task_52cc9ad0`; promotion
  is blocked for the current credential-free record and succeeds for a synthetic
  complete manual validation record.
- Docker image verification completed as `task_1d0171a0`; `scripts/docker_image_verify.sh`
  builds `ostag:deep-fork` and validates in-container Feishu dry-run doctor and
  reply behavior.
- Release bundle packaging completed as `task_b607da9d`; `scripts/package_release_bundle.sh`
  and `scripts/validate_release_bundle.sh` generate and verify the handoff tarball
  plus sha256 checksum while excluding secrets, logs, synthetic live fixtures,
  and production-candidate fixtures.
- MFS live smoke gate completed as `task_3f9f86a2`; `scripts/mfs_validation_record.sh`
  and `scripts/validate_mfs_validation_record.sh` now record either a real live
  MFS search pass or a structured external blocker.
- Completion audit gate completed as `task_c754fc3a`; `scripts/generate_completion_audit.py`
  and `scripts/validate_completion_audit.py` map the plan Definition of Done to
  current evidence and keep the goal open while external live blockers remain.
- Launch status gate completed as `task_9db34f83`; `scripts/launch_status.sh`
  now returns `0` for production-candidate, `2` for automated release failure,
  and `3` for externally blocked launch status.
- Production scope gate completed as `task_0cd7cac4`; `scripts/production_scope_record.sh`
  and `scripts/validate_production_scope.sh` make MFS production inclusion or
  exclusion an explicit auditable launch input.
- External validation handoff gate completed as `task_c657d330`; `scripts/external_validation_handoff.sh`
  and `scripts/validate_external_validation_handoff.sh` consolidate remaining
  live blockers into one human-validator checklist.
- Secret hygiene gate completed as `task_73b45f33`; `scripts/secret_hygiene_check.sh`
  and `scripts/validate_secret_hygiene.sh` verify release handoff artifacts do
  not contain detected secret-like values. The first attempt `task_b2ca50a9`
  remains in paused history after a script executable-bit mismatch.
- Feishu/Lark live validation was completed on 2026-06-28 in chat
  `oc_1ae82ede7eb6fe9a2cf644b43b97611b`; the LaunchAgent
  `ai.ostag.feishu` is running under launchd and replied to production
  validation message `om_x100b6b26971d54a0c31106829e5085f` with
  `om_x100b6b26973e08a4c03bc167d84d75b`.

## Status Commands

```bash
/Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/loop-doctor.sh
find tasks -maxdepth 2 -type f -name 'task_*.json' -print
/Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/osstack-loop.sh status
```
