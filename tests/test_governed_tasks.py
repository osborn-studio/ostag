from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.connectors.apply import (
    TaskCreateApplyExecutor,
    build_apply_executors,
    task_system_for_platform,
)
from ostag.core.approvals import ApprovalService
from ostag.core.event import NormalizedEvent
from ostag.core.receipts import ReceiptCommand
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import ActionReceipt, SchemaError, new_id
from ostag.workflows.auto_base import AutoBaseWorkflow


class FakeCLI:
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
                        {"name": "需求负责人", "type": "user", "field_id": "fld_owner"},
                        {"name": "任务链接", "type": "text", "field_id": "fld_task"},
                    ]
                },
            }
        if args[:2] == ("base", "+record-search"):
            return {
                "ok": True,
                "data": {
                    "records": [
                        {
                            "record_id": "rec_002",
                            "fields": {"需求描述": "市场周报：自动生成周报"},
                        }
                    ]
                },
            }
        if args[:2] == ("task", "+create"):
            return {"ok": True, "data": {"url": "https://todo.feishu.cn/task/demo"}}
        if args[:2] == ("base", "+record-batch-update"):
            return {"ok": True, "data": {"record_id_list": ["rec_002"]}}
        raise AssertionError(f"unexpected lark-cli call: {args}")

    def calls_of(self, *prefix: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[: len(prefix)] == prefix]


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
                        {"id": "ou_assignee", "name": "李四"},
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


def test_task_system_mapping() -> None:
    assert task_system_for_platform("feishu") == "feishu_task"
    assert task_system_for_platform("lark") == "lark_task"
    assert task_system_for_platform("slack") == "lark_task"


@pytest.mark.asyncio
async def test_governed_task_creation_is_proposed_not_executed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    fake_cli = FakeCLI()
    workflow = AutoBaseWorkflow(config, cli=fake_cli)
    workflow.current_run_id = "run_task1"

    reply = await workflow.handle(_event("请为 市场周报 创建飞书任务并分配给 @李四"))

    assert reply is not None
    assert "待审批" in reply
    assert "创建飞书任务" in reply
    assert fake_cli.calls_of("task", "+create") == []
    assert fake_cli.calls_of("base", "+record-batch-update") == []

    receipts = RunLedger(config.run_ledger_path).list_receipts(chat_id="oc_demo")
    task_receipts = [r for r in receipts if r.apply_payload.get("operation") == "create_task"]
    assert len(task_receipts) == 1
    payload = task_receipts[0].apply_payload
    assert task_receipts[0].target_system == "feishu_task"
    assert payload["summary"].startswith("市场周报")
    assert payload["assignee_open_id"] == "ou_assignee"
    assert payload["backfill_fields"] == ["任务链接"]
    assert payload["idempotency_key"] == "ostag-rec_002"


@pytest.mark.asyncio
async def test_approved_task_receipt_creates_task_and_backfills(tmp_path: Path) -> None:
    config = _config(tmp_path, apply_enabled=True, actor_permissions=("ou_sender:write",))
    fake_cli = FakeCLI()
    workflow = AutoBaseWorkflow(config, cli=fake_cli)
    workflow.current_run_id = "run_task2"
    await workflow.handle(_event("请为 市场周报 创建飞书任务并分配给 @李四"))

    ledger = RunLedger(config.run_ledger_path)
    service = ApprovalService(config, ledger, build_apply_executors(workflow))
    await service.execute(
        ReceiptCommand(verb="approve", reference="1"), actor_id="ou_sender", chat_id="oc_demo"
    )
    result = await service.execute(
        ReceiptCommand(verb="apply", reference="1"), actor_id="ou_sender", chat_id="oc_demo"
    )

    assert result["action"]["state"] == "applied"
    assert "https://todo.feishu.cn/task/demo" in result["evidence"]
    assert "任务链接" in result["evidence"]

    task_calls = fake_cli.calls_of("task", "+create")
    assert len(task_calls) == 1
    assert "--assignee" in task_calls[0]
    update_calls = fake_cli.calls_of("base", "+record-batch-update")
    assert len(update_calls) == 1
    update_payload = json.loads(update_calls[0][update_calls[0].index("--json") + 1])
    assert update_payload["patch"] == {"任务链接": "https://todo.feishu.cn/task/demo"}
    assert update_payload["record_id_list"] == ["rec_002"]


