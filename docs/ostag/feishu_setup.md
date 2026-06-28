# Feishu Setup

## Create The App

1. Open Feishu Open Platform and create an enterprise self-built app.
2. Enable bot capability.
3. Copy App ID and App Secret into `.env` as `FEISHU_APP_ID` and
   `FEISHU_APP_SECRET`.
4. Set the bot display name to `OS Tag` or update `OSTAG_BOT_NAME`.

## Configure Events

1. Enable event subscription.
2. Prefer WebSocket long connection for local or intranet deployment.
3. Subscribe to the message receive event, commonly `im.message.receive_v1`.
4. Copy the verification token and encrypt key into `.env` when configured.
5. Publish the app and set the app availability scope.

## Run

Install optional live dependencies:

```bash
uv sync --extra feishu
```

Start live mode:

```bash
python -m ostag.main --platform feishu
```

Invite the bot into a group and test:

```text
@OS Tag 请总结这个项目下一步应该做什么
```

## Troubleshooting

- `FEISHU_APP_ID and FEISHU_APP_SECRET are required`: fill `.env` and restart.
- No event received: check app publish status, event subscription, bot group
  membership, and app availability scope.
- Reply fails: verify message permissions and that the message id is present in
  the audit log.
- SDK import error: run `uv sync --extra feishu`.
- Backend timeout: increase `OSTAG_AGENT_TIMEOUT_SECONDS` or switch to mock.
