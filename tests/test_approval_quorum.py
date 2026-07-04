from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.approvals import ApprovalService
from ostag.core.channel_brain import ChannelBrainStore
from ostag.core.receipts import ReceiptCommand
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import ActionReceipt, SchemaError, new_id
from ostag.main import main_async


def _config(tmp_path: Path, **overrides: object):
    return load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        actor_permissions=("alice:write", "bob:write", "carol:write"),
        **overrides,
    )


def _receipt(risk: str = "high") -> ActionReceipt:
    return ActionReceipt(
        action_id=new_id("action"),
        run_id="run_q1",
        title="删库跑路级别的高危操作",
        target_system="feishu_base",
        target_object="rec_9",
        mutation_type="update",
        risk=risk,
        state="needs_approval",
    )


@pytest.mark.asyncio
async def test_low_risk_receipt_needs_single_approval(tmp_path: Path) -> None:
    config = _config(tmp_path, approval_quorum_high_risk=3)
    ledger = RunLedger(config.run_ledger_path)
    receipt = _receipt(risk="low")
    ledger.record_receipt(receipt, chat_id="chat1", platform="feishu")
    service = ApprovalService(config, ledger)

    result = await service.execute(
        ReceiptCommand(verb="approve", reference=receipt.action_id),
        actor_id="alice",
        chat_id="chat1",
        platform="feishu",
    )

    assert result["action"]["state"] == "ready_to_apply"
    assert result["quorum"] == {"required": 1, "approved": 1, "approvers": ["alice"]}


@pytest.mark.asyncio
async def test_high_risk_receipt_requires_two_distinct_approvers(tmp_path: Path) -> None:
    config = _config(tmp_path, approval_quorum_high_risk=2)
    ledger = RunLedger(config.run_ledger_path)
    receipt = _receipt()
    ledger.record_receipt(receipt, chat_id="chat1", platform="feishu")
    service = ApprovalService(config, ledger)

    first = await service.execute(
        ReceiptCommand(verb="approve", reference=receipt.action_id),
        actor_id="alice",
        chat_id="chat1",
        platform="feishu",
    )
    assert first["action"]["state"] == "needs_approval"  # quorum not met
    assert first["quorum"]["approved"] == 1

    # the same actor approving again does not advance the quorum
    again = await service.execute(
        ReceiptCommand(verb="approve", reference=receipt.action_id),
        actor_id="alice",
        chat_id="chat1",
        platform="feishu",
    )
    assert again["quorum"]["approved"] == 1
    assert again["action"]["state"] == "needs_approval"

    # apply before quorum is refused by the state machine
    with pytest.raises(SchemaError):
        await service.execute(
            ReceiptCommand(verb="apply", reference=receipt.action_id),
            actor_id="alice",
            chat_id="chat1",
            platform="feishu",
        )

    second = await service.execute(
        ReceiptCommand(verb="approve", reference=receipt.action_id),
        actor_id="bob",
        chat_id="chat1",
        platform="feishu",
    )
    assert second["action"]["state"] == "ready_to_apply"
    assert second["quorum"] == {
        "required": 2,
        "approved": 2,
        "approvers": ["alice", "bob"],
    }


@pytest.mark.asyncio
async def test_reject_is_immediate_even_under_quorum(tmp_path: Path) -> None:
    config = _config(tmp_path, approval_quorum_high_risk=3)
    ledger = RunLedger(config.run_ledger_path)
    receipt = _receipt()
    ledger.record_receipt(receipt, chat_id="chat1", platform="feishu")
    service = ApprovalService(config, ledger)
    await service.execute(
        ReceiptCommand(verb="approve", reference=receipt.action_id),
        actor_id="alice",
        chat_id="chat1",
        platform="feishu",
    )

    result = await service.execute(
        ReceiptCommand(verb="reject", reference=receipt.action_id),
        actor_id="bob",
        chat_id="chat1",
        platform="feishu",
    )

    assert result["action"]["state"] == "rejected"


@pytest.mark.asyncio
async def test_channel_policy_quorum_overrides_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))
    config = _config(tmp_path, approval_quorum_high_risk=1)
    ChannelBrainStore(config).update_policy("feishu", "chat1", approval_quorum_high_risk=2)
    ledger = RunLedger(config.run_ledger_path)
    receipt = _receipt()
    ledger.record_receipt(receipt, chat_id="chat1", platform="feishu")
    service = ApprovalService(config, ledger)

    result = await service.execute(
        ReceiptCommand(verb="approve", reference=receipt.action_id),
        actor_id="alice",
        chat_id="chat1",
        platform="feishu",
    )

    assert result["action"]["state"] == "needs_approval"
    assert result["quorum"]["required"] == 2


@pytest.mark.asyncio
async def test_partial_approval_chat_reply_shows_progress(tmp_path: Path) -> None:
    config = _config(tmp_path, approval_quorum_high_risk=2)
    ledger = RunLedger(config.run_ledger_path)
    receipt = _receipt()
    ledger.record_receipt(receipt, chat_id="chat1", platform="feishu")
    service = ApprovalService(config, ledger)

    reply = await service.handle_text_command(
        ReceiptCommand(verb="approve", reference=receipt.action_id),
        actor_id="alice",
        chat_id="chat1",
        platform="feishu",
    )

    assert "1/2" in reply
    assert "还需 1 位" in reply
    assert receipt.action_id in reply


async def test_cli_channel_policy_quorum_set_and_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))

    exit_code = await main_async(
        [
            "channel",
            "policy",
            "set",
            "--platform",
            "feishu",
            "--chat",
            "oc_q",
            "--quorum-high-risk",
            "2",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["channel"]["approval_quorum_high_risk"] == 2
    assert payload["effective"]["approval_quorum_high_risk"] == 2

    exit_code = await main_async(
        [
            "channel",
            "policy",
            "set",
            "--platform",
            "feishu",
            "--chat",
            "oc_q",
            "--quorum-high-risk",
            "inherit",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["channel"]["approval_quorum_high_risk"] is None
    assert payload["effective"]["approval_quorum_high_risk"] == 1  # global default

    exit_code = await main_async(
        [
            "channel",
            "policy",
            "set",
            "--platform",
            "feishu",
            "--chat",
            "oc_q",
            "--quorum-high-risk",
            "zero",
            "--json",
        ]
    )
    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_quorum"
