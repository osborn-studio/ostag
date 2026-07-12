# OS Tag DingTalk Setup

OS Tag can run in DingTalk as a group-chat robot adapter.

## Modes

The DingTalk adapter supports two transport paths:

- Incoming group messages through DingTalk Stream mode.
- Replies through the incoming message `sessionWebhook`.
- Proactive messages, such as daily digests, through a custom robot webhook.

## DingTalk App

Create a DingTalk robot/app in DingTalk Open Platform and enable Stream mode for
robot messages. Record:

- `DINGTALK_CLIENT_ID`
- `DINGTALK_CLIENT_SECRET`

For proactive messages, create or reuse a custom robot webhook and record:

- `DINGTALK_ROBOT_WEBHOOK`
- `DINGTALK_ROBOT_SECRET` if the webhook uses signed requests

## Environment

```bash
OSTAG_PLATFORM=dingtalk
DINGTALK_CLIENT_ID=ding...
DINGTALK_CLIENT_SECRET=...
DINGTALK_ROBOT_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=...
DINGTALK_ROBOT_SECRET=SEC...
OSTAG_BACKEND=codex_cli
OSTAG_AUTO_BASE_DEFAULT_BASE_TOKEN=<base_token>
OSTAG_AUTO_BASE_DEFAULT_TABLE_ID=EMSgbTd
OSTAG_AUTO_BASE_DEFAULT_TABLE_NAME=需求收集管理
DWS_CLI_BIN=/Users/gilbertchen/.nvm/versions/node/v22.19.0/bin/npx -y dingtalk-workspace-cli
```

Keep production DingTalk credentials in `.env.dingtalk`; this file is ignored
by git. The current production app metadata is:

```text
unifiedAppId: 5a83bd9f-1fdb-4da9-b3aa-5090f7f57947
robotCode/clientId: dingmradv5eeaywwkj4o
robotName: OS Tag
launchd label: ai.ostag.dingtalk
```

Install DingTalk dependencies:

```bash
uv sync --extra dingtalk
```

Preflight:

```bash
uv run python -m ostag.main --platform dingtalk --live --doctor
```

Run:

```bash
uv run python -m ostag.main --config .env.dingtalk --platform dingtalk --live
```

Render and install the macOS launchd service:

```bash
scripts/render_dingtalk_launchd_plist.sh
cp release/ai.ostag.dingtalk.plist ~/Library/LaunchAgents/ai.ostag.dingtalk.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/ai.ostag.dingtalk.plist
launchctl kickstart -k "gui/$(id -u)/ai.ostag.dingtalk"
```

Check service status:

```bash
launchctl list | rg 'ai\.ostag\.dingtalk'
tail -80 logs/launchd-dingtalk.err.log
```

Dry-run fixture:

```bash
uv run python -m ostag.main --platform dingtalk --dry-run --event examples/dingtalk_event.json
```

## Requirement Operations

DingTalk messages use the same `NormalizedEvent` contract as Feishu, Lark, and
Slack. In DingTalk mode, OS Tag uses the DWS AI Table connector to manage the
configured DingTalk AI Table:

```text
Base: OS Tag 需求管理
Table: 需求收集管理
URL: https://alidocs.dingtalk.com/i/nodes/vNG4YZ7JnPmQKkapTN6DDzzeW2LD0oRE?iframeQuery=sheetId%3DEMSgbTd
Source parity target: 飞书「🛠️ 需求收集与管理 底表」/「需求收集管理」
```

The table mirrors the Feishu source schema as closely as DingTalk AI Table
allows: field names, status and priority options, user/group/date fields, form
view, grid views, and kanban status view are present. Compatibility fields:
`建群按钮`, `工作日天数`, and `需求持续时长` are stored as maintainable text/number
fields because the exact Feishu button/formula field behavior is not available
through the current DingTalk DWS CLI.

The following roles were created with advanced permissions enabled:

```text
需求协作编辑者: edit-own on 需求收集管理
需求只读观察者: read on 需求收集管理
```

DWS currently does not support binding members to AI Table roles. Assign people
in the AI Table Web UI: Base 设置 -> 高级权限 -> 角色管理.

Supported commands:

```text
@OS Tag 请把 交易盯盘 状态改为 开发交付中
@OS Tag 主动巡检状态
```

DingTalk user IDs are not automatically interchangeable with Feishu/Lark
`open_id`s. Owner assignment in DingTalk should use DingTalk identities; cross
tool assignment requires an explicit identity mapping.

## Live Validation

On 2026-07-01, the OS Tag DingTalk app was created, released, and launched via
launchd. A controlled single-chat validation message was sent to the robot and
processed through DingTalk Stream:

```text
input_preview: 请用一句话回复：dingtalk inbound validation ok
status: ok
```

The production service was then switched from `mock` to `codex_cli` and
validated again:

```text
backend: codex_cli
input_preview: 请用一句话回复：dingtalk codex backend validation ok
status: ok
```

Adding the robot to one existing single-member group through DWS returned
`bot and group not match`; group installation may still require the DingTalk
client's group robot picker or a group type that allows this internal robot.

Requirement table smoke validation also passed on 2026-07-01:

```text
created: 1 record in 需求收集管理
searched: OS Tag 钉钉需求表 smoke validation
updated: 需求状态 -> 方案设计中
tests: 102 passed
```

End-to-end DingTalk robot validation also passed after launchd was configured
with a Node/NPM PATH:

```text
input_preview: 请把「OS Tag 钉钉需求表 smoke validation」状态改为 方案设计中
backend: auto_base
status: ok
verified table value: 需求状态 = 方案设计中
```
