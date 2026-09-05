# GitHub PR Review + Vercel Production Release Implementation Plan

Date: 2026-08-17

Branch: `codex/github-vercel-pr-release`

Spec: `docs/superpowers/specs/2026-08-17-github-vercel-pr-release-design.md`

## Architecture Decisions

- The chat mention automatically prepares evidence and a high-risk receipt; it does not bypass
  OStag's `approve -> apply` governance.
- The receipt freezes repository, PR number, head ref, and head SHA. Apply re-reads GitHub and
  refuses drift before either provider write.
- One composite executor orders GitHub review before Vercel Production deployment and records
  bounded, non-secret evidence for partial failure.
- GitHub continues through authenticated `gh`; Vercel uses its official REST API with a server-only
  Access Token and fixed endpoint.
- The initial configuration is deliberately one repository to one Vercel project, with explicit
  `platform:chat_id` admission.

## Task 1: Configuration contract

**Acceptance criteria:**

- [ ] All release settings default disabled/empty and load from documented environment variables.
- [ ] Enabling the workflow without repository, channel, token, team, project ID, or project name
  fails startup with a targeted message.
- [ ] Repository/channel syntax and timeout bounds are validated.

**Verification:** `uv run pytest -q tests/test_config.py tests/test_github_vercel_release.py`

**Files:** `ostag/config.py`, `.env.example`, `tests/test_config.py`,
`tests/test_github_vercel_release.py`

## Task 2: Provider contracts and strict validation

**Acceptance criteria:**

- [ ] GitHub resolves an open, same-repository PR source and produces a SHA-bound APPROVE request.
- [ ] Malformed/oversized/wrong-SHA GitHub responses fail closed.
- [ ] Vercel creates the documented Production deployment payload, polls boundedly, and accepts
  only READY production deployments with an assigned alias.
- [ ] Vercel exceptions cannot contain the Access Token.

**Verification:** `uv run pytest -q tests/test_github_vercel_release.py tests/test_github_connector.py`

**Files:** `ostag/connectors/github.py`, `ostag/connectors/vercel.py`,
`tests/test_github_vercel_release.py`

## Checkpoint: Provider boundary

- [ ] Focused tests pass.
- [ ] Ruff passes for touched Python files.
- [ ] No real provider call was made.

## Task 3: Receipt preparation and composite apply

**Acceptance criteria:**

- [ ] A valid allowlisted group notification creates exactly one high-risk receipt with frozen
  source evidence and no mutation.
- [ ] Private/non-allowlisted/wrong-repository/draft/fork/check-failing inputs are deterministic
  refusals.
- [ ] Apply revalidates SHA/head ref/base ref and the configured trusted checks,
  then orders GitHub APPROVE before Vercel deploy.

**Verification:** `uv run pytest -q tests/test_github_vercel_release.py tests/test_approvals.py`

**Files:** `ostag/workflows/github_vercel_release.py`, `ostag/connectors/apply.py`,
`tests/test_github_vercel_release.py`

## Task 4: Runner integration and operator documentation

**Acceptance criteria:**

- [ ] Runner routes the deterministic workflow before MFS/general backend handling.
- [ ] Proposal/audit/run completion and existing receipt permission/quorum/four-eyes behavior remain
  intact.
- [ ] README, connector/deployment docs, and `.env.example` explain setup, permissions, evidence,
  rollback, and the absence of live validation without credentials.

**Verification:** focused runner test plus documentation/config review.

**Files:** `ostag/core/runner.py`, `README.md`, `docs/github_connector.md`,
`docs/ostag/deployment.md`, `.env.example`, `tests/test_github_vercel_release.py`

## Checkpoint: End-to-end local flow

- [ ] Fake event -> receipt -> approve -> apply -> GitHub evidence -> Vercel READY evidence passes.
- [ ] Failed/drifted cases perform no downstream Vercel write.
- [ ] Existing GitHub issue connector tests remain green.

## Task 5: Release verification

**Acceptance criteria:**

- [ ] `uv run pytest -q` passes with no skipped new tests.
- [ ] `uv run ruff check .` passes.
- [ ] `scripts/release_check.sh` passes or reports a pre-existing/external blocker precisely.
- [ ] Git diff secret scan finds no credentials.
- [ ] Real Production deployment is not claimed without an explicitly authorized target and token.

**Verification:** commands above plus `git diff --check` and staged diff review.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| PR changes after group approval | Wrong code approved/deployed | Freeze SHA/ref and re-read immediately before mutation |
| Unauthorized group/repository | Privilege escalation | Explicit channel/repository allowlists and trusted adapter identity |
| Checks missing, untrusted, pending, or failed | Unsafe release | Require the configured trusted checks and require all observed checks passed |
| GitHub succeeds, Vercel fails | Partial completion | Ordered execution, failed receipt evidence, no false success claim |
| Vercel token leakage | Account compromise | Server-only env, fixed endpoint, bounded sanitized errors, secret scan |
| Deployment ready but not serving | False online claim | Require production target and assigned alias |
| Chat handler waits too long | Poor UX / timeout | Bounded configurable deployment poll; surface timeout as failed evidence |

## Rollback

Set `OSTAG_GITHUB_VERCEL_RELEASE_ENABLED=false` and restart OStag. This removes admission of new
release notifications without changing historical ledger evidence or existing GitHub issue support.
No database migration or provider-side permission change is introduced by the code rollout.
