from __future__ import annotations

import re
from urllib.parse import urlparse

from ostag.core.requirement_board import DailyRequirementBoard, FLOW_STATUSES


OPEN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def render_requirement_board_card(
    board: DailyRequirementBoard, *, group_name: str, base_url: str
) -> dict:
    parsed_url = urlparse(base_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError("daily requirement board URL must be an absolute HTTPS URL")
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
            mentions = " ".join(
                f"<at id={user_id}></at>"
                for user_id in item.mention_ids
                if OPEN_ID_RE.fullmatch(user_id)
            )
            owner = f"\n负责人：{mentions or _safe_markdown(item.owner)}"
        focus_lines.append(
            f"**{index}. {_safe_markdown(item.title)}**\n"
            f"{_safe_markdown(item.detail)}{owner}"
        )
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
                {"tag": "button", "text": {"tag": "plain_text", "content": "打开需求表"}, "type": "primary", "behaviors": [{"type": "open_url", "default_url": base_url}]},
                {"tag": "button", "text": {"tag": "plain_text", "content": "查看全部风险"}, "type": "default", "behaviors": [{"type": "open_url", "default_url": base_url}]},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": quality}]},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": "只读巡检｜每天 12:00（北京时间）推送一次｜今日仅发送 1 次"}]},
            ]
        },
    }


def _safe_markdown(value: str) -> str:
    """Neutralize tags and markdown controls from Base-provided text."""
    text = value.replace("\\", "\\\\").replace("<", "‹").replace(">", "›")
    return re.sub(r"([*_~`\[\]()#])", r"\\\1", text)
