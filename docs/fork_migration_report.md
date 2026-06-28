# OS Tag Fork Migration Report

Baseline: `16574f9b365bc67f3716c552dad416da730088b8`

## Upstream Open Tag Inventory

| Upstream file | Decision | Notes |
|---|---|---|
| `examples/open-tag-skill/README.md` | rewritten | Replaced root README with OS Tag product README while preserving upstream attribution. |
| `examples/open-tag-skill/open-tag/SKILL.md` | rewritten | Converted admin/operator intent into OS Tag docs under `docs/ostag/`; runtime skill packaging is not carried forward. |
| `examples/open-tag-skill/open-tag/references/backends.md` | rewritten | Backend policy is represented in `ostag/backends/*`, doctor checks, and backend command tests. |
| `examples/open-tag-skill/open-tag/references/memory.md` | rewritten | MFS memory model is represented in docs, `ostag/mfs/client.py`, and `ostag/mfs/helpers.py`. |
| `examples/open-tag-skill/open-tag/references/runtime-agent.md` | rewritten | Runtime prompt contract is represented in `ostag/core/prompt.py` and covered by prompt contract tests. |
| `examples/open-tag-skill/open-tag/references/slack-adapter.md` | rewritten | Used as adapter-boundary reference; runtime implementation now targets Feishu/Lark only. |
| `examples/open-tag-skill/open-tag/scripts/mfs_cat.py` | ported | Scoped read command construction is ported as `build_mfs_cat_command()`. |
| `examples/open-tag-skill/open-tag/scripts/mfs_search.py` | rewritten | Replaced by `ostag/mfs/client.py` with allowlisted CLI search behavior. |
| `examples/open-tag-skill/open-tag/scripts/opentag_agent.py` | rewritten | Replaced by `ostag/core/runner.py` and backend modules. |
| `examples/open-tag-skill/open-tag/scripts/opentag_doctor.py` | rewritten | Replaced by `ostag/core/doctor.py` and `python -m ostag.main --doctor`. |
| `examples/open-tag-skill/open-tag/scripts/opentag_memory.py` | ported | Local markdown memory is ported as `ostag/mfs/memory.py` with Feishu/Lark chat scoping and explicit MFS sync. |
| `examples/open-tag-skill/open-tag/scripts/slack_socket_agent.py` | rewritten | Slack transport pattern replaced by Feishu/Lark adapters. |

## Phase 0 Verification

- Fork directory: `/Users/gilbertchen/Documents/CODE/ostag-deep-fork`
- Branch: `ostag/deep-fork`
- Upstream remote preserved as `upstream`.

## Phase 1 Product Extraction

- OS Tag package added at `ostag/`.
- OS Tag tests added at `tests/test_event_normalization.py`,
  `tests/test_mock_backend.py`, `tests/test_prompt.py`,
  `tests/test_safety.py`, and `tests/test_doctor.py`.
- OS Tag examples added at `examples/feishu_event.json`,
  `examples/lark_event.json`, and `examples/context/project_alpha.md`.
- Root README and `pyproject.toml` now describe OS Tag as the product entrypoint.
- Upstream `examples/open-tag-skill` remains in place for provenance and future
  comparison.

## Phase 2 Adapter Abstraction

- Added `ostag/adapters/base.py` with the runtime-checkable `Adapter` protocol.
- Tightened `NormalizedEvent.platform` to the active Feishu/Lark product
  platforms.
- `create_adapter()` now returns the `Adapter` protocol.
- Feishu and Lark event fixtures pass through the same adapter -> runner ->
  reply path used by dry-run.
- Runtime Slack placeholder code was removed; Slack remains a documented roadmap
  item only.

## Phase 3 Backend And MFS Migration

- Added testable command builders for Codex CLI and Claude CLI backends.
- Verified dangerous CLI flags are absent by default and present only when
  explicitly configured.
- Added `ostag/mfs/helpers.py` to carry scoped MFS search/cat helper behavior.
- `MFSClient` now uses helper command construction for search.
- Backend doctor checks pass for mock and local Codex CLI.

## Phase 4 Safety, Audit, Doctor, Governance

- Added production audit coverage to ensure full input text and secrets are not
  stored in prod-mode audit records.
- Added `docs/ostag/security.md` with default boundaries, audit policy,
  prompt-injection note, and live bot governance checklist.
- Safety and doctor tests cover unsafe switch warnings, live credential errors,
  message length rejection, scope validation, and secret redaction.

## Phase 5 Live Feishu/Lark Adapter

- Added `--live` CLI switch so live doctor checks cannot be masked by default
  dry-run settings.
- Feishu/Lark WebSocket client now receives the configured API base URL/domain.
- Added `scripts/live_smoke.sh` to run credential-gated live doctor and startup.
- Without credentials, live doctor fails clearly; with credentials, the same
  command becomes the preflight gate for manual group validation.

## Phase 6 CI, Packaging, Deployment

- Added GitHub Actions workflow at `.github/workflows/ostag-ci.yml`.
- Added `CHANGELOG.md` for the deep-fork baseline.
- Release check and Docker Compose config are the package/deploy gates.
- Added `deploy/launchd/ai.ostag.feishu.plist.template`.
- Added `scripts/render_launchd_plist.sh` to generate a host-specific launchd
  plist under `release/`.
- Added `scripts/deploy_preflight.sh` to run release check, Compose validation,
  launchd rendering, and credential-gated live validation record generation as a
  single repeatable launch gate.
- Docker image build is supported by `Dockerfile`; local verification depends on
  a running Docker daemon.
- Pytest discovery is scoped to OS Tag tests so the product gate does not
  accidentally require unrelated upstream MFS monorepo test dependencies.
- Local Docker build was attempted; Docker reached base image metadata loading
  and was canceled after prolonged no-output wait. Release check and Compose
  config are the completed automated deployment gates for this phase.

## Phase 7 Launch Evidence And Scorecard

- Added `release/ostag-launch-evidence.md`.
- Added `release/ostag-scorecard.md`.
- Current score: 92/100, pilot-ready by automated gates.
- Production-candidate remains pending live Feishu/Lark group validation.

## Live Validation Kit

- Added `docs/ostag/live_validation.md`.
- Added `scripts/live_validation_record.sh`.
- Added `release/live-validation-template.md`.
- The generator creates a blocked record when credentials are missing and a
  preflight-passed record when live doctor succeeds.

## Deployment Preflight Hardening

- Added osstack-managed deployment preflight verification as `task_6a154f84`.
- Generated `release/ai.ostag.feishu.plist` from the launchd template.
- Re-ran release check, Docker Compose config, launchd plist linting, and live
  validation record generation through `scripts/deploy_preflight.sh`.

## Prompt Contract Migration

- Added `ostag/core/prompt.py` as the dedicated runtime prompt contract module.
- `OSTagRunner` now delegates prompt construction to `build_chat_prompt()`.
- Added `tests/test_prompt.py` and included it in the default release test gate.
- osstack verified this migration as `task_e5ed23d9`.

## Memory Utility Migration

- Ported upstream `opentag_memory.py` into `ostag/mfs/memory.py`.
- Replaced Slack channel scoping with Feishu/Lark platform + chat id scoping.
- Added `python -m ostag.main memory ...` commands for `path`, `show`,
  `remember`, and explicit `sync`.
- Added `tests/test_memory.py` and included it in the default release test gate.

## MFS Client Verification

- Added `tests/test_mfs_client.py`.
- Enabled MFS search is now tested through a fake `mfs` CLI subprocess, covering
  success, failed-scope tolerance, and configured result limits.
