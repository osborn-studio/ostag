# Deployment

The MVP deployment target is a controlled Mac mini or intranet host running one
OS Tag process per bot.

## Local Dry-run Release

Use this before live credentials:

```bash
cp .env.example .env
uv sync --extra dev
pytest
python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
```

The release is acceptable when tests pass and the command prints
`OS Tag mock reply:`.

## Live Feishu/Lark Process

1. Fill `.env` with app credentials.
2. Install live dependency: `uv sync --extra feishu`.
3. Set `OSTAG_DRY_RUN=false`.
4. Choose `OSTAG_BACKEND=mock` for first live validation.
5. Check live readiness: `python -m ostag.main --platform feishu --live --doctor`.
6. Start: `python -m ostag.main --platform feishu --live`.
7. Mention the bot in a group and verify audit logs.

## Release Check

Run this before each deployment:

```bash
scripts/release_check.sh
```

It compiles the package, runs tests, runs doctor, and validates Feishu/Lark
dry-run examples.

## Requirement Table Operations

To let OS Tag maintain an existing Feishu Base requirement table without a link
in every group command, configure:

```bash
OSTAG_AUTO_BASE_DEFAULT_BASE_TOKEN=<base_token>
OSTAG_AUTO_BASE_DEFAULT_TABLE_ID=<tbl_xxx>
OSTAG_AUTO_BASE_IDENTITY=user
```

Required capabilities:

- Base record read: locate the target requirement before update.
- Base record write: update owner, status, priority, and optional task link
  fields that already exist in the table.
- Task write: create Feishu tasks when the user asks OS Tag to create or assign
  a task.

Example group commands:

```text
@OS Tag 请把 交易-001 分配给 @张三，状态改为 开发交付中
@OS Tag 请将 市场周报 优先级改为 高
@OS Tag 请为 交易盯盘 创建飞书任务并分配给 @李四
```

If multiple records match, OS Tag refuses to update and asks for a more specific
requirement keyword or code.

## Deployment Preflight

Run the full deploy preflight before a pilot launch:

```bash
scripts/deploy_preflight.sh
```

It runs the release check, validates Docker Compose configuration, renders the
launchd plist, validates the plist when `plutil` is available, and writes a
credential-gated live validation record under `release/`.

After a real group test, validate the manual evidence:

```bash
scripts/validate_live_validation_record.sh feishu
```

For a single launch裁决 command, run:

```bash
scripts/launch_status.sh
```

Exit code contract:

- `0`: production-candidate gates are complete.
- `2`: an automated release gate failed.
- `3`: automated gates pass, but external live validation is still blocked.

## Production Scope Decision

MFS is included in production-candidate scope by default:

```bash
scripts/production_scope_record.sh
scripts/validate_production_scope.sh
```

To exclude MFS from the current production-candidate scope, an operator must
write `release/production-scope.json` with `mfs_mode=excluded`, plus a non-empty
owner and rationale. `scripts/launch_status.sh` consumes this decision.

## MFS Smoke Gate

MFS is disabled by default. To record the live MFS deployment state:

```bash
scripts/mfs_validation_record.sh
scripts/validate_mfs_validation_record.sh
```

Without live MFS configuration, the validator exits `3` and records a
structured external blocker in `release/mfs-validation.md`. With
`MFS_ENABLED=true`, an executable `MFS_BIN`, and an allowlisted scope, the gate
passes when the live `mfs search` command exits `0`.

## Docker Compose

For a host that already has `.env` configured:

```bash
docker compose up --build -d
docker compose logs -f ostag
```

The Compose service mounts `./logs` so audit logs remain on the host. Start with
`OSTAG_BACKEND=mock`, verify group replies, then switch backend.

Docker Desktop or another Docker daemon must be running before `docker compose
up` or `docker build`. The Compose file allows `.env` to be absent for static
validation, but live mode still requires app credentials.

## launchd Template

Render the launchd plist from the checked-in template:

```bash
scripts/render_launchd_plist.sh
```

The default output is `release/ai.ostag.feishu.plist`. Copy or symlink it to
`~/Library/LaunchAgents/ai.ostag.feishu.plist` after `.env` is configured.

Load it:

```bash
launchctl load ~/Library/LaunchAgents/ai.ostag.feishu.plist
launchctl list | grep ai.ostag.feishu
```

## Rollback

```bash
launchctl unload ~/Library/LaunchAgents/ai.ostag.feishu.plist
OSTAG_DRY_RUN=true python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
```

## Production Notes

- Start with `OSTAG_BACKEND=mock`, then switch to Codex/OpenAI after message
  delivery is confirmed.
- Keep `MFS_ALLOWED_SCOPES` narrow.
- Keep unsafe CLI flags disabled unless testing on an isolated machine.
- Rotate Feishu/Lark secrets if audit logs or shell history expose credentials.
