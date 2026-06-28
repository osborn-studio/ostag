# OS Tag Deep-Fork Scorecard

Rubric source: `docs/ostag/fork_deep_rebuild_plan.md`.

| Area | Points | Score | Evidence |
|---|---:|---:|---|
| Fork provenance | 12 | 12 | `FORK_ORIGIN.md`, upstream remote, baseline commit. |
| Upstream migration depth | 12 | 12 | `docs/fork_migration_report.md`; all inventoried Open Tag files are ported or rewritten with attribution. |
| Product rename and ownership | 8 | 8 | Runtime package uses OS Tag naming and `OSTAG_*`; old names absent from `ostag/`. |
| Core architecture | 10 | 10 | `NormalizedEvent`, `Adapter` protocol, runner/backend/MFS boundaries. |
| Feishu/Lark dry-run | 8 | 8 | Release check validates both fixtures and replies. |
| Live Feishu/Lark adapter | 12 | 8 | SDK path, `--live`, reply API, credential preflight, validation record generator, and manual evidence gate exist; manual group validation pending credentials. |
| Backends | 8 | 8 | mock, Codex CLI, Claude CLI, OpenAI API modules and command tests. |
| MFS integration | 8 | 8 | Scoped search/cat helpers, disabled mode, fake CLI subprocess search, and local memory utility tests pass. |
| Safety and audit | 10 | 10 | Safety, prod audit hashing, doctor, and unsafe flag tests pass. |
| Documentation | 5 | 5 | README, setup, architecture, deployment, security, migration report. |
| CI/release/deploy | 5 | 5 | CI, release check, Compose config, deployment preflight, launchd plist generation, and Docker image verification pass. |
| osstack evidence | 2 | 2 | `STATE.md`, task archive, execution board, live validation record, and this evidence pack. |

Total: `96 / 100`.

Gate result:

- Pilot-ready: pass, with no current P0 blocker.
- Production-candidate: not yet, because live Feishu/Lark group validation is
  not recorded.
- Full target complete: not yet, because live Feishu/Lark group validation is
  still credential-gated.

## Required Human Validation To Upgrade

Record:

- Feishu/Lark app id used.
- Test chat id.
- Message id.
- Backend.
- Audit log path.
- Screenshot or transcript of bot reply.
