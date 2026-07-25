# EdgeOne origin contract

EdgeOne terminates public HTTPS and reverse-proxies to the Tencent CVM or
Lighthouse origin. It does not run Pages Functions, store SQLite, or own a
scheduler.

- Origin: the host private/public address on TCP 8000; the host firewall allows
  it only from the current EdgeOne origin address ranges and an operator bastion.
- TLS: redirect HTTP to HTTPS, use a valid certificate, and use HTTPS to origin
  when the selected CVM network path supports it.
- Routes: proxy `/healthz`, `/v1/*`, `/webhooks/feishu/card-action`, and
  `/webhooks/telegram` without rewriting request bodies or authentication headers.
- WAF: enable managed rules; block unexpected methods and paths. Do not cache API
  or webhook responses.
- Limits: maximum request body 1 MiB (matching
  `OSTAG_MONITOR_SERVER_MAX_BODY_BYTES`); rate-limit ingestion per instance IP,
  webhooks per source, and human APIs per authenticated principal. Start at 60
  requests/minute with a small burst and tune from redacted metrics.
- Probe: require `GET /healthz` to return HTTP 200 through both the restricted
  origin path and the EdgeOne hostname.

Save the redacted EdgeOne rule export, certificate status, firewall rule IDs, and
both probe timestamps in the live validation record. A checked-in config example
is not evidence that these external controls are active.
