# Executor-neutral runtime

OStag exposes versioned executor manifests and a deterministic conformance suite
for CLI agent backends. The suite validates the manifest, direct argv execution,
platform-credential scrubbing, process timeout, resume declarations, and command
availability without sending a model request.

```bash
ostag executor list --json
ostag executor test kimi_cli --json
```

Kimi is permissioned and disabled by default. Enable it only after conformance
passes on the deployment host:

```dotenv
OSTAG_EXECUTOR_RUNTIME_ENABLED=true
OSTAG_KIMI_EXECUTOR_ENABLED=true
OSTAG_BACKEND=kimi_cli
KIMI_BIN=kimi
```

The Kimi adapter uses prompt mode and never adds `--yolo` or `--auto`. It declares
session-id resume and stdout artifacts. A configured session id is validated and
passed as one argv value; prompts never pass through a shell.

`OSTAG_EXECUTOR_OSSTACK_REGISTERED` is an explicit, comma-separated deployment
claim such as `codex_cli,claude_cli`. A local binary does not imply OSStack
registration. Do not add `kimi_cli` unless the active OSStack profile actually
registers it.

Rollback requires no data migration: set `OSTAG_BACKEND` to an existing backend
and disable `OSTAG_EXECUTOR_RUNTIME_ENABLED` and `OSTAG_KIMI_EXECUTOR_ENABLED`.
