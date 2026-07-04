# Upstream Research Note — 2026-07-04

Snapshot of upstream repos reviewed before implementing the governance
protocol increment (schemas, run ledger, action receipts, connector
capabilities, inspection CLI).

| Repo | Commit | Date | Head commit |
|---|---|---|---|
| [zilliztech/mfs](https://github.com/zilliztech/mfs) | `d7b6d3f83daf` | 2026-07-02 | Adversarial-testing fixes: config drift, health checks, cancellation, CLI robustness (#159) |
| [amplifthq/opentag](https://github.com/amplifthq/opentag) | `c6b4f75a9936` | 2026-07-03 | add discord-adapter (#70) |
| [CopilotKit/OpenTag](https://github.com/CopilotKit/OpenTag) | `df93bc0dccd0` | 2026-06-29 | docs/readme-package-list merge |
| [stablyai/agent-slack](https://github.com/stablyai/agent-slack) | `6775fa878dfe` | 2026-06-05 | fix/windows-store-slack-package-id merge |

Public Claude Tag references reviewed (strategy captured in
`docs/claude_tag_strategy.md` §2):

- <https://www.anthropic.com/news/introducing-claude-tag>
- <https://code.claude.com/docs/en/slack>

## What this increment adopted from each

- **OpenTag (amplift)** — run ledger + action receipt lifecycle
  (approve/reject/apply/continue), admission → running → terminal state
  model, "no apply without approval" as a state-machine invariant rather
  than a convention. Their new discord-adapter work reinforces the
  platform-adapter + capability-declaration boundary we formalized in
  `ConnectorCapability`.
- **agent-slack** — agent-first CLI contract: `--json` everywhere, compact
  sorted single-line output, null/empty pruning (`prune()`), structured
  errors with recovery hints, explicit read/write safety boundary (all new
  CLI commands are read-only).
- **MFS** — allowed-scope model already present in this fork; `ContextPacket`
  schema (with content hash and citations) is the landing zone for
  MFS-provided context in the Phase 6 context-provider work.
- **CopilotKit/OpenTag** — human-in-the-loop gate before writes and thread
  continuation informed the receipt states `needs_approval`/`approved_only`
  and the text-command fallback for platforms without buttons.
- **Claude Tag docs** — durable multi-stage async runs, admin-visible logs
  per actor/run, channel-scoped governance. The run ledger records actor,
  source, intent, and outcome per run so "who requested each task" is
  answerable locally.
