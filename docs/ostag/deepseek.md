# DeepSeek business model configuration

OStag Brief/meeting composition can use DeepSeek with the fixed official origin
`https://api.deepseek.com`. Operators enter only a dedicated API key. Neither an
environment base URL nor a caller-supplied URL can redirect private source data.
Codex/Claude development and audit credentials are unrelated.

The default profile selects `deepseek-flash`, non-thinking, text-only JSON Chat
Completions, 2,048 output tokens, one call, a 120-second deadline, a $0.10 job cap
and a $1 UTC-day cap. There is no automatic OpenAI fallback. Existing OpenAI
transport remains available for already-reviewed explicit compositions.

## Private key entry

Run on the machine that should own the credential, in an interactive terminal:

```sh
python -m ostag.briefs.deepseek_credentials --directory /absolute/private/secrets
```

The parent directory must already exist. This creates a directory with mode 0700
and an exclusive `deepseek-api-key` file with mode 0600. Existing keys are never
silently replaced. Input is hidden and refuses a terminal that cannot disable
echo. Never pass the key as an argument, paste it in chat, or commit it to Git.
The input command saves the key only; it does not start jobs or send data.

An optional explicit check sends one small synthetic prompt, incurs API usage,
and reports only a fixed status and a conservative cost estimate:

```sh
python -m ostag.briefs.deepseek_check --directory /absolute/private/secrets
```

A locally entered key is **local only**, not evidence of server configuration.
On production, provision the file for the application's UID (container UID 10001),
mount the protected directory read-only, and invoke `production_inputs(directory)`
from the trusted host composition. This returns the DeepSeek model transport and
its generation configuration. It does not construct authentication, issue grants,
activate Brief flags or bypass startup reconciliation. Do not mount the entire
host runtime directory into the application.

## Pricing and validation

Official sources inspected 2026-09-22:

- [Models, origin and USD prices](https://api-docs.deepseek.com/quick_start/pricing/)
- [Chat Completions fields](https://api-docs.deepseek.com/api/create-chat-completion/)
- [Thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)

The ledger records measured token counts priced at the **peak USD tariff**:
$0.30/M uncached input, $0.006/M cached input, $1.20/M output, rounded up to an
integer microUSD. This is a conservative estimate, not an invoice. Off-peak
pricing and RMB account bills differ. Admission rounds rates up further to
1 microUSD/input token and 2 microUSD/output token. Cache details must be mutually
consistent; missing cache detail receives no discount. Unknown model/usage stays
unpriced and quarantined. Measured overruns are never clamped.

Admission uses UTF-8 input bytes plus 512 framing headroom, not a remote token
count receipt. A request too close to the byte limit is refused before egress.
Upstream usage is checked again before accepting content. Provider tokenization
and pricing are external assumptions; caps are local controls, not a contractual
provider billing guarantee. Do not infer zero spend from a timeout.

`deepseek-flash` is a mutable provider alias, not an immutable snapshot. Quality
acceptance applies to the evaluated deployment window; repeat the real-model
holdout evaluation after provider changes. Never carry OpenAI evaluation receipts
forward as DeepSeek acceptance.

The adapter retains bounded responses, duplicate-key rejection, fixed diagnostics,
no redirects/retries/environment proxies, cancellation cleanup, per-write current
authorization and strict content validation. No tools, web search, attachments,
reasoning history or conversation persistence are requested.

## Rollout status

Provider integration and synthetic tests do not complete the larger production
activation. The authenticated host factory/ingress, ledger host migration, real
key smoke check, independent semantic evaluation and real business pilots retain
their separate acceptance gates. Switching the provider does not turn them green.
