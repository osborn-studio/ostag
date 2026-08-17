# Connector Capabilities

Every platform adapter/connector declares what it can and cannot do, so
renderers can degrade gracefully and policy can refuse operations a platform
does not support. Declarations live in `ostag.core.capabilities` and reflect
the actual implementation state of this repo — they are honest, not
aspirational.

## Declared connectors

| Platform | Connector | Status | Highlights |
|---|---|---|---|
| slack | chat | available | reply/proactive text; no buttons yet |
| feishu | chat | available | reply/proactive text |
| feishu | base | available | read/create/update records, batch 30, no delete |
| lark | chat | available | reply/proactive text |
| lark | base | available | read/create/update records, batch 30, no delete |
| dingtalk | chat | available | reply/proactive text |
| dingtalk | ai_table | available | read/create/update records, batch 30, no delete |
| github | issues | available | create/comment via gh CLI; no close/delete (`docs/github_connector.md`) |
| github | pull_requests | available | bounded reads; governed APPROVE + Vercel Production release; no merge/close |
| gitlab | issues | available | create/comment via glab CLI; no close/delete (`docs/gitlab_connector.md`) |

Policy defaults for every connector: `default_mode: propose`,
`destructive_requires_approval: true`. Table connectors deliberately declare
`can_delete_records: false` — deletion is not part of the current product
surface.

## Schema

```yaml
platform: dingtalk
connector: ai_table
capabilities:
  can_read_records: true
  can_write_records: true
  can_create_records: true
  can_delete_records: false
  can_send_proactive: true
limits:
  batch_size: 30
policy:
  default_mode: propose
  destructive_requires_approval: true
status: available
```

Capability values are `true`, `false`, or a qualifier string such as
`"limited"`. `ConnectorCapability.can(name)` returns `True` only for a strict
`true`, so `"limited"` never accidentally authorizes an operation.

## API

```python
from ostag.core.capabilities import get_capability, list_capabilities

list_capabilities()             # all declarations
list_capabilities("dingtalk")   # one platform
get_capability("feishu", "base")  # raises SchemaError(unknown_connector) if absent
```

CLI:

```bash
ostag connector list --json
ostag connector list --platform dingtalk --json
```

## Adding a connector

1. Add a `ConnectorCapability` entry in `ostag/core/capabilities.py`.
2. Declare only what the code can actually do today; use `status: planned`
   for stubs.
3. Add a test in `tests/test_capabilities.py`.
4. Update the table above.
