# Lark Setup

## Create The App

1. Open Lark Developer Console and create an app.
2. Enable bot capability.
3. Copy App ID and App Secret into `.env` as `LARK_APP_ID` and `LARK_APP_SECRET`.
4. Set `OSTAG_PLATFORM=lark` and optionally `LARK_API_BASE_URL`.

## Configure Events

1. Enable event subscription.
2. Use WebSocket long connection for the MVP.
3. Subscribe to message receive events.
4. Configure app visibility.
5. Add the bot to the target group.

## Run

```bash
uv sync --extra feishu
python -m ostag.main --platform lark
```

Test in the group:

```text
@OS Tag Please summarize the next action items
```

## Feishu vs Lark Notes

The MVP reuses the Feishu event model because Lark and Feishu message events are
closely aligned. Lark deployments should verify API base URL, permission names,
and app visibility in the Lark console before live testing.

## Troubleshooting

- Confirm `LARK_APP_ID`, `LARK_APP_SECRET`, and app visibility.
- Confirm message receive events are subscribed.
- Use `examples/lark_event.json` for dry-run before live testing.
