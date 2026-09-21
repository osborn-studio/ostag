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

## Governed GitHub PR -> Vercel Production release

This optional workflow lets collaborators without direct Vercel project access
request a release from an authorized group while OStag retains the service
credentials and audit trail. It does not grant collaborators a Vercel token and
does not merge the PR.

Prerequisites:

1. Authenticate `gh` as a dedicated reviewer identity that is not the PR author
   and has GitHub `Pull requests: write` on the one release repository.
2. Connect the Vercel Project to that GitHub repository.
3. Create a Vercel Access Token scoped to the owning Team with access to the
   target Project. Keep it only in the OStag host's `.env`/secret manager.
4. Grant intended release operators platform-scoped OStag `write` permission.
5. Configure exactly one repository, one Vercel Project, and the allowed groups:

```dotenv
OSTAG_GITHUB_VERCEL_RELEASE_ENABLED=true
OSTAG_GITHUB_VERCEL_RELEASE_REPOSITORY=owner/repo
OSTAG_GITHUB_VERCEL_RELEASE_CHANNELS=feishu:oc_release_group
OSTAG_GITHUB_VERCEL_RELEASE_BASE_BRANCH=main
OSTAG_GITHUB_VERCEL_REQUIRED_CHECKS=release-check
OSTAG_VERCEL_ACCESS_TOKEN=<server-only-team-scoped-token>
OSTAG_VERCEL_TEAM_ID=team_xxx
OSTAG_VERCEL_PROJECT_ID=prj_xxx
OSTAG_VERCEL_PROJECT_NAME=release-app
OSTAG_VERCEL_DEPLOY_TIMEOUT_SECONDS=600
OSTAG_VERCEL_POLL_INTERVAL_SECONDS=2
OSTAG_ACTOR_PERMISSIONS=feishu:ou_release_owner:write
OSTAG_APPLY_ENABLED=true
```

The release workflow always requires every configured trusted check to be
present and passing in the complete check set for the current PR head SHA.
This Production safety gate has no disable switch.

Operate this JSONL-ledger workflow with one OStag writer process. If the process
exits while a provider call is in flight, treat an `applying` receipt as
indeterminate: inspect GitHub and Vercel and do not automatically replay it.

Run the non-mutating startup/config check before enabling a live group:

```bash
python -m ostag.main --platform feishu --live --doctor
gh auth status
ostag connector list --platform github --json
```

The doctor confirms local executable/config readiness and never prints the
Vercel token; it deliberately does not create a review or deployment. Then use
a designated test PR in the allowlisted repository:

```text
@OS Tag PR 已提交，请处理 https://github.com/owner/repo/pull/123
@OS Tag approve 1
@OS Tag apply 1
```

Acceptance evidence must show all of the following:

- proposal output contains the exact PR number, head SHA, Vercel Project and
  Production target, while provider write count is still zero;
- GitHub shows an `APPROVED` review for that exact commit SHA;
- Vercel shows the same SHA, `READY`, `target=production`, and an assigned
  Production alias;
- `ostag ledger export --run <run_id> --json` contains the applied receipt and
  bounded provider URLs without credentials.

If GitHub approves but Vercel fails, treat the run as partial failure: inspect
GitHub/Vercel, resolve the provider issue, and send a new PR notification to
create a fresh receipt. Failed receipts cannot be replayed. Do not report the
release as online unless the alias evidence is present.

Rollback admission immediately by setting:

```dotenv
OSTAG_GITHUB_VERCEL_RELEASE_ENABLED=false
```

and restarting OStag. This prevents new release receipts without altering
provider state or historical audit evidence. Provider rollback is intentionally
out of scope and must use the project's existing Vercel rollback procedure.

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
launchd plist, validates the plist when `plutil` is available, and writes
credential-gated live validation plus Feishu, DingTalk, GitHub, and GitLab
connector evidence under `release/`. A missing credential or target is retained
as a structured external blocker; malformed evidence fails the preflight.

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

## Feishu osstack status dashboards

The private-chat status dashboard is opt-in. Enable it on the central work-monitor
server only after the verified Feishu owner and each device have been bound:

