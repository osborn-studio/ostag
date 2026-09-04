# Spec: OpenTag upstream sync — 2026-09-04

## Objective

Selectively upgrade OStag from the OpenTag capabilities already represented in
OStag 0.5.0 to the portable security and governance semantics in the current
`amplifthq/opentag` repository. Preserve OStag's multi-platform control-plane
product direction instead of mechanically adopting OpenTag's breaking,
Slack-only Agent Presence reset.

This first implementation slice ports the OpenTag 0.11 fail-closed GitHub check
completeness contract. A GitHub pull-request observation may report passing
checks only when the complete current-head check-run and commit-status pages
were observed. Empty, truncated, malformed, or cross-SHA observations remain
pending or fail validation; they never authorize OStag's GitHub/Vercel release.

## Tech Stack

- Python 3.11+ package managed by `uv` (`pyproject.toml`).
- Frozen dataclasses with explicit strict wire validation.
- Async provider access through the authenticated `gh` CLI; OStag never owns a
  GitHub token.
- Pytest 8.2+ and Ruff 0.12+.
- Upstream facts: `amplifthq/opentag` at
  `2689b07c50095974e224468fd1fb0f89d8c3c621`; released behavior is taken from
  OpenTag 0.11.0 commit `491dd79fd9e53e1813e9299e33ecba6cdd85b801`
  and hardening commit `89b61fd4b2bbaaf512ce096e2ab1b7ce9aaab348`.

## Commands

- Install: `uv sync --extra dev --extra server --extra feishu --extra slack --extra dingtalk`
- Focused tests: `uv run pytest -q tests/test_provider_evidence.py tests/test_github_vercel_release.py`
- Full tests: `uv run pytest -q`
- Static analysis: `uv run ruff check .`
- Release gate: `scripts/release_check.sh`

## Project Structure

- `ostag/core/provider_evidence.py`: strict, provider-neutral evidence contract.
- `ostag/connectors/github.py`: bounded GitHub read and current-head normalization.
- `ostag/workflows/github_vercel_release.py`: governed release admission and apply-time
  revalidation.
- `tests/test_provider_evidence.py`: provider normalization and wire compatibility.
- `tests/test_github_vercel_release.py`: release fail-closed integration behavior.
- `docs/upgrade/`: permanent upstream mapping and divergence notes.

## Code Style

Use existing strict dataclass and `SchemaError` patterns. Provider payloads are
untrusted, bounded, and validated before construction:

```python
if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
    raise SchemaError(
        "invalid_provider_evidence",
        "GitHub check-run total_count is invalid",
        "Retry the provider read.",
    )
```

New wire names use camelCase while Python fields remain snake_case. No shell
interpolation, token handling, or mutation command is added.

## Testing Strategy

- RED: prove empty checks and an all-passed but truncated provider page cannot
  produce `checksState=passed`; prove legacy evidence missing completeness is
  interpreted fail-closed.
- GREEN: carry explicit `checksComplete` through the evidence model and digest,
  collect GitHub check runs and commit statuses with their provider totals, and
  bind every item to the frozen PR head SHA.
- REFACTOR: keep parsing helpers small and reuse the existing bounded provider
  payload validator.
- Regression: existing GitLab evidence remains compatible, release preparation
  and apply-time checks still require current-head passing evidence, and the full
  repository suite and release gate remain green.

## Boundaries

- Always: pin upstream commits, preserve SHA binding, fail closed, keep reads
  bounded, add tests before behavior changes, and retain existing OStag product
  capabilities.
- Ask first: adopt the upstream PostgreSQL Control Plane, remove any supported
  platform/runtime/workstream, change provider credentials, publish, deploy, or
  perform live provider mutations.
- Never: treat an executor assertion as provider evidence, accept incomplete
  GitHub pagination as passing, expose credentials, merge a PR, or overwrite
  historical evidence.

## Success Criteria

1. OStag records whether GitHub checks were completely observed and includes
   that value in the semantic evidence digest.
2. Empty, truncated, and cross-SHA check observations cannot pass completion or
   authorize a governed GitHub/Vercel release.
3. Complete, non-empty, all-passing current-head checks retain their existing
   successful behavior.
4. Legacy schema-v1 evidence remains readable but defaults to incomplete when it
   lacks an explicit completeness fact.
5. Focused tests, full pytest, Ruff, and the repository release gate pass.
6. A mapping note records why the current Slack-only upstream reset is not
   mechanically ported into OStag.

## Open Questions

- The approved follow-up Agent Presence slice is specified separately in
  `2026-09-04-ostag-agent-presence.md` and remains decoupled from this security
  fix.
- Durable reassessment obligations and accepted-progress attribution remain
  candidates for later slices after this evidence contract is trustworthy.
