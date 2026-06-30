from __future__ import annotations

import json
from pathlib import Path

from ostag.config import load_settings
from ostag.core.event import NormalizedEvent
from ostag.workflows.auto_base import (
    BaseContext,
    ChatFile,
    FieldInfo,
    WorkflowContext,
    build_rows_for_fields,
    extract_base_context,
    extract_docx_records,
    find_workflow_context,
    is_auto_base_request,
    is_auto_base_status_request,
)


def test_auto_base_request_detection_is_intentional() -> None:
    assert is_auto_base_request("请把docx里的需求填入到需求收集和管理表格中去")
    assert is_auto_base_request("把这个文件写入 Base")
    assert not is_auto_base_request("请总结下一步")


def test_auto_base_status_request_detection_is_intentional() -> None:
    assert is_auto_base_status_request("自动入表能力是否已经上线？")
    assert is_auto_base_status_request("请告诉我自动入表功能现在能不能用")
    assert not is_auto_base_status_request("请把docx里的需求填入表格")


def test_extract_base_context_supports_app_links_and_table_params() -> None:
    context = extract_base_context(
        "https://a2ekucppds.feishu.cn/app/TqcVbKRiZa6YClshTqxcOzblncy?table=tblABC&view=vew1"
    )

    assert context == BaseContext(
        base_token="TqcVbKRiZa6YClshTqxcOzblncy",
        table_id="tblABC",
        url="https://a2ekucppds.feishu.cn/app/TqcVbKRiZa6YClshTqxcOzblncy?table=tblABC&view=vew1",
    )


def test_find_workflow_context_uses_recent_file_and_base_link() -> None:
    messages = [
        {
            "message_id": "om_request",
            "msg_type": "text",
            "content": "@Z Studio FEISHU 请把docx里的需求填入到需求收集和管理表格中去",
        },
        {
            "message_id": "om_file",
            "msg_type": "file",
            "content": '<file key="file_v3_docx" name="需求模板.docx"/>',
        },
        {
            "message_id": "om_base",
            "msg_type": "text",
            "content": "<p>https://tenant.feishu.cn/app/TqcVbKRiZa6YClshTqxcOzblncy</p>",
        },
    ]

    context = find_workflow_context(messages)

    assert context == WorkflowContext(
        base=BaseContext(
            base_token="TqcVbKRiZa6YClshTqxcOzblncy",
            table_id=None,
            url="https://tenant.feishu.cn/app/TqcVbKRiZa6YClshTqxcOzblncy",
        ),
        file=ChatFile(message_id="om_file", file_key="file_v3_docx", name="需求模板.docx"),
    )


def test_extract_docx_records_falls_back_to_single_requirement() -> None:
    records = extract_docx_records(
        """
        项目名称：大道寰球 AI 助手
        需求背景：希望沉淀投研群需求并自动跟踪
        优先级：高
        """,
        source_name="需求模板.docx",
    )

    assert records == [
        {
            "需求名称": "大道寰球 AI 助手",
            "需求描述": "希望沉淀投研群需求并自动跟踪",
            "优先级": "高",
        }
    ]


def test_build_rows_for_fields_maps_known_fields_and_keeps_primary_title() -> None:
    fields = [
        FieldInfo(name="需求名称", field_id="fld_title", field_type="text"),
        FieldInfo(name="需求描述", field_id="fld_desc", field_type="text"),
        FieldInfo(name="状态", field_id="fld_status", field_type="select"),
        FieldInfo(name="来源文件", field_id="fld_source", field_type="text"),
        FieldInfo(name="来源消息", field_id="fld_msg", field_type="text"),
        FieldInfo(name="创建人", field_id="fld_readonly", field_type="created_by"),
    ]
    records = [{"需求名称": "AI 助手", "需求描述": "自动收集需求"}]

    payload = build_rows_for_fields(
        records,
        fields,
        source_file="需求模板.docx",
        source_message_id="om_file",
        now_text="2026-06-29 22:30:00",
    )

    assert payload == {
        "fields": ["需求名称", "需求描述", "状态", "来源文件", "来源消息"],
        "rows": [["AI 助手", "自动收集需求", "待处理", "需求模板.docx", "om_file"]],
    }


class FakeAutoBaseWorkflow:
    async def handle(self, event):
        return "自动入表完成"


async def test_runner_uses_auto_base_workflow_before_backend(tmp_path: Path) -> None:
    from ostag.core.runner import OSTagRunner

    class FailingBackend:
        async def run(self, prompt):
            raise AssertionError("backend should not run")

    config = load_settings(None, audit_log=tmp_path / "audit.log")
    runner = OSTagRunner(config, backend=FailingBackend(), auto_base_workflow=FakeAutoBaseWorkflow())
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="oc_demo",
        message_id="om_demo",
        sender_id="ou_demo",
        text="请把docx里的需求填入表格",
        mention_bot=True,
    )

    assert await runner.handle_event(event) == "自动入表完成"


async def test_auto_base_workflow_answers_status_without_cli(tmp_path: Path) -> None:
    from ostag.workflows.auto_base import AutoBaseWorkflow

    class FailingCLI:
        async def json(self, *args):
            raise AssertionError("status check should not call lark-cli")

    config = load_settings(None, audit_log=tmp_path / "audit.log")
    workflow = AutoBaseWorkflow(config, cli=FailingCLI())
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="oc_demo",
        message_id="om_demo",
        sender_id="ou_demo",
        text="自动入表能力是否已经上线？",
        mention_bot=True,
    )

    reply = await workflow.handle(event)

    assert reply is not None
    assert "已经上线" in reply
