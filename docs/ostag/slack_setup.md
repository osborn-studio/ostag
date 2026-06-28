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
