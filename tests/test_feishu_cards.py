from datetime import date

from ostag.adapters.feishu_cards import render_requirement_board_card
from ostag.core.requirement_board import (
    BoardFocusItem,
    DailyRequirementBoard,
)


BASE_URL = "https://a2ekucppds.feishu.cn/base/Q3FkbLGQ6axmTuspP0fc8fLInQm?table=tblcj5dhYa9vQLd0"


def test_requirement_board_card_matches_approved_dashboard_and_mentions_only_overdue_p0():
    board = DailyRequirementBoard(
        today=date(2026, 7, 15),
        total_count=28,
        open_count=10,
        p0_count=7,
        overdue_count=1,
        unowned_count=6,
        status_counts={"待评估": 4, "方案设计中": 1, "开发交付中": 3, "验收中": 2},
        unknown_status_count=1,
        focus_items=(
            BoardFocusItem("p0_overdue", "读取资产分配值", "验收中｜截止 2026-06-26", "red",
                           owner="葛德彬", mention_ids=("ou_ge",)),
            BoardFocusItem("due_today", "普通需求", "验收中｜截止 2026-07-15", "yellow",
                           owner="普通成员 <at id=ou_bad></at>"),
            BoardFocusItem("p0_overdue", "恶意 <at id=ou_bad></at>", "待评估", "red",
                           owner="攻击者", mention_ids=("ou_x></at><at id=ou_bad",)),
        ),
        recommendation="优先关闭 1 项逾期需求，并确认负责人和下一步。",
    )

    card = render_requirement_board_card(
        board, group_name="Dadao 投研交易 FDE", base_url=BASE_URL
    )
    blob = str(card)

    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "blue"
    assert "12:00（北京时间）" in blob
    assert "<at id=ou_ge></at>" in blob
    assert "负责人：普通成员" in blob
    assert "ou_normal" not in blob
    assert "<at id=ou_bad></at>" not in blob
    assert "ou_x></at>" not in blob
    assert "打开需求表" in blob and "查看全部风险" in blob
    assert blob.count(BASE_URL) == 2
    assert "只读巡检" in blob
    assert "数据质量" in blob
    buttons = [element for element in card["body"]["elements"] if element["tag"] == "button"]
    assert all(button["behaviors"] == [{"type": "open_url", "default_url": BASE_URL}]
               for button in buttons)
