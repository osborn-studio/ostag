# OS Tag Acceptance Criteria And Scoring Standard

This document is the Loop Engineering guidance file for OS Tag MVP delivery.
Every loop iteration should map work to these criteria, run the listed checks,
and record unresolved gaps in `STATE.md` or the release notes.

## Scorecard

Total score: 100 points. MVP release gate: 85+ points, no critical blocker, and
all P0 checks passing.

| Area | Points | Acceptance standard |
|---|---:|---|
| Project structure | 10 | Required directories, package metadata, env example, examples, scripts, docs, LICENSE, and NOTICE exist. |
| Event abstraction | 12 | Feishu/Lark raw events normalize into `NormalizedEvent`; runner does not depend on platform-specific raw schemas. |
| Dry-run flow | 12 | `python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json` prints a stable mock reply. |
| Backend runners | 12 | mock, codex_cli, claude_cli, and openai_api are selectable; CLI backends enforce timeout and do not enable unsafe flags by default. |
| MFS context layer | 8 | `MFS_ENABLED=false` skips safely; enabled mode searches only configured scopes and failure does not break replies. |
| Safety controls | 12 | input length, secret redaction, shell execution switch, scope validation, context cap, and unsafe backend flags are implemented. |
| Audit logging | 8 | Every handled, ignored, rejected, and errored event writes JSONL without full secrets. |
| Feishu/Lark adapter | 10 | dry-run supported; live mode documents SDK/credential requirements and can reply to message id when configured. |
| Documentation | 10 | README, architecture, Feishu setup, Lark setup, and roadmap are clear enough for a new operator. |
| Tests and verification | 6 | `pytest` passes and covers required product scenarios. |

## P0 Release Gates

- `pytest` passes from a clean checkout.
- `python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json`
  prints `OS Tag mock reply:` and includes the user message.
- No default path enables dangerous shell or CLI bypass flags.
- `.env.example` contains all required runtime, backend, MFS, Feishu/Lark, and
  safety variables.
- README includes Apache 2.0 attribution and states OS Tag is not a Zilliz
  official product.

## P1 Acceptance Checks

- Non-mention messages return an empty reply and write an `ignored` audit record.
- Oversized messages are rejected or truncated according to safety policy and
  write a `rejected` audit record.
- Mock backend returns deterministic text for local tests.
- MFS disabled mode returns an empty context list without error.
- Backend timeout or exception returns a user-visible failure message and writes
  an `error` audit record.
- Lark adapter reuses the Feishu event shape but marks platform as `lark`.

## Manual Live Validation

Use these checks after credentials are configured:

1. Start live mode: `python -m ostag.main --platform feishu`.
2. Mention the bot in a Feishu group: `@OS Tag 请总结这个项目下一步应该做什么`.
3. Confirm the local process receives an event and writes an audit row.
4. Confirm the bot replies in the same group or message thread.
5. Switch `OSTAG_BACKEND=codex_cli` and confirm a Codex-generated reply appears.
6. Confirm no full secrets or unbounded chat history are written to audit logs.

## Scoring Method

For each area:

- Full points: all checks pass, documented, and covered by automated or manual verification.
- Half points: implementation exists but has a documented limitation or missing secondary test.
- Zero points: missing, unsafe by default, or not runnable.

Critical blockers override score:

- Secret leakage into logs.
- Dangerous command execution enabled by default.
- Dry-run cannot run from a clean checkout.
- Live adapter requires undocumented manual code changes.
