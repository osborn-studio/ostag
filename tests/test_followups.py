from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.approvals import ApprovalService
from ostag.core.event import NormalizedEvent
from ostag.core.followups import FollowUpService
from ostag.core.receipts import ReceiptCommand
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import ActionReceipt, new_id
from ostag.main import main_async


def _config(tmp_path: Path, **overrides: object):
    return load_settings(None, audit_log=tmp_path / "audit.log", **overrides)


def _receipt(run_id: str = "run_fu1", state: str = "needs_approval") -> ActionReceipt:
    return ActionReceipt(
        action_id=new_id("action"),
        run_id=run_id,
        title="更新需求 REQ-5",
        target_system="feishu_base",
        target_object="rec_5",
        mutation_type="update",
        state=state,
    )


class CollectingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


def test_schedule_and_list_continuations(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = RunLedger(config.run_ledger_path)
    service = FollowUpService(config, ledger)
    receipt = _receipt()
    ledger.record_receipt(receipt, chat_id="chat1", platform="feishu")

    continuation_id = service.schedule_receipt_follow_up(
        receipt, chat_id="chat1", platform="feishu", delay_seconds=3600
    )

    pending = ledger.list_continuations()
    assert len(pending) == 1
    assert pending[0]["continuation_id"] == continuation_id
    assert pending[0]["kind"] == "receipt_reminder"
    assert pending[0]["run_id"] == receipt.run_id
    # not due yet
    assert ledger.due_continuations("2020-01-01T00:00:00+00:00") == []


@pytest.mark.asyncio
async def test_due_receipt_reminder_fires_once_and_cites_reason(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = RunLedger(config.run_ledger_path)
    service = FollowUpService(config, ledger)
    receipt = _receipt()
    ledger.record_receipt(receipt, chat_id="chat1", platform="feishu")
    service.schedule_receipt_follow_up(
        receipt, chat_id="chat1", platform="feishu", delay_seconds=-60
    )
    sender = CollectingSender()

    outcomes = await service.run_once(sender)

    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "sent"
    assert len(sender.sent) == 1
    chat_id, message = sender.sent[0]
    assert chat_id == "chat1"
    assert "触发原因" in message
    assert receipt.action_id in message or "更新需求 REQ-5" in message
    assert f"continue {receipt.run_id}" in message

    # firing is once-only: a second sweep sends nothing
    assert await service.run_once(sender) == []
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_resolved_receipt_cancels_reminder_without_sending(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = RunLedger(config.run_ledger_path)
    service = FollowUpService(config, ledger)
    receipt = _receipt()
    ledger.record_receipt(receipt, chat_id="chat1", platform="feishu")
    service.schedule_receipt_follow_up(
        receipt, chat_id="chat1", platform="feishu", delay_seconds=-60
    )
    rejected = receipt.with_state("rejected")
    ledger.record_receipt_state(rejected, previous_state=receipt.state)
    sender = CollectingSender()

    outcomes = await service.run_once(sender)

    assert outcomes[0]["outcome"] == "cancelled"
    assert "rejected" in outcomes[0]["message"]
    assert sender.sent == []


@pytest.mark.asyncio
async def test_run_follow_up_cancels_when_run_finished(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = RunLedger(config.run_ledger_path)
    service = FollowUpService(config, ledger)
    run = ledger.open_run(
        NormalizedEvent(
            platform="feishu",
            event_id="evt",
            chat_id="chat1",
            message_id="m1",
            sender_id="u1",
            text="任务",
            mention_bot=True,
            chat_type="group",
            destination_id="chat",
        )
    )
    service.schedule_run_follow_up(
        run.run_id, chat_id="chat1", platform="feishu", delay_seconds=-60
    )
    ledger.record_status(run.run_id, "succeeded")
    sender = CollectingSender()

    outcomes = await service.run_once(sender)

    assert outcomes[0]["outcome"] == "cancelled"
    assert sender.sent == []


@pytest.mark.asyncio
async def test_run_follow_up_fires_for_unfinished_run(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = RunLedger(config.run_ledger_path)
    service = FollowUpService(config, ledger)
    run = ledger.open_run(
        NormalizedEvent(
            platform="feishu",
            event_id="evt",
            chat_id="chat1",
            message_id="m1",
            sender_id="u1",
            text="任务",
            mention_bot=True,
            chat_type="group",
            destination_id="chat",
        )
    )
    ledger.record_status(run.run_id, "waiting_approval")
    service.schedule_run_follow_up(
        run.run_id, chat_id="chat1", platform="feishu", delay_seconds=-60
    )
    sender = CollectingSender()

    outcomes = await service.run_once(sender)

    assert outcomes[0]["outcome"] == "sent"
    assert "waiting_approval" in sender.sent[0][1]


@pytest.mark.asyncio
async def test_continue_verb_schedules_follow_up(tmp_path: Path) -> None:
    config = _config(tmp_path, actor_permissions=("dev:write",))
    ledger = RunLedger(config.run_ledger_path)
    run = ledger.open_run(
        NormalizedEvent(
            platform="feishu",
            event_id="evt",
            chat_id="chat1",
            message_id="m1",
            sender_id="dev",
            text="任务",
            mention_bot=True,
            chat_type="group",
            destination_id="chat",
        )
    )
    service = ApprovalService(config, ledger)

    result = await service.execute(
        ReceiptCommand(verb="continue", reference=run.run_id),
        actor_id="dev",
        chat_id="chat1",
        platform="feishu",
    )

    assert result["continuation_id"].startswith("cont_")
    assert "follow-up scheduled" in result["note"]
    pending = ledger.list_continuations()
    assert pending[0]["kind"] == "run_follow_up"
    assert pending[0]["chat_id"] == "chat1"


@pytest.mark.asyncio
async def test_governed_proposal_schedules_reminder(tmp_path: Path) -> None:
    # covered end-to-end: propose via workflow helper then check continuation exists
    from ostag.workflows.auto_base import AutoBaseWorkflow

    config = _config(
        tmp_path,
        governed_writes=True,
        auto_base_default_base_token="bt",
        auto_base_default_table_id="tbl",
    )
    workflow = AutoBaseWorkflow(config, cli=object())
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="chat9",
        message_id="m1",
        sender_id="u1",
        text="x",
        mention_bot=True,
        chat_type="group",
        destination_id="chat",
    )
    workflow.current_run_id = "run_prop1"

    workflow._propose_write_receipt(
        event,
        title="t",
        target_object="rec_9",
        mutation_type="update",
        summary="s",
        risk="low",
        apply_payload={"operation": "batch_update"},
    )

    ledger = RunLedger(config.run_ledger_path)
    pending = ledger.list_continuations()
    assert len(pending) == 1
    assert pending[0]["kind"] == "receipt_reminder"
    assert pending[0]["run_id"] == "run_prop1"


async def test_cli_followups_list_and_run_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "run_ledger.jsonl"
    monkeypatch.setenv("OSTAG_RUN_LEDGER_PATH", str(ledger_path))
    config = load_settings(None, audit_log=tmp_path / "audit.log", run_ledger_path=ledger_path)
    ledger = RunLedger(ledger_path)
    receipt = _receipt()
    ledger.record_receipt(receipt, chat_id="chat1", platform="feishu")
    FollowUpService(config, ledger).schedule_receipt_follow_up(
        receipt, chat_id="chat1", platform="feishu", delay_seconds=-60
    )

    exit_code = await main_async(["followups", "list", "--json"])
    assert exit_code == 0
    pending = json.loads(capsys.readouterr().out)
    assert len(pending) == 1

    exit_code = await main_async(["followups", "run-once", "--json"])
    assert exit_code == 0
    outcomes = json.loads(capsys.readouterr().out)
    assert outcomes[0]["outcome"] == "rendered"  # no chat sender in CLI context

    exit_code = await main_async(["followups", "list", "--json"])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == []
