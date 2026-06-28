# OS Tag Deep Fork Plan

This is the corrective target plan for OS Tag. The intended outcome is not a
thin greenfield MVP, but a real fork-derived product: fork `zilliztech/mfs`,
extract and deeply rebuild `examples/open-tag-skill` into a first-class
Feishu/Lark product named `ostag`.

Fork baseline:

- Upstream: `https://github.com/zilliztech/mfs`
- Upstream area: `examples/open-tag-skill`
- Baseline commit: `16574f9b365bc67f3716c552dad416da730088b8`
- License: Apache License 2.0
- Product name: OS Tag
- Internal code name: `ostag`

## 1. Definition Of Done

The work is done only when all of these are true:

1. A real fork or fork-derived git history exists from `zilliztech/mfs`.
2. The upstream `examples/open-tag-skill` code has been traced, mapped, and
   intentionally migrated or deleted with a migration record.
3. OS Tag is no longer a loose greenfield reimplementation. It is a productized
   fork-derived repo with clear attribution, renamed package/module boundaries,
   Feishu/Lark adapters, backend runners, MFS integration, tests, CI, release
   checks, and deployment paths.
4. Feishu dry-run, Lark dry-run, backend checks, safety checks, and audit logging
   pass automatically.
5. Live Feishu/Lark validation is either completed with credentials or explicitly
   blocked by missing external credentials, with all preflight checks passing.
6. osstack `STATE.md` records every phase, verifier command, and final score.

## 2. Fork Strategy

Recommended approach:

1. Create a new branch from a real clone/fork of `zilliztech/mfs` at the baseline
   commit.
2. Keep upstream remote as `upstream`.
3. Add product remote as `origin`, for example `git@github.com:<owner>/ostag.git`.
4. Preserve Apache 2.0 `LICENSE`, upstream attribution, and a fork notice.
5. Extract `examples/open-tag-skill/open-tag` into the OS Tag product root or
   into `packages/ostag`, depending on repo strategy.

Repo strategy options:

| Option | Recommendation | Rationale |
|---|---:|---|
| Keep full MFS monorepo fork | Use only if OS Tag will track MFS internals closely | Preserves full history but heavy and noisy. |
| Fork then extract product subtree with history | Recommended | Keeps provenance while producing a focused `ostag` repo. |
| Greenfield rewrite with attribution only | Not acceptable for this goal | This is what the current MVP mostly is; it does not satisfy deep fork intent. |

The product repo should keep a `FORK_ORIGIN.md` file that records:

- upstream URL and commit;
- files migrated from Open Tag;
- files rewritten from scratch;
- files intentionally not migrated;
- license/NOTICE obligations.

## 3. Upstream Mapping

Upstream Open Tag files to evaluate:

| Upstream file | Required OS Tag decision |
|---|---|
| `README.md` | Rewrite as OS Tag product README, preserve attribution. |
| `open-tag/SKILL.md` | Convert into operator/admin docs or remove with migration note. |
| `references/backends.md` | Migrate backend rules into OS Tag backend docs/tests. |
| `references/memory.md` | Migrate MFS memory model into OS Tag MFS docs. |
| `references/runtime-agent.md` | Convert into OS Tag prompt/runtime contract. |
| `references/slack-adapter.md` | Use as adapter abstraction reference; Slack becomes optional roadmap. |
| `scripts/mfs_cat.py` | Decide whether to port scoped read helper. |
| `scripts/mfs_search.py` | Port or replace with `ostag/mfs/client.py`. |
| `scripts/opentag_agent.py` | Deeply refactor into backend runner abstraction. |
| `scripts/opentag_doctor.py` | Deeply refactor into `ostag doctor`. |
| `scripts/opentag_memory.py` | Defer or port as explicit memory utility. |
| `scripts/slack_socket_agent.py` | Use as adapter pattern; replace Slack transport with Feishu/Lark. |

Every row must end with one of: `ported`, `rewritten`, `deleted`, `deferred`.

## 4. Target Architecture

```text
ostag/
  FORK_ORIGIN.md
  LICENSE
  NOTICE
  README.md
  pyproject.toml
  .env.example
  ostag/
    main.py
    config.py
    core/
      event.py
      runner.py
      context.py
      logging.py
      safety.py
      doctor.py
      prompt.py
    adapters/
      base.py
      feishu.py
      lark.py
      slack.py
    backends/
      base.py
      mock.py
      codex_cli.py
      claude_cli.py
      openai_api.py
    mfs/
      client.py
      helpers.py
    tests/
  docs/
    architecture.md
    acceptance_criteria.md
    fork_migration_report.md
    feishu_setup.md
    lark_setup.md
    deployment.md
  examples/
  scripts/
  deploy/
```

Core rule: adapters own platform transport and reply mechanics; runner owns only
normalized events, safety, context, backend invocation, and audit.

## 5. osstack Phase Plan

### Phase 0: Fork Baseline And Inventory

Goal: prove the work starts from a fork, not a greenfield repo.

Tasks:

