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

Normal messages and card actions deliberately use different transports:

- `im.message.receive_v1` is received only by the supervised WebSocket client.
- interactive-card callbacks use only `POST /webhooks/feishu/card-action`.
- do not configure a generic `/webhooks/feishu` callback. Message events sent to
  the card endpoint are rejected.

## Reproducible `lark-cli` Provisioning

Authenticate `lark-cli` with an administrator identity, then inspect the schema
before each mutation. API availability differs by tenant, so keep IDs and
secrets as placeholders in shell history and inject the real values from a
secret manager:

```bash
lark-cli auth login
lark-cli schema application.application.create
lark-cli schema application.application.patch

export FEISHU_APP_ID='<APP_ID_FROM_SECRET_MANAGER>'
export FEISHU_APP_SECRET='<APP_SECRET_FROM_SECRET_MANAGER>'
export FEISHU_VERIFICATION_TOKEN='<VERIFICATION_TOKEN_FROM_SECRET_MANAGER>'
export FEISHU_ENCRYPT_KEY='<ENCRYPT_KEY_FROM_SECRET_MANAGER>'
```

Use the tenant's supported application-management schema to create the
enterprise self-built app, or update the existing app. Record the returned app
ID, but never paste the app secret, verification token, encrypt key, WebSocket
ticket, or tenant token into tickets, logs, commands, or documentation.

The minimum runtime permission set is:

- receive bot messages (`im:message.receive_v1`);
- send/reply to messages as the app (`im:message`);
- no Base, Drive, Calendar, Contact, or tenant-admin scopes unless another
  explicitly enabled OStag connector needs them.

Configure the card callback as:

```text
https://<PUBLIC_HTTPS_HOST>/webhooks/feishu/card-action
```

The endpoint must pass Feishu URL verification before publication. Keep the
encrypt key enabled: the server authenticates the signature over the exact raw
body, persists timestamp/nonce replay state, and only then decrypts and validates
the verification token and strict callback schema.

Publish a test version to the smallest availability scope first. Validate one
private delivery (recipient is the user's `open_id`) and one group delivery
(recipient is the group's `chat_id`), including the plain-text fallback when an
interactive card cannot be delivered. Expand availability only after both paths
pass.

Rollback procedure:

1. Restore the previous published app version and callback URL.
2. Disable the new card callback and keep WebSocket message receipt running.
3. Rotate `<APP_SECRET>`, `<VERIFICATION_TOKEN>`, and `<ENCRYPT_KEY>` if any may
   have been exposed; update the secret manager and restart OStag.
4. Revoke scopes added by the failed version and retain the audit evidence.

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
