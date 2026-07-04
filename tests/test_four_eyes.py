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
        actor_permissions=("alice:write", "bob:write"),
        **overrides,
    )


def _receipt() -> ActionReceipt:
    return ActionReceipt(
        action_id=new_id("action"),
        run_id="run_fe1",
        title="更新需求 REQ-4",
        target_system="feishu_base",
        target_object="rec_4",
        mutation_type="update",
        state="needs_approval",
    )


def _service(tmp_path: Path, **overrides):
    config = _config(tmp_path, **overrides)
    ledger = RunLedger(config.run_ledger_path)
    return ApprovalService(config, ledger), ledger


@pytest.mark.asyncio
async def test_proposer_cannot_approve_own_receipt_when_enabled(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path, four_eyes_enabled=True)
    receipt = _receipt()
    service.propose(receipt, chat_id="chat1", platform="feishu", proposer_id="alice")

    with pytest.raises(SchemaError) as excinfo:
        await service.execute(
            ReceiptCommand(verb="approve", reference=receipt.action_id),
            actor_id="alice",
            chat_id="chat1",
            platform="feishu",
        )

    assert excinfo.value.code == "four_eyes_violation"
    assert f"reject {receipt.action_id}" in excinfo.value.recovery_hint
    security = [
        record
        for record in ledger._read_all()
        if record["event_type"] == "security_event"
    ]
    assert security
    assert "four_eyes_violation" in security[0]["payload"]["reason"]
    # receipt untouched
    assert ledger.get_receipt(receipt.action_id).state == "needs_approval"


@pytest.mark.asyncio
async def test_other_actor_can_approve_and_proposer_can_reject(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path, four_eyes_enabled=True)
    receipt = _receipt()
    service.propose(receipt, chat_id="chat1", platform="feishu", proposer_id="alice")

    result = await service.execute(
        ReceiptCommand(verb="approve", reference=receipt.action_id),
        actor_id="bob",
        chat_id="chat1",
        platform="feishu",
    )
    assert result["action"]["state"] == "ready_to_apply"

    # withdrawing your own proposal is always allowed
    other = _receipt()
    service.propose(other, chat_id="chat1", platform="feishu", proposer_id="alice")
    rejected = await service.execute(
        ReceiptCommand(verb="reject", reference=other.action_id),
        actor_id="alice",
        chat_id="chat1",
        platform="feishu",
    )
    assert rejected["action"]["state"] == "rejected"


@pytest.mark.asyncio
async def test_disabled_by_default_and_legacy_receipts_unrestricted(tmp_path: Path) -> None:
    # default: proposer self-approval allowed
    service, _ = _service(tmp_path)
    receipt = _receipt()
    service.propose(receipt, chat_id="chat1", platform="feishu", proposer_id="alice")
    result = await service.execute(
        ReceiptCommand(verb="approve", reference=receipt.action_id),
        actor_id="alice",
        chat_id="chat1",
        platform="feishu",
    )
    assert result["action"]["state"] == "ready_to_apply"

    # enabled but receipt has no recorded proposer (pre-tracking): unrestricted
    service2, ledger2 = _service(tmp_path, four_eyes_enabled=True)
    legacy = _receipt()
    ledger2.record_receipt(legacy, chat_id="chat1", platform="feishu")
    result = await service2.execute(
        ReceiptCommand(verb="approve", reference=legacy.action_id),
        actor_id="alice",
        chat_id="chat1",
        platform="feishu",
    )
    assert result["action"]["state"] == "ready_to_apply"


@pytest.mark.asyncio
async def test_channel_policy_four_eyes_overrides_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))
    config = _config(tmp_path, four_eyes_enabled=False)
    ChannelBrainStore(config).update_policy("feishu", "chat1", four_eyes=True)
    ledger = RunLedger(config.run_ledger_path)
    service = ApprovalService(config, ledger)
    receipt = _receipt()
    service.propose(receipt, chat_id="chat1", platform="feishu", proposer_id="alice")

    with pytest.raises(SchemaError) as excinfo:
        await service.execute(
            ReceiptCommand(verb="approve", reference=receipt.action_id),
            actor_id="alice",
            chat_id="chat1",
            platform="feishu",
        )

    assert excinfo.value.code == "four_eyes_violation"


async def test_cli_policy_set_four_eyes_and_propose_records_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))
    monkeypatch.setenv("OSTAG_RUN_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))

    exit_code = await main_async(
        [
            "channel",
            "policy",
            "set",
            "--platform",
            "feishu",
            "--chat",
            "oc_fe",
            "--four-eyes",
            "true",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["channel"]["four_eyes"] is True
    assert payload["effective"]["four_eyes"] is True

    receipt_file = tmp_path / "receipt.json"
    receipt_file.write_text(json.dumps(_receipt().to_dict()), encoding="utf-8")
    exit_code = await main_async(
        [
            "receipts",
            "propose",
            "--file",
            str(receipt_file),
            "--chat",
            "oc_fe",
            "--actor",
            "agent_x",
            "--json",
        ]
    )
    assert exit_code == 0
    proposed = json.loads(capsys.readouterr().out)

    ledger = RunLedger(tmp_path / "ledger.jsonl")
    assert ledger.receipt_proposer(proposed["action_id"]) == "agent_x"
