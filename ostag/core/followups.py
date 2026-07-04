"""Durable scheduled continuations (follow-ups).

Continuations live in the run ledger (``continuation_scheduled`` /
``continuation_fired`` / ``continuation_cancelled`` events), so they survive
process restarts and are auditable like everything else. A sweep — run by the
ambient scheduler or `ostag followups run-once` — fires the due ones.

Follow-ups are deliberately quiet:

- each continuation fires at most once (re-follow-up is an explicit
  ``continue <run_id>``);
- a follow-up whose subject is already resolved (receipt terminal/stale, run
  finished) is cancelled instead of sent;
- every message cites why it triggered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from ostag.config import Settings
from ostag.core.receipts import is_stale, mark_stale, render_receipt_text
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import ActionReceipt, SchemaError, utc_now_iso

Sender = Callable[[str, str], Awaitable[None]]

PENDING_RECEIPT_STATES = ("proposed", "needs_approval", "ready_to_apply")


class FollowUpService:
    def __init__(self, config: Settings, ledger: RunLedger) -> None:
        self.config = config
        self.ledger = ledger

    def _due_at(self, delay_seconds: int | None) -> str:
        delay = self.config.followup_after_seconds if delay_seconds is None else delay_seconds
        return (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(timespec="seconds")

    def schedule_receipt_follow_up(
        self,
        receipt: ActionReceipt,
        *,
        chat_id: str,
        platform: str,
        delay_seconds: int | None = None,
    ) -> str:
        due_at = self._due_at(delay_seconds)
        return self.ledger.schedule_continuation(
            receipt.run_id,
            due_at=due_at,
            kind="receipt_reminder",
            reason=f"receipt {receipt.action_id} pending approval",
            chat_id=chat_id,
            platform=platform,
            action_id=receipt.action_id,
        )

    def schedule_run_follow_up(
        self,
        run_id: str,
        *,
        chat_id: str = "",
        platform: str = "",
        delay_seconds: int | None = None,
    ) -> str:
        due_at = self._due_at(delay_seconds)
        return self.ledger.schedule_continuation(
            run_id,
            due_at=due_at,
            kind="run_follow_up",
            reason="requested via continue",
            chat_id=chat_id,
            platform=platform,
        )

    def _receipt_reminder(self, entry: dict[str, Any]) -> tuple[str, str]:
        """Return (outcome, message). outcome: fire | cancel."""
        action_id = entry.get("action_id", "")
        try:
            receipt = self.ledger.get_receipt(action_id)
        except SchemaError:
            return "cancel", f"receipt {action_id} not found"
        if receipt.state not in PENDING_RECEIPT_STATES:
            return "cancel", f"receipt already {receipt.state}"
        if is_stale(receipt, ttl_seconds=self.config.receipt_ttl_seconds):
            stale = mark_stale(receipt)
            self.ledger.record_receipt_state(
                stale, previous_state=receipt.state, detail="ttl_expired_at_followup"
            )
            return "cancel", "receipt went stale"
        message = (
            f"跟进提醒（触发原因：{entry.get('reason', '审批待处理')}，"
            f"计划于 {entry.get('due_at')} 跟进）：\n"
            + render_receipt_text(receipt)
            + f"\n如需继续跟进，回复：continue {receipt.run_id}"
        )
        return "fire", message

    def _run_reminder(self, entry: dict[str, Any]) -> tuple[str, str]:
        run_id = entry.get("run_id", "")
        try:
            snapshot = self.ledger.run_status(run_id)
        except SchemaError:
            return "cancel", f"run {run_id} not found"
        if snapshot.get("status") in ("succeeded", "failed", "cancelled"):
            return "cancel", f"run already {snapshot.get('status')}"
        message = (
            f"跟进提醒（触发原因：{entry.get('reason', 'continue')}）："
            f"运行 {run_id} 当前状态 {snapshot.get('status')}，"
            f"事件数 {snapshot.get('event_count')}。"
            f"查询：status {run_id}"
        )
        return "fire", message

    async def run_once(self, send: Sender | None = None) -> list[dict[str, Any]]:
        """Fire every due continuation once. Returns outcome records."""
        outcomes: list[dict[str, Any]] = []
        for entry in self.ledger.due_continuations(utc_now_iso()):
            continuation_id = entry.get("continuation_id", "")
            run_id = entry.get("run_id", "")
            kind = entry.get("kind", "")
            if kind == "receipt_reminder":
                outcome, message = self._receipt_reminder(entry)
            elif kind == "run_follow_up":
                outcome, message = self._run_reminder(entry)
            else:
                outcome, message = "cancel", f"unknown continuation kind {kind!r}"
            delivered = "cancelled"
            if outcome == "fire":
                chat_id = entry.get("chat_id", "")
                if send is not None and chat_id:
                    await send(chat_id, message)
                    delivered = "sent"
                else:
                    delivered = "rendered"
            self.ledger.record_continuation_done(
                continuation_id, run_id, fired=outcome == "fire", result=f"{delivered}: {message}"
            )
            outcomes.append(
                {
                    "continuation_id": continuation_id,
                    "run_id": run_id,
                    "kind": kind,
                    "outcome": delivered,
                    "message": message,
                }
            )
        return outcomes
