# Requirement Weekly Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only weekly requirement review chat command for Feishu and DingTalk OS Tag groups.

**Architecture:** Reuse AutoBase's default table resolution and Ambient's requirement snapshot parsing. Add a pure formatter in `ostag.core.ambient`, then route matching chat requests through `AutoBaseWorkflow` before mutation command parsing.

**Tech Stack:** Python 3.11, pytest, existing OS Tag AutoBase and Ambient modules.

---

### Task 1: Pure Weekly Review Formatter

**Files:**
- Modify: `ostag/core/ambient.py`
- Test: `tests/test_ambient.py`

- [ ] Add a failing test named `test_format_weekly_requirement_review_summarizes_risks`.
- [ ] Implement `format_weekly_requirement_review(requirements, today)` with deterministic counts and task lines.
- [ ] Run `uv run pytest tests/test_ambient.py::test_format_weekly_requirement_review_summarizes_risks -q`.

### Task 2: AutoBase Chat Route

**Files:**
- Modify: `ostag/workflows/auto_base.py`
- Test: `tests/test_auto_base_workflow.py`

- [ ] Add a failing workflow test for the approved Chinese prompt.
- [ ] Add `is_requirement_weekly_review_request(text)`.
- [ ] Add `_handle_requirement_weekly_review(event)` that reads the default table, lists records, and formats the review.
- [ ] Route the request before requirement mutation parsing.
- [ ] Run the focused workflow test.

### Task 3: Verification And Deploy

**Files:**
- Verify: `tests/test_ambient.py`, `tests/test_auto_base_workflow.py`
- Sync: `/Users/gilbertchen/Documents/CODE/ostag-deep-fork`

- [ ] Run focused tests for ambient and AutoBase workflow.
- [ ] Commit implementation in `/Users/gilbertchen/Documents/CODE/ostag`.
- [ ] Copy or cherry-pick the implementation into `/Users/gilbertchen/Documents/CODE/ostag-deep-fork`.
- [ ] Run doctor checks and restart `ai.ostag.feishu` and `ai.ostag.dingtalk`.
