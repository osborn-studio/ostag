# Copilot Relay rollback evidence

Date: 2026-07-14 (Asia/Shanghai)

## Verified rollback controls

The rollback drill exercised the documented order: stop the local reporter,
disable relay writes, recreate the central service from the previous immutable
image, verify health and data invariants, restore the candidate image, then
restart the reporter. No queue, relay table, binding, or spool row was deleted.

The final release corrected the storage ambiguity discovered during rollout:

- Compose project: `work-monitor`
- Required volume: `work-monitor_work-monitor-data`
- Required mount: `/var/lib/ostag`
- Forbidden for this deployment: `docker-compose.production.yml`, because its
  obsolete host bind path is not the authoritative database.
- Protected environment source:
  `/opt/ostag-work-monitor/deploy/work-monitor/.env`
- Final immutable image: `ostag-work-monitor:copilot-01a2c73`
- Previous known-good image retained locally:
  `ostag-work-monitor:copilot-4407567`

## Recovery invariants

- A fresh pre-release SQLite backup exists in the authoritative named volume:
  `/var/lib/ostag/backups/work_monitor-before-01a2c73-20260714T131656Z.sqlite3`.
- Backup size is 2,363,392 bytes, mode `0600`, integrity `ok`.
- Live database integrity is `ok` after final recreation.
- Final container health is `healthy` and external health is HTTP 200.
- The accepted canary remains session version 7, state `idle`.
- Decision `decision_canary_live_1` remains `answered`; its applied receipt and
  delivered assistant snapshot remain durable.

## Exact rollback command shape

Run from the immutable release directory with the protected `.env` symlink and
the base Compose file only:

```sh
OSTAG_WORK_MONITOR_IMAGE=ostag-work-monitor:copilot-4407567 \
  docker compose up -d --no-build work-monitor
```

After recreation, require `healthy`, the exact named-volume mount, database
integrity `ok`, and external HTTP 200 before re-enabling local relay delivery.
