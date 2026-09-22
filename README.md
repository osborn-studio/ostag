# OStag

[English](README.md) · [简体中文](README.zh-CN.md) · [Documentation](docs/README.md)

OStag is a self-hosted control plane for human–agent collaboration. It turns work
threads and CLI requests into scoped, auditable runs with permissions, approval
receipts, artifacts and durable follow-ups. It also provides a remote work monitor
and local meeting preparation from GitHub activity and selected Feishu chats.

## Capabilities and availability

| Capability | What is available | Setup and limits |
|---|---|---|
| Thread and CLI work | Event adapters, workflow routing, agent backends, replies and run ledgers | Configure each platform and executor; mock/dry-run is the default |
| Governed actions | Frozen proposals, approval/apply receipts, actor and channel policies, budgets | Enable governed writes explicitly; connector support varies |
| Requirements and follow-ups | Feishu Base / DingTalk table workflows, durable continuations, proposal-only ambient monitoring | Requires the relevant connector, permissions and channel configuration |
| Remote work monitor | Persistent task board, Feishu/Telegram delivery, receipt-governed controls, optional scoped reports | Requires server, bot and deployment configuration |
| Local meeting preparation | One GitHub repository and up to two selected Feishu chats, time-windowed Markdown/JSON with citations and gaps | Uses your local authenticated CLIs; no model call or message sending |
| Brief/meeting service | Durable stores, authorization checks, revision/card handling and DeepSeek transport implemented | Disabled by default; trusted production composition and live acceptance are still required |
| GitHub → Vercel release | SHA-bound PR review and production-release workflow | Separate opt-in workflow; not OStag's own Tencent deployment pipeline |

Implementation, deployment and business acceptance are separate gates. Feature
flags alone do not provide authenticated ingress or grants. Full ledger host
migration/recovery, production Brief activation and real business pilots remain
open. See the [delivery plan](docs/ostag/delivery-implementation-plan-2026-09-21.md)
and [host rollout](docs/ostag/host-rollout.md) before enabling new service features.

## Quick start

Requires Python 3.11+, Git and [uv](https://docs.astral.sh/uv/). Run from the
repository root:

```bash
git clone https://github.com/osborn-studio/ostag.git
cd ostag
uv sync --locked --extra dev
uv run python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
```

For a fresh checkout, defaults use the mock backend and do not send a platform
reply. Local audit/ledger files may be created. Review `.env.example` before
adding your own `.env`; never commit credentials. Existing environment settings
can change the backend and other behavior.

Submit a local task through the same pipeline:

```bash
uv run ostag run --platform feishu --chat oc_demo --text "Summarize the next steps" --json
```

This returns the reply locally; platform delivery requires a configured live
adapter. See [CLI reference](docs/cli_reference.md) and
[platform setup](docs/README.md#platforms-and-integrations).

## Prepare a meeting locally

Authenticate `gh` and `lark-cli` separately using your own accounts. Select a
completed UTC time window of at most 31 days and the exact chats you can access:

```bash
uv run ostag meeting collect \
  --repo osborn-studio/ostag \
  --start 2026-09-13T00:00:00Z --end 2026-09-20T00:00:00Z \
  --timezone Asia/Shanghai --goal 'Weekly project review' \
  --chat-id oc_REPLACE_WITH_YOUR_CHAT_ID \
  --format markdown
```

Replace the example dates and chat ID. Output is private stdout; the command does
not save or send it automatically. Missing or incomplete sources are reported as
gaps (exit code 3), not as complete history. Omitting `--chat-id` produces a
GitHub-only draft with a Feishu coverage gap. This is single-repository collection,
not an organization-wide change feed. See [collection guide](docs/ostag/meeting-collect.md).

## Models and executors

These are separate configuration surfaces:

- **Task execution:** `OSTAG_BACKEND=mock|codex_cli|claude_cli|kimi_cli|openai_api`.
  Install/configure the selected executor; Kimi requires its feature gates and
  conformance check. See [executor conformance](docs/ostag/executor_conformance.md).
- **Brief/meeting generation:** the DeepSeek profile uses a fixed
  `https://api.deepseek.com` origin, a protected key file and explicit trusted
  composition. It is not a `deepseek` value for `OSTAG_BACKEND`. Install its
  dependency with `uv sync --locked --extra deepseek`; follow the
  [DeepSeek guide](docs/ostag/deepseek.md) for hidden key entry and an optional
  paid synthetic connection check. No automatic OpenAI fallback is enabled.
- **Development and auditing:** Codex, Claude CLI and osstack tooling are developer
  tools; their credentials do not configure the deployed business model.

## Common commands

```bash
uv run ostag runs list --json
uv run ostag status --run run_x --json
uv run ostag ledger export --run run_x --json
uv run ostag context export --run run_x --json
uv run ostag receipts list --pending --json
uv run ostag followups list --json
uv run ostag connector list --json
uv run ostag executor list --json
```

For configured governed writes, the interaction is proposal → `approve <ref>` →
`apply <ref>`. Approval binds the frozen payload; actor permissions, channel
policies and other applicable gates still apply. See
[receipts](docs/action_receipt_protocol.md), [governed writes](docs/governed_writes.md)
and [channel policy](docs/channel_policy.md).

## Architecture

```text
Platform event / CLI request
  → adapter → normalized event → runner
  → identity, policy and scoped context
  → workflow / executor → reply and artifacts
  → receipts, run ledger and audit

Work monitor: signed events → durable board → governed controls
Local meeting collect: authenticated CLIs → evidence + gaps → private draft
Brief service: explicit trusted composition → guarded worker and private delivery
```

Adapters own platform transport; core owns governance and ledgers; connectors own
external operations; workflows coordinate domain behavior. MFS retrieval and
channel memory provide optional scoped context. Start with the
[architecture guide](docs/ostag/architecture.md) and [documentation index](docs/README.md).

## Development and validation

Full release checks additionally require ripgrep, Pandoc, the SQLite and GitHub
CLIs, and Docker with Compose. Pandoc is required by meeting integration checks.

```bash
uv sync --all-extras --locked
uv run ruff check ostag tests
uv run pytest
scripts/release_check.sh
```

If osstack is installed, run repository knowledge hygiene separately:

```bash
osstack neat check --repo . --all --fail-on warning
```

A clean mechanical report does not prove that claims match runtime behavior or
that live acceptance passed. See [documentation maintenance](docs/README.md#documentation-maintenance).

## Deployment and recovery

OStag's production workflow targets Tencent Cloud Lighthouse after successful
main CI. Consult [deployment](docs/ostag/deployment.md),
[work monitor](docs/ostag/work_monitor.md) and [security](docs/ostag/security.md).
The installed host launcher and its configured hash are a separate release gate.

The backup helper verifies a single SQLite copy into a **new, unused path**. It
refuses existing destinations even with `--force`; it is not an in-place or
multi-store recovery controller. Never substitute a healthy endpoint or model
connection check for migration, rollback or business acceptance evidence.

## Origins and license

OStag originated from the `zilliztech/mfs` open-tag skill and incorporates research
and adaptations inspired by OpenTag. See [fork provenance](FORK_ORIGIN.md),
[upstream research](docs/research/) and [product strategy](docs/claude_tag_strategy.md).
It is an independent project, not an official Zilliz product. Licensed under
[Apache-2.0](LICENSE).
