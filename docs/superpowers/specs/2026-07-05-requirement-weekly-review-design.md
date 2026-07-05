# Requirement Weekly Review Design

## Goal

Let users ask OS Tag in a Feishu or DingTalk group for a read-only weekly
requirement review, for example:

```text
@OS Tag 看一下需求表里有哪些逾期或没人负责的需求，做一个本周的周报回顾
```

The bot should query the group's configured requirement table, summarize
overdue, unowned, missing-status, stale, completed, and open requirements, and
reply in the source thread. It must not write to the table, create tasks, or
create action receipts.

## Scope

- Supported platforms: Feishu, Lark, Slack, and DingTalk through the existing
  AutoBase workflow, with production focus on Feishu and DingTalk.
- Source of truth: the default base/table from channel policy first, then the
  environment defaults.
- Output: compact Chinese weekly review text with counts and representative
  records.
- Safety: read-only query only; no apply executors, no governed-write path, no
  task creation.

## Architecture

`AutoBaseWorkflow.handle()` will detect weekly-review intent before mutation
commands. The workflow will load the default table context, list fields, query
records with the ambient projection fields, convert records into
`RequirementSnapshot`, then call a formatter in `ostag.core.ambient`.

The formatter will stay pure and deterministic so it can be tested without
external services. The workflow test will use the existing fake requirement
connector pattern to prove the chat path works end-to-end.

## Acceptance

- A Chinese request containing weekly-review and issue-summary intent triggers
  a read-only table query.
- The reply includes counts for overdue, unowned, missing-status, stale, open,
  and completed-this-week requirements.
- The reply includes representative overdue/unowned records.
- Missing default table configuration returns a clear error.
- Existing requirement update and import paths keep working.
