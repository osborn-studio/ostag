from __future__ import annotations

from ostag.core.event import NormalizedEvent
from ostag.core.prompt import PromptContext, build_chat_prompt


def test_prompt_contract_contains_runtime_fields() -> None:
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="chat",
        message_id="msg",
        sender_id="sender",
        text="raw",
        mention_bot=True,
        chat_type="group",
        destination_id="chat",
        thread_id="thread",
    )

    prompt = build_chat_prompt(
        PromptContext(event=event, user_text="请总结项目下一步", mfs_context="Roadmap")
    )

    assert "你是 OS Tag" in prompt
    assert "用户消息：请总结项目下一步" in prompt
    assert "当前平台：feishu" in prompt
    assert "chat_id：chat" in prompt
    assert "message_id：msg" in prompt
    assert "thread_id：thread" in prompt
    assert "sender_id：sender" in prompt
    assert "MFS 检索结果：\nRoadmap" in prompt
    assert "不执行危险操作" in prompt


def test_prompt_contract_uses_explicit_empty_mfs_placeholder() -> None:
    event = NormalizedEvent(
        platform="lark",
        event_id="evt",
        chat_id="chat",
        message_id="msg",
        sender_id="sender",
        text="raw",
        mention_bot=True,
        chat_type="group",
        destination_id="chat",
    )

    prompt = build_chat_prompt(PromptContext(event=event, user_text="hello", mfs_context=""))

    assert "MFS disabled or no results" in prompt
    assert "thread_id：" in prompt