```dotenv
OSTAG_COPILOT_STATUS_BOARD_ENABLED=true
```

Bind the global owner (or an administrator) from the trusted local CLI:

```bash
uv run ostag monitor access bind-instance \
  --platform feishu \
  --actor '<verified-open-id>' \
  --private-chat '<verified-private-chat-id>' \
  --instance 'ostag-local' \
  --display-name 'Mac mini' \
  --role admin \
  --json
```

Bind each employee only to that employee's own osstack instance by using the same
command with their verified identifiers and `--role member`. Never copy actor or
chat identifiers from message text; obtain them from authenticated Feishu events.

Inspect or revoke active ownership without exposing credentials:

```bash
uv run ostag monitor access list --json
uv run ostag monitor access list --actor '<verified-open-id>' --json
uv run ostag monitor access revoke-instance --instance '<instance-id>' --json
```

`现在 osstack 在干嘛` and equivalent read-only queries return the personal status
dashboard directly and never create a relay inbox/outbox message. A member sees
only explicitly bound devices. The verified global owner may switch to the team
dashboard; that authorization is checked again on every card callback. Device and
agent names, freshness, and all displayed times come from the central projection;
times are rendered in `Asia/Shanghai` and labeled `北京时间`.

Before enabling or upgrading this feature, make an online SQLite backup and verify
`PRAGMA integrity_check` (the production commands below use
`scripts/backup_work_monitor.sh`). Also back up the protected `.env` with mode
`0600`. Retain the previous immutable image tag. To roll back, restore that image
tag and disable `OSTAG_COPILOT_STATUS_BOARD_ENABLED`; restore the database only if
an incompatible migration requires it.

## Production Notes

- Start with `OSTAG_BACKEND=mock`, then switch to Codex/OpenAI after message
  delivery is confirmed.
- Keep `MFS_ALLOWED_SCOPES` narrow.
- Keep unsafe CLI flags disabled unless testing on an isolated machine.
- Rotate Feishu/Lark secrets if audit logs or shell history expose credentials.
- Keep `OSTAG_OPERATIONAL_REPORT_ENABLED=false` until human identity bindings,
  connector smoke evidence, and the protected workstream path have been verified.
  See [operational_report.md](operational_report.md).

## GitHub Actions production deployment

The production workflow in `.github/workflows/deploy-production.yml` runs only
after `OS Tag CI` succeeds on `main`, or by an explicit manual dispatch of the
current `main` commit. It refuses malformed SHAs, stale commits, and manual
deployments unless the latest main-push run of the complete `OS Tag CI` workflow succeeds for the exact commit.

Configure the GitHub `production` environment with these values:

| Type | Name | Purpose |
|---|---|---|
| Variable | `PRODUCTION_SSH_HOST` | Tencent Lighthouse origin address |
| Variable | `PRODUCTION_SSH_USER` | Production SSH account selected for the forced-command key |
| Variable | `PRODUCTION_SSH_PORT` | SSH port |
| Variable | `PRODUCTION_URL` | `https://ostag-monitor.ihiking.me` |
| Variable | `PRODUCTION_DEPLOY_SCRIPT_SHA256` | Expected hash of the trusted server deploy script |
| Variable | `PRODUCTION_SSH_WRAPPER_SHA256` | Expected hash of the forced-command SSH wrapper |
| Secret | `PRODUCTION_SSH_PRIVATE_KEY` | OStag-only deployment identity |
| Secret | `PRODUCTION_SSH_KNOWN_HOSTS` | Pinned origin host key; never use runtime `ssh-keyscan` as trust |

Restrict the environment to the `main` branch. The repository's protected
`main` branch requires one approval, dismisses stale approvals, requires the
last push to be approved, and enforces those rules for administrators. The
environment does not replace that review gate.

The workflow builds a `linux/amd64` image tagged from the full commit SHA,
creates a source archive with `git archive`, checksums both artifacts, and
streams them over strict-host-key SSH. The dedicated key is installed with
OpenSSH `restrict` and a forced command. The wrapper accepts only version
checks, preflight, two checksum-bound archive uploads, and one bounded deploy
command; it cannot open an interactive shell or forward ports. The trusted
server-side `scripts/deploy_work_monitor_release.sh` then:

