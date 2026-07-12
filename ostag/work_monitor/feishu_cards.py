"""Mobile-first Feishu card rendering for the work monitor."""

from __future__ import annotations

from typing import Any

from .rendering import MobileBoardView, TaskDetailView


def render_feishu_card(view: MobileBoardView | TaskDetailView) -> dict[str, Any]:
    if isinstance(view, MobileBoardView):
        elements: list[dict[str, Any]] = []
        for task in view.cards:
            elements.extend((
                {"tag": "markdown", "content": f"**{task['title']}**\n`{task['status']}` · **{task['progress']}%**\n{task['task_id']}"},
                {"tag": "hr"},
            ))
        return _card("OStag 工作看板", elements)
    task = view.task
    elements = [
        {"tag": "markdown", "content": f"**{task.get('title')}**\n`{task.get('status')}` · **{task.get('progress', 0)}%**\n任务 `{task.get('task_id')}`"}
    ]
    for action in view.capabilities:
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button", "text": {"tag": "plain_text", "content": action.upper()},
                "type": "danger" if action == "cancel" else "primary",
                "value": {"command": action, "task_id": str(task.get("task_id", ""))},
            }],
        })
    return _card("OStag 任务详情", elements)


def _card(title: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": False, "enable_forward": False},
        "header": {"template": "indigo", "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }
