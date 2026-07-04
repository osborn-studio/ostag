# GitHub Connector

GitHub issues as a governed system of record. All operations are transported
over an already-authenticated `gh` CLI — consistent with the repo's other
connectors (lark-cli, dws-cli), OS Tag never handles GitHub tokens itself.

## Scope

| Operation | Supported | Notes |
|---|---|---|
| `create_issue` | ✅ | title, body, assignee, labels; returns issue URL |
| `comment_issue` | ✅ | positive issue number + non-empty body; returns comment URL |
| close / delete / merge | ❌ | deliberately not implemented — no destructive path |

Reading GitHub threads (issues/PRs as source threads) is a later phase;
today's connector is write-side only, and every write goes through the
receipt protocol.

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

## Configuration

- `OSTAG_GH_BIN` — path to the `gh` binary (default `gh`).
- `OSTAG_GITHUB_DEFAULT_REPO` — `owner/name` used when a payload omits
  `repo`.
- `gh` must be installed and authenticated (`gh auth login`); the subprocess
  runs with OS Tag's chat-platform secrets scrubbed from its environment,
  while `gh`'s own auth (keyring or `GH_TOKEN`) is untouched.

## Capability declaration

`ostag connector list --platform github --json` reports the honest surface:
issue creation and commenting available, close/delete declared `false`,
`default_mode: propose`, `destructive_requires_approval: true`. GitLab
remains a planned stub.

## Errors

- `invalid_apply_payload` — missing/invalid repo, title, number, or body,
  with recovery hints (including `OSTAG_GITHUB_DEFAULT_REPO`).
- `unsupported_operation` — anything but `create_issue` / `comment_issue`.
- `apply_failed` — `gh` CLI errors (e.g. authentication) surface with the
  stderr tail and mark the receipt `failed` with ledger detail.
