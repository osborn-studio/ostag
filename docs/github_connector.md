# GitHub Connector

GitHub issues and the governed PR-to-Vercel release path. GitHub operations are
transported over an already-authenticated `gh` CLI — consistent with the repo's
other connectors (lark-cli, dws-cli), OS Tag never handles GitHub tokens itself.

## Scope

| Operation | Supported | Notes |
|---|---|---|
| `create_issue` | ✅ | title, body, assignee, labels; returns issue URL |
| `comment_issue` | ✅ | positive issue number + non-empty body; returns comment URL |
| read PR/check evidence | ✅ | bounded, normalized, head-SHA-bound |
| approve PR review | ✅ | only inside the high-risk GitHub/Vercel release receipt |
| Vercel Production release | ✅ | same frozen PR SHA; waits for READY + assigned alias |
| close / delete / merge | ❌ | deliberately not implemented — no merge/destructive path |

Reading arbitrary GitHub threads as chat source threads remains a later phase.
The release reader only retrieves the one allowlisted PR and its bounded checks.
Every write goes through the receipt protocol.

## Governed flow

There is **no direct-write path to GitHub**. The only way to reach it:

```bash
cat > issue.json <<'EOF'
{"action_id": "action_abc123def456", "run_id": "run_abc123def456",
 "title": "Create bug issue", "target_system": "github_issues",
 "target_object": "owner/repo", "mutation_type": "create", "risk": "medium",
 "state": "needs_approval",
 "apply_payload": {"operation": "create_issue", "repo": "owner/repo",
   "title": "Bug: crash on startup", "body": "Details...", "labels": ["bug"]}}
EOF
ostag receipts propose --file issue.json --actor agent_x --json
ostag approve action_abc123def456 --json       # write-level actor
ostag apply action_abc123def456 --json         # requires OSTAG_APPLY_ENABLED=true
# {"action": {"state": "applied", ...}, "evidence": "created issue https://github.com/..."}
```

Chat approvals work the same way (`approve 1` / `apply 1`), and all the
governance layers apply: actor permissions, per-channel policy, high-risk
quorum, four-eyes, TTL staleness, ledger evidence, follow-up reminders.

## GitHub PR -> Vercel Production flow

When explicitly enabled, an allowlisted group can send:

```text
@OS Tag PR 已提交，请处理 https://github.com/owner/repo/pull/123
```

OStag reads the PR and checks, rejects draft/closed/fork/non-allowlisted or
non-passing inputs, and creates a high-risk receipt. Proposal does not write to
either provider. A write-level actor then runs:

```text
@OS Tag approve 1
@OS Tag apply 1
```

Apply re-reads the PR and rejects any head SHA/ref drift. It then calls GitHub's
create-review endpoint with `event=APPROVE` and the frozen `commit_id`, creates
a Vercel `target=production` deployment whose `gitSource.sha` is the same SHA,
and waits until Vercel reports `READY` with a Production alias assigned. OStag
does not merge the PR.

This ordered pair is not atomic. If GitHub succeeds and Vercel fails, the
receipt becomes `failed` with bounded partial-failure evidence; it never claims
that Production is online. Inspect the providers and create a fresh receipt
instead of applying a failed receipt again.

Run the ledger and live chat adapter as a single writer. A process exit after a
provider accepts a write can leave the receipt in `applying`; OStag deliberately
does not retry an indeterminate Production write automatically. Inspect GitHub
and Vercel, then create a fresh receipt only after reconciling provider state.

## Configuration

- `OSTAG_GH_BIN` — path to the `gh` binary (default `gh`).
- `OSTAG_GITHUB_DEFAULT_REPO` — `owner/name` used when a payload omits
  `repo`.
- `OSTAG_GITHUB_VERCEL_RELEASE_ENABLED` — opt-in release admission.
- `OSTAG_GITHUB_VERCEL_RELEASE_REPOSITORY` — the only accepted `owner/name`.
- `OSTAG_GITHUB_VERCEL_RELEASE_CHANNELS` — comma-separated
  `platform:chat_id` group allowlist.
- `OSTAG_GITHUB_VERCEL_RELEASE_BASE_BRANCH` — the only Production target branch
  (default `main`); retargeting a PR invalidates its receipt.
- `OSTAG_GITHUB_VERCEL_REQUIRED_CHECKS` — comma-separated trusted check names
  that must all be present and passing (for example `release-check,security-scan`).
- At least one complete, current-head GitHub check is mandatory and every
  observed check must pass; this production gate cannot be disabled.
- `OSTAG_VERCEL_ACCESS_TOKEN` — server-only, team-scoped Vercel Access Token.
- `OSTAG_VERCEL_TEAM_ID`, `OSTAG_VERCEL_PROJECT_ID`,
  `OSTAG_VERCEL_PROJECT_NAME` — fixed Production target.
- `OSTAG_VERCEL_DEPLOY_TIMEOUT_SECONDS`, `OSTAG_VERCEL_POLL_INTERVAL_SECONDS`
  — bounded completion polling.
- `gh` must be installed and authenticated (`gh auth login`); the subprocess
  runs with OS Tag's chat-platform secrets scrubbed from its environment,
  while `gh`'s own auth (keyring or `GH_TOKEN`) is untouched.

GitHub's authenticated bot/user needs `Pull requests: write` and must be a
different identity from the PR author because GitHub does not permit authors to
approve their own PR. The Vercel token must have access to the configured Team
and Project, and that Vercel Project must be connected to the configured GitHub
repository.

## Capability declaration

`ostag connector list --platform github --json` reports the honest surface:
issue creation/commenting plus PR/check reads and governed PR approval/Vercel
release are available; merge/close/delete remain `false`, with
`default_mode: propose` and `destructive_requires_approval: true`.

## Errors

- `invalid_apply_payload` — missing/invalid repo, title, number, or body,
  with recovery hints (including `OSTAG_GITHUB_DEFAULT_REPO`).
- `unsupported_operation` — anything but `create_issue` / `comment_issue`.
- `apply_failed` — `gh` CLI errors (e.g. authentication) surface with the
  stderr tail and mark the receipt `failed` with ledger detail.
- `provider_sha_mismatch` / `provider_ref_mismatch` — the PR changed after the
  receipt was created; generate a fresh receipt.
- `provider_checks_missing` / `provider_checks_incomplete` — checks do not
  authorize Production release.
- Vercel terminal/timeout errors — deployment did not become a serving
  Production alias; GitHub partial-success evidence is retained without token
  or request-header leakage.

Official API references:

- <https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request>
- <https://vercel.com/docs/rest-api/deployments/create-a-new-deployment>
- <https://vercel.com/docs/rest-api/deployments/get-a-deployment-by-id-or-url>
