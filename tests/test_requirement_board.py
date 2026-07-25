from datetime import date

from ostag.core.requirement_board import (
    RequirementSnapshot,
    analyze_daily_requirement_board,
    format_daily_requirement_board_text,
)


TODAY = date(2026, 7, 15)


def item(record_id, title, *, status, priority="中 - P1", owner="", owner_ids=(), due=None,
         proposed=None):
    return RequirementSnapshot(
        record_id=record_id,
        title=title,
        status=status,
        priority=priority,
        owner=owner,
        owner_ids=owner_ids,
        due_date=due,
        proposed_date=proposed,
    )


def test_daily_board_metrics_focus_order_and_mentions():
    board = analyze_daily_requirement_board(
        [
            item("r1", "逾期 P0", status="验收中", priority="高 - P0", owner="葛德彬",
                 owner_ids=("ou_ge",), due=date(2026, 6, 26)),
            item("r2", "合规", status="待评估", priority="高 - P0",
                 proposed=date(2026, 7, 1)),
            item("r3", "运营", status="待评估", priority="高 - P0",
                 proposed=date(2026, 7, 2)),
            item("r4", "今日到期", status="验收中", priority="高 - P0", owner="普通成员",
                 owner_ids=("ou_normal",), due=TODAY),
            item("r6", "普通无负责人", status="开发交付中"),
            item("r5", "完成", status="已完成", priority="高 - P0"),
        ],
        today=TODAY,
    )

    assert (board.open_count, board.p0_count, board.overdue_count, board.unowned_count) == (5, 4, 1, 3)
    assert [focus.kind for focus in board.focus_items] == [
        "p0_overdue", "p0_unowned_group", "due_today"
    ]
    assert board.focus_items[0].mention_ids == ("ou_ge",)
    assert board.focus_items[1].mention_ids == ()
    assert board.status_counts == {"待评估": 2, "方案设计中": 0, "开发交付中": 1, "验收中": 2}


def test_daily_board_groups_unowned_and_is_deterministic():
    tasks = [
        item(f"r{i}", f"{'很长的需求标题' * 8}{i}", status="待评估", priority="高-P0",
             proposed=date(2026, 7, i))
        for i in range(1, 6)
    ]
    board = analyze_daily_requirement_board(tasks, today=TODAY)

    assert len(board.focus_items) == 1
    assert board.focus_items[0].kind == "p0_unowned_group"
    assert "共 5 项" in board.focus_items[0].detail
    assert "r5" not in board.focus_items[0].detail
    assert len(board.focus_items[0].title) <= 48
    assert "只读巡检" in format_daily_requirement_board_text(board)


def test_daily_board_handles_empty_unknown_status_and_other_overdue():
    empty = analyze_daily_requirement_board([], today=TODAY)
    assert empty.open_count == 0
    assert "当前没有未完成需求" in empty.recommendation

    board = analyze_daily_requirement_board(
        [item("r1", "普通逾期", status="未知状态", owner="普通成员",
              owner_ids=("ou_normal",), due=date(2026, 7, 10))],
        today=TODAY,
    )
    assert board.unknown_status_count == 1
    assert board.focus_items[0].kind == "overdue"
    assert board.focus_items[0].mention_ids == ()
