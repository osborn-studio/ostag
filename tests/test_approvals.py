from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.approvals import ApprovalService
from ostag.core.event import NormalizedEvent
from ostag.core.receipts import ReceiptCommand
from ostag.core.run_ledger import RunLedger
from ostag.core.runner import OSTagRunner
from ostag.core.schemas import ActionReceipt, SchemaError, new_id


def _config(tmp_path: Path, **overrides: object):
    return load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        actor_permissions=(
            "boss:admin", "dev:write", "member:propose", "viewer:read",
            "feishu:boss:admin", "feishu:dev:write", "feishu:member:propose",
            "feishu:viewer:read",
        ),
        **overrides,
    )


def _receipt(run_id: str = "run_test1", state: str = "needs_approval") -> ActionReceipt:
    return ActionReceipt(
        action_id=new_id("action"),
        run_id=run_id,
        title="更新需求 REQ-1 状态为完成",
        target_system="dingtalk_ai_table",
        target_object="REQ-1",
        mutation_type="update",
        risk="low",
        state=state,
    )


class MockExecutor:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.applied: list[str] = []

    async def apply(self, receipt: ActionReceipt) -> str:
        if self.fail:
            raise RuntimeError("table write failed")
        self.applied.append(receipt.action_id)
        return f"updated {receipt.target_object}"


def _service(tmp_path: Path, executors=None, **overrides):
    config = _config(tmp_path, **overrides)
    ledger = RunLedger(config.run_ledger_path)
    return ApprovalService(config, ledger, executors), ledger


def test_propose_records_and_lists_pending(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path)
    receipt = _receipt()

    service.propose(receipt, chat_id="chat1", platform="dingtalk")

    stored = ledger.get_receipt(receipt.action_id)
    assert stored.state == "needs_approval"
    assert ledger.list_receipts(chat_id="chat1")[0].action_id == receipt.action_id
    assert ledger.list_receipts(chat_id="other") == []


def test_propose_rejects_pre_approved_state(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)

    with pytest.raises(SchemaError) as excinfo:
        service.propose(_receipt(state="ready_to_apply"), chat_id="chat1")
    assert excinfo.value.code == "invalid_proposal_state"


async def test_approve_by_index_records_decision(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path)
    receipt = _receipt()
    service.propose(receipt, chat_id="chat1")

    result = await service.execute(
        ReceiptCommand(verb="approve", reference="1"), actor_id="dev", chat_id="chat1"
    )

    assert result["action"]["state"] == "ready_to_apply"
    events = ledger.export_run(receipt.run_id)
    types = [event["event_type"] for event in events]
    assert "action_state_changed" in types
    assert "approval_recorded" in types
    decision = next(
        event["payload"]["decision"]
        for event in events
        if event["event_type"] == "approval_recorded"
    )
    assert decision["actor_id"] == "dev"
    assert decision["decision"] == "approve"


async def test_reject_by_action_id(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path)
    receipt = _receipt()
    service.propose(receipt, chat_id="chat1")

    result = await service.execute(
        ReceiptCommand(verb="reject", reference=receipt.action_id),
        actor_id="member",
        chat_id="chat1",
    )

    assert result["action"]["state"] == "rejected"
    assert ledger.get_receipt(receipt.action_id).state == "rejected"


async def test_permission_denied_is_ledgered_as_security_event(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path)
    receipt = _receipt()
    service.propose(receipt, chat_id="chat1")

    with pytest.raises(SchemaError) as excinfo:
        await service.execute(
            ReceiptCommand(verb="approve", reference="1"), actor_id="viewer", chat_id="chat1"
        )

    assert excinfo.value.code == "permission_denied"
    security = [record for record in ledger._read_all() if record["event_type"] == "security_event"]
    assert security
    assert "viewer" in security[0]["payload"]["reason"]


async def test_apply_disabled_by_default(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path, executors={"dingtalk_ai_table": MockExecutor()})
    receipt = _receipt()
    service.propose(receipt, chat_id="chat1")
    await service.execute(
        ReceiptCommand(verb="approve", reference="1"), actor_id="dev", chat_id="chat1"
    )

    with pytest.raises(SchemaError) as excinfo:
        await service.execute(
            ReceiptCommand(verb="apply", reference=receipt.action_id),
            actor_id="dev",
            chat_id="chat1",
        )

    assert excinfo.value.code == "apply_disabled"
    # approval kept, nothing applied
    assert ledger.get_receipt(receipt.action_id).state == "ready_to_apply"


async def test_apply_with_executor_produces_applied_and_evidence(tmp_path: Path) -> None:
    executor = MockExecutor()
    service, ledger = _service(
        tmp_path, executors={"dingtalk_ai_table": executor}, apply_enabled=True
    )
    receipt = _receipt()
    service.propose(receipt, chat_id="chat1")
    await service.execute(
        ReceiptCommand(verb="approve", reference="1"), actor_id="dev", chat_id="chat1"
    )

    result = await service.execute(
        ReceiptCommand(verb="apply", reference=receipt.action_id),
        actor_id="dev",
        chat_id="chat1",
    )

    assert result["action"]["state"] == "applied"
    assert result["evidence"] == "updated REQ-1"
    assert executor.applied == [receipt.action_id]
    assert ledger.get_receipt(receipt.action_id).state == "applied"


