# Spec: OStag-native Agent Presence

## Objective

Adapt the read-only Agent Presence projection from `amplifthq/opentag` at
`2689b07c50095974e224468fd1fb0f89d8c3c621` to OStag's existing multi-platform,
multi-device work monitor. The projection makes operational state concise
without creating a second lifecycle, changing execution authority, or removing
any OStag platform.

The existing identity-scoped operational report becomes schema v2 and exposes
one presence summary plus one derived presence value per authorized instance.
No presence row is stored and no command is emitted by a presence read.

## Tech Stack

- Python 3.11+ frozen dataclasses and pure derivation functions.
- Existing SQLite work-monitor projections and identity bindings.
- Existing JSON/HTML operational report, CLI, and authenticated HTTP surfaces.
- Pytest 8.2+ and Ruff 0.12+.
- Upstream semantic sources:
  - `apps/control-plane/src/modules/console-reads/index.ts`
  - `docs/adr/0004-always-on-channel-ingress-local-execution.md`
  - `README.md` Presence states table.

## Commands

- Focused tests: `uv run pytest -q tests/test_agent_presence.py tests/test_operational_report.py`
- Full tests: `uv run pytest -q`
- Static analysis: `uv run ruff check .`
- Release gate: `scripts/release_check.sh`

## Project Structure

- `ostag/work_monitor/presence.py`: provider-neutral, read-only state derivation.
- `ostag/work_monitor/operational_report.py`: authorized fact collection and
  schema-v2 JSON/HTML projection.
- `tests/test_agent_presence.py`: state precedence, fail-closed freshness, and
  deterministic summary tests.
- `tests/test_operational_report.py`: scoped report integration and rendering.
- `docs/ostag/operational_report.md`: public schema and operational behavior.
- `docs/upgrade/2026-09-04-opentag-sync.md`: upstream mapping and divergence.

## Code Style

Keep the state machine pure and explicit. It accepts only already-authorized,
bounded aggregate facts and returns stable state and reason codes:

```python
presence = derive_agent_presence(
    instance_id=instance_id,
    configured=configured,
    freshness_state=freshness["state"],
    task_counts=task_counts,
    session_counts=session_counts,
    pending_decisions=pending_decisions,
    active_escalations=active_escalations,
)
```

Dynamic task titles, prompts, summaries, paths, decision questions, escalation
reasons, and credentials never enter this projection.

## Testing Strategy

- RED: assert all six states and upstream precedence before implementation;
  assert an empty authorized scope is `setup_required`, stale activity cannot be
  `working`, and queued durable work remains `queued` while offline.
- GREEN: derive presence from instance/device heartbeat, task/session state,
  pending decisions, and active escalations already present in the monitor.
- REFACTOR: isolate state and summary priority in a pure module; keep SQL reads
  aggregate-only and identity-scoped.
- Regression: verify JSON stability, HTML escaping, personal/team authorization,
  empty/error behavior, full pytest, Ruff, and the release gate.

## Boundaries

- Always: enforce the existing personal/team instance boundary before reading
  facts; require fresh heartbeat evidence for `working` and `available`; keep
  reads deterministic, bounded, and free of side effects.
- Ask first: add a durable presence table, change execution/lease/completion
  authority, enable the report in production, publish, deploy, or remove an
  existing platform/runtime.
- Never: infer completion from presence, enqueue/retry/cancel work during a
  presence read, expose raw content, or treat a stale heartbeat as availability.

## State Contract

Presence uses the upstream six states with OStag-native facts:

1. `setup_required`: an authorized binding has neither a registered device nor
   a legacy instance projection; an empty authorized scope summarizes likewise.
2. `needs_attention`: a pending human decision, open/acknowledged escalation, or
   blocked task/session exists. This takes precedence over connectivity.
3. `queued`: a `triage`, `todo`, or `ready` task is durably projected. Like
   upstream, queued work remains visible even when its device is offline.
4. `offline`: setup exists but heartbeat freshness is not `fresh`.
5. `working`: a task or relay session is `running` under a fresh heartbeat.
6. `available`: setup and fresh heartbeat exist with no queued, running, or
   attention-requiring work.

The overall summary selects the highest-priority visible state in this order:
`setup_required`, `needs_attention`, `offline`, `working`, `queued`, `available`.

## Success Criteria

1. Schema-v2 operational reports contain a deterministic `presence` summary and
   a presence object for every authorized instance.
2. All six states and their precedence are covered by tests.
3. `working` and `available` require fresh heartbeat evidence; presence never
   changes task, session, command, completion, or escalation data.
4. Personal and team scopes retain their existing authorization behavior and
   report no raw task or decision content.
5. JSON, HTML, CLI, and HTTP report surfaces expose the same projection.
6. Focused tests, full pytest, Ruff, and the release gate pass.

## Open Questions

- A future slice may reuse this pure projection in platform-native status cards.
- OpenTag accepted-progress attribution and durable reassessment obligations
  remain separate governance changes and are not coupled to presence.
