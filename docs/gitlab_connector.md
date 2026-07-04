# GitLab Connector

GitLab issues as a governed system of record, mirroring the GitHub connector.
All operations are transported over an already-authenticated `glab` CLI — OS
Tag never handles GitLab tokens itself.

## Scope

| Operation | Supported | Notes |
|---|---|---|
| `create_issue` | ✅ | title, body, assignee, labels; returns glab stdout (issue URL) |
| `comment_issue` | ✅ | `glab issue note`; positive number + non-empty body |
| close / delete | ❌ | deliberately not implemented — no destructive path |

## Governed flow

There is no direct-write path to GitLab. Reach it only through receipts
(target `gitlab_issues`):

```bash
ostag receipts propose --file issue.json --actor agent_x --json
ostag approve <action_id> --json
ostag apply <action_id> --json           # requires OSTAG_APPLY_ENABLED=true
```

`apply_payload` shape:

```json
{"operation": "create_issue", "repo": "group/project",
 "title": "Bug: ...", "body": "Details", "assignee": "user", "labels": ["bug"]}
```

or `{"operation": "comment_issue", "repo": "group/project", "number": 12,
"body": "..."}`.

All governance layers apply: actor permissions, per-channel policy, high-risk
quorum, four-eyes, TTL staleness, cost budget, ledger evidence, follow-ups.

## Configuration

- `OSTAG_GLAB_BIN` — path to the `glab` binary (default `glab`).
- `OSTAG_GITLAB_DEFAULT_REPO` — `group/project` used when a payload omits
  `repo`.
- `glab` must be installed and authenticated (`glab auth login`); the
  subprocess runs with OS Tag's chat-platform secrets scrubbed from its
  environment.

## Capability declaration

`ostag connector list --platform gitlab --json` reports create/comment
available, close/delete `false`, `default_mode: propose`,
`destructive_requires_approval: true`.

## Errors

- `invalid_apply_payload` — missing/invalid repo, title, number, or body.
- `unsupported_operation` — anything but `create_issue` / `comment_issue`.
- `apply_failed` — `glab` CLI errors surface with the stderr tail and mark
  the receipt `failed` with ledger detail.
