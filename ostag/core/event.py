from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Platform = Literal["slack", "feishu", "lark", "dingtalk"]


@dataclass(frozen=True)
class NormalizedEvent:
    platform: Platform
    event_id: str
    chat_id: str
    message_id: str
    sender_id: str
    text: str
    mention_bot: bool
    thread_id: str | None = None
    raw: dict[str, Any] | None = None