1. verifies the current container, local health endpoint, protected mode-0600
   environment file, and SQLite integrity;
2. verifies both artifact checksums and rejects unsafe archive paths;
3. installs the source under `/opt/ostag-releases/<commit-sha>` and loads the
   immutable image;
4. creates and integrity-checks an online SQLite backup inside the existing
   named volume;
5. recreates the `work-monitor` Compose project with the base Compose file and
   waits for container health;
6. records the previous image, Compose file, and backup path in
   `deploy-receipt.json` before GitHub checks the public `/healthz` endpoint.

If the new container does not become healthy, the deploy script restores the
previous image and Compose file. It deliberately does not restore the database
automatically: retain the failed database and use the verified backup only
after an operator confirms that a migration is incompatible.

Run a non-mutating server check at any time:

```bash
/usr/local/sbin/ostag-deploy-work-monitor preflight
```

### Host deployment mutex (D02 prerequisite, repository support only)

The repository deploy script takes one exclusive, nonblocking Python 3
`fcntl.flock` lock at `/run/ostag-work-monitor-deploy.lock`. Valid `preflight`
and `deploy` calls use the same host-wide lock, independent of release SHA,
Compose project, and `OSTAG_RELEASE_ROOT`. Argument count, command, SHA and
checksum syntax are checked before locking or runtime access. Acquisition
precedes the first Docker call and production environment/database reads.
The lock covers the whole preflight or deployment, including archive checks,
backup, image switch, health/image verification, every receipt, cleanup, and
internal rollback. Rollback runs under the original lock without reacquiring it.

A concurrent call exits immediately with status 1 and exactly
`production deploy refused: deployment lock is busy`, before Docker, curl,
database access, or rollback. Unsafe/unavailable lock paths also fail closed
with status 1 and the fixed message
`production deploy refused: deployment lock is unavailable or unsafe`;
neither error includes paths, exception details, or environment values.

Python acquires the lock once, then replaces itself with the Bash continuation
in the same parent PID, carrying the open descriptor. There is no skip-lock
argument or environment marker. The kernel releases ownership when the last
inherited descriptor closes on success, failure, or process exit. Runtime
children inherit the descriptor: killing only the parent does not unlock while
an in-flight child still holds it. A leftover file is normal and reusable;
never unlink, replace, or truncate it to clear a lock, since that can create
independent inode owners. This is an advisory lock for cooperating invocations
of the updated script; direct Docker commands and older installed scripts do
not participate.

Lock path components must be real directories owned by root or the effective
deploy UID, with no group/other write permission. Root-owned sticky ancestors
(such as `/tmp`) are allowed only above a protected immediate parent. The
script walks directory descriptors without following symlinks. The lock itself
must be a regular file owned by the effective UID, exactly mode `0600`, with
one link; symlinks, hardlinks, special files, and permissive files are refused.
The default `/run` location assumes the existing trusted root host invocation.

`OSTAG_DEPLOY_LOCK_PATH` is an explicit absolute path override solely for trusted
isolated tests or host administration. Tests must choose a private local temp
directory and share that one path across all competing invocations, even when
their release roots differ. An empty or unsafe override fails; it never disables
locking. The launcher, its environment/PATH, script, and lock directory owners
are trusted: allowing an untrusted caller to choose a different safe lock path
would defeat coordination. Do not accept this override from release artifacts,
SSH command arguments, or untrusted environment forwarding; production callers
must share the default identity. No wrapper or Actions changes are part of this
prerequisite.

This is **repository support only**, not evidence of a production installation
or a completed D02. Trusted host installation of the reviewed script and the
corresponding `PRODUCTION_DEPLOY_SCRIPT_SHA256` hash update remain future gates.
The full A11 drain, multistore backup, and rollback rehearsal/evidence also remain
future gates. The mutex does not expand the existing single-SQLite backup schema
or implement that procedure. Nothing in this slice installs, deploys, changes
production credentials/control flags, or marks D02 done.

Local verification uses subprocesses and real kernel locks with fake Docker,
curl, and local archives; it never opens a production database. Run:

