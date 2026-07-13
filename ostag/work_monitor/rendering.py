"""Platform-neutral mobile monitor views."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


_PRIORITY = {"blocked": 0, "running": 1, "ready": 2, "todo": 3, "triage": 4, "done": 5}


def _title(value: object, limit: int = 48) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class MobileBoardView:
    cards: tuple[dict[str, Any], ...]
    page: int
    page_size: int
    total: int
    stage_counts: dict[str, int] = field(default_factory=dict)
    kpis: dict[str, int] = field(default_factory=dict)
    attention: tuple[dict[str, Any], ...] = ()
    running_tasks: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_tasks(
        cls, tasks: Iterable[Mapping[str, Any]], *, page: int = 1, page_size: int = 6
    ) -> MobileBoardView:
        if page < 1 or page_size < 1:
            raise ValueError("page and page_size must be positive")
        ordered = sorted(
            tasks,
            key=lambda task: (_PRIORITY.get(str(task.get("status")), 99), str(task.get("task_id"))),
        )
        start = (page - 1) * page_size
        projected = tuple(_board_task(task) for task in ordered)
        stage_counts = {status: 0 for status in _PRIORITY}
        for task in projected:
            status = task["status"]
            if status in stage_counts:
                stage_counts[status] += 1
        attention = tuple(task for task in projected if task["status"] == "blocked")
        running_tasks = tuple(task for task in projected if task["status"] == "running")
        return cls(
            cards=projected[start : start + page_size],
            page=page,
            page_size=page_size,
            total=len(ordered),
            stage_counts=stage_counts,
            kpis={
                "running": stage_counts["running"],
                "blocked": stage_counts["blocked"],
                "attention": len(attention),
                "done": stage_counts["done"],
            },
            attention=attention,
            running_tasks=running_tasks,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "mobile_board",
            "cards": [dict(card) for card in self.cards],
            "stage_counts": dict(self.stage_counts),
            "kpis": dict(self.kpis),
            "attention": [dict(task) for task in self.attention],
            "running_tasks": [dict(task) for task in self.running_tasks],
            "pagination": {
                "page": self.page,
                "page_size": self.page_size,
                "total": self.total,
                "has_more": self.page * self.page_size < self.total,
            },
        }

    def to_text(self) -> str:
        lines = ["OStag 工作看板"]
        lines.extend(
            f"[{card['status']}] {card['title']} · {card['progress']}% · {card['task_id']}"
            for card in self.cards
        )
        if self.page * self.page_size < self.total:
            lines.append(f"第 {self.page} 页，发送 /board {self.page + 1} 查看更多")
        return "\n".join(lines)


def _board_task(task: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(task["task_id"]),
        "title": _title(task.get("title", "")),
        "status": str(task.get("status", "")),
        "progress": int(task.get("progress", 0)),
        "instance_id": str(task.get("instance_id", "")),
        "agent": str(task.get("agent", "")),
        "project": str(task.get("project", "")),
        "summary": _title(task.get("summary", ""), limit=120),
        "duration_seconds": int(task.get("duration_seconds", 0)),
    }


@dataclass(frozen=True)
class TaskDetailView:
    task: dict[str, Any]
    capabilities: tuple[str, ...]

    @classmethod
    def from_task(
        cls, task: Mapping[str, Any], *, capabilities: Iterable[str] = ()
    ) -> TaskDetailView:
        supported = ("pause", "resume", "retry", "cancel")
        allowed = tuple(action for action in supported if action in set(capabilities))
        item = dict(task)
        item["title"] = _title(item.get("title", ""), limit=72)
        return cls(task=item, capabilities=allowed)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "task_detail", "task": dict(self.task), "actions": list(self.capabilities)}

    def to_text(self) -> str:
        actions = " / ".join(self.capabilities) or "无远程操作"
        return (
            f"[{self.task.get('status')}] {self.task.get('title')}\n"
            f"任务：{self.task.get('task_id')} · 进度 {self.task.get('progress', 0)}%\n"
            f"操作：{actions}"
        )
