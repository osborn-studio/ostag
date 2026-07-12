"""Mobile-first Feishu card rendering for the work monitor."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from .rendering import MobileBoardView, TaskDetailView


MAX_CARD_BYTES = 24_000
MAX_ELEMENTS = 20
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+.!|>~-])")


def _safe(value: object, limit: int) -> str:
    text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped = _MARKDOWN.sub(r"\\\1", text)
    return escaped if len(escaped) <= limit else escaped[: limit - 1] + "…"


def render_feishu_card(
    view: MobileBoardView | TaskDetailView,
    *, callback_factory: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    if isinstance(view, MobileBoardView):
        elements: list[dict[str, Any]] = []
        for task in view.cards[: min(6, MAX_ELEMENTS // 2)]:
            elements.extend((
                {"tag": "markdown", "content": f"**{_safe(task['title'], 160)}**\n"
                 f"`{_safe(task['status'], 24)}` · **{int(task['progress'])}%**\n"
                 f"{_safe(task['task_id'], 80)}"},
                {"tag": "hr"},
            ))
        return _card("OStag 工作看板", elements)
    task = view.task
    elements = [
        {"tag": "markdown", "content": f"**{_safe(task.get('title'), 240)}**\n"
         f"`{_safe(task.get('status'), 24)}` · **{int(task.get('progress', 0))}%**\n"
         f"任务 `{_safe(task.get('task_id'), 80)}`"}
    ]
    for action in view.capabilities:
        if callback_factory is None:
            continue
        callback_id = callback_factory(action, str(task.get("task_id", "")))
        if not callback_id or len(callback_id.encode()) > 64:
            raise ValueError("Feishu callback ID must be compact and opaque")
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button", "text": {"tag": "plain_text", "content": action.upper()},
                "type": "danger" if action == "cancel" else "primary",
                "value": {"callback_id": callback_id},
            }],
        })
    return _card("OStag 任务详情", elements)


def _card(title: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    card = {
        "config": {"wide_screen_mode": False, "enable_forward": False},
        "header": {"template": "indigo", "title": {"tag": "plain_text", "content": title}},
        "elements": elements[:MAX_ELEMENTS],
    }
    if len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_CARD_BYTES:
        raise ValueError("Feishu card exceeds safe encoded size")
    return card
