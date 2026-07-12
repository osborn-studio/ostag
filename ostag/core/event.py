from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .identity import Principal

Platform = Literal["slack", "feishu", "lark", "dingtalk", "telegram"]
ChatType = Literal["private", "group"]


@dataclass(frozen=True)
class NormalizedEvent:
    platform: Platform
    event_id: str
    chat_id: str
    message_id: str
    sender_id: str
    text: str
    mention_bot: bool
    chat_type: ChatType
    destination_id: str
    thread_id: str | None = None
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.chat_type not in ("private", "group"):
            raise ValueError("chat_type must be private or group")
        if not self.destination_id:
            raise ValueError("destination_id is required")

    @property
    def principal(self) -> Principal:
        return Principal(self.platform, self.sender_id)
