# OpenTag Benchmark P0-P2 Delivery Contract

Date: 2026-08-02
Branch: `codex/opentag-p0-p2`
Baseline: OStag v0.4.0, 955 tests passing, Ruff clean

## Runtime Truth

- Host: `OsborNs-Mac-mini.local`.
- OSStack 0.12.0 daemon and Loop Engine are healthy.
- Coding role: `claude-code`, fallback `codex-cli`.
- Test/verify role: `codex-cli`, fallback `claude-code`.
- Independent gates: `ds-review` and `ds-verify`.
- Kimi CLI is installed but is not a registered OSStack agent.
- A standalone K3 executable is not available. No release may claim K3 validation.
- The Claude SDK writer did not complete its handshake during the initial Loop run.
  Implementation therefore uses local TDD and sends each git diff back through
  OSStack command, reviewer, checker, report, scorecard, and proof gates.

## Non-Negotiable Invariants

1. Read-only operations never create or mutate action receipts.
2. Connector diagnostics never print credentials, command stderr, or token-bearing URLs.
3. Live probes are bounded, opt-in, and non-mutating unless a dedicated disposable target is used.
4. Every provider mutation remains approval-gated and attributable to a principal.
5. Completion is evidence-based; a model assertion is not merge or deployment evidence.
6. New executor commands are structured argv, allowlisted, time-bounded, and environment-scrubbed.
7. Admin reports are identity-scoped and redact prompts, local paths, secrets, and raw logs.
8. Production rollout is immutable, health-checked, observable, and rollback-ready.

## Delivery Order

### P0.1 Connector Diagnostics

Deliver `ostag connector test <platform> --json` for Feishu, Lark, DingTalk,
GitHub, and GitLab. The stable result includes platform, overall status,
non-secret checks, declared read/write capabilities, target readiness, and a
recovery hint. Default execution is local/non-mutating; `--live` adds bounded
read-only provider probes.

Acceptance: deterministic JSON, exit 0 only when required checks pass, injected
probe tests for success/error/timeout, and tests proving configured secrets do
not appear in output.

Likely files: `ostag/core/connector_diagnostics.py`, `ostag/main.py`,
`tests/test_connector_diagnostics.py`, `tests/test_cli_inspection.py`.

Rollback: remove the subcommand and module; no stored state or migration exists.

### P0.2 Read-Only Query Lane

Add a platform-neutral bounded lane for status, audit, weekly review, and backlog
queries. Admission requires an allowlisted query kind plus channel authorization.
It has an independent concurrency bound, timeout, retry budget, and result-size
limit. It has no receipt/apply dependency and rejects mutation-shaped requests.

Acceptance: authorization, bounds, retry, timeout, result-size, and receipt
non-creation tests; safe escaped response payloads.

Likely files: `ostag/core/query_lane.py`, `ostag/core/channel_brain.py`,
`tests/test_query_lane.py`, one CLI or relay integration test.

Rollback: disable the lane feature flag and retain existing direct read paths.

### P0.3 Credential-Gated Live Harness

Replace platform-specific assumptions with a single harness that calls connector
diagnostics first, skips with structured external-blocker evidence when credentials
are absent, and runs disposable read-only smoke cases when present. Release checks
must not rewrite checked-in historical evidence or depend on old absolute paths.

Acceptance: fixture pass, missing-credential skip, timeout/failure records,
portable management paths, and no secret values in artifacts.

Likely files: `scripts/connector_live_smoke.sh`, `scripts/ci_release_gates.sh`,
`scripts/release_readiness_audit.sh`, and focused tests.

Rollback: stop invoking the new harness; existing manual validation remains valid.

### P1.1 Native Diagnostic Cards

Render shared connector diagnostic and completion-evidence views as Feishu and
DingTalk native cards. Controls use opaque callback identifiers and appear only
for authorized actions. Text fallback remains available.

Acceptance: card schema, truncation, escaping, status colors, authorization,
callback opacity, and fallback tests for both platforms.

Rollback: turn off the card feature flag and use text rendering.

### P1.2 Workstreams and Recipes

Add immutable recipe definitions and durable workstreams grouping multiple runs
under one goal. Admission freezes recipe digest, budgets, required evidence, and
allowed connector operations. Completion derives only from child-run evidence.

Acceptance: strict schema, immutable digest, idempotent admission, bounded batch,
restart recovery, budget enforcement, and completion projection tests.

Rollback: disable new admission while preserving read-only historical records.

### P1.3 Provider Completion Evidence

Extend GitHub/GitLab support from issue operations to read-only PR/MR, check,
merge, and commit evidence collection. Evidence is normalized and can satisfy a
completion contract; it never merges or closes resources.

Acceptance: fixture and provider-live tests for open/merged/failed/pending states,
SHA binding, pagination bounds, malformed provider output, and no mutation calls.

Rollback: stop collecting provider evidence; existing issue writes are unchanged.

### P2.1 Executor-Neutral Conformance

Define an executor manifest and conformance suite above current backends. Add a
safe Kimi CLI profile when present without claiming OSStack registration. Profiles
declare resume support, artifact support, timeout, and risk mode. The default is
disabled until conformance passes.

Acceptance: manifest validation, argv safety, environment scrubbing, timeout,
resume declaration, deterministic fake-executor conformance, and Kimi unavailable
degradation tests.

Rollback: select the existing backend names; no data migration is required.

### P2.2 Admin and Reporting Surface

Expose an identity-scoped read-only operational report from the existing monitor
server and CLI. Include workstream state, completion evidence, connector health,
cost, escalations, and freshness. Return JSON and a compact HTML rendering without
raw prompt/log/path/credential leakage.

Acceptance: admin/member scoping, redaction, empty/stale/error states, stable JSON,
HTML escaping, and endpoint authorization tests.

Rollback: disable the endpoint/CLI feature flag; durable stores remain compatible.

## Release Gates

Each increment follows RED, GREEN, REFACTOR and ends with focused tests, full
`uv run pytest -q`, `uv run ruff check .`, secret diff scan, and an atomic commit.
Before release, run `scripts/release_check.sh`, OSStack git-diff reviewer/checker,
governance, report, scorecard, and proof. Push the feature branch and require green
GitHub Actions. Provider-live tests run only with configured credentials and write
structured blocker evidence otherwise.

Production promotion requires an immutable commit/image, backup and restore check,
exact deployed digest, health endpoint, one safe real-platform smoke, logs flowing,
and a documented previous digest/commit rollback point. If those external checks
cannot be executed, the result is `production_candidate`, never `production`.
