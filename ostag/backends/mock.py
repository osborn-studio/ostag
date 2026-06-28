from __future__ import annotations

import re

from ostag.config import Settings


class MockBackend:
    def __init__(self, config: Settings):
        self.config = config

    async def run(self, prompt: str) -> str:
        message = _extract(prompt, "用户消息") or _extract(prompt, "User message") or prompt
        platform = _extract(prompt, "当前平台") or "unknown"
        chat_id = _extract(prompt, "chat_id") or "unknown"
        return (
            "OS Tag mock reply:\n"
            "我已经收到你的消息：\n"
            f"{message}\n\n"
            f"当前平台：{platform}\n"
            f"当前频道：{chat_id}"
        )


def _extract(prompt: str, label: str) -> str:
    pattern = re.compile(rf"^{re.escape(label)}[:：]\s*(.*)$", re.MULTILINE)
    match = pattern.search(prompt)
    return match.group(1).strip() if match else ""
