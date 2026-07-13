"""Platform-neutral view model for morning and evening task boards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Iterable, Literal

from .ambient import DONE_STATUSES, RequirementSnapshot


DigestKind = Literal["morning", "evening"]


@dataclass(frozen=True)
class DailyDigestItem:
    record_id: str
    title: str
    owner: str
    owner_ids: tuple[str, ...]
    status: str
    priority: str
    due_date: date | None
    days_overdue: int
    unowned: bool


@dataclass(frozen=True)
class DailyDigestView:
    kind: DigestKind
    today: date
    generated_at: datetime
    source_updated_at: datetime | None
    table_url: str
    kpis: dict[str, int | None]
    priority_items: tuple[DailyDigestItem, ...]
    completed_items: tuple[DailyDigestItem, ...]
    open_items: tuple[DailyDigestItem, ...]
    overflow_count: int

    @classmethod
    def build(
        cls,
        kind: DigestKind,
        requirements: Iterable[RequirementSnapshot],
        *,
        today: date,
        generated_at: datetime | None = None,
        source_updated_at: datetime | None = None,
        table_url: str = "",
        item_limit: int = 6,
    ) -> DailyDigestView:
        if kind not in {"morning", "evening"}:
            raise ValueError("kind must be morning or evening")
        if not 1 <= item_limit <= 10:
            raise ValueError("item_limit must be in 1..10")

        snapshots = tuple(requirements)
        open_items = tuple(item for item in snapshots if not _is_done(item))
        completed_today = tuple(
            item for item in snapshots if _is_done(item) and item.due_date == today
        )
        projected_open = tuple(_item(item, today) for item in open_items)
        projected_completed = tuple(_item(item, today) for item in completed_today)
        morning_focus = tuple(
            item for item in projected_open if item.due_date is None or item.due_date <= today
        )
        escalations = tuple(
            item
            for item in projected_open
            if item.days_overdue > 0 or _is_high(item.priority) or item.unowned
        )

        if kind == "morning":
            ordered_priority = tuple(sorted(morning_focus, key=_morning_key))
            kpis: dict[str, int | None] = {
                "focus": len(morning_focus),
                "overdue": sum(item.days_overdue > 0 for item in projected_open),
                "high_priority": sum(_is_high(item.priority) for item in projected_open),
                "unowned": sum(item.unowned for item in projected_open),
            }
        else:
            ordered_priority = tuple(sorted(escalations, key=_evening_key))
            due_today_open = sum(item.due_date == today for item in projected_open)
            denominator = len(projected_completed) + due_today_open
            completion_rate = (
                round(len(projected_completed) * 100 / denominator) if denominator else None
            )
            kpis = {
                "completed_today": len(projected_completed),
                "completion_rate": completion_rate,
                "open": len(projected_open),
                "escalation": len(escalations),
            }

        ordered_open = tuple(sorted(projected_open, key=_evening_key))
        visible_priority = ordered_priority[:item_limit]
        return cls(
            kind=kind,
            today=today,
            generated_at=generated_at or datetime.now(UTC),
            source_updated_at=source_updated_at,
            table_url=table_url,
            kpis=kpis,
            priority_items=visible_priority,
            completed_items=projected_completed[:item_limit],
            open_items=ordered_open[:item_limit],
            overflow_count=max(0, len(ordered_priority) - len(visible_priority)),
        )


def _item(requirement: RequirementSnapshot, today: date) -> DailyDigestItem:
    title = requirement.title or requirement.record_id
    if len(title) > 96:
        title = title[:95] + "…"
    return DailyDigestItem(
        record_id=requirement.record_id,
        title=title,
        owner=requirement.owner,
        owner_ids=tuple(requirement.owner_ids),
        status=requirement.status,
        priority=requirement.priority,
        due_date=requirement.due_date,
        days_overdue=(today - requirement.due_date).days
        if requirement.due_date and requirement.due_date < today
        else 0,
        unowned=not bool(requirement.owner.strip()),
    )


def _is_done(requirement: RequirementSnapshot) -> bool:
    return requirement.status.strip().casefold() in {
        status.casefold() for status in DONE_STATUSES
    }


def _is_high(priority: str) -> bool:
    value = priority.strip().casefold()
    return "p0" in value or "高" in value or value == "high"


def _morning_key(item: DailyDigestItem) -> tuple[int, date, str]:
    high = _is_high(item.priority)
    rank = (
        0
        if item.days_overdue and high
        else 1
        if item.days_overdue
        else 2
        if high and item.due_date is not None
        else 3
        if item.due_date is not None
        else 4
    )
    return rank, item.due_date or date.max, item.record_id


def _evening_key(item: DailyDigestItem) -> tuple[int, int, date, str]:
    rank = 0 if item.days_overdue else 1 if _is_high(item.priority) else 2 if item.unowned else 3
    return rank, -item.days_overdue, item.due_date or date.max, item.record_id
