"""Mobile-first, data-minimized Feishu cards for Copilot Relay."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from .models import RelayEnvelope
from .redaction import sanitize_assistant_text
from .store import RelaySession


MAX_CARD_BYTES = 24_000
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+.!|>~-])")


def _safe(value: object, limit: int = 240) -> str:
    filtered = sanitize_assistant_text(str(value), max_codepoints=limit)
    public = filtered.text if filtered.text else "[内容已隐藏]"
    text = public.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped = _MARKDOWN.sub(r"\\\1", text)
    return escaped if len(escaped) <= limit else escaped[: limit - 1] + "…"


def safe_plain_text(value: object, limit: int = 500) -> str:
    filtered = sanitize_assistant_text(str(value), max_codepoints=limit)
    return filtered.text if filtered.text else "[内容已隐藏]"


def _action(
    callback_factory: Callable[[str, str], str] | None,
    actions: tuple[tuple[str, str, str], ...],
) -> dict[str, Any]:
    rendered = []
    for action, label, reference in actions:
        if callback_factory is None:
            continue
        callback_id = callback_factory(action, reference)
        if not callback_id or len(callback_id.encode()) > 64:
            raise ValueError("Feishu callback ID must be compact and opaque")
        rendered.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "primary" if action not in {"cancel"} else "default",
                "value": {"callback_id": callback_id},
            }
        )
    return {"tag": "action", "actions": rendered}


def _card(title: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    card = {
        "config": {"wide_screen_mode": False, "enable_forward": False},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
        "elements": elements[:20],
    }
    if len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()) >= MAX_CARD_BYTES:
        raise ValueError("Feishu card exceeds safe encoded size")
    return card


def _context(session: RelaySession) -> str:
    return (
        f"**{_safe(session.title)}**\n"
        f"{_safe(session.instance_id, 64)} · {_safe(session.agent_id, 48)} · "
        f"{_safe(session.state, 40)}"
    )


def render_current_task_card(
    session: RelaySession,
    *,
    can_act: bool,
    callback_factory: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": _context(session)},
        {
            "tag": "markdown",
            "content": f"**当前阶段**  {_safe(session.state, 40)}\n"
            f"**最近进展**  {_safe(session.latest_summary, 600)}",
        },
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"最近更新 {session.updated_at[:19]} UTC"}
            ],
        },
    ]
    if can_act:
        elements.append(
            _action(
                callback_factory,
                (("snapshot", "查看最新回复", "current"), ("switch", "切换任务", "current")),
            )
        )
    return _card("OStag Copilot · 当前任务", elements)


def render_decision_card(
    session: RelaySession,
    envelope: RelayEnvelope,
    *,
    can_act: bool,
    callback_factory: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    if envelope.kind != "decision.request":
        raise ValueError("decision card requires decision.request")
    payload = envelope.payload
    choices = tuple(payload["choices"])
    recommended = payload["recommended_choice_id"]
    lines = [f"**需要你的决策**\n{_safe(payload['question'], 1_000)}"]
    for choice in choices:
        marker = "（AI 推荐）" if choice["choice_id"] == recommended else ""
        lines.append(f"• {_safe(choice['label'], 160)} {marker}")
    if payload["safe_work"]:
        lines.append("**等待期间可继续**  " + "；".join(_safe(item, 200) for item in payload["safe_work"]))
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": _context(session)},
        {"tag": "markdown", "content": "\n".join(lines)},
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"回复此消息也可作答 · {envelope.expires_at[:19]} UTC 前有效"}
            ],
        },
    ]
    if can_act:
        elements.append(
            _action(
                callback_factory,
                tuple(
                    ("decision", str(choice["label"]), str(choice["choice_id"]))
                    for choice in choices
                ),
            )
        )
    return _card("OStag Copilot · 等待决策", elements)


def render_task_proposal_card(
    session: RelaySession,
    envelope: RelayEnvelope,
    *,
    can_act: bool,
    callback_factory: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    if envelope.kind != "task.create.proposal":
        raise ValueError("proposal card requires task.create.proposal")
    payload = envelope.payload
    criteria = "；".join(_safe(item, 200) for item in payload["acceptance_criteria"]) or "待补充"
    risk = "；".join(_safe(item, 160) for item in payload["sensitive_operations"]) or "无"
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": _context(session)},
        {
            "tag": "markdown",
            "content": f"**目标**  {_safe(payload['objective'], 1_000)}\n"
            f"**目标实例**  {_safe(session.instance_id, 64)}\n"
            f"**建议 Agent**  {_safe(payload['suggested_agent'], 64)}\n"
            f"**验收条件**  {criteria}\n**敏感操作**  {risk}\n**Workspace**  受管隔离环境",
        },
    ]
    if can_act:
        elements.append(
            _action(
                callback_factory,
                (("confirm", "确认创建", str(payload["proposal_id"])), ("cancel", "取消", str(payload["proposal_id"]))),
            )
        )
    return _card("OStag Copilot · 新任务确认", elements)


def render_snapshot_card(session: RelaySession, envelope: RelayEnvelope) -> dict[str, Any]:
    if envelope.kind != "assistant.snapshot" or not envelope.payload["assistant_text"]:
        raise ValueError("snapshot card requires a non-empty assistant.snapshot")
    elements = [
        {"tag": "markdown", "content": _context(session)},
        {
            "tag": "markdown",
            "content": "**AI 最新回复**\n" + _safe(envelope.payload["assistant_text"], 4_000),
        },
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "内容已按安全策略过滤"
                    + (" · 已截断" if envelope.payload["truncated"] else ""),
                }
            ],
        },
    ]
    return _card("OStag Copilot · 最新回复", elements)


def render_receipt_card(session: RelaySession, envelope: RelayEnvelope) -> dict[str, Any]:
    if envelope.kind != "delivery.receipt":
        raise ValueError("receipt card requires delivery.receipt")
    state = str(envelope.payload["state"])
    labels = {
        "queued": "已排队", "delivered": "已送达本地", "accepted": "原会话已接受",
        "applied": "原会话已应用", "rejected": "原会话已拒绝", "expired": "已过期",
        "indeterminate": "结果待核对",
    }
    elements = [
        {"tag": "markdown", "content": _context(session)},
        {
            "tag": "markdown",
            "content": f"**{labels.get(state, _safe(state, 40))}**\n"
            + _safe(envelope.payload["summary"], 1_000),
        },
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "此状态仅代表消息处理，不代表代码或部署验收完成"}
            ],
        },
    ]
    return _card("OStag Copilot · 指令回执", elements)
