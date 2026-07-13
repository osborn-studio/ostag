# Cross-Platform Daily Task Boards Design

**Date:** 2026-07-13  
**Status:** Approved for implementation  
**Platforms:** Feishu group and DingTalk group

## Goal

Replace the existing long-text 09:00 and 18:00 task reminders in the configured
Feishu and DingTalk groups with concise Operations Minimalist boards. Keep each
platform's existing task table as its source of truth while making both boards
use the same statistics, ordering, language, permissions, and delivery rules.

## Product decisions

- Preserve both Beijing-time reminders: 09:00 and 18:00.
- Feishu continues to read its existing Feishu Base table.
- DingTalk continues to read its existing DingTalk AI Table.
- Use native structured messages, not generated images or a new Web dashboard.
- Feishu renders an interactive native card. DingTalk renders an ActionCard or
  structured Markdown supported by the configured robot.
- Group cards are read-only. They link to the task table but never expose pause,
  retry, cancel, approval, or administrator controls.

## GUI and information hierarchy

Both platforms use a flat, high-contrast Operations Minimalist visual language:
white surface, blue header, restrained semantic colors, no gradients, no heavy
shadows, and no decorative card grid. Status is always expressed with text and
number in addition to color.

### 09:00 今日作战看板

1. Header: `OS Tag · 今日作战看板`, date, time, and data freshness.
2. KPI row: 今日重点, 已逾期, 高优先, 待认领.
3. `需要优先处理`: the highest-risk overdue/high-priority item, including
   status, overdue days, and responsible person.
4. `今日推进`: up to six due or undated open tasks ordered by urgency.
5. Overflow note: `另有 N 项重点任务` when the list is truncated.
6. Primary link: `打开任务表`.

### 18:00 今日复盘看板

1. Header: `OS Tag · 今日复盘看板`, date, time, and data freshness.
2. KPI row: 今日完成, 完成率, 仍未完成, 需升级.
3. `今日完成`: up to six completed tasks with responsible people.
4. `仍未完成`: overdue, unowned, and high-priority open work.
5. `明日关注`: concise count and recommendation for tomorrow.
6. Primary link: `更新任务进展`.

### Content bounds

- Show at most six items in each detailed list.
- Truncate individual Chinese titles safely without breaking Unicode.
- Escape all table-provided Markdown and card markup.
- Remain below Feishu's existing 24 KB safety limit and DingTalk robot limits.
- Empty data produces a healthy, explicit empty state rather than a blank card.

## Architecture

The Singapore central service owns one `DailyDigestSupervisor` alongside the
existing monitor scheduler. It starts two isolated digest profiles:

```text
Feishu profile  -> Lark/Base connector -> DailyDigestView -> Feishu renderer
DingTalk profile -> DWS/AI Table connector -> DailyDigestView -> DingTalk renderer
                                                        -> durable outbox
```

`DailyDigestView` is platform-neutral and contains source freshness, digest
kind, KPI values, priority items, completed/open lists, overflow counts, and
the task-table URL. A shared builder owns filtering, statistics, and urgency
ordering. Platform renderers only map the view to native message components.

The supervisor uses Asia/Shanghai for due-window calculation. A durable window
key contains date, morning/evening kind, platform, and group ID. Successful
delivery records the key exactly once so a restart cannot duplicate a reminder.
Each profile is isolated: a connector or transport failure on one platform does
not suppress the other platform's delivery.

The central worker uses the already configured group IDs. Credentials remain in
the production environment and are never committed. Feishu continues using the
existing central app. DingTalk proactive delivery uses the existing signed robot
webhook and does not require a persistent local DingTalk Stream connection.

## Data rules

The normalized snapshot retains title, owner display names and IDs, status,
priority, due date, staleness, and completion date when available.

Morning priority order:

1. overdue and P0/high priority;
2. overdue;
3. P0/high priority due today;
4. due today;
5. no due date;
6. remaining open work.

Evening priority order:

1. completed today;
2. overdue and still open;
3. high priority and still open;
4. unowned open work;
5. remaining open work.

Completion rate is `completed today / (completed today + open tasks due today)`.
When the denominator is zero, display `100%` only if there is completed work;
otherwise display `—`.

## Delivery and failure handling

- A successful native send is the only event that closes a delivery window.
- Connector or delivery failures retry after 1, 2, 4, and 8 minutes.
- Native card validation/send failure falls back once to structured Markdown.
- A failed fallback remains retryable and produces a redacted audit event.
- Retries reuse a stable delivery ID for platform idempotency.
- Stale source data is labeled with its timestamp; unavailable data is never
  presented as current.
- One platform's retry queue cannot block the other platform.

## Deployment

Package both source connectors and renderers in the production image. Configure
two digest profiles through secret-backed environment variables. Before cutover,
back up the production SQLite database and Compose/environment configuration.
Deploy one worker, validate health and ownership, send controlled previews to
both configured groups, then enable the 09:00 and 18:00 windows. Keep the local
DingTalk Stream launchd service disabled because it is neither required for
proactive digests nor reliable enough to own production scheduling.

## Verification

Automated tests cover:

- morning/evening statistics and ordering;
- empty, long-Chinese, unsafe-markup, overflow, and stale-data states;
- Feishu native structure and 24 KB bound;
- DingTalk ActionCard/Markdown structure and platform bounds;
- mention conversion for both identity formats;
- Beijing-time windows, catch-up, duplicate suppression, and stable IDs;
- isolated connector/transport failures, retry backoff, and Markdown fallback;
- group cards containing no governed control callbacks.

Production acceptance requires:

- the full release suite and secret-hygiene gate pass;
- one scheduler owner and healthy central service;
- a real Feishu preview delivered as `interactive`;
- a real DingTalk preview delivered as ActionCard or structured Markdown;
- both previews match the approved morning/evening hierarchy;
- the durable outbox is empty after delivery;
- rollback artifacts pass SQLite integrity validation.
