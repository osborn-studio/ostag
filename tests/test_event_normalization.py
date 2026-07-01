from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from ostag.adapters.base import Adapter
from ostag.adapters.dingtalk import DingTalkAdapter, signed_webhook_url
from ostag.adapters.feishu import FeishuAdapter
from ostag.adapters.lark import LarkAdapter
from ostag.adapters.slack import SlackAdapter
from ostag.config import load_settings
from ostag.core.event import NormalizedEvent
from ostag.core.runner import OSTagRunner


@pytest.mark.asyncio
async def test_feishu_raw_event_normalizes_to_core_event() -> None:
    config = load_settings(None, audit_log=Path("logs/test-audit.log"))
    raw = json.loads(Path("examples/feishu_event.json").read_text(encoding="utf-8"))

    event = await FeishuAdapter(config).normalize_event(raw)

    assert event.platform == "feishu"
    assert event.event_id == "evt_feishu_example"
    assert event.chat_id == "oc_demo_chat"
    assert event.message_id == "om_demo_message"
    assert event.sender_id == "ou_demo_sender"
    assert event.mention_bot is True
    assert "下一步" in event.text


@pytest.mark.asyncio
async def test_feishu_post_message_normalizes_text_and_link() -> None:
    config = load_settings(None, audit_log=Path("logs/test-audit.log"))
    raw = {
        "header": {"event_id": "evt_post"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_demo_sender"}},
            "message": {
                "chat_id": "oc_demo_chat",
                "message_id": "om_post_message",
                "message_type": "post",
                "content": json.dumps(
                    {
                        "title": "",
                        "content": [
                            [
                                {"tag": "at", "user_name": "Z Studio FEISHU", "user_id": "ou_bot"},
                                {"tag": "text", "text": " 请把这个 docx 文件中的需求写入"},
                                {
                                    "tag": "a",
                                    "text": "https://a2ekucppds.feishu.cn/app/TqcVbKRiZa6YClshTqxcOzblncy?pageId=pgeWAf9mD4KtSJhA",
                                    "href": "https://a2ekucppds.feishu.cn/app/TqcVbKRiZa6YClshTqxcOzblncy?pageId=pgeWAf9mD4KtSJhA",
                                },
                            ]
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        },
    }

    event = await FeishuAdapter(config).normalize_event(raw)

    assert event.mention_bot is True
    assert "请把这个 docx 文件中的需求写入" in event.text
    assert "https://a2ekucppds.feishu.cn/app/TqcVbKRiZa6YClshTqxcOzblncy" in event.text


def test_platform_adapters_implement_contract(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")

    assert isinstance(FeishuAdapter(config), Adapter)
    assert isinstance(LarkAdapter(config), Adapter)
    assert isinstance(SlackAdapter(config), Adapter)
    assert isinstance(DingTalkAdapter(config), Adapter)


@pytest.mark.asyncio
async def test_slack_app_mention_normalizes_to_core_event() -> None:
    config = load_settings(None, audit_log=Path("logs/test-audit.log"))
    raw = json.loads(Path("examples/slack_event.json").read_text(encoding="utf-8"))

    event = await SlackAdapter(config).normalize_event(raw)

    assert event.platform == "slack"
    assert event.event_id == "1710000000.000100"
    assert event.chat_id == "C123456"
    assert event.message_id == "1710000000.000100"
    assert event.sender_id == "U123456"
    assert event.mention_bot is True
    assert event.thread_id == "1710000000.000100"
    assert event.text == "Summarize the launch blockers and next action."


@pytest.mark.asyncio
async def test_dingtalk_robot_message_normalizes_to_core_event() -> None:
    config = load_settings(None, audit_log=Path("logs/test-audit.log"))
    raw = json.loads(Path("examples/dingtalk_event.json").read_text(encoding="utf-8"))

    event = await DingTalkAdapter(config).normalize_event(raw)

    assert event.platform == "dingtalk"
    assert event.event_id == "msg_demo_message"
    assert event.chat_id == "cid_demo_conversation"
    assert event.message_id == "msg_demo_message"
    assert event.sender_id == "staff_demo_sender"
    assert event.mention_bot is True
    assert event.text == "请总结这个项目下一步应该做什么"


@pytest.mark.asyncio
async def test_lark_mention_does_not_strip_english_prompt() -> None:
    config = load_settings(None, audit_log=Path("logs/test-audit.log"))
    raw = json.loads(Path("examples/lark_event.json").read_text(encoding="utf-8"))

    event = await LarkAdapter(config).normalize_event(raw)

    assert event.platform == "lark"
    assert event.mention_bot is True
    assert event.text == "Please summarize the next action items"


@pytest.mark.asyncio
async def test_non_mention_message_is_ignored(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    raw = json.loads(Path("examples/feishu_event.json").read_text(encoding="utf-8"))
    raw["event"]["message"]["mentions"] = []
    raw["event"]["message"]["content"] = json.dumps({"text": "普通群聊消息"}, ensure_ascii=False)

    event = await FeishuAdapter(config).normalize_event(raw)
    reply = await OSTagRunner(config).handle_event(event)

    assert event.mention_bot is False
    assert reply == ""


@pytest.mark.asyncio
async def test_feishu_live_start_runs_blocking_sdk_in_thread(monkeypatch, tmp_path: Path) -> None:
    started = []
    threaded = []

    class FakeProcessor:
        def __init__(self, callback):
            self.callback = callback

    class FakeBuilder:
        def register_p2_im_message_receive_v1(self, processor):
            self.processor = processor
            return self

        def build(self):
            return object()

    class FakeEventDispatcherHandler:
        @staticmethod
        def builder(*_args):
            return FakeBuilder()

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            started.append(True)

    async def fake_to_thread(func, *args, **kwargs):
        threaded.append(func)
        return func(*args, **kwargs)

    fake_lark = types.SimpleNamespace(
        EventDispatcherHandler=FakeEventDispatcherHandler,
        LogLevel=types.SimpleNamespace(INFO="INFO"),
        ws=types.SimpleNamespace(Client=FakeClient),
    )
    fake_dispatcher = types.SimpleNamespace(P2ImMessageReceiveV1Processor=FakeProcessor)
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_lark)
    monkeypatch.setitem(sys.modules, "lark_oapi.event", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "lark_oapi.event.dispatcher_handler", fake_dispatcher)
    monkeypatch.setattr("ostag.adapters.feishu.asyncio.to_thread", fake_to_thread)

    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        dry_run=False,
        feishu_app_id="cli_test",
        feishu_app_secret="secret_test",
    )

    await FeishuAdapter(config).start(runner=object())

    assert started == [True]
    assert threaded


@pytest.mark.asyncio
async def test_feishu_reply_includes_required_message_type(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    async def fake_token():
        return "tenant-token"

    async def fake_to_thread(func, req):
        captured["body"] = req.data
        return {"ok": True}

    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        dry_run=False,
        feishu_app_id="cli_test",
        feishu_app_secret="secret_test",
    )
    adapter = FeishuAdapter(config)
    monkeypatch.setattr(adapter, "_tenant_access_token", fake_token)
    monkeypatch.setattr("ostag.adapters.feishu.asyncio.to_thread", fake_to_thread)

    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="oc_demo",
        message_id="om_demo",
        sender_id="ou_demo",
        text="hello",
        mention_bot=True,
    )

    await adapter.reply(event, "live validation ok")

    payload = json.loads(captured["body"].decode())
    assert payload["msg_type"] == "text"
    assert json.loads(payload["content"]) == {"text": "live validation ok"}


@pytest.mark.asyncio
async def test_dingtalk_reply_uses_session_webhook(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    async def fake_to_thread(func, req):
        captured["url"] = req.full_url
        captured["body"] = req.data
        return {"errcode": 0}

    config = load_settings(None, audit_log=tmp_path / "audit.log", dry_run=False)
    adapter = DingTalkAdapter(config)
    monkeypatch.setattr("ostag.adapters.dingtalk.asyncio.to_thread", fake_to_thread)

    event = NormalizedEvent(
        platform="dingtalk",
        event_id="msg",
        chat_id="cid",
        message_id="msg",
        sender_id="staff",
        text="hello",
        mention_bot=True,
        raw={"sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=demo"},
    )

    await adapter.reply(event, "live validation ok")

    assert captured["url"].startswith("https://oapi.dingtalk.com/robot/sendBySession")
    assert json.loads(captured["body"].decode()) == {
        "msgtype": "text",
        "text": {"content": "live validation ok"},
    }


def test_dingtalk_signed_webhook_url_uses_timestamp_and_secret() -> None:
    url = signed_webhook_url(
        "https://oapi.dingtalk.com/robot/send?access_token=token",
        "secret",
        timestamp_ms=1700000000000,
    )

    assert "timestamp=1700000000000" in url
    assert "sign=" in url