@pytest.mark.asyncio
async def test_confirmation_policy_satisfied_by_receipt_under_governed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ostag.core.channel_brain import ChannelBrainStore

    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))
    config = _config(tmp_path)
    store = ChannelBrainStore(config)
    store.update_policy("feishu", "oc_demo", task_create_requires_confirmation=True)
    fake_cli = FakeCLI()
    workflow = AutoBaseWorkflow(config, cli=fake_cli)

    reply = await workflow.handle(_event("请为 市场周报 创建飞书任务并分配给 @李四"))

    # governed: receipt is the confirmation — proposal succeeds
    assert reply is not None
    assert "待审批" in reply

    # direct mode: the confirmation gate still blocks
    direct_config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        auto_base_default_base_token="base_token",
        auto_base_default_table_id="tblDefault",
        governed_writes=False,
    )
    direct_workflow = AutoBaseWorkflow(direct_config, cli=FakeCLI())
    direct_reply = await direct_workflow.handle(_event("请为 市场周报 创建飞书任务并分配给 @李四"))
    assert direct_reply is not None
    assert "人工确认" in direct_reply


@pytest.mark.asyncio
async def test_combined_command_proposes_two_receipts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    fake_cli = FakeCLI()
    workflow = AutoBaseWorkflow(config, cli=fake_cli)
    workflow.current_run_id = "run_task3"

    reply = await workflow.handle(
        _event("请为 市场周报 创建飞书任务并分配给 @李四，状态改为 开发交付中")
    )

    assert reply is not None
    receipts = RunLedger(config.run_ledger_path).list_receipts(chat_id="oc_demo")
    operations = sorted(r.apply_payload.get("operation", "") for r in receipts)
    assert operations == ["batch_update", "create_task"]
    assert "approve 1" in reply and "approve 2" in reply
    assert fake_cli.calls_of("task", "+create") == []
    assert fake_cli.calls_of("base", "+record-batch-update") == []


class DirectTaskConnector:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_task(self, *, summary, description, assignee_open_id, idempotency_key):
        self.created.append(
            {
                "summary": summary,
                "assignee": assignee_open_id,
                "idempotency_key": idempotency_key,
            }
        )
        return {"data": {"url": "https://todo.feishu.cn/task/x"}}


class DirectRequirements:
    def __init__(self) -> None:
        self.updates: list[tuple] = []

    async def batch_update(self, base_token, table_id, record_id, patch):
        self.updates.append((base_token, table_id, record_id, patch))
        return {"ok": True}


def _task_receipt(payload: dict) -> ActionReceipt:
    return ActionReceipt(
        action_id=new_id("action"),
        run_id="run_x",
        title="t",
        target_system="feishu_task",
        target_object="rec_1",
        mutation_type="create",
        apply_payload=payload,
    )


async def test_task_executor_without_backfill_fields() -> None:
    tasks = DirectTaskConnector()
    requirements = DirectRequirements()
    executor = TaskCreateApplyExecutor(tasks, requirements)

    evidence = await executor.apply(
        _task_receipt(
            {
                "operation": "create_task",
                "summary": "任务A",
                "description": "",
                "idempotency_key": "k1",
            }
        )
    )

    assert tasks.created[0]["summary"] == "任务A"
    assert requirements.updates == []
    assert "https://todo.feishu.cn/task/x" in evidence


async def test_task_executor_rejects_bad_payloads() -> None:
    executor = TaskCreateApplyExecutor(DirectTaskConnector(), DirectRequirements())

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(_task_receipt({"operation": "create_task"}))
    assert excinfo.value.code == "invalid_apply_payload"

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(_task_receipt({"operation": "batch_update"}))
    assert excinfo.value.code == "unsupported_operation"