- Clone/fork `zilliztech/mfs` at commit `16574f9...`.
- Create product branch `ostag/deep-fork`.
- Record upstream remotes and commit in `FORK_ORIGIN.md`.
- Generate upstream file inventory for `examples/open-tag-skill`.
- Decide repo shape: full monorepo fork vs extracted subtree.

Verification:

```bash
git remote -v
git rev-parse HEAD
git log --oneline -- examples/open-tag-skill | head
test -f FORK_ORIGIN.md
```

Exit criteria:

- Fork provenance is auditable.
- No OS Tag implementation begins before inventory exists.

### Phase 1: Product Extraction And Rename

Goal: convert Open Tag from example skill into OS Tag product skeleton.

Tasks:

- Move or extract Open Tag skill code into OS Tag product layout.
- Rename Open Tag identifiers to OS Tag.
- Replace `OPENTAG_*` environment variables with `OSTAG_*`.
- Preserve references to upstream license and modifications.
- Remove Slack-only assumptions from core paths.

Verification:

```bash
rg "Open Tag|OpenClaude|OpenCodex|OPENTAG|Slack" .
python -m compileall ostag
```

Exit criteria:

- Remaining upstream names are either attribution/docs or tracked TODOs.
- Package imports compile.

### Phase 2: Adapter Abstraction

Goal: make platform adapter boundaries real and testable.

Tasks:

- Define `Adapter` protocol/base class.
- Implement `NormalizedEvent`.
- Refactor Slack event ideas into platform-independent runner input.
- Implement Feishu dry-run normalization.
- Implement Lark dry-run normalization.
- Add fixture coverage for realistic Feishu/Lark event payloads.

Verification:

```bash
uv run pytest tests/test_event_normalization.py
uv run python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
uv run python -m ostag.main --platform lark --dry-run --event examples/lark_event.json
```

Exit criteria:

- Runner never reads raw Feishu/Lark/Slack payloads directly.

### Phase 3: Backend And MFS Migration

Goal: turn upstream CLI helper scripts into OS Tag backend and MFS modules.

Tasks:

- Migrate/refactor `opentag_agent.py` into backend interface.
- Implement `mock`, `codex_cli`, `claude_cli`, `openai_api`.
- Migrate/refactor `mfs_search.py` and optionally `mfs_cat.py`.
- Enforce timeout, stdout/stderr capture, and no unsafe flags by default.
- Add backend doctor checks.

Verification:

```bash
uv run python -m ostag.main --doctor --backend mock --dry-run --event examples/feishu_event.json
uv run python -m ostag.main --doctor --backend codex_cli --dry-run --event examples/feishu_event.json
uv run pytest tests/test_mock_backend.py
```

Exit criteria:

- Backend selection is config-driven.
- MFS disabled mode and failed search do not break replies.

### Phase 4: Safety, Audit, And Governance

Goal: make MVP safe enough for controlled pilots.

Tasks:

- Implement input limit, context limit, secret redaction, scope validation.
- Add command execution switch and unsafe backend switches.
- Write JSONL audit log for ok/ignored/rejected/error statuses.
- Make prod audit mode record hashes/lengths, not full user text.
- Add explicit prompt-injection warning and operator docs.

Verification:

```bash
uv run pytest tests/test_safety.py
uv run pytest tests/test_doctor.py
rg "dangerously|skip-permissions|bypass" ostag docs README.md
```

Exit criteria:

- No dangerous mode is default.
- Logs do not contain complete secrets in tests.

### Phase 5: Live Feishu/Lark Adapter

Goal: move beyond dry-run into real bot operation.

Tasks:

- Implement official Feishu/Lark SDK WebSocket event receiver.
- Implement tenant access token retrieval.
- Implement reply-to-message API.
- Add live-mode doctor checks for credentials and SDK.
- Add integration smoke script that can run when credentials exist.
- Document app scopes and event subscription setup.

Verification without credentials:

```bash
uv sync --extra feishu
uv run python -m ostag.main --platform feishu --doctor
uv run pytest
```

Verification with credentials:

```bash
OSTAG_DRY_RUN=false OSTAG_BACKEND=mock uv run python -m ostag.main --platform feishu
```

Manual validation:

- Invite bot to Feishu group.
- Send `@OS Tag 请总结这个项目下一步应该做什么`.
- Confirm receive event, backend run, reply in group, audit row.

Exit criteria:

- If credentials exist, live group message works.
- If credentials are missing, doctor blocks with clear errors.

### Phase 6: Packaging, CI, Deployment

Goal: product can be installed, checked, and deployed repeatedly.

Tasks:

- Add `scripts/release_check.sh`.
- Add Dockerfile and Compose.
- Add launchd docs for Mac mini.
- Add CI workflow for lint/test/release check.
- Add versioning and changelog.

Verification:

```bash
scripts/release_check.sh
docker compose config
docker build -t ostag:local .
```

Exit criteria:

- Release check passes in clean checkout.
- Docker build passes when daemon is running.

### Phase 7: Launch And Evidence Pack

Goal: give the user an evidence-backed launch candidate.

Tasks:

