"""Approval service: receipt verbs from chat and CLI, end to end.

Wires together command parsing, actor permissions, the receipt state machine,
apply executors, and the run ledger. Every state change, approval decision,
and permission denial leaves ledger evidence.

Apply is disabled by default (``OSTAG_APPLY_ENABLED=false``) and additionally
requires an executor registered for the receipt's ``target_system`` — safe by
default, twice.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ostag.config import Settings
from ostag.core.followups import FollowUpService
from ostag.core.permissions import ensure_verb_allowed, resolve_permission
from ostag.core.receipts import (
    ReceiptCommand,
    is_stale,
    mark_applied,
    mark_stale,
    render_receipt_text,
    transition_receipt,
)
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import (
    ActionReceipt,
    ApprovalDecision,
    SchemaError,
    new_id,
)

PENDING_STATES = ("proposed", "needs_approval", "ready_to_apply")


@runtime_checkable
class ApplyExecutor(Protocol):
    async def apply(self, receipt: ActionReceipt) -> str:
        """Execute the mutation described by the receipt; return evidence text."""


class ApprovalService:
    def __init__(
        self,
        config: Settings,
        ledger: RunLedger,
        executors: dict[str, ApplyExecutor] | None = None,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.executors = executors or {}

    def propose(
        self, receipt: ActionReceipt, *, chat_id: str = "", platform: str = ""
    ) -> ActionReceipt:
        """Record a new proposed receipt in the ledger."""
        if receipt.state not in ("proposed", "needs_approval"):
            raise SchemaError(
                code="invalid_proposal_state",
                message=f"New receipts must start in proposed/needs_approval, got {receipt.state!r}",
                recovery_hint="Create the receipt with state 'proposed' or 'needs_approval'.",
            )
        self.ledger.record_receipt(receipt, chat_id=chat_id, platform=platform)
        return receipt

    def _resolve_receipt(self, reference: str, chat_id: str | None) -> ActionReceipt:
        if reference.isdigit():
            pending = self.ledger.list_receipts(chat_id=chat_id, states=PENDING_STATES)
            index = int(reference)
            if index < 1 or index > len(pending):
                raise SchemaError(
                    code="index_out_of_range",
                    message=f"No pending receipt at index {index} (pending: {len(pending)})",
                    recovery_hint="List pending receipts and use a valid index or the action_id.",
                )
            return pending[index - 1]
        if reference.startswith("action_"):
            return self.ledger.get_receipt(reference)
        raise SchemaError(
            code="invalid_reference",
            message=f"{reference!r} does not identify an action receipt",
            recovery_hint="Use a pending index (e.g. 1) or an action_... id.",
        )

    def _check_stale(self, receipt: ActionReceipt, actor_id: str) -> None:
        if receipt.state in PENDING_STATES and is_stale(
            receipt, ttl_seconds=self.config.receipt_ttl_seconds
        ):
            stale = mark_stale(receipt)
            self.ledger.record_receipt_state(
                stale, previous_state=receipt.state, actor_id=actor_id, detail="ttl_expired"
            )
            raise SchemaError(
                code="stale_receipt",
                message=f"Action {receipt.action_id} expired after "
                f"{self.config.receipt_ttl_seconds}s and is now stale",
                recovery_hint="Ask OS Tag to re-propose the action to get a fresh receipt.",
            )

    async def execute(
        self,
        command: ReceiptCommand,
        *,
        actor_id: str,
        chat_id: str | None = None,
        platform: str = "",
    ) -> dict[str, Any]:
        """Execute a receipt verb. Raises SchemaError on any violation."""
        level = resolve_permission(self.config, actor_id)
        try:
            ensure_verb_allowed(actor_id, level, command.verb)
        except SchemaError as exc:
            if exc.code == "permission_denied":
                run_id = (
                    command.reference if command.reference.startswith("run_") else "run_unknown"
                )
                self.ledger.record_security_event(
                    run_id, f"permission_denied: {actor_id} tried {command.verb}"
                )
            raise

        if command.verb == "status":
            if command.reference.startswith("run_"):
                return {"run": self.ledger.run_status(command.reference)}
            receipt = self._resolve_receipt(command.reference, chat_id)
            return {"action": receipt.to_dict()}

        if command.verb == "continue":
            run_id = command.reference
            if not run_id.startswith("run_"):
                receipt = self._resolve_receipt(command.reference, chat_id)
                run_id = receipt.run_id
            snapshot = self.ledger.run_status(run_id)
            followups = FollowUpService(self.config, self.ledger)
            continuation_id = followups.schedule_run_follow_up(
                run_id, chat_id=chat_id or "", platform=platform
            )
            due = self.ledger.list_continuations(pending_only=True)
            due_at = next(
                (
                    entry.get("due_at", "")
                    for entry in due
                    if entry.get("continuation_id") == continuation_id
                ),
                "",
            )
            return {
                "run": snapshot,
                "continuation_id": continuation_id,
                "note": f"follow-up scheduled at {due_at}",
            }

        receipt = self._resolve_receipt(command.reference, chat_id)
        self._check_stale(receipt, actor_id)

        if command.verb in ("approve", "reject"):
            updated = transition_receipt(receipt, command.verb)
            self.ledger.record_receipt_state(
                updated, previous_state=receipt.state, actor_id=actor_id
            )
            self.ledger.record_approval(
                ApprovalDecision(
                    decision_id=new_id("dec"),
                    action_id=receipt.action_id,
                    run_id=receipt.run_id,
                    actor_id=actor_id,
                    decision=command.verb,
                )
            )
            return {"action": updated.to_dict()}

        if command.verb == "apply":
            if not self.config.apply_enabled:
                raise SchemaError(
                    code="apply_disabled",
                    message="Apply is disabled by policy (OSTAG_APPLY_ENABLED=false)",
                    recovery_hint=(
                        "An admin can enable live applies with OSTAG_APPLY_ENABLED=true; "
                        "the approval itself has been kept."
                    ),
                )
            applying = transition_receipt(receipt, "apply")
            self.ledger.record_receipt_state(
                applying, previous_state=receipt.state, actor_id=actor_id
            )
            executor = self.executors.get(receipt.target_system)
            if executor is None:
                failed = applying.with_state("failed")
                self.ledger.record_receipt_state(
                    failed,
                    previous_state=applying.state,
                    actor_id=actor_id,
                    detail=f"no apply executor for {receipt.target_system}",
                )
                raise SchemaError(
                    code="no_apply_executor",
                    message=f"No apply executor registered for {receipt.target_system!r}",
                    recovery_hint=(
                        "Register an executor for this target system or apply the "
                        "change manually and record it."
                    ),
                )
            try:
                evidence = await executor.apply(applying)
            except Exception as exc:  # noqa: BLE001
                failed = applying.with_state("failed")
                self.ledger.record_receipt_state(
                    failed,
                    previous_state=applying.state,
                    actor_id=actor_id,
                    detail=f"{type(exc).__name__}: {exc}",
                )
                raise SchemaError(
                    code="apply_failed",
                    message=f"Apply failed: {type(exc).__name__}: {exc}",
                    recovery_hint=(
                        f"Inspect the ledger with: ostag ledger export --run {receipt.run_id}"
                    ),
                ) from exc
            applied = mark_applied(applying)
            self.ledger.record_receipt_state(
                applied, previous_state=applying.state, actor_id=actor_id, detail=evidence[:500]
            )
            return {"action": applied.to_dict(), "evidence": evidence}

        raise AssertionError(command.verb)

    async def handle_text_command(
        self,
        command: ReceiptCommand,
        *,
        actor_id: str,
        chat_id: str,
        platform: str = "",
    ) -> str:
        """Chat-facing wrapper: always returns a reply string."""
        try:
            result = await self.execute(
                command, actor_id=actor_id, chat_id=chat_id, platform=platform
            )
        except SchemaError as exc:
            return f"OS Tag 无法执行 {command.verb}：{exc}。{exc.recovery_hint}"
        if "run" in result:
            run = result["run"]
            note = result.get("note", "")
            reply = (
                f"运行 {run.get('run_id')} 状态：{run.get('status')}，"
                f"事件数：{run.get('event_count')}。"
            )
            return f"{reply}{note and '（' + note + '）'}"
        receipt = ActionReceipt.from_dict(result["action"])
        verb_labels = {"approve": "已批准", "reject": "已拒绝", "apply": "已执行"}
        prefix = verb_labels.get(command.verb, command.verb)
        return f"{prefix} {receipt.action_id}。\n{render_receipt_text(receipt)}"
