from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.connectors.apply import (
    RequirementTableApplyExecutor,
    build_apply_executors,
    target_system_for_platform,
)
from ostag.core.approvals import ApprovalService
from ostag.core.event import NormalizedEvent
from ostag.core.receipts import ReceiptCommand
from ostag.core.run_ledger import RunLedger
from ostag.core.runner import OSTagRunner
from ostag.core.schemas import ActionReceipt, SchemaError, new_id
from ostag.workflows.auto_base import AutoBaseWorkflow


class FakeCLI:
    """lark-cli fake covering table/field/search/update calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def json(self, *args: str):
        self.calls.append(args)
        if args[:2] == ("base", "+table-list"):
            return {
                "ok": True,
                "data": {"tables": [{"table_id": "tblDefault", "name": "需求收集管理"}]},
            }
        if args[:2] == ("base", "+field-list"):
            return {
                "ok": True,
                "data": {
                    "fields": [
                        {"name": "需求描述", "type": "text", "field_id": "fld_desc"},
                        {
                            "name": "需求状态",
                            "type": "select",
                            "field_id": "fld_status",
                            "options": [{"name": "待评估"}, {"name": "开发交付中"}],
                        },
                        {"name": "需求负责人", "type": "user", "field_id": "fld_owner"},
                    ]
                },
            }
        if args[:2] == ("base", "+record-search"):
            return {
                "ok": True,
                "data": {
                    "records": [
                        {
                            "record_id": "rec_001",
                            "fields": {"需求描述": "交易盯盘：行情异动提醒"},
                        }
                    ]
                },
            }
        if args[:2] == ("base", "+record-batch-update"):
            return {"ok": True, "data": {"record_id_list": ["rec_001"]}}
        if args[:2] == ("base", "+record-batch-create"):
            return {"ok": True, "data": {"record_ids": ["rec_100"]}}
        raise AssertionError(f"unexpected lark-cli call: {args}")

    def update_calls(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[:2] == ("base", "+record-batch-update")]


def _event(text: str) -> NormalizedEvent:
    return NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="oc_demo",
        message_id="om_demo",
        sender_id="ou_sender",
        text=text,
        mention_bot=True,
        chat_type="group",
        destination_id="chat",
        raw={
            "event": {
                "message": {
                    "mentions": [
                        {"id": "ou_bot", "name": "OS Tag"},
                        {"id": "ou_assignee", "name": "张三"},
                    ]
                }
            }
        },
    )


def _config(tmp_path: Path, **overrides: object):
    return load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        auto_base_default_base_token="base_token",
        auto_base_default_table_id="tblDefault",
        governed_writes=True,
        **overrides,
    )


def test_target_system_for_platform_mapping() -> None:
    assert target_system_for_platform("feishu") == "feishu_base"
    assert target_system_for_platform("lark") == "lark_base"
    assert target_system_for_platform("slack") == "lark_base"
    assert target_system_for_platform("dingtalk") == "dingtalk_ai_table"


@pytest.mark.asyncio
async def test_governed_patch_is_proposed_not_written(tmp_path: Path) -> None:
    config = _config(tmp_path)
    fake_cli = FakeCLI()
    workflow = AutoBaseWorkflow(config, cli=fake_cli)
    workflow.current_run_id = "run_gov1"

    reply = await workflow.handle(_event("请把 交易盯盘 分配给 @张三，状态改为 开发交付中"))

    assert reply is not None
    assert "待审批" in reply
    assert "approve 1" in reply
    assert fake_cli.update_calls() == []  # nothing written

    ledger = RunLedger(config.run_ledger_path)
    receipts = ledger.list_receipts(chat_id="oc_demo")
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.run_id == "run_gov1"
    assert receipt.state == "needs_approval"
    assert receipt.target_system == "feishu_base"
    assert receipt.apply_payload["operation"] == "batch_update"
    assert receipt.apply_payload["record_id"] == "rec_001"
    assert receipt.apply_payload["patch"]["需求状态"] == "开发交付中"


@pytest.mark.asyncio
async def test_approved_receipt_applies_via_executor(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        apply_enabled=True,
        actor_permissions=("ou_sender:write", "feishu:ou_sender:write"),
    )
    fake_cli = FakeCLI()
    workflow = AutoBaseWorkflow(config, cli=fake_cli)
    workflow.current_run_id = "run_gov2"
    await workflow.handle(_event("请把 交易盯盘 分配给 @张三，状态改为 开发交付中"))

    ledger = RunLedger(config.run_ledger_path)
    service = ApprovalService(config, ledger, build_apply_executors(workflow))
    await service.execute(
        ReceiptCommand(verb="approve", reference="1"), actor_id="ou_sender", chat_id="oc_demo"
    )
    result = await service.execute(
        ReceiptCommand(verb="apply", reference="1"), actor_id="ou_sender", chat_id="oc_demo"
    )

    assert result["action"]["state"] == "applied"
    update_calls = fake_cli.update_calls()
    assert len(update_calls) == 1
    payload = json.loads(update_calls[0][update_calls[0].index("--json") + 1])
    assert payload == {
        "record_id_list": ["rec_001"],
        "patch": {
            "需求负责人": [{"id": "ou_assignee"}],
            "需求状态": "开发交付中",
        },
    }
    assert "rec_001" in result["evidence"]


@pytest.mark.asyncio
async def test_full_chat_loop_propose_approve_apply(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        apply_enabled=True,
        actor_permissions=("ou_sender:write", "feishu:ou_sender:write"),
        backend="mock",
    )
    fake_cli = FakeCLI()
    workflow = AutoBaseWorkflow(config, cli=fake_cli)
    runner = OSTagRunner(config, auto_base_workflow=workflow)

    propose_reply = await runner.handle_event(
        _event("请把 交易盯盘 分配给 @张三，状态改为 开发交付中")
    )
    assert "待审批" in propose_reply
    assert fake_cli.update_calls() == []

    approve_reply = await runner.handle_event(_event("approve 1"))
    assert "已批准" in approve_reply

    apply_reply = await runner.handle_event(_event("apply 1"))
    assert "已执行" in apply_reply
    assert len(fake_cli.update_calls()) == 1


@pytest.mark.asyncio
async def test_read_actor_cannot_apply_governed_write(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        apply_enabled=True,
        actor_permissions=("ou_sender:read",),
        backend="mock",
    )
    fake_cli = FakeCLI()
    workflow = AutoBaseWorkflow(config, cli=fake_cli)
    runner = OSTagRunner(config, auto_base_workflow=workflow)
    receipt = ActionReceipt(
        action_id=new_id("action"),
        run_id="run_gov3",
        title="t",
        target_system="feishu_base",
        target_object="rec_001",
        mutation_type="update",
        state="needs_approval",
        apply_payload={
            "operation": "batch_update",
            "base_token": "base_token",
            "table_id": "tblDefault",
            "record_id": "rec_001",
            "patch": {"需求状态": "已完成"},
        },
    )
    runner.run_ledger.record_receipt(receipt, chat_id="oc_demo", platform="feishu")

    reply = await runner.handle_event(_event(f"apply {receipt.action_id}"))

    assert "无法执行" in reply
    assert fake_cli.update_calls() == []


class DirectConnector:
    def __init__(self) -> None:
        self.updates: list[tuple] = []
        self.creates: list[tuple] = []

    async def batch_update(self, base_token, table_id, record_id, patch):
        self.updates.append((base_token, table_id, record_id, patch))
        return {"ok": True}

    async def batch_create(self, base_token, table_id, payload):
        self.creates.append((base_token, table_id, payload))
        return {"ok": True}


def _receipt(payload: dict) -> ActionReceipt:
    return ActionReceipt(
        action_id=new_id("action"),
        run_id="run_x",
        title="t",
        target_system="feishu_base",
        target_object="o",
        mutation_type="update",
        apply_payload=payload,
    )


async def test_executor_batch_create() -> None:
    connector = DirectConnector()
    executor = RequirementTableApplyExecutor(connector)
    rows_payload = {"rows": [{"需求描述": "A"}, {"需求描述": "B"}]}

    evidence = await executor.apply(
        _receipt(
            {
                "operation": "batch_create",
                "base_token": "bt",
                "table_id": "tbl",
                "payload": rows_payload,
            }
        )
    )

    assert connector.creates == [("bt", "tbl", rows_payload)]
    assert "2 records" in evidence


async def test_executor_rejects_bad_payloads() -> None:
    executor = RequirementTableApplyExecutor(DirectConnector())

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(_receipt({"operation": "batch_update"}))
    assert excinfo.value.code == "invalid_apply_payload"

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(
            _receipt({"operation": "drop_table", "base_token": "bt", "table_id": "tbl"})
        )
    assert excinfo.value.code == "unsupported_operation"

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(
            _receipt(
                {
                    "operation": "batch_create",
                    "base_token": "bt",
                    "table_id": "tbl",
                    "payload": {"rows": []},
                }
            )
        )
    assert excinfo.value.code == "invalid_apply_payload"
