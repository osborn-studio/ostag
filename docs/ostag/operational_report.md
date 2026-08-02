# Operational report

The operational report is an identity-scoped, read-only projection over the
existing work monitor. It summarizes instance freshness, governed workstreams,
completion evidence counts, connector smoke status, attached-run cost, and human
escalations. Raw prompts, task titles, summaries, logs, repository paths,
credentials, connector targets, and escalation reasons are never projected.

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
Both surfaces share the same schema-v1 report projection.

## Deployment and rollback

For the container deployment, keep the workstream database and connector-evidence
directory on the protected `/var/lib/ostag` volume. Run connector smoke checks on
the deployment host and place only the resulting structured JSON in
`/var/lib/ostag/connector-evidence`.

To roll back, set `OSTAG_OPERATIONAL_REPORT_ENABLED=false` and restart the monitor.
The two routes are no longer registered. No schema or data migration is required.
