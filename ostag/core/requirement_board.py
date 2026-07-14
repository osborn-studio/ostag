from __future__ import annotations

from dataclasses import dataclass
from datetime import date


DONE_STATUSES = {"已完成", "完成", "done", "completed"}
FLOW_STATUSES = ("待评估", "方案设计中", "开发交付中", "验收中")


@dataclass(frozen=True)
class RequirementSnapshot:
    record_id: str
    title: str
    owner: str = ""
    owner_ids: tuple[str, ...] = ()
    status: str = ""
    priority: str = ""
    due_date: date | None = None
    proposed_date: date | None = None
    days_stale: int = 0


@dataclass(frozen=True)
class BoardFocusItem:
    kind: str
    title: str
    detail: str
    tone: str
    owner: str = ""
    mention_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DailyRequirementBoard:
    today: date
    total_count: int
    open_count: int
    p0_count: int
    overdue_count: int
    unowned_count: int
    status_counts: dict[str, int]
    unknown_status_count: int
    focus_items: tuple[BoardFocusItem, ...]
    recommendation: str


def analyze_daily_requirement_board(
    requirements: list[RequirementSnapshot], *, today: date
) -> DailyRequirementBoard:
    open_items = [item for item in requirements if not _is_done(item)]
    p0_items = [item for item in open_items if _priority_rank(item.priority) == 0]
    overdue = [item for item in open_items if item.due_date and item.due_date < today]
    unowned = [item for item in open_items if not item.owner.strip()]
    status_counts = {status: 0 for status in FLOW_STATUSES}
    for item in open_items:
        if item.status in status_counts:
            status_counts[item.status] += 1
    unknown_status_count = sum(item.status not in status_counts for item in open_items)

    focuses: list[BoardFocusItem] = []
    selected: set[str] = set()
    for item in sorted(
        (item for item in overdue if _priority_rank(item.priority) == 0),
        key=lambda item: (item.due_date or date.max, item.record_id),
    ):
        focuses.append(_focus(item, "p0_overdue", "red", mention=True))
        selected.add(item.record_id)
        if len(focuses) == 3:
            break

    remaining_unowned = [
        item for item in p0_items if not item.owner.strip() and item.record_id not in selected
    ]
    if remaining_unowned and len(focuses) < 3:
        ordered = sorted(
            remaining_unowned,
            key=lambda item: (item.proposed_date or date.max, item.record_id),
        )
        titles = "、".join(_short(item.title or item.record_id, 22) for item in ordered[:4])
        focuses.append(
            BoardFocusItem(
                kind="p0_unowned_group",
                title=_short("P0 待认领", 48),
                detail=f"共 {len(ordered)} 项：{titles}",
                tone="orange",
            )
        )
        selected.update(item.record_id for item in ordered)

    due_today = sorted(
        (item for item in open_items if item.due_date == today and item.record_id not in selected),
        key=lambda item: (_priority_rank(item.priority), item.record_id),
    )
    for item in due_today:
        if len(focuses) == 3:
            break
        focuses.append(_focus(item, "due_today", "yellow"))
        selected.add(item.record_id)

    for item in sorted(overdue, key=lambda item: (item.due_date or date.max, item.record_id)):
        if len(focuses) == 3:
            break
        if item.record_id not in selected:
            focuses.append(_focus(item, "overdue", "orange"))
            selected.add(item.record_id)

    recommendation = _recommendation(open_items, p0_items, overdue, unowned)
    return DailyRequirementBoard(
        today=today,
        total_count=len(requirements),
        open_count=len(open_items),
        p0_count=len(p0_items),
        overdue_count=len(overdue),
        unowned_count=len(unowned),
        status_counts=status_counts,
        unknown_status_count=unknown_status_count,
        focus_items=tuple(focuses),
        recommendation=recommendation,
    )


def format_daily_requirement_board_text(board: DailyRequirementBoard) -> str:
    flow = " / ".join(f"{name} {board.status_counts[name]}" for name in FLOW_STATUSES)
    lines = [
        f"📋 OStag 每日需求看板 · {board.today.isoformat()} 12:00（北京时间）",
        f"未完成 {board.open_count}｜P0 {board.p0_count}｜已逾期 {board.overdue_count}｜无负责人 {board.unowned_count}",
        flow,
    ]
    for item in board.focus_items:
        owner = f"；负责人：{item.owner}" if item.owner else ""
        lines.append(f"- {item.title}：{item.detail}{owner}")
    lines.extend((f"OStag 建议：{board.recommendation}", "只读巡检｜每天 12:00（北京时间）推送一次"))
    return "\n".join(lines)


def _focus(item: RequirementSnapshot, kind: str, tone: str, *, mention: bool = False) -> BoardFocusItem:
    due = item.due_date.isoformat() if item.due_date else "未设置截止日"
    return BoardFocusItem(
        kind=kind,
        title=_short(item.title or item.record_id, 48),
        detail=f"{item.status or '未设置状态'}｜截止 {due}",
        tone=tone,
        owner=item.owner,
        mention_ids=item.owner_ids if mention else (),
    )


def _recommendation(open_items, p0_items, overdue, unowned) -> str:
    if not open_items:
        return "当前没有未完成需求，继续保持需求表及时更新。"
    if overdue:
        return f"优先关闭 {len(overdue)} 项逾期需求，并确认负责人和下一步。"
    if unowned:
        return f"先为 {len(unowned)} 项无负责人需求完成认领。"
    if p0_items:
        return f"集中推进 {len(p0_items)} 项 P0，并在今日更新进展。"
    return "按当前优先级推进，并及时维护状态与截止日期。"


def _priority_rank(priority: str) -> int:
    value = priority.upper().replace(" ", "")
    if "P0" in value or value == "高":
        return 0
    if "P1" in value or value == "中":
        return 1
    if "P2" in value or value == "低":
        return 2
    return 3


def _is_done(item: RequirementSnapshot) -> bool:
    return item.status.strip().lower() in DONE_STATUSES


def _short(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
