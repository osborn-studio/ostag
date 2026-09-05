# Operational report

The operational report is an identity-scoped, read-only projection over the
existing work monitor. It summarizes instance freshness, governed workstreams,
completion evidence counts, connector smoke status, attached-run cost, and human
escalations. Schema v2 also includes a read-only Agent Presence summary and a
presence value for each authorized instance returned within the configured
`OSTAG_OPERATIONAL_REPORT_MAX_ITEMS` bound. Raw prompts, task titles,
summaries, logs, repository paths, credentials, connector targets, and
escalation reasons are never projected.

Agent Presence is derived on every read; there is no presence table. The six
states are:

| State | OStag meaning |
| --- | --- |
| `setup_required` | An authorized binding has no registered device or legacy instance projection. |
| `needs_attention` | A human decision/escalation is pending, or work is blocked. |
| `offline` | Setup exists but the monitor heartbeat is not fresh. |
| `working` | A fresh instance projects a running task or relay session, even if more work is queued. |
| `queued` | A `triage`, `todo`, or `ready` task is durably waiting, including while offline. |
| `available` | The instance is fresh with no queued, running, or attention-requiring work. |

These values are status projections only. They never claim, retry, cancel, or
settle work and are not completion evidence.

The feature is disabled by default:

```dotenv
OSTAG_OPERATIONAL_REPORT_ENABLED=true
OSTAG_OPERATIONAL_REPORT_CONNECTOR_EVIDENCE_DIR=/var/lib/ostag/connector-evidence
OSTAG_OPERATIONAL_REPORT_MAX_ITEMS=100
OSTAG_WORKSTREAM_STORE_PATH=/var/lib/ostag/workstreams.sqlite3
```

Connector status comes only from bounded `connector-live-<platform>.json` files
created by `scripts/connector_live_smoke.sh`. The report never performs a live
provider request. A missing file is `unknown`; malformed evidence is `error`
without exposing the file path or parser error.

## CLI

```bash
ostag monitor report \
  --platform feishu \
  --actor ou_member \
  --scope personal \
  --format json

ostag monitor report \
  --platform feishu \
  --actor ou_owner \
  --scope team \
  --format html \
  --output release/operational-report.html
```

Members see only instances they own or were explicitly granted. `team` scope
requires a currently bound private owner/admin identity.

## HTTP

Authenticated human credentials use the existing monitor bearer-token boundary:

```text
GET /v1/report?scope=personal
GET /v1/report.html?scope=team
Authorization: Bearer <human credential>
```

The HTML renderer escapes every dynamic value and contains no client-side script.
Both surfaces share the same schema-v2 report projection. The top-level
`presence` object contains the highest-priority state among the deterministically
bounded `instances[]` projection and counts for all six states. Each returned
instance contains its own `presence` object with a stable `reasonCode` and
bounded human-readable reason.

## Deployment and rollback

For the container deployment, keep the workstream database and connector-evidence
directory on the protected `/var/lib/ostag` volume. Run connector smoke checks on
the deployment host and place only the resulting structured JSON in
`/var/lib/ostag/connector-evidence`.

To roll back, set `OSTAG_OPERATIONAL_REPORT_ENABLED=false` and restart the monitor.
The two routes are no longer registered. No schema or data migration is required.
Reverting only the Presence slice restores the schema-v1 response shape without
altering stored monitor data.
