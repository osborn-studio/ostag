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
Slack. If a default Feishu/Lark Base is configured, DingTalk can trigger the
same governed requirement-table operations:

```text
@OS Tag 请把 交易盯盘 状态改为 开发交付中
@OS Tag 主动巡检状态
```

DingTalk user IDs are not automatically treated as Feishu/Lark `open_id`s.
Owner assignment across tools requires an explicit identity mapping in a later
phase or a Feishu/Lark-side command.

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
