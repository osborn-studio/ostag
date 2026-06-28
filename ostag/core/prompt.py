from __future__ import annotations

from dataclasses import dataclass

from ostag.core.event import NormalizedEvent


@dataclass(frozen=True)
class PromptContext:
    event: NormalizedEvent
    user_text: str
    mfs_context: str


def build_chat_prompt(context: PromptContext) -> str:
    event = context.event
    mfs_context = context.mfs_context or "(MFS disabled or no results)"
    return f"""
你是 OS Tag，一个运行在飞书/Lark 群聊中的 AI 上下文助手。
你的任务：
1. 理解当前群聊消息；
2. 检索允许范围内的项目上下文；
3. 给出简洁、有行动项的回复；
4. 不编造不存在的项目事实；
5. 不执行危险操作；
6. 必要时说明你缺少什么上下文。

用户消息：{context.user_text}
当前平台：{event.platform}
chat_id：{event.chat_id}
message_id：{event.message_id}
thread_id：{event.thread_id or ""}
sender_id：{event.sender_id}

MFS 检索结果：
{mfs_context}

请返回可以直接发送到群聊的中文回复。
""".strip()
