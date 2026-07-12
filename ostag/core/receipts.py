"""Action receipt protocol: rendering, command parsing, and state transitions.

Receipts are the platform-independent contract for proposed mutations. Rich
platforms may render them as cards or buttons; every platform gets the plain
text command fallback rendered here. No receipt may reach ``applied`` without
passing through an approval-bearing state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from ostag.core.schemas import (
    ActionReceipt,
    SchemaError,
    prune,
)

COMMAND_VERBS = ("approve", "reject", "apply", "continue", "status")

# verb -> (allowed source states, resulting state)
_TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "approve": (("proposed", "needs_approval"), "ready_to_apply"),
    "reject": (("proposed", "needs_approval", "ready_to_apply"), "rejected"),
    "apply": (("ready_to_apply",), "applying"),
}

_COMMAND_PATTERN = re.compile(
    r"^\s*(?:@\S+\s+)?(approve|reject|apply|continue|status)\s+"
    r"(\d+|action_[A-Za-z0-9]+|run_[A-Za-z0-9]+)\s*$",
    re.IGNORECASE,
)

_RISK_LABELS = {"low": "低", "medium": "中", "high": "高"}
_STATE_LABELS = {
    "proposed": "已提议",
    "needs_approval": "等待审批",
    "ready_to_apply": "已批准待执行",
    "applying": "执行中",
    "applied": "已执行",
    "approved_only": "仅批准未执行",
    "rejected": "已拒绝",
    "blocked": "已阻止",
    "stale": "已过期",
    "failed": "执行失败",
}


@dataclass(frozen=True)
class ReceiptCommand:
    verb: str
    reference: str

    @property
    def is_index(self) -> bool:
        return self.reference.isdigit()


def parse_receipt_command(text: str) -> ReceiptCommand | None:
    """Parse an approval command from source-thread text.

    Accepts ``approve/reject/apply/continue/status`` followed by a numeric
    index, an ``action_*`` id, or a ``run_*`` id. Returns ``None`` for
    anything else so normal chat is never mistaken for a command.
    """
    match = _COMMAND_PATTERN.match(text)
    if not match:
        return None
    return ReceiptCommand(verb=match.group(1).lower(), reference=match.group(2))


def transition_receipt(receipt: ActionReceipt, verb: str) -> ActionReceipt:
    """Apply an approval verb to a receipt, enforcing the state machine."""
    if verb not in _TRANSITIONS:
        raise SchemaError(
            code="unsupported_verb",
            message=f"Verb {verb!r} does not change receipt state",
            recovery_hint=f"Use one of: {', '.join(_TRANSITIONS)}.",
        )
    allowed, target = _TRANSITIONS[verb]
    if receipt.state == "stale":
        raise SchemaError(
            code="stale_receipt",
            message=f"Action {receipt.action_id} is stale and can no longer be {verb}d",
            recovery_hint="Ask OS Tag to re-propose the action to get a fresh receipt.",
        )
    if receipt.state not in allowed:
        raise SchemaError(
            code="invalid_transition",
            message=(f"Cannot {verb} action {receipt.action_id} in state {receipt.state!r}"),
            recovery_hint=(
                f"'{verb}' requires state in {list(allowed)}; "
                f"check current state with: status {receipt.run_id}"
            ),
        )
    return receipt.with_state(target)


def mark_applied(receipt: ActionReceipt) -> ActionReceipt:
    if receipt.state != "applying":
        raise SchemaError(
            code="invalid_transition",
            message=f"Cannot mark {receipt.action_id} applied from state {receipt.state!r}",
            recovery_hint="Only receipts in 'applying' may become 'applied'.",
        )
    return receipt.with_state("applied")


def mark_stale(receipt: ActionReceipt) -> ActionReceipt:
    if receipt.state in ("applied", "rejected", "failed"):
        return receipt
    return receipt.with_state("stale")


def is_stale(receipt: ActionReceipt, *, ttl_seconds: int, now: datetime | None = None) -> bool:
    """A non-terminal receipt older than ttl_seconds is stale."""
    if receipt.state in ("applied", "approved_only", "rejected", "stale", "failed"):
        return receipt.state == "stale"
    moment = now or datetime.now(UTC)
    updated = datetime.fromisoformat(receipt.updated_at)
    return (moment - updated).total_seconds() > ttl_seconds


def ensure_fresh_confirmation(
    receipt: ActionReceipt, *, now: datetime | None = None, ttl_seconds: int = 300
) -> None:
    """Fail closed when a confirmation window has expired."""
    if is_stale(receipt, ttl_seconds=ttl_seconds, now=now):
        raise SchemaError(
            code="stale_receipt",
            message=f"Action {receipt.action_id} confirmation has expired",
            recovery_hint="Propose the control again to obtain a fresh five-minute confirmation.",
        )


def render_receipt_text(receipt: ActionReceipt, *, index: int | None = None) -> str:
    """Plain text renderer — the universal fallback for every platform."""
    prefix = f"[{index}] " if index is not None else ""
    lines = [
        f"{prefix}{receipt.title}",
        f"目标：{receipt.target_system} / {receipt.target_object}",
        f"变更：{receipt.mutation_type}｜风险：{_RISK_LABELS.get(receipt.risk, receipt.risk)}"
        f"｜状态：{_STATE_LABELS.get(receipt.state, receipt.state)}",
        f"所需权限：{', '.join(receipt.required_permissions)}",
    ]
    if receipt.summary:
        lines.append(f"说明：{receipt.summary}")
    if receipt.state in ("proposed", "needs_approval"):
        ref = str(index) if index is not None else receipt.action_id
        lines.append(f"回复：approve {ref} / reject {ref}")
    elif receipt.state == "ready_to_apply":
        ref = str(index) if index is not None else receipt.action_id
        lines.append(f"回复：apply {ref} 执行，或 reject {ref} 撤销")
    lines.append(f"查询：status {receipt.run_id}")
    return "\n".join(lines)


def render_receipt_json(receipt: ActionReceipt) -> dict[str, object]:
    """Compact JSON renderer for agents — null/empty fields pruned."""
    return prune(receipt.to_dict())
