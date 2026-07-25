from datetime import UTC, date, datetime

import pytest

from ostag.core.ambient import RequirementSnapshot
from ostag.core.daily_digest import DailyDigestView


def _requirements() -> list[RequirementSnapshot]:
    return [
        RequirementSnapshot(
            "p0-overdue", "P0 overdue", "Alice", ("alice",), "开发交付中", "高 - P0",
            date(2026, 7, 10), 3,
        ),
        RequirementSnapshot(
            "overdue", "Overdue", "Bob", ("bob",), "方案设计中", "中 - P1",
            date(2026, 7, 12), 1,
        ),
        RequirementSnapshot(
            "p0-today", "P0 today", "Carol", ("carol",), "待处理", "高 - P0",
            date(2026, 7, 13), 0,
        ),
        RequirementSnapshot(
            "today", "Today", "Dan", ("dan",), "待处理", "中 - P1",
            date(2026, 7, 13), 0,
        ),
        RequirementSnapshot(
            "unowned", "Unowned", "", (), "待处理", "中 - P1", None, 0,
        ),
        RequirementSnapshot(
            "done-1", "Done 1", "Eve", ("eve",), "已完成", "中 - P1",
            date(2026, 7, 13), 0,
        ),
        RequirementSnapshot(
            "done-2", "Done 2", "Frank", ("frank",), "completed", "低 - P2",
            date(2026, 7, 13), 0,
        ),
        RequirementSnapshot(
            "done-old", "Done old", "Grace", ("grace",), "完成", "低 - P2",
            date(2026, 7, 12), 0,
        ),
    ]


def test_morning_board_computes_kpis_and_urgency_order() -> None:
    generated_at = datetime(2026, 7, 13, 1, 0, tzinfo=UTC)
    source_at = datetime(2026, 7, 13, 0, 59, tzinfo=UTC)

    view = DailyDigestView.build(
        "morning",
        _requirements(),
        today=date(2026, 7, 13),
        generated_at=generated_at,
        source_updated_at=source_at,
        table_url="https://example.test/tasks",
    )

    assert view.kpis == {
        "focus": 5,
        "overdue": 2,
        "high_priority": 2,
        "unowned": 1,
    }
    assert [item.record_id for item in view.priority_items] == [
        "p0-overdue", "overdue", "p0-today", "today", "unowned"
    ]
    assert view.generated_at == generated_at
    assert view.source_updated_at == source_at
    assert view.table_url == "https://example.test/tasks"


def test_evening_board_computes_completion_and_escalations() -> None:
    view = DailyDigestView.build(
        "evening", _requirements(), today=date(2026, 7, 13)
    )

    assert view.kpis == {
        "completed_today": 2,
        "completion_rate": 50,
        "open": 5,
        "escalation": 4,
    }
    assert [item.record_id for item in view.completed_items] == ["done-1", "done-2"]
    assert [item.record_id for item in view.open_items[:3]] == [
        "p0-overdue", "overdue", "p0-today"
    ]


def test_board_bounds_items_titles_and_empty_completion_rate() -> None:
    requirements = [
        RequirementSnapshot(
            f"item-{index}", "任" * 140, "", (), "待处理", "中 - P1", None, 0
        )
        for index in range(12)
    ]
    view = DailyDigestView.build(
        "morning", requirements, today=date(2026, 7, 13), item_limit=6
    )
    empty = DailyDigestView.build("evening", [], today=date(2026, 7, 13))

    assert len(view.priority_items) == 6
    assert view.overflow_count == 6
    assert len(view.priority_items[0].title) == 96
    assert view.priority_items[0].title.endswith("…")
    assert empty.kpis["completion_rate"] is None
    assert empty.priority_items == empty.completed_items == empty.open_items == ()


def test_board_rejects_invalid_kind_and_item_limit() -> None:
    with pytest.raises(ValueError, match="kind"):
        DailyDigestView.build("noon", [], today=date(2026, 7, 13))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="item_limit"):
        DailyDigestView.build("morning", [], today=date(2026, 7, 13), item_limit=0)
