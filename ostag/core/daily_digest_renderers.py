"""Native Feishu and DingTalk renderers for daily task boards."""

from __future__ import annotations

import html
import json
import re
from typing import Any, Iterable
from urllib.parse import urlparse

from .daily_digest import DailyDigestItem, DailyDigestView


MAX_FEISHU_CARD_BYTES = 24_000
MAX_FEISHU_ELEMENTS = 20
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+.!|>~-])")


def _safe(value: object, limit: int = 160) -> str:
    text = html.escape(str(value), quote=True)
    escaped = _MARKDOWN.sub(r"\\\1", text)
    return escaped if len(escaped) <= limit else escaped[: limit - 1] + "…"


def _safe_url(value: str) -> str:
    parsed = urlparse(value.strip())
    return value.strip() if parsed.scheme in {"https", "http"} and parsed.netloc else ""


def render_daily_feishu_card(view: DailyDigestView) -> dict[str, Any]:
    morning = view.kind == "morning"
    title = "OS Tag · 今日作战看板" if morning else "OS Tag · 今日复盘看板"
    freshness = (
        view.source_updated_at.astimezone().strftime("%H:%M")
        if view.source_updated_at
        else view.generated_at.astimezone().strftime("%H:%M")
    )
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**{view.today.strftime('%m月%d日')} · 数据已同步**\n更新时间 {freshness}",
        },
        _feishu_kpis(view),
    ]
    if morning:
        elements.append({"tag": "markdown", "content": "**需要优先处理**"})
        if view.priority_items:
            elements.append(_feishu_item(view.priority_items[0], danger=True))
        else:
            elements.append(_feishu_empty("当前没有需要推进的重点任务"))
        elements.append({"tag": "markdown", "content": "**今日推进**"})
        elements.extend(_feishu_item(item) for item in view.priority_items[1:6])
        if view.overflow_count:
            elements.append(_feishu_note(f"另有 {view.overflow_count} 项重点任务"))
    else:
        elements.append({"tag": "markdown", "content": "**今日完成**"})
        if view.completed_items:
            elements.extend(_feishu_item(item, completed=True) for item in view.completed_items)
        else:
            elements.append(_feishu_empty("今天暂未记录已完成任务"))
        elements.append({"tag": "markdown", "content": "**仍未完成**"})
        if view.open_items:
            elements.extend(_feishu_item(item, danger=item.days_overdue > 0) for item in view.open_items)
        else:
            elements.append(_feishu_empty("当前没有未完成任务"))
        elements.append(
            _feishu_note(
                f"明日关注：{int(view.kpis['escalation'] or 0)} 项需要升级处理"
            )
        )
    url = _safe_url(view.table_url)
    if url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "打开任务表" if morning else "更新任务进展",
                        },
                        "type": "primary",
                        "url": url,
                    }
                ],
            }
        )
    card = {
        "config": {"wide_screen_mode": False, "enable_forward": False},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
        "elements": elements[:MAX_FEISHU_ELEMENTS],
    }
    if len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_FEISHU_CARD_BYTES:
        raise ValueError("Feishu daily digest card exceeds safe encoded size")
    return card


def render_daily_dingtalk_payload(
    view: DailyDigestView, manager_ids: tuple[str, ...] = ()
) -> dict[str, Any]:
    title = "OS Tag · 今日作战看板" if view.kind == "morning" else "OS Tag · 今日复盘看板"
    mentions = _ordered_ids(manager_ids, view)
    text = render_daily_digest_text(view)
    url = _safe_url(view.table_url)
    if url:
        text += f"\n\n[{'打开任务表' if view.kind == 'morning' else '更新任务进展'}]({url})"
    return {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
        "at": {"atUserIds": list(mentions), "isAtAll": False},
    }


def render_daily_digest_text(view: DailyDigestView) -> str:
    morning = view.kind == "morning"
    title = "今日作战看板" if morning else "今日复盘看板"
    lines = [f"## OS Tag · {title}", view.today.strftime("%m月%d日")]
    labels = (
        (("今日重点", "focus"), ("已逾期", "overdue"),
         ("高优先", "high_priority"), ("待认领", "unowned"))
        if morning
        else (("今日完成", "completed_today"), ("完成率", "completion_rate"),
              ("仍未完成", "open"), ("需升级", "escalation"))
    )
    summary = []
    for label, key in labels:
        value = view.kpis.get(key)
        suffix = "%" if key == "completion_rate" and value is not None else ""
        summary.append(f"**{label} {value if value is not None else '—'}{suffix}**")
    lines.append(" · ".join(summary))
    if morning:
        lines.append("\n### 需要优先处理")
        lines.extend(_text_item(item) for item in view.priority_items[:1])
        if not view.priority_items:
            lines.append("当前没有需要推进的重点任务")
        lines.append("\n### 今日推进")
        lines.extend(_text_item(item) for item in view.priority_items[1:6])
        if view.overflow_count:
            lines.append(f"另有 {view.overflow_count} 项重点任务")
    else:
        lines.append("\n### 今日完成")
        lines.extend(_text_item(item, completed=True) for item in view.completed_items)
        if not view.completed_items:
            lines.append("今天暂未记录已完成任务")
        lines.append("\n### 仍未完成")
        lines.extend(_text_item(item) for item in view.open_items)
        if not view.open_items:
            lines.append("当前没有未完成任务")
        lines.append(f"\n### 明日关注\n{int(view.kpis['escalation'] or 0)} 项需要升级处理")
    return "\n".join(lines)


def _feishu_kpis(view: DailyDigestView) -> dict[str, Any]:
    labels = (
        (("🔵 今日重点", "focus"), ("🔴 已逾期", "overdue"),
         ("🟠 高优先", "high_priority"), ("⚪ 待认领", "unowned"))
        if view.kind == "morning"
        else (("🟢 今日完成", "completed_today"), ("🔵 完成率", "completion_rate"),
              ("🟠 未完成", "open"), ("🔴 需升级", "escalation"))
    )
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey",
        "columns": [
            {
                "tag": "column", "width": "weighted", "weight": 1,
                "vertical_align": "top",
                "elements": [{
                    "tag": "markdown",
                    "content": f"{label}\n**{view.kpis.get(key) if view.kpis.get(key) is not None else '—'}"
                    f"{'%' if key == 'completion_rate' and view.kpis.get(key) is not None else ''}",
                }],
            }
            for label, key in labels
        ],
    }


def _feishu_item(
    item: DailyDigestItem, *, danger: bool = False, completed: bool = False
) -> dict[str, Any]:
    marker = "🟢" if completed else "🔴" if danger else "🔵"
    owner = f" · @{_safe(item.owner, 40)}" if item.owner else " · 待认领"
    overdue = f"逾期 {item.days_overdue} 天 · " if item.days_overdue else ""
    return {
        "tag": "markdown",
        "content": f"{marker} **{_safe(item.title, 180)}**\n{overdue}{_safe(item.status or '未设置', 40)}"
        f" · {_safe(item.priority or '未设置', 32)}{owner}",
    }


def _feishu_note(content: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}


def _feishu_empty(content: str) -> dict[str, Any]:
    return _feishu_note(content)


def _text_item(item: DailyDigestItem, *, completed: bool = False) -> str:
    marker = "✓" if completed else "!" if item.days_overdue else "-"
    owner = f" · @{_safe(item.owner, 40)}" if item.owner else " · 待认领"
    overdue = f"逾期 {item.days_overdue} 天 · " if item.days_overdue else ""
    return f"{marker} **{_safe(item.title, 180)}**\n  {overdue}{_safe(item.status or '未设置', 40)}{owner}"


def _ordered_ids(manager_ids: Iterable[str], view: DailyDigestView) -> tuple[str, ...]:
    result: list[str] = []
    for identifier in (
        *manager_ids,
        *(owner for item in (*view.priority_items, *view.completed_items, *view.open_items)
          for owner in item.owner_ids),
    ):
        if identifier and identifier not in result:
            result.append(identifier)
    return tuple(result)
