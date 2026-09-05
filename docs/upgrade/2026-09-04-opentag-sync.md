# OpenTag upstream mapping — 2026-09-04

## Pinned sources

- Repository: `https://github.com/amplifthq/opentag`
- Current `main`: `2689b07c50095974e224468fd1fb0f89d8c3c621`
- Latest coordinated release in the changelog: OpenTag 0.11.0 at
  `491dd79fd9e53e1813e9299e33ecba6cdd85b801`
- GitHub evidence hardening source:
  `89b61fd4b2bbaaf512ce096e2ab1b7ce9aaab348`
- Earlier OStag completion-governance mapping remains pinned at OpenTag 0.7.0
  and its Phase 2/3 commits; see the other files in this directory.

Authoritative references:

- OpenTag 0.11 fail-closed change:
  https://github.com/amplifthq/opentag/commit/89b61fd4b2bbaaf512ce096e2ab1b7ce9aaab348
- Current upstream Agent Presence reset changelog:
  https://github.com/amplifthq/opentag/blob/2689b07c50095974e224468fd1fb0f89d8c3c621/CHANGELOG.md
- Accepted-progress attribution decision:
  https://github.com/amplifthq/opentag/blob/2689b07c50095974e224468fd1fb0f89d8c3c621/docs/adr/0001-accepted-progress-attribution.md
- Current-profile clarification of the former reassessment design:
  https://github.com/amplifthq/opentag/blob/2689b07c50095974e224468fd1fb0f89d8c3c621/docs/adr/0002-durable-reassessment-obligations.md
- GitHub check-runs response and pagination contract:
  https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference
- GitHub combined-status response and pagination contract:
  https://docs.github.com/en/rest/commits/statuses#get-the-combined-status-for-a-specific-reference

## Upstream change classification

| Upstream area | OStag decision | Reason |
| --- | --- | --- |
| OpenTag 0.11 complete GitHub check rollup | `ported` | Prevents empty, truncated, or cross-SHA checks from satisfying completion. |
| Agent Presence read-only projection | `ported` (adapted) | Schema-v2 operational reports derive the same six states from OStag's authorized multi-device monitor facts without a second lifecycle. |
| Accepted-progress attribution | `deferred` | OStag does not yet durably persist completion artifacts plus trusted WorkThread-to-Run lineage, so unique source-Run attribution cannot be proved. |
| Durable reassessment obligations | `not ported` from current profile | Upstream ADR 0002 now labels this the former generic design; the Agent Presence reset has no reassessment worker or obligation table. |
| Self-hosted PostgreSQL team relay | `deferred` | OStag already has a deployed SQLite work monitor and multi-device relay; adopting a second authority needs a separate migration design. |
| Slack-only Source App and one paired Runner | `deleted` (not applicable) | Conflicts with OStag's Feishu/Lark, DingTalk, Slack, Telegram, and multi-runner product contract. |
| Removal of recipes/workstreams and non-Slack adapters | `deleted` (not applicable) | OStag 0.5.0 intentionally ships these governed, tested surfaces. Removing them would be a product regression. |
| Draft-PR publication and exact-head evidence | `rewritten` | OStag's existing receipt-governed GitHub/Vercel flow keeps exact-head revalidation and gains complete-check proof without adopting upstream custody boundaries wholesale. |

## Wire mapping for complete GitHub checks

OpenTag 0.11 adds `checksComplete` to its verified pull-request snapshot and
includes it in the semantic digest. OStag maps that fact to
`ProviderChangeEvidence.checks_complete` and emits `checksComplete`.

OStag uses schema version 2 for new evidence. Schema version 1 remains readable
for stored/imported history; because v1 lacks provider completeness proof, it is
interpreted as `checksComplete=false`. This is deliberately fail-closed and does
not rewrite the historical payload.

GitHub evidence is assembled from three bounded reads:

1. pull-request metadata for state, draft flag, URL, merge SHA, and head SHA;
2. current-head check runs with `total_count`;
3. current-head combined commit statuses with `total_count`.

Completeness is true only when each returned list length equals its provider
total and every returned item is bound to the frozen head SHA. The resulting
checks rollup is:

- `failed` if any observed current-head check failed;
- `pending` if completeness is false or no checks exist;
- `passed` only when completeness is true and every observed check passed.

The governed GitHub/Vercel workflow already re-reads evidence both before
proposal and immediately before apply. It therefore consumes this strengthened
contract at both authority boundaries.

## Deliberate divergences

1. OStag continues to use the authenticated `gh` CLI and scrubbed subprocess
   environment instead of accepting a GitHub API token in-process.
2. OStag preserves provider-neutral GitHub/GitLab evidence rather than replacing
   it with an OpenTag-only GitHub snapshot.
3. OStag requires non-empty passing checks for the governed release flow even
   when a complete GitHub rollup contains zero checks.
4. No upstream platform or workstream deletion is ported.

## OStag-native Agent Presence mapping

OpenTag derives presence from one active Slack binding, one Project Target, a
paired Runner readiness receipt, and the binding's canonical Run. OStag keeps
the same read-model rule and six public states, but maps them onto its existing
multi-platform authority boundaries:

| OpenTag fact | OStag fact |
| --- | --- |
| active Slack binding | identity-authorized instance binding on any supported platform |
| registered paired Runner | registered monitor device or legacy signed instance projection |
| fresh readiness receipt | fresh device/instance heartbeat under report freshness policy |
| queued canonical Run | projected task in `triage`, `todo`, or `ready` |
| current assigned/running Attempt | projected running task or relay session under a fresh heartbeat |
| approval/reconciliation needed | pending relay decision, active human escalation, or blocked work |

OStag does not claim that a heartbeat or running projection is a lease. Presence
therefore remains UI/report status and cannot authorize execution, completion,
retry, cancellation, publication, or provider mutation. The implementation adds
no database table or migration; it aggregates already-authorized counts and
timestamps at read time. Because OStag may run several work items on one device,
fresh active work takes precedence over that device's additional queued items;
an offline durable queue remains `queued` rather than disappearing into
`offline`.

## Remaining governance assessment

Accepted-progress attribution remains a valid upstream governance primitive,
but it cannot be truthfully surfaced from OStag's current durable records. The
upstream algorithm requires all of the following:

1. immutable adjacent assessments under one WorkThread, contract version, and
   cycle;
2. a passed gate transition and the accepted assessment's exact target binding;
3. a durably stored completion artifact identified by that binding;
4. a trusted `sourceRunId` on that artifact; and
5. the complete set of Run IDs belonging to the same WorkThread.

OStag currently persists assessments and target-binding artifact IDs, but the
`CompletionEvaluationInput.artifacts` and `run_results` used to create an
assessment are not persisted by `CompletionStore`. Implementing only the final
projection would therefore force a latest-Run or trigger-Run guess. OpenTag ADR
0001 explicitly rejects both. The safe prerequisite is a separately reviewed
trusted-ingestion and migration slice that stores immutable artifact provenance
and WorkThread Run membership before any attribution metric is exposed.

Durable reassessment obligations are not a current upstream requirement after
the Agent Presence reset. Upstream ADR 0002 is retained as historical design
context and says the current paired Runner has neither a reassessment worker nor
an obligation table. OStag should not add that machinery merely to match an
earlier changelog entry; a future crash-window analysis may justify an
OStag-native design independently.

## Rollback

Revert the evidence-model and GitHub connector slice together. The independent
Presence slice can be reverted separately to restore operational-report schema
v1. Neither slice introduces a database migration, and historical schema-v1
provider evidence remains intact.
