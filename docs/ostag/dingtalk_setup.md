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
OSTAG_BACKEND=mock
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
uv run python -m ostag.main --platform dingtalk --live
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
