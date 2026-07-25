"""Escaped, mobile-first Telegram monitor messages."""

from __future__ import annotations

import html
from collections.abc import Callable

from .rendering import MobileBoardView, TaskDetailView


CallbackFactory = Callable[[str, str], str]


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_telegram_view(
    view: MobileBoardView | TaskDetailView, *, callback_factory: CallbackFactory | None = None
) -> tuple[str, dict[str, object] | None]:
    if isinstance(view, MobileBoardView):
        lines = ["<b>OStag 工作看板</b>"]
        for task in view.cards:
            lines.append(
                f"<b>{_escape(task['title'])}</b>\n"
                f"{_escape(task['status'])} · {int(task['progress'])}% · "
                f"<code>{_escape(task['task_id'])}</code>"
            )
        if not view.cards:
            lines.append("暂无任务")
        buttons: list[dict[str, str]] = []
        if view.page > 1:
            if callback_factory is not None:
                buttons.append({"text": "‹ 上一页", "callback_data": callback_factory(
                    "page", str(view.page - 1))})
        if view.page * view.page_size < view.total:
            if callback_factory is not None:
                buttons.append({"text": "下一页 ›", "callback_data": callback_factory(
                    "page", str(view.page + 1))})
        if any(len(button["callback_data"].encode()) > 64 for button in buttons):
            raise ValueError("Telegram callback_data must be a compact opaque ID")
        markup = {"inline_keyboard": [buttons]} if buttons else None
        return "\n\n".join(lines), markup
    task = view.task
    text = (
        f"<b>{_escape(task.get('title', ''))}</b>\n"
        f"{_escape(task.get('status', ''))} · {int(task.get('progress', 0))}%\n"
        f"任务 <code>{_escape(task.get('task_id', ''))}</code>"
    )
    rows = []
    if callback_factory is not None:
        for action in view.capabilities:
            opaque = callback_factory(action, str(task.get("task_id", "")))
            if not opaque or len(opaque.encode("utf-8")) > 64:
                raise ValueError("Telegram callback_data must be a compact opaque ID")
            rows.append([{"text": action.upper(), "callback_data": opaque}])
    return text, ({"inline_keyboard": rows} if rows else None)