- Generate release evidence under `release/`.
- Include test logs, doctor output, dry-run output, compose config, live test
  notes, and known limitations.
- Score the build against the rubric below.
- Mark osstack task complete only if score meets release gate.

Verification:

```bash
test -f release/ostag-launch-evidence.md
test -f release/ostag-scorecard.md
```

Exit criteria:

- Human validator can reproduce the launch checks.

## 6. Scoring Rubric

Total: 100 points.

Release gates:

- Pilot-ready: 85+ points, no P0 blocker.
- Production-candidate: 92+ points, live Feishu or Lark validated.
- Full target complete: 96+ points, fork provenance, live validation, CI, and
  deployment all proven.

| Area | Points | Full-credit standard |
|---|---:|---|
| Fork provenance | 12 | Real fork/extracted history from `zilliztech/mfs`, baseline commit recorded, `FORK_ORIGIN.md` complete. |
| Upstream migration depth | 12 | Every Open Tag file has a tracked `ported/rewritten/deleted/deferred` decision with rationale. |
| Product rename and ownership | 8 | Open Tag names removed from runtime code; OS Tag branding and env vars consistent; attribution remains only in docs/NOTICE. |
| Core architecture | 10 | Adapter/core/backend/MFS boundaries are enforced by tests; runner consumes only `NormalizedEvent`. |
| Feishu/Lark dry-run | 8 | Realistic fixtures normalize and reply through the same runner path; Chinese and English mention cases pass. |
| Live Feishu/Lark adapter | 12 | SDK WebSocket receive, credential checks, reply API, and manual group validation succeed or are externally blocked with evidence. |
| Backends | 8 | mock, Codex CLI, Claude CLI, OpenAI API selectable; timeout/stdout/stderr behavior tested; no unsafe flags by default. |
| MFS integration | 8 | Disabled mode safe; enabled mode allowlisted; failures logged; migrated helper behavior covered. |
| Safety and audit | 10 | input/context limits, secret redaction, scope validation, prod audit behavior, and unsafe switches tested. |
| Documentation | 5 | README, setup docs, architecture, deployment, migration report, and roadmap are operator-ready. |
| CI/release/deploy | 5 | release check, CI, Docker/Compose, launchd or service deployment documented and verified. |
| osstack evidence | 2 | `STATE.md`, archived tasks, scorecard, and launch evidence generated. |

## 7. P0 Blockers

Any P0 blocker makes the release score fail regardless of points:

- No actual fork provenance.
- Apache 2.0 license/notice missing or misleading.
- Default unsafe CLI or shell execution.
- Runner depends directly on raw platform payloads.
- Dry-run command fails.
- Tests fail.
- Secrets are written unredacted to logs.
- Live mode fails silently instead of doctor/preflight erroring.

## 8. osstack Task Queue Seeds

Use these as task prompts:

```bash
osstack loop enqueue "Phase 0: create real zilliztech/mfs fork baseline for ostag, record FORK_ORIGIN.md, inventory open-tag-skill files" --capability code.feature --priority 100
osstack loop enqueue "Phase 1: extract and rename Open Tag into OS Tag product layout while preserving Apache attribution" --capability code.feature --priority 95
osstack loop enqueue "Phase 2: implement platform adapter abstraction and Feishu/Lark dry-run fixtures" --capability code.feature --priority 90
osstack loop enqueue "Phase 3: migrate Open Tag backend and MFS helpers into OS Tag backend/MFS modules" --capability code.feature --priority 88
osstack loop enqueue "Phase 4: implement safety, audit, doctor, and governance tests for OS Tag" --capability code.feature --priority 86
osstack loop enqueue "Phase 5: implement and validate live Feishu/Lark SDK adapter with reply API" --capability code.feature --priority 84
osstack loop enqueue "Phase 6: add CI, release_check, Docker Compose, launchd deployment, and packaging" --capability code.feature --priority 78
osstack loop enqueue "Phase 7: generate launch evidence pack and score OS Tag against fork rubric" --capability code.test --priority 70
```

Recommended verifier per phase:

```bash
scripts/release_check.sh
docker compose config
uv run pytest
uv run python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
uv run python -m ostag.main --platform lark --dry-run --event examples/lark_event.json
```

For live adapter phases, add a credential-gated verifier:

```bash
uv run python -m ostag.main --platform feishu --doctor
```

If credentials are available, require a human/manual validation note:

```text
Feishu live validation:
- app id:
- test chat id:
- message id:
- backend:
- audit log path:
- result:
```

## 9. Current Repo Status Against This Plan

The current `/Users/gilbertchen/Documents/CODE/ostag` repo is useful as a
prototype and reference implementation, but it is not yet the deep fork target.

Current status:

- MVP dry-run implementation exists.
- `scripts/release_check.sh` passes.
- `uv run pytest` passes.
- osstack loop has recorded local successful checks.
- Real fork provenance is missing.
- Upstream migration report is missing.
- Live Feishu/Lark manual validation is missing.

Therefore current score against this deep-fork rubric is approximately 48/100,
not release-complete. It should be treated as a prototype to merge into the real
fork-derived product during Phases 1-4.
