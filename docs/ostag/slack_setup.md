# OS Tag Slack Setup

OS Tag can run in Slack through Slack Bolt Socket Mode.

## Slack App

Create a Slack app at `https://api.slack.com/apps`, enable Socket Mode, and
create an app-level token with:

- `connections:write`

Add bot scopes and reinstall the app:

- `app_mentions:read`
- `chat:write`
- `channels:history`
- `channels:read`
- `groups:history`
- `groups:read`

Subscribe the app to the bot event:

- `app_mention`

Invite the bot to the target channel.

## Environment

```bash
OSTAG_PLATFORM=slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
OSTAG_BACKEND=mock
```

## Channel Brain And Requirement Operations

Slack channels use the same Channel Brain store as Feishu/Lark:

```text
@OS Tag 记住：这个频道用于产品需求评审
@OS Tag 这个群你记住了什么
```

If `OSTAG_AUTO_BASE_DEFAULT_BASE_TOKEN` and
`OSTAG_AUTO_BASE_DEFAULT_TABLE_ID` are configured, Slack can also trigger
governed requirement-table operations against the configured Feishu/Lark Base:

```text
@OS Tag 请把 交易盯盘 状态改为 开发交付中
@OS Tag 请将 市场周报 优先级改为 高
```

Slack user IDs are not automatically treated as Feishu/Lark `open_id`s. Owner
assignment from Slack requires an explicit identity mapping in a later phase or
a Feishu/Lark-side command.

Install Slack dependencies:

```bash
uv sync --extra slack
```

Preflight:

```bash
uv run python -m ostag.main --platform slack --live --doctor
```

Run:

```bash
uv run python -m ostag.main --platform slack --live
```

Dry-run fixture:

```bash
uv run python -m ostag.main --platform slack --dry-run --event examples/slack_event.json
```
