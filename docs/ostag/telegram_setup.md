# Telegram Work Monitor Setup

Telegram support is an HTTPS webhook transport for the persistent work-monitor
server. It is disabled unless the bot token, webhook secret, and webhook URL are
all configured; there is no placeholder route.

## Create and configure the bot

1. In Telegram, open the verified `@BotFather` account and run `/newbot`.
2. Store the issued token only in the deployment secret store as
   `TELEGRAM_BOT_TOKEN`. Do not paste it into source, logs, tickets, or examples.
3. Generate a separate high-entropy `TELEGRAM_WEBHOOK_SECRET`. Telegram sends it
   in `X-Telegram-Bot-Api-Secret-Token`; OStag authenticates this header before
   reading or parsing the request body.
4. Publish the monitor server at a public TLS endpoint and set
   `TELEGRAM_WEBHOOK_URL=https://example.com/webhooks/telegram`.
5. Register the webhook with Bot API `setWebhook`, passing that HTTPS URL and the
   same value as `secret_token`. Do this from a secret-aware deployment tool so
   neither the bot-token URL nor the secret enters shell history or logs.

Production configuration fails closed if the Telegram token or secret is absent,
or if the webhook URL is not HTTPS. Keep the request-body limit enabled. The
server persists every accepted `update_id`, so retries cannot repeat an action.

## Private binding and group access

Private chats are not automatically trusted. Bind the Telegram principal
`('telegram', '<from.id>')` to destination `('telegram', '<chat.id>', 'private')`
using the existing local-admin enrollment procedure. Put administrator actor IDs
in `TELEGRAM_ADMIN_IDS`; destructive cancel controls require an admin private chat
and a second confirmation.

Groups and supergroups are rejected unless their numeric chat IDs appear in
`TELEGRAM_GROUP_ALLOWLIST`. Enroll each allowed group with an explicit monitor
scope and grants. Telegram's verified `chat.type` decides whether a destination is
private or group; callback data cannot override actor, destination, or chat type.

BotFather privacy mode controls which group messages Telegram delivers. Keep
privacy mode enabled when commands and button callbacks are sufficient. Disable
it only if the bot truly needs all group messages, and review the wider data
exposure before doing so.

## Operations and rollback

- Treat callback IDs as one-time opaque values. They contain no actor, task, or
  authorization claims and remain within Telegram's 64-byte limit. Every monitor
  control first creates a governed receipt, then presents a second opaque
  confirmation valid for five minutes. Only that confirmation approves and
  applies the receipt; expired, mismatched, or repeated confirmations create no
  command.
- Rotate both bot token and webhook secret after suspected exposure. Re-register
  the webhook after rotation.
- To roll back, call `deleteWebhook`, remove the three Telegram configuration
  values, and restart the server. The Telegram route then disappears while other
  monitor transports continue to operate.
- No live token is included in this repository. Complete a private-chat and an
  allowlisted-group validation only after an operator supplies real credentials.
