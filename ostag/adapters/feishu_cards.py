from __future__ import annotations

from ostag.core.requirement_board import DailyRequirementBoard, FLOW_STATUSES


def render_requirement_board_card(
    board: DailyRequirementBoard, *, group_name: str, base_url: str
) -> dict:
    metrics = (
        f"**未完成**  {board.open_count}　　**P0**  {board.p0_count}　　"
        f"**已逾期**  {board.overdue_count}　　**无负责人**  {board.unowned_count}"
    )
    blocks = ("🟦", "🟪", "🟧", "🟩")
    flow_bar = "".join(
        blocks[index] * min(max(board.status_counts.get(status, 0), 1), 8)
        for index, status in enumerate(FLOW_STATUSES)
    )
    flow_labels = "　".join(
        f"{status} {board.status_counts.get(status, 0)}" for status in FLOW_STATUSES
    )
    focus_lines = []
    for index, item in enumerate(board.focus_items, start=1):
        owner = ""
        if item.owner:
            mentions = " ".join(f"<at id={user_id}></at>" for user_id in item.mention_ids)
            owner = f"\n负责人：{mentions or item.owner}"
        focus_lines.append(f"**{index}. {item.title}**\n{item.detail}{owner}")
    focus = "\n\n".join(focus_lines) or "✅ 当前没有需要重点提示的风险项。"
    quality = (
        f"数据质量：另有 {board.unknown_status_count} 项状态未纳入流程分段，请及时补齐。"
        if board.unknown_status_count
        else "数据质量：当前未发现未知状态。"
    )
    subtitle = f"{group_name} · {board.today.isoformat()} · 12:00（北京时间）"
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "📋 OStag 每日需求看板"},
            "subtitle": {"tag": "plain_text", "content": subtitle},
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": metrics},
                {"tag": "hr"},
                {"tag": "markdown", "content": f"**需求流转**\n{flow_bar}\n{flow_labels}"},
                {"tag": "markdown", "content": f"**今日重点**\n{focus}"},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": f"OStag 建议：{board.recommendation}"}]},
                {"tag": "button", "text": {"tag": "plain_text", "content": "打开需求表"}, "type": "primary", "url": base_url},
                {"tag": "button", "text": {"tag": "plain_text", "content": "查看全部风险"}, "type": "default", "url": base_url},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": quality}]},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": "只读巡检｜每天 12:00（北京时间）推送一次｜今日仅发送 1 次"}]},
            ]
        },
    }