async def test_apply_without_executor_fails_with_ledger_evidence(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path, apply_enabled=True)
    receipt = _receipt()
    service.propose(receipt, chat_id="chat1")
    await service.execute(
        ReceiptCommand(verb="approve", reference="1"), actor_id="dev", chat_id="chat1"
    )

    with pytest.raises(SchemaError) as excinfo:
        await service.execute(
            ReceiptCommand(verb="apply", reference=receipt.action_id),
            actor_id="dev",
            chat_id="chat1",
        )

    assert excinfo.value.code == "no_apply_executor"
    assert ledger.get_receipt(receipt.action_id).state == "failed"


async def test_apply_executor_failure_marks_failed(tmp_path: Path) -> None:
    service, ledger = _service(
        tmp_path, executors={"dingtalk_ai_table": MockExecutor(fail=True)}, apply_enabled=True
    )
    receipt = _receipt()
    service.propose(receipt, chat_id="chat1")
    await service.execute(
        ReceiptCommand(verb="approve", reference="1"), actor_id="dev", chat_id="chat1"
    )

    with pytest.raises(SchemaError) as excinfo:
        await service.execute(
            ReceiptCommand(verb="apply", reference=receipt.action_id),
            actor_id="dev",
            chat_id="chat1",
        )

    assert excinfo.value.code == "apply_failed"
    assert ledger.get_receipt(receipt.action_id).state == "failed"


async def test_stale_receipt_is_marked_and_refused(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path, receipt_ttl_seconds=1)
    old_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    receipt = ActionReceipt.from_dict(
        _receipt().to_dict() | {"created_at": old_time, "updated_at": old_time}
    )
    service.propose(receipt, chat_id="chat1")

    with pytest.raises(SchemaError) as excinfo:
        await service.execute(
            ReceiptCommand(verb="approve", reference=receipt.action_id),
            actor_id="dev",
            chat_id="chat1",
        )

    assert excinfo.value.code == "stale_receipt"
    assert ledger.get_receipt(receipt.action_id).state == "stale"
    # stale receipts no longer appear as pending index targets
    with pytest.raises(SchemaError):
        await service.execute(
            ReceiptCommand(verb="approve", reference="1"), actor_id="dev", chat_id="chat1"
        )


async def test_status_and_continue_return_run_snapshot(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path)
    run = ledger.open_run(
        NormalizedEvent(
            platform="feishu",
            event_id="evt",
            chat_id="chat1",
            message_id="m1",
            sender_id="dev",
            text="做点什么",
            mention_bot=True,
            chat_type="group",
            destination_id="chat",
        )
    )
    ledger.record_status(run.run_id, "succeeded")

    status = await service.execute(
        ReceiptCommand(verb="status", reference=run.run_id), actor_id="viewer", chat_id="chat1"
    )
    assert status["run"]["status"] == "succeeded"

    cont = await service.execute(
        ReceiptCommand(verb="continue", reference=run.run_id), actor_id="member", chat_id="chat1"
    )
    assert cont["run"]["run_id"] == run.run_id
    assert "note" in cont


async def test_index_out_of_range(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)

    with pytest.raises(SchemaError) as excinfo:
        await service.execute(
            ReceiptCommand(verb="approve", reference="3"), actor_id="dev", chat_id="chat1"
        )
    assert excinfo.value.code == "index_out_of_range"


@pytest.mark.asyncio
async def test_runner_routes_chat_commands_to_approvals(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = OSTagRunner(config)
    receipt = _receipt()
    runner.approvals.propose(receipt, chat_id="chat1", platform="feishu")

    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="chat1",
        message_id="m1",
        sender_id="dev",
        text="approve 1",
        mention_bot=True,
        chat_type="group",
        destination_id="chat",
    )
    reply = await runner.handle_event(event)

    assert "已批准" in reply
    assert runner.run_ledger.get_receipt(receipt.action_id).state == "ready_to_apply"
    audit = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert '"backend": "receipts"' in audit


@pytest.mark.asyncio
async def test_runner_chat_command_permission_denied_replies_hint(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = OSTagRunner(config)
    receipt = _receipt()
    runner.approvals.propose(receipt, chat_id="chat1", platform="feishu")

    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="chat1",
        message_id="m1",
        sender_id="viewer",
        text=f"apply {receipt.action_id}",
        mention_bot=True,
        chat_type="group",
        destination_id="chat",
    )
    reply = await runner.handle_event(event)

    assert "无法执行" in reply
    assert "OSTAG_ACTOR_PERMISSIONS" in reply
    # receipt untouched
    assert runner.run_ledger.get_receipt(receipt.action_id).state == "needs_approval"


@pytest.mark.asyncio
async def test_runner_normal_chat_still_reaches_backend(tmp_path: Path) -> None:
    config = _config(tmp_path, backend="mock")
    runner = OSTagRunner(config)

    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="chat1",
        message_id="m1",
        sender_id="dev",
        text="请总结一下今天的讨论",
        mention_bot=True,
        chat_type="group",
        destination_id="chat",
    )
    reply = await runner.handle_event(event)

    assert reply.startswith("OS Tag mock reply:")
