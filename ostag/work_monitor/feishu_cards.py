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
        return _board_card(view)
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
    return _card("OStag Control · 任务详情", elements)


def _board_card(view: MobileBoardView) -> dict[str, Any]:
    cards = tuple(view.cards)
    attention = view.attention or tuple(task for task in cards if task.get("status") == "blocked")
    running = view.running_tasks or tuple(task for task in cards if task.get("status") == "running")
    counts = dict(view.stage_counts) or {
        status: sum(task.get("status") == status for task in cards)
        for status in ("triage", "todo", "ready", "running", "blocked", "done")
    }
    kpis = dict(view.kpis) or {
        "running": counts["running"],
        "blocked": counts["blocked"],
        "attention": len(attention),
        "done": counts["done"],
    }
    health = "🔴 需要处理" if attention else "🟢 运行健康"
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**{health}**\n共 **{view.total}** 项工作 · 关键状态实时汇总",
        },
        _columns(
            ("🔵 运行中", kpis["running"]),
            ("🔴 阻塞", kpis["blocked"]),
            ("🟠 待处理", kpis["attention"]),
            ("🟢 已完成", kpis["done"]),
        ),
        {"tag": "markdown", "content": "**需要你处理**"},
    ]
    if attention:
        elements.extend(_task_module(task, danger=True) for task in attention[:3])
    else:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "当前没有阻塞事项"}]})
    elements.append({"tag": "markdown", "content": "**运行中的任务**"})
    if running:
        elements.extend(_task_module(task) for task in running[:4])
    else:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "当前没有运行中的任务"}]})
    elements.extend((
        {"tag": "markdown", "content": "**六阶段概览**"},
        _columns(
            ("收件箱", counts["triage"]),
            ("待办", counts["todo"]),
            ("就绪", counts["ready"]),
        ),
        _columns(
            ("🔵 运行", counts["running"]),
            ("🔴 阻塞", counts["blocked"]),
            ("🟢 完成", counts["done"]),
        ),
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"第 {view.page} 页 · OStag 中央监控"}],
        },
    ))
    return _card("OStag Control · 今日看板", elements)


def _columns(*items: tuple[str, int]) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [{"tag": "markdown", "content": f"{_safe(label, 32)}\n**{int(value)}**"}],
            }
            for label, value in items
        ],
    }


def _task_module(task: dict[str, Any], *, danger: bool = False) -> dict[str, Any]:
    marker = "🔴" if danger else "🔵"
    progress = max(0, min(100, int(task.get("progress", 0))))
    context = " · ".join(
        value
        for value in (
            _safe(task.get("project", ""), 40),
            _safe(task.get("agent", ""), 32),
            _safe(task.get("instance_id", ""), 48),
        )
        if value
    )
    summary = _safe(task.get("summary", ""), 120)
    lines = [
        f"{marker} **{_safe(task.get('title', ''), 160)}**",
        f"{_safe(task.get('status', ''), 24)} · **{progress}%** · `{_safe(task.get('task_id', ''), 80)}`",
    ]
    if summary:
        lines.append(summary)
    if context:
        lines.append(context)
    return {"tag": "markdown", "content": "\n".join(lines)}


def _card(title: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    card = {
        "config": {"wide_screen_mode": False, "enable_forward": False},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
        "elements": elements[:MAX_ELEMENTS],
    }
    if len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_CARD_BYTES:
        raise ValueError("Feishu card exceeds safe encoded size")
    return card
