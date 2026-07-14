# OStag Daily Requirement Board Design

## Goal

OStag sends one read-only requirement dashboard card every day at 12:00
Asia/Shanghai time, including weekends, to the Feishu group `Dadao 投研交易
FDE 群`.

The dashboard reads the existing Base without changing it:

- Base token: `Q3FkbLGQ6axmTuspP0fc8fLInQm`
- Table ID: `tblcj5dhYa9vQLd0`
- Base title: `🛠️ 需求收集与管理 底表`
- Target chat ID: `oc_ef8aba337000ae1c69aae11dfdfd8ef0`

The scheduled flow never updates records. OStag only writes to the Base when an
authorized member sends an explicit maintenance instruction through the existing
governed command path.

## Product Decisions

- Schedule: every calendar day at 12:00, including Saturday and Sunday.
- Time zone: `Asia/Shanghai`, independent of the server's local time zone.
- Delivery: one card per day per configured chat.
- Notification strength: show owners for normal items; mention an owner only
  when an item is both P0 and overdue.
- Information density: use the approved “经营驾驶舱” layout.
- Scope: Feishu card first; other adapters retain a compact text fallback.
- Safety: the scheduled workflow is read-only and does not create action
  receipts or Base writes.

## Card Design

### Header

- Title: `📋 OStag 每日需求看板`
- Subtitle: group name plus the send date and `12:00（北京时间）`
- Blue gradient visual treatment consistent with the approved mockup.

### Summary Metrics

The first row shows four metrics:

1. `未完成`: records whose status is not `已完成`.
2. `P0`: unfinished records whose priority is `高 - P0`.
3. `已逾期`: unfinished records whose due date is earlier than today.
4. `无负责人`: unfinished records without a requirement owner.

### Flow Summary

Show the unfinished status distribution in this order:

1. `待评估`
2. `方案设计中`
3. `开发交付中`
4. `验收中`

The card renders both a compact segmented bar and numeric labels. Unknown or
blank statuses are included in the unfinished total and surfaced as a data
quality risk, but do not create a new visible segment in the first release.

### Priority Items

Show no more than three items. Select and sort them deterministically:

1. P0 overdue items, oldest due date first.
2. P0 items without an owner, oldest requirement first.
3. Items due today, P0 before P1 before P2.
4. Other overdue items, oldest due date first.

If several P0 unowned items would consume the card, group them into one summary
line and show up to four shortened titles. Only P0 overdue items mention their
owners. Missing owners never trigger an `@all` mention.

### Recommendation And Actions

The card ends with one deterministic OStag recommendation derived from the
highest-ranked risks. It does not use an LLM at send time.

Actions:

- `打开需求表`: opens the approved Base URL.
- `查看全部风险`: opens the Base table view in the first release. A later
  version may route this action to an interactive OStag command, but that is not
  required for launch.

Footer text states that the sweep is read-only, the daily schedule, and the
daily delivery count.

## Architecture

### Scheduler

Extend the existing `AmbientScheduler` rather than creating another daemon.
The scheduler keeps its bounded polling loop and asks a daily-board schedule
policy whether the current Asia/Shanghai time is inside the delivery window.

Configuration includes:

- enable flag;
- dashboard hour, default `12`;
- IANA time zone, default `Asia/Shanghai`;
- target chats;
- Base token and table ID;
- record read limit;
- dashboard URL.

The existing morning/evening digest behavior remains backward compatible.

### Base Read Path

Use the existing requirement connector boundary and a dedicated dashboard
projection containing:

- `需求描述`
- `需求负责人`
- `需求状态`
- `优先级`
- `预计/实际完成时间`
- `需求提出日期`
- `需求持续时长`

Production uses the OStag application identity with Base record-read scope and
document access inherited from the authorized FDE group. No scheduled write
scope is introduced.

### Domain Model And Formatter

Convert Base rows into `RequirementSnapshot` values, retaining owner open IDs
for Feishu mentions. Add a pure dashboard analysis function that produces:

- summary metrics;
- status distribution;
- ranked risk items;
- one action recommendation.

A separate pure renderer converts that model into a Feishu interactive-card
payload. Text rendering remains available as a fallback.

### Adapter Boundary

Add a structured-card delivery method to the adapter boundary. Feishu sends an
interactive message card through the existing tenant-token transport. Other
platform adapters may render the dashboard as text until native cards are
implemented.

## Idempotency And State

The channel brain records a key scoped by platform, chat, feature, and local
date. A successful card send writes the key. Scheduler polling, process restarts,
or overlapping delivery windows therefore do not create duplicate cards.

The success marker is written only after Feishu confirms delivery. A failed
send remains eligible for retry during the configured window. Once the window
closes, the scheduler records the failure in the audit log and waits for the
next day; it does not send a late card without a manual run.

## Error Handling

- Empty table: send a healthy-state card with zero counts.
- Missing optional fields: continue with available data and show a concise data
  quality note.
- Missing required Base or chat configuration: skip safely and audit the reason.
- Base timeout or API error: audit the error and retry within the delivery
  window.
- Feishu send error: audit the response and retry without writing the success
  marker.
- Malformed dates or cells: treat them as missing data; never abort the whole
  card for one record.

## Security And Permissions

- Apply only the OStag app's Base record-read capability needed for the
  scheduled path.
- Keep the existing explicit-command write path and its authorization checks
  unchanged.
- Do not include full requirement descriptions in the card; titles are
  shortened for mobile display.
- Do not log credentials, tenant tokens, or raw Base payloads.
- Mention only the owner IDs already present in the Base record.

## Testing

### Unit Tests

- summary metric calculation;
- status distribution;
- overdue and due-today boundary behavior;
- priority ordering and grouped unowned P0 items;
- owner mention only for overdue P0;
- deterministic recommendation text;
- malformed and missing fields;
- Feishu card payload structure and Base links.

### Scheduler Tests

- 12:00 Asia/Shanghai triggers delivery;
- 11:59 does not trigger;
- weekends trigger;
- a successful send is idempotent for the local date;
- a failed send retries inside the window;
- restart with persisted success state does not resend;
- legacy ambient digest behavior remains unchanged.

### Integration And Live Validation

- exercise the connector with representative Base records;
- run the existing release suite;
- run a manual production `ambient run-once --send` equivalent for the daily
  board;
- verify one real card in the FDE group, including layout, numbers, links, and
  owner mention behavior;
- verify the service, logs, production time zone configuration, and next daily
  schedule.

## Deployment And Rollback

1. Apply and publish the required OStag app read scope.
2. Deploy the tested build to the current OStag production host.
3. Configure the original Base, table, target FDE chat, Asia/Shanghai time zone,
   and the daily dashboard enable flag.
4. Run one manual live card send and compare it with the Base.
5. Leave the daily schedule enabled after live validation passes.

Rollback disables the daily dashboard flag and restarts OStag. Existing message
handling, explicit Base maintenance commands, and the original Base remain
available and unchanged.

## Acceptance Criteria

- One OStag dashboard card appears in the FDE group every calendar day at 12:00
  Asia/Shanghai time.
- The card uses the approved operating-dashboard layout and the original Base.
- Counts and ranked risks match the Base at send time.
- Only overdue P0 owners are mentioned.
- The scheduled run performs no Base mutation.
- A successful daily send is not duplicated.
- Failures are visible in audit logs and do not stop the bot process.
- Existing OStag commands and ambient behavior keep passing their tests.
