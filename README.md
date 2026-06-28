# OS Tag

OS Tag is a Feishu/Lark/Slack AI group-chat context assistant inspired by Open Tag.
Mention `@OS Tag` in a group, the adapter normalizes the event, the core runner
applies safety checks, optional MFS context is retrieved from allowlisted scopes,
and a backend such as mock, Codex CLI, Claude CLI, or OpenAI API returns a reply.

OS Tag is not a Zilliz official product.

## Architecture

```text
Feishu/Lark/Slack group @OS Tag
  -> Feishu/Lark/Slack Adapter
  -> NormalizedEvent
  -> OSTagRunner
  -> Safety + MFS Context
  -> Backend
  -> Adapter reply
```

See [docs/ostag/architecture.md](docs/ostag/architecture.md) for details.

## Relationship To Open Tag

This project is inspired by and may include adapted concepts from
`zilliztech/mfs` open-tag-skill, licensed under the Apache License 2.0.
Original project: https://github.com/zilliztech/mfs
Modifications: adapted and renamed as OS Tag / ostag with Feishu/Lark/Slack
integration.

The upstream example is Slack-first. OS Tag keeps the Brain/Memory/Tools idea
but moves all platform-specific code behind adapters.

## Quick Start

```bash
cp .env.example .env
uv sync --extra dev
python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
python -m ostag.main --platform slack --dry-run --event examples/slack_event.json
```

Expected output starts with:

```text
OS Tag mock reply:
我已经收到你的消息...
```

Run tests:

```bash
pytest
```

Run the full release check:

```bash
scripts/release_check.sh
```

## Environment

Copy `.env.example` to `.env`. The important defaults are:

- `OSTAG_DRY_RUN=true`
- `OSTAG_BACKEND=mock`
- `MFS_ENABLED=false`
- `OSTAG_ALLOW_SHELL=false`
- `OSTAG_CODEX_UNSAFE_MODE=false`
- `OSTAG_CLAUDE_UNSAFE_MODE=false`

## Backends

Mock:

```bash
OSTAG_BACKEND=mock python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
```

Codex CLI:

```bash
OSTAG_BACKEND=codex_cli python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
```

Claude Code CLI:

```bash
OSTAG_BACKEND=claude_cli python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
```

OpenAI API:

```bash
uv sync --extra openai
OSTAG_BACKEND=openai_api OSTAG_OPENAI_MODEL=gpt-5.5 python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
```

If a model name is unavailable in your account, set `OSTAG_OPENAI_MODEL` to an
available Responses API model.

## Feishu And Lark

Read [docs/ostag/feishu_setup.md](docs/ostag/feishu_setup.md) or
[docs/ostag/lark_setup.md](docs/ostag/lark_setup.md). Live mode requires app credentials and
the optional `feishu` dependency group:

```bash
uv sync --extra feishu
python -m ostag.main --platform feishu
```

See [docs/ostag/deployment.md](docs/ostag/deployment.md) for local release and launchd
deployment steps, plus Docker Compose.

## MFS

MFS is disabled by default. To enable it:

```bash
MFS_ENABLED=true MFS_ALLOWED_SCOPES=file://./examples/context python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
```

OS Tag only searches scopes listed in `MFS_ALLOWED_SCOPES`. Search failures are
logged and do not stop the main reply.

Optional channel memory is managed as local markdown and can be synced to MFS
explicitly:

```bash
python -m ostag.main memory remember --platform feishu --chat-id oc_demo_chat --text "Use mock backend for pilot."
python -m ostag.main memory show --platform feishu --chat-id oc_demo_chat
python -m ostag.main memory sync
```

The default memory root is `OSTAG_MEMORY_ROOT=~/.mfs/ostag-memory`.

## Safety

The MVP is designed for controlled pilots. It does not provide enterprise
governance, approvals, or a hardened sandbox. Defaults are conservative:

- dry-run enabled;
- MFS disabled;
- shell execution disabled;
- full chat history is not fetched by default;
- backend timeouts are enforced;
- unsafe CLI flags require explicit environment variables;
- audit logs redact common API keys.

See [docs/ostag/security.md](docs/ostag/security.md) for the governance checklist.

## Acceptance

The test and scoring standard lives in
[docs/ostag/acceptance_criteria.md](docs/ostag/acceptance_criteria.md). MVP release requires
85+ points, no critical blockers, `pytest` passing, and dry-run working.

## Roadmap

- Slack live adapter production hardening.
- Feishu Docs connector.
- Feishu Base connector.
- GitHub connector.
- Deeper MFS SDK integration.
- Project-level scope permissions.
- Prompt template management.
- Web admin console.
- Cost tracking.
- Approval mode.
- Docker deployment.
- Tailscale intranet deployment.
- OpenClaw / Hermes Agent integration.
