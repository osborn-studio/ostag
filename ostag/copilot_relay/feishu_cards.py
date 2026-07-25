"""Mobile-first, data-minimized Feishu cards for Copilot Relay."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .models import RelayEnvelope
from .redaction import sanitize_assistant_text
from .status_board import DeviceStatusItem, OsstackStatusBoardView, StatusSessionItem
from .store import RelaySession


MAX_CARD_BYTES = 24_000
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+.!|>~-])")
_BEIJING = ZoneInfo("Asia/Shanghai")


def _safe(value: object, limit: int = 240) -> str:
    filtered = sanitize_assistant_text(str(value), max_codepoints=limit)
    public = filtered.text if filtered.text else "[内容已隐藏]"
    text = public.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped = _MARKDOWN.sub(r"\\\1", text)
    return escaped if len(escaped) <= limit else escaped[: limit - 1] + "…"


def safe_plain_text(value: object, limit: int = 500) -> str:
    filtered = sanitize_assistant_text(str(value), max_codepoints=limit)
    return filtered.text if filtered.text else "[内容已隐藏]"


def _beijing_time(value: str) -> str:
    raw = value.removesuffix("Z")
    timestamp = datetime.fromisoformat(raw + ("+00:00" if value.endswith("Z") else ""))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
    return timestamp.astimezone(_BEIJING).strftime("%Y-%m-%d %H:%M 北京时间")


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
                {"tag": "plain_text", "content": f"最近更新 {_beijing_time(session.updated_at)}"}
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
                {"tag": "plain_text", "content": f"回复此消息也可作答 · {_beijing_time(envelope.expires_at)}前有效"}
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


def _status_label(item: StatusSessionItem) -> str:
    if item.waiting_decision:
        return "等待决策"
    return {
        "running": "运行中",
        "blocked": "阻塞",
        "idle": "空闲",
        "completed": "已完成",
        "failed": "失败",
    }.get(item.state, item.state)


def _agent_label(value: str) -> str:
    return {
        "codex": "Codex",
        "codex-cli": "Codex",
        "claude-code": "Claude Code",
        "other": "其他 Agent",
    }.get(value.casefold(), value)


def _connection_label(value: str) -> str:
    return {
        "online": "🟢 在线",
        "degraded": "🟠 连接不稳",
        "offline": "⚪ 离线",
    }.get(value, "⚪ 状态未知")


def render_device_overview_card(
    view: OsstackStatusBoardView,
    *,
    can_view_team: bool,
    callback_factory: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    """Render a bounded device-first status overview without native identifiers."""
    scope = "团队总览" if view.scope == "team" else "我的设备"
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**{scope}**　在线 **{view.online_count}/{view.device_count}**\n"
                f"运行中 **{view.running_count}**　等你决策 **{view.waiting_decision_count}**　"
                f"阻塞 **{view.blocked_count}**　今日完成 **{view.completed_today_count}**"
            ),
        }
    ]
    if not view.devices:
        message = (
            "尚未绑定 osstack，请联系管理员绑定设备。"
            if view.device_count == 0
            else "当前没有可显示的设备状态。"
        )
        elements.append(
            {"tag": "note", "elements": [{"tag": "plain_text", "content": message}]}
        )
    for device in view.devices[:4]:
        session = device.sessions[0] if device.sessions else None
        runtime = device.runtimes[0] if device.runtimes else None
        lines = [
            f"**{_safe(device.display_name, 80)}**　{_connection_label(device.connection_state)}"
        ]
        if runtime is not None:
            lines.append(f"**Agent**　{_safe(_agent_label(runtime.agent_type), 64)}")
        if session is not None:
            lines.extend(
                (
                    f"**当前会话**　{_safe(session.title, 180)} · {_safe(_status_label(session), 32)}",
                    f"**最近进展**　{_safe(session.latest_summary, 420)}",
                    f"最近更新 {_beijing_time(session.updated_at)}",
                )
            )
        else:
            lines.append("**当前会话**　暂无活跃会话")
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
        if callback_factory is not None:
            label = "处理决策" if device.attention_count else "查看设备"
            elements.append(
                _action(
                    callback_factory,
                    (("device_detail", label, device.device_id),),
                )
            )
    if callback_factory is not None:
        actions: list[tuple[str, str, str]] = [
            ("status_refresh", "刷新看板", view.scope),
            ("switch_picker", "切换任务", "current"),
        ]
        if can_view_team:
            actions.append(
                (
                    "status_personal" if view.scope == "team" else "status_team",
                    "我的设备" if view.scope == "team" else "团队总览",
                    "personal" if view.scope == "team" else "team",
                )
            )
        elements.append(_action(callback_factory, tuple(actions)))
    freshness = " · 数据可能已过期" if view.stale else ""
    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"只读状态查询 · {_beijing_time(view.generated_at)}{freshness}",
                }
            ],
        }
    )
    return _card("OStag · osstack 实时进度看板", elements)


def render_device_detail_card(
    device: DeviceStatusItem,
    *,
    callback_factory: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    """Render one authorized device and its bounded runtime/session inventory."""
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**{_safe(device.display_name, 80)}**　{_connection_label(device.connection_state)}",
        }
    ]
    for runtime in device.runtimes[:4]:
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    f"**Agent**　{_safe(_agent_label(runtime.agent_type), 64)}\n"
                    f"状态 {_safe(_connection_label(runtime.state), 32)} · "
                    f"会话 {runtime.session_count}"
                ),
            }
        )
    if not device.sessions:
        elements.append(
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "暂无活跃会话"}]}
        )
    for session in device.sessions[:4]:
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    f"**{_safe(session.title, 180)}**　{_safe(_status_label(session), 32)}\n"
                    f"{_safe(_agent_label(session.agent_id), 64)} · "
                    f"最近更新 {_beijing_time(session.updated_at)}\n"
                    f"{_safe(session.latest_summary, 420)}"
                ),
            }
        )
        if callback_factory is not None:
            elements.append(
                _action(
                    callback_factory,
                    (("device_continue", "继续会话", session.session_id),),
                )
            )
    if callback_factory is not None:
        elements.append(
            _action(
                callback_factory,
                (("status_recover", "返回最新看板", "personal"),),
            )
        )
    return _card("OStag · 设备详情", elements)


def render_targeted_decision_card(
    session: StatusSessionItem,
    envelope: RelayEnvelope,
    *,
    callback_factory: Callable[[str, str], str] | None,
) -> dict[str, Any]:
    if envelope.kind != "decision.request":
        raise ValueError("decision card requires decision.request")
    payload = envelope.payload
    target = (
        f"本次回复将发送到：{safe_plain_text(session.device_name, 80)} · "
        f"{safe_plain_text(_agent_label(session.agent_id), 64)} · "
        f"{safe_plain_text(session.title, 180)}"
    )
    lines = [f"**需要你的决策**\n{_safe(payload['question'], 1_000)}"]
    recommended = payload["recommended_choice_id"]
    for choice in payload["choices"]:
        marker = "（AI 推荐）" if choice["choice_id"] == recommended else ""
        lines.append(f"• {_safe(choice['label'], 160)} {marker}")
    elements: list[dict[str, Any]] = [
        {"tag": "note", "elements": [{"tag": "plain_text", "content": target}]},
        {"tag": "markdown", "content": "\n".join(lines)},
    ]
    if callback_factory is not None:
        elements.append(
            _action(
                callback_factory,
                tuple(
                    ("decision_answer", str(choice["label"]), str(choice["choice_id"]))
                    for choice in payload["choices"]
                ),
            )
        )
    return _card("OStag · 等待决策", elements)


def render_expired_status_recovery_card(
    *, callback_factory: Callable[[str, str], str]
) -> dict[str, Any]:
    elements = [
        {
            "tag": "markdown",
            "content": "原看板操作已过期。获取最新状态后再继续操作。",
        },
        _action(
            callback_factory,
            (("status_recover", "获取最新看板", "personal"),),
        ),
    ]
    return _card("OStag · 操作已过期", elements)


def render_osstack_status_card(
    view: OsstackStatusBoardView,
    *,
    can_view_team: bool,
    callback_factory: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    """Render the identity-scoped, read-only osstack status dashboard."""
    if view.devices:
        return render_device_overview_card(
            view,
            can_view_team=can_view_team,
            callback_factory=callback_factory,
        )
    scope = "团队总览" if view.scope == "team" else "我的设备"
    health = (
        "需要处理"
        if view.blocked_count or view.waiting_decision_count
        else "正在运行"
        if view.running_count
        else "当前空闲"
    )
    metrics = (
        ("运行中", view.running_count),
        ("等你决策", view.waiting_decision_count),
        ("阻塞", view.blocked_count),
        ("今日完成", view.completed_today_count),
    )
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**{scope}**　在线 **{view.online_count}/{view.device_count}**　{health}"
            ),
        },
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {"tag": "markdown", "content": f"{label}\n**{value}**"}
                    ],
                }
                for label, value in metrics
            ],
        },
        {"tag": "markdown", "content": "**当前协作会话**"},
    ]
    current = view.current_session
    if current is None:
        message = (
            "尚未绑定 osstack，请联系管理员绑定设备。"
            if view.device_count == 0
            else "当前没有 osstack 会话。"
        )
        elements.append(
            {"tag": "note", "elements": [{"tag": "plain_text", "content": message}]}
        )
    else:
        online = "在线" if current.online else "离线"
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    f"**{_safe(current.title, 240)}**　`{_safe(_status_label(current), 32)}`\n"
                    f"**设备**　{_safe(current.device_name, 80)} · "
                    f"`{_safe(current.instance_id, 128)}` · {online}\n"
                    f"**Agent**　{_safe(current.agent_id, 64)}\n"
                    f"**最近进展**　{_safe(current.latest_summary, 600)}\n"
                    f"最近更新 {_beijing_time(current.updated_at)}"
                ),
            }
        )

    other_sessions = tuple(
        item
        for item in view.sessions
        if current is None
        or (item.instance_id, item.session_id)
        != (current.instance_id, current.session_id)
    )
    if other_sessions:
        elements.append({"tag": "markdown", "content": "**其他活跃会话**"})
        elements.extend(
            {
                "tag": "markdown",
                "content": (
                    f"• **{_safe(item.title, 160)}**\n"
                    f"{_safe(item.device_name, 80)} · `{_safe(item.instance_id, 128)}` · "
                    f"{_safe(item.agent_id, 64)} · {_safe(_status_label(item), 32)}"
                ),
            }
            for item in other_sessions[:3]
        )

    elements.append({"tag": "markdown", "content": "**需要你处理**"})
    if view.attention:
        elements.extend(
            {
                "tag": "markdown",
                "content": (
                    f"• **{_safe(item.title, 160)}**\n"
                    f"{_safe(item.device_name, 80)} · {_safe(item.agent_id, 64)} · "
                    f"{_safe(_status_label(item), 32)}"
                ),
            }
            for item in view.attention[:3]
        )
    else:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "当前没有等待决策或阻塞事项。"}
                ],
            }
        )

    recommendation = (
        "优先处理等待决策和阻塞事项。"
        if view.attention
        else "当前无阻塞，可以下发新方向或切换到其他会话。"
    )
    elements.append(
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"OStag 建议：{recommendation}"}
            ],
        }
    )

    if callback_factory is not None:
        actions: list[tuple[str, str, str]] = [
            ("status_refresh", "刷新看板", view.scope),
            ("current", "当前会话", "current"),
            ("switch_picker", "切换任务", "current"),
        ]
        if can_view_team:
            actions.append(
                (
                    "status_personal" if view.scope == "team" else "status_team",
                    "我的设备" if view.scope == "team" else "团队总览",
                    "personal" if view.scope == "team" else "team",
                )
            )
        elements.append(_action(callback_factory, tuple(actions)))

    freshness = " · 数据可能已过期" if view.stale else ""
    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": (
                        f"只读状态查询 · {_beijing_time(view.generated_at)}{freshness}"
                    ),
                }
            ],
        }
    )
    return _card("OStag · osstack 实时进度看板", elements)