```bash
python -m pytest -q tests/test_production_deploy.py tests/test_production_deploy_lock.py
ruff check tests/test_production_deploy.py tests/test_production_deploy_lock.py
bash -n scripts/deploy_work_monitor_release.sh
```

Contention, unsafe-path, malformed-input, and process-exit tests run on macOS
as well. Full deploy/receipt/rollback tests require Linux because the existing
runtime uses GNU `stat -c` and Bash `mapfile`; their macOS skips are not Linux
deployment evidence. Run those tests on real Linux before advancing the gate.

Rotate the GitHub deployment identity independently of developer SSH keys.
Install the replacement public key first, update the environment secret, prove
one preflight/deploy connection, and only then remove the previous key.

### Network access from GitHub-hosted runners

The Lighthouse firewall restricts administrative TCP/22 to the deployment
workstation's fixed IP. GitHub-hosted runners have changing egress addresses, so
using TCP/22 from GitHub times out before authentication. Production uses a
separate TCP/2222 listener for automation, configured by
`deploy/work-monitor/sshd_config_ostag_deploy` and
`deploy/work-monitor/ostag-deploy-sshd.service`.

The separate daemon reads only `/etc/ssh/ostag-deploy-authorized-keys`, containing
the OStag deployment public key with the existing
`restrict,command="/usr/local/sbin/ostag-github-actions-ssh"` restrictions. Both
the daemon and the key enforce the deployment wrapper. Password authentication,
PTYs, user startup scripts, and all forwarding are disabled. General-purpose
administrator keys are not accepted. This follows OpenSSH's
[`forced-commands-only` and `DisableForwarding` settings](https://man.openbsd.org/sshd_config).
The trusted wrapper still runs as root because loading images and switching the
Compose service require host privileges; compromise of the deployment identity
must therefore be treated as a production incident.

Install the two configuration files as root, mode `0644`, at
`/etc/ssh/sshd_config_ostag_deploy` and
`/etc/systemd/system/ostag-deploy-sshd.service`. Install the restricted key file
as root, mode `0600`. Validate before starting:

```bash
sshd -t -f /etc/ssh/sshd_config_ostag_deploy
systemctl daemon-reload
systemctl enable --now ostag-deploy-sshd.service
ufw limit proto tcp from 0.0.0.0/0 to any port 2222 comment 'OStag restricted deploy'
```

Add a Lighthouse TCP/2222 ACCEPT rule for IPv4 runners while retaining the
existing administrative TCP/22 rule unchanged. The dedicated deployment port is
publicly reachable; authentication is limited to the dedicated key and its
forced commands. Do not allow password login or general-purpose keys on it.
Set the GitHub production variable `PRODUCTION_SSH_PORT=2222`. Pin the existing,
independently verified server host key in `PRODUCTION_SSH_KNOWN_HOSTS` under
`[<production-host>]:2222`; do not trust a fresh network scan.

Before enabling automated releases, prove that `version` and `preflight` work
with the deployment key, an arbitrary shell command is refused, and a developer
key cannot authenticate on TCP/2222. Rerun the production GitHub workflow for
the current, CI-approved `main` commit and verify its release receipt, image
revision, database backup, and public health endpoint.

To withdraw the automation ingress, stop and disable
`ostag-deploy-sshd.service`, remove only the TCP/2222 cloud and UFW rules, and
set `PRODUCTION_SSH_PORT=22`. Workstation deployment through the existing
restricted key remains available; GitHub-hosted deployment will again require
a network path. Key rotation must update the separate authorized-keys file too.

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

### Native production-image CI

The `image-smoke` job is part of **OS Tag CI**, so its failure prevents that
workflow from succeeding and triggering automatic production deployment. Manual redeploy
also requires the latest matching main-push run of that complete workflow to have
succeeded; an older success, PR-only run or isolated release-check is insufficient. It builds
`deploy/work-monitor/Dockerfile` from the exact checkout's Git archive on the
native Linux/amd64 runner, without repository credentials in the build context.

`scripts/verify_work_monitor_image.py` performs one cold container start with no
network, host mounts or published ports. It verifies UID10001, all six Brief flags
OFF, running disabled status, no Brief business databases, exact imported source
hashes, a fresh current-boot drain acknowledgement and rejection of a wrong-boot
control. It saves a receipt and container logs as the workflow artifact
`work-monitor-image-verification`. A health HTTP200 alone does not pass; the
container is not restarted to turn a failed cold boot into a passing result.

The verifier requires a native Linux/amd64 daemon. Local ARM-to-amd64 emulation
can hit the status subprocess's existing startup deadline; that failure must be
recorded rather than weakening the production timeout or silently accepting a
warm restart. This image check does not enable Brief, prove provider permissions,
install host tools, exercise multistore rollback or replace D02/live acceptance.

The production workflow rebuilds its image after CI. It therefore runs the same
isolated native image verifier against that exact rebuilt image and release SHA
before packaging or configuring SSH. Automatic and manual deployments both stop
on verification failure; evidence is retained as a GitHub Actions artifact. This
closes the gap where mutable base images or dependency resolution could make the
image uploaded to production differ from the earlier CI-tested image.
## Explicit Brief host composition and production observation v3

The A11 lifespan integration supports an explicit authenticated application API;
it does not install a host preflight, activate a provider, or change production
pins. All six business flags still default off. Environment flags alone cannot
supply a trusted issuer or construct a functioning business runtime.

Host application code constructs `TrustedBriefComposition` from independently
verified local adapters and passes it to
`ostag.work_monitor.server.create_app(settings=..., brief_composition=...)`.
Required fields are `directory`, `image_sha256`, `authority`, `model_transport`,
`generation`, `authenticate`, `resolve_binding`, `card_transport`, `owners`, and
`policy`. `inventory` pins the current schemas and capacities. `initialize_new`
defaults false; explicitly setting it true permits a wholly absent five-store set,
under the acquired persistence lock, in a precreated mode-0700 directory. It never
repairs partial sets or migrates existing databases. Business files must be owned
by the process UID, regular, single-link and mode 0600. Symlink paths, aliases,
WAL/SHM/journal recovery and incompatible schemas fail closed. Do not copy a live
store or clear a lock file to make startup succeed.

Orphan recovery sidecars block even a wholly absent database set, before any file
reservation or schema initialization. Store permissions must be exactly 0600;
read-only or executable database files are not accepted as ready. Inventory
capacities and budgets are validated before construction.

During the running FastAPI lifespan, the verified adapter uses:

```python
application = app.state.brief_application
await application.submit(verified_context, normalized_manual_event)
await application.submit_meeting(verified_context, event_id, durable_meeting_request)
await application.status(verified_context, job_id)
await application.private_export(verified_context, job_id)
await application.cancel(verified_context, job_id)
```

`verified_context` is opaque to this module. The supplied `authenticate` callback
must derive a current `RequesterContext` from an already authenticated adapter
session; no fabricated default user is provided. Manual event principal/namespace
must match it. Meeting requests must be present in the independent current
`TrustedBriefAuthority`. There is no public HTTP grant, control or bearer endpoint.
The module-level default server supplies no host composition and therefore stays
closed if BRIEF is enabled. Deploying a reviewed provider issuer and connecting its
verified ingress are still explicit outstanding work, not implied by this API.

Trusted host/operator code controls the live owner, with fresh boot and identities:

```python
owner = app.state.brief_owner
await owner.request_control(
    boot_id=owner.status.boot_id, generation=1, admission="on",
    **owner.control_identity(),
)
# Later: a strictly higher generation closes admission immediately.
await owner.request_control(
    boot_id=owner.status.boot_id, generation=2, admission="off",
    **owner.control_identity(),
)
```

This API is not exposed to application callers. The protected control parent must
be mode 0700. It atomically writes control **version 2**, binding boot, generation,
admission, maintenance, image digest, config digest and persistence identity. It
serializes with publication. Direct external file replacements are still checked
but a replacement during publication may fail Brief closed. New boots reject old
controls. Submitted generations are reserved immediately, so even two writes before
the next sample cannot reuse a generation. The existing 250ms sampling, 500ms status IO budget, 5s child bootstrap
budget and 750ms authority lease are unchanged. On an unchanged production ON
renewal, operations can use only the original unexpired authority until ACK;
OFF and changed controls seal admission before observation.

Production **status version 3** (`brief-production/3`) includes the same local
custody observation plus image/config/persistence and reconciliation identities.
`drain_ack` requires current OFF control, zero local custody, no uncertainty and a
fresh successful five-store reconciliation under exclusion. A pending queue of
unclaimed registered jobs is allowed: it has no active lease/remote effect and is
preserved for an authorized restart. Reconciliation never uses a caller-provided
ready boolean. Persisted job generation configuration must match the host's pinned
configuration; mismatches quarantine startup. During active work the durable counts refer to the last completed
scan; only `production.reconciled=true` marks current all-store drain evidence.
Legacy attached **v2 continues to have `drain_ack=false`**; disabled v1 retains its
existing compatibility contract.

`validate_production_drain(owner, observation, boot_id=..., generation=...,
image_sha256=..., config_sha256=..., persistence=...)` requires the current live
owner observation sealed from the acknowledged IPC result, matching
identities/generation and freshness. Mutated, copied or stale JSON
cannot satisfy it. A future host preflight needs a live challenge to this owner;
reading saved `status.json` is insufficient. This protocol alone does not install
D02's preflight, authorize activation, or establish global atomicity against
noncooperating database writers.

For read-only maintenance, construct the same-image trusted composition with
`maintenance=True, initialize_new=False`; no service/worker/card runner is
constructed. Request OFF with `maintenance=True`. All five business stores remain
unchanged. Missing/incompatible schemas and unresolved effects remain blockers.
This is not a downgrade migration. Older images that cannot read these schemas
must not open them; supported multistore restore/repinning and downgrade procedures
still require an explicit reviewed rehearsal. Unknown remote effects forbid drain
until independent verified reconciliation exists; do not clear them or kill
unrelated processes.

Runnable synthetic server-lifespan and fixed regression commands (no credentials,
external collection, model service or external messages):

```bash
.venv/bin/python -m pytest -q tests/test_brief_production_owner.py tests/test_brief_startup_reconciliation.py
.venv/bin/python -m pytest -q tests/test_brief_*.py tests/test_meeting_*.py
pandoc --from=commonmark --to=html docs/ostag/architecture.md --output=/tmp/ostag-architecture.html
pandoc --from=commonmark --to=html docs/ostag/deployment.md --output=/tmp/ostag-deployment.html
```

The current meeting suites still require installed Pandoc; keep those parser
assertions and the real loopback TCP/TLS revocation tests. Required macOS and
isolated Linux evidence, the actual final Claude CLI Opus5 audit, and all original
A11/D02/provider/live gates must be recorded separately. Synthetic passes never
close the frozen 31+9 scope or all120AC.


Production Brief construction has a separate bounded allowance of five seconds
for setup plus `0.5 + 2 * sum(store_scan_budget_ms) / 1000` seconds for the two
five-store reconciliation passes (at most 55.5 seconds total with the supported
per-store limits). This does not change the status child's five-second bootstrap,
500 ms IPC deadline, or 750 ms authority lease. Invalid composition types degrade
Brief without preventing Monitor startup. A v3 drain consumer must challenge the
current owner through `await owner.challenge_drain(...)`: a new pending reconciliation
immediately invalidates the preceding proof, and the 750 ms freshness limit is
measured from scan completion as well as status publication, without adding the
two windows together. Detached JSON remains insufficient.


Business construction now runs as an application-owned startup task after the
bounded status-child handshake; the Monitor lifespan does not await its database
scan. Brief intake stays denied until startup completes. Shutdown seals the task
and retains the business future and persistence lock until the actual constructor
and finalizer return, including a worker created concurrently with shutdown.
Trusted host consumers should use `await owner.challenge_drain(...)`: it samples
and validates the current observation under the control/publication lock. False
while a scan is pending is retryable observation, not permission to bypass it.
This is a point-in-time challenge, not a lease for a later mutation or saved JSON.
The current paused implementation performs periodic two-pass reconciliation;
its IO cost and remaining host preflight/backup integration are not live-accepted.
