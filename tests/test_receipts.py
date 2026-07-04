from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ostag.core.receipts import (
    is_stale,
    mark_applied,
    mark_stale,
    parse_receipt_command,
    render_receipt_json,
    render_receipt_text,
    transition_receipt,
)
from ostag.core.schemas import ActionReceipt, SchemaError, VerificationCheck


def _receipt(state: str = "proposed") -> ActionReceipt:
    return ActionReceipt(
        action_id="action_9",
        run_id="run_9",
        title="更新需求 REQ-12 owner",
        target_system="dingtalk_ai_table",
        target_object="REQ-12",
        mutation_type="update",
        summary="把 owner 从空改为 @小王",
        risk="low",
        state=state,
    )


def test_parse_receipt_command_variants() -> None:
    assert parse_receipt_command("approve 1").verb == "approve"
    assert parse_receipt_command("approve 1").reference == "1"
    assert parse_receipt_command("approve 1").is_index
    assert parse_receipt_command("REJECT action_9").verb == "reject"
    assert parse_receipt_command("apply action_9").reference == "action_9"
    assert parse_receipt_command("continue run_9").verb == "continue"
    assert parse_receipt_command("status run_9").verb == "status"
    assert parse_receipt_command("@OS Tag approve 2") is None  # bot name with space is ambiguous
    assert parse_receipt_command("@ostag approve 2").verb == "approve"


def test_parse_receipt_command_ignores_normal_chat() -> None:
    assert parse_receipt_command("大家好，今天的需求都处理了吗") is None
    assert parse_receipt_command("approve") is None
    assert parse_receipt_command("approve the plan please") is None
    assert parse_receipt_command("") is None


def test_approve_then_apply_then_applied() -> None:
    receipt = _receipt("needs_approval")

    approved = transition_receipt(receipt, "approve")
    assert approved.state == "ready_to_apply"

    applying = transition_receipt(approved, "apply")
    assert applying.state == "applying"

    applied = mark_applied(applying)
    assert applied.state == "applied"


def test_apply_without_approval_is_rejected() -> None:
    with pytest.raises(SchemaError) as excinfo:
        transition_receipt(_receipt("proposed"), "apply")
    assert excinfo.value.code == "invalid_transition"
    assert "status run_9" in excinfo.value.recovery_hint


def test_rejected_receipt_cannot_be_applied() -> None:
    rejected = transition_receipt(_receipt("needs_approval"), "reject")
    assert rejected.state == "rejected"

    with pytest.raises(SchemaError):
        transition_receipt(rejected, "apply")
    with pytest.raises(SchemaError):
        transition_receipt(rejected, "approve")


def test_stale_receipt_blocks_all_verbs() -> None:
    stale = mark_stale(_receipt("needs_approval"))
    assert stale.state == "stale"

    for verb in ("approve", "reject", "apply"):
        with pytest.raises(SchemaError) as excinfo:
            transition_receipt(stale, verb)
        assert excinfo.value.code == "stale_receipt"


def test_mark_stale_preserves_terminal_states() -> None:
    applied = mark_applied(transition_receipt(_receipt("ready_to_apply"), "apply"))

    assert mark_stale(applied).state == "applied"
    rejected = transition_receipt(_receipt("proposed"), "reject")
    assert mark_stale(rejected).state == "rejected"


def test_is_stale_uses_ttl() -> None:
    receipt = _receipt("needs_approval")
    now = datetime.fromisoformat(receipt.updated_at)

    assert not is_stale(receipt, ttl_seconds=3600, now=now + timedelta(minutes=59))
    assert is_stale(receipt, ttl_seconds=3600, now=now + timedelta(hours=2))
    assert not is_stale(
        mark_applied(transition_receipt(_receipt("ready_to_apply"), "apply")),
        ttl_seconds=1,
        now=datetime.now(UTC) + timedelta(days=1),
    )


def test_render_receipt_text_contains_target_risk_and_commands() -> None:
    text = render_receipt_text(_receipt("needs_approval"), index=1)

    assert "[1] 更新需求 REQ-12 owner" in text
    assert "dingtalk_ai_table / REQ-12" in text
    assert "风险：低" in text
    assert "approve 1" in text
    assert "reject 1" in text
    assert "status run_9" in text


def test_render_receipt_text_without_index_uses_action_id() -> None:
    text = render_receipt_text(_receipt("ready_to_apply"))

    assert "apply action_9" in text


def test_render_receipt_json_is_pruned_and_complete() -> None:
    receipt = ActionReceipt(
        action_id="action_9",
        run_id="run_9",
        title="t",
        target_system="s",
        target_object="o",
        mutation_type="send",
        verification=(VerificationCheck(name="dry_run", status="pass", evidence="ok"),),
    )

    payload = render_receipt_json(receipt)

    assert payload["mutation_type"] == "send"
    assert payload["commands"]["approve"] == "approve action_9"
    assert payload["verification"][0]["status"] == "pass"
    assert "diff" not in payload  # empty fields pruned for agents
