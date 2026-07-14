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

## Daily FDE Requirement Dashboard

OStag can read the original requirement Base and send one interactive dashboard
card every calendar day at 12:00 Asia/Shanghai time, including weekends. The
scheduled path is read-only; Base writes remain limited to separately authorized
explicit commands.

Production values for the approved FDE rollout:

```dotenv
OSTAG_AMBIENT_ENABLED=true
OSTAG_AMBIENT_CHAT_IDS=oc_ef8aba337000ae1c69aae11dfdfd8ef0
OSTAG_AMBIENT_INTERVAL_SECONDS=300
OSTAG_AMBIENT_RECORD_LIMIT=200
OSTAG_AMBIENT_DAILY_BOARD_ENABLED=true
OSTAG_AMBIENT_DAILY_BOARD_HOUR=12
OSTAG_AMBIENT_DAILY_BOARD_WINDOW_MINUTES=60
OSTAG_AMBIENT_TIMEZONE=Asia/Shanghai
OSTAG_AMBIENT_DAILY_BOARD_GROUP_NAME="Dadao 投研交易 FDE"
OSTAG_AMBIENT_DAILY_BOARD_BASE_URL=<approved_original_base_https_url>
OSTAG_AUTO_BASE_DEFAULT_BASE_TOKEN=<approved_base_token>
OSTAG_AUTO_BASE_DEFAULT_TABLE_ID=<approved_table_id>
OSTAG_AUTO_BASE_IDENTITY=bot
```

Publish the OStag app with `base:field:read` and `base:record:read`, in addition
to its existing group-message send capability. Do not add scheduled Base write
scope for this feature.

Render without sending, then perform exactly one controlled live validation:

```bash
uv run python -m ostag.main ambient board-once --json
uv run python -m ostag.main ambient board-once --send --json
```

Confirm counts against a fresh Base read, verify the two buttons, and verify that
only overdue P0 owners are mentioned. The success marker prevents a second send
for the same Asia/Shanghai date. Roll back by setting
`OSTAG_AMBIENT_DAILY_BOARD_ENABLED=false` and restarting the single scheduler
process.

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

## Production work monitor on Tencent Cloud

Prepare a CVM or Lighthouse Linux host with Docker, a protected registry login,
and a host persistent directory:

```bash
sudo install -d -m 0700 -o 10001 -g 10001 /var/lib/ostag /var/backups/ostag
sudo install -m 0600 /dev/null /opt/ostag/source/deploy/work-monitor/.env
docker login <registry>
docker pull <registry>/ostag-work-monitor@sha256:<approved-digest>
cd /opt/ostag/source
export OSTAG_WORK_MONITOR_IMAGE=<registry>/ostag-work-monitor@sha256:<approved-digest>
export OSTAG_MONITOR_DATA_DIR=/var/lib/ostag
docker compose -f deploy/work-monitor/docker-compose.yml \
  -f deploy/work-monitor/docker-compose.production.yml config
docker compose -f deploy/work-monitor/docker-compose.yml \
  -f deploy/work-monitor/docker-compose.production.yml up -d --no-build
curl --fail --silent http://127.0.0.1:8000/healthz
```

The checked-in image command fixes uvicorn at `--workers 1`; the scheduler writes
its runtime owner lease to SQLite and doctor rejects zero or duplicate live owners.
Restrict origin TCP 8000 to EdgeOne origin ranges and an operator bastion. Configure TLS, WAF, 1 MiB body limit,
rate limits, no caching, and origin proxying exactly as described in
`deploy/work-monitor/edgeone-origin.md`.

Back up the live database with SQLite's online API and verify a disposable
restore before every upgrade:

```bash
sudo scripts/backup_work_monitor.sh backup \
  /var/lib/ostag/work_monitor.sqlite3 \
  /var/backups/ostag/work_monitor-$(date -u +%Y%m%dT%H%M%SZ).sqlite3
sudo scripts/backup_work_monitor.sh restore \
  /var/backups/ostag/<selected>.sqlite3 /tmp/ostag-restore-check.sqlite3
rm /tmp/ostag-restore-check.sqlite3
```

Deploy a new digest only after local gates and backup verification. Roll back
without changing the database first; if the new release migrated incompatibly,
stop the service and restore to a new path, then atomically select it:

```bash
docker compose -f deploy/work-monitor/docker-compose.yml \
  -f deploy/work-monitor/docker-compose.production.yml down
docker pull <registry>/ostag-work-monitor@sha256:<previous-digest>
export OSTAG_WORK_MONITOR_IMAGE=<registry>/ostag-work-monitor@sha256:<previous-digest>
sudo scripts/backup_work_monitor.sh restore \
  /var/backups/ostag/<pre-upgrade>.sqlite3 /var/lib/ostag/work_monitor.restore.sqlite3
sudo mv /var/lib/ostag/work_monitor.sqlite3 /var/lib/ostag/work_monitor.failed.sqlite3
sudo mv /var/lib/ostag/work_monitor.restore.sqlite3 /var/lib/ostag/work_monitor.sqlite3
docker compose -f deploy/work-monitor/docker-compose.yml \
  -f deploy/work-monitor/docker-compose.production.yml up -d --no-build
```

These commands are operator instructions, not evidence they were run. Record
actual external and mobile validation in
`release/work-monitor-live-validation-template.md`; it intentionally remains
incomplete until Tencent, EdgeOne, Feishu, Telegram, and device state is inspected.

For local verification, default Compose intentionally uses a named volume so the
non-root container can initialize SQLite without host ownership mismatch:

```bash
docker compose -f deploy/work-monitor/docker-compose.yml up -d --build
docker compose -f deploy/work-monitor/docker-compose.yml down  # volume retained
```

Run `scripts/work_monitor_container_smoke.sh` to build, verify Feishu/Crypto
imports, health, SQLite write, restart, and persistence. It retains its unique
test volume unless `OSTAG_CONTAINER_SMOKE_CLEANUP_VOLUME=1` is explicitly set.
