"""Shared bounded views for connector health and completion evidence cards."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from ostag.core.connector_diagnostics import ConnectorDiagnosticReport


_OPAQUE_CAPABILITY = re.compile(r"cap_[A-Za-z0-9_-]{16,96}")
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+.!|>~-])")
_STATUS_COLOR = {"ready": "green", "degraded": "orange", "blocked": "red"}
_STATUS_LABEL = {"ready": "READY", "degraded": "DEGRADED", "blocked": "BLOCKED"}


def _safe(value: object, limit: int = 180) -> str:
    flat = " ".join(str(value).split())
    escaped = _MARKDOWN.sub(r"\\\1", html.escape(flat, quote=True))
    return escaped if len(escaped) <= limit else escaped[: limit - 1] + "…"


@dataclass(frozen=True)
class DiagnosticAction:
    label: str
    callback_id: str
    authorized: bool = False

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("diagnostic action label is required")
        if not _OPAQUE_CAPABILITY.fullmatch(self.callback_id):
            raise ValueError("diagnostic callback id must be an opaque cap_ capability")


@dataclass(frozen=True)
class DiagnosticCheckView:
    id: str
    status: str
    summary: str
    required: bool


@dataclass(frozen=True)
class CompletionEvidenceView:
    work_thread_key: str
    completion: str
    evidence_backed: bool
    tests_passed: bool
    deploy_verified: bool
    prod_accepted: bool


@dataclass(frozen=True)
class DiagnosticCardView:
    platform: str
    mode: str
    status: str
    read_ready: bool
    write_ready: bool
    checks: tuple[DiagnosticCheckView, ...]
    completion: tuple[CompletionEvidenceView, ...] = ()


def build_diagnostic_view(
    report: ConnectorDiagnosticReport,
    *,
    completion_rows: tuple[Mapping[str, Any], ...] = (),
) -> DiagnosticCardView:
    checks = tuple(
        DiagnosticCheckView(item.id, item.status, _safe(item.summary), item.required)
        for item in report.checks[:10]
    )
    completion = tuple(
        CompletionEvidenceView(
            work_thread_key=_safe(row.get("workThreadKey", ""), 120),
            completion=_safe(row.get("completion", "unknown"), 32),
            evidence_backed=row.get("evidenceBacked") is True,
            tests_passed=row.get("testsPassed") is True,
            deploy_verified=row.get("deployVerified") is True,
            prod_accepted=row.get("prodAccepted") is True,
        )
        for row in completion_rows[:4]
    )
    return DiagnosticCardView(
        platform=_safe(report.platform, 32),
        mode=report.mode,
        status=report.status,
        read_ready=report.read_ready,
        write_ready=report.write_ready,
        checks=checks,
        completion=completion,
    )


def render_diagnostic_text(view: DiagnosticCardView) -> str:
    label = _STATUS_LABEL.get(view.status, "UNKNOWN")
    lines = [
        f"OS Tag connector · {view.platform} [{label}]",
        f"mode={view.mode} read={'ready' if view.read_ready else 'blocked'} "
        f"write={'ready' if view.write_ready else 'disabled'}",
    ]
    for check in view.checks:
        marker = "OK" if check.status == "pass" else "WARN" if check.status == "warn" else "FAIL"
        lines.append(f"- [{marker}] {check.id}: {check.summary}")
    for row in view.completion:
        badges = " ".join(
            label
            for label, enabled in (
                ("evidence", row.evidence_backed),
                ("tests", row.tests_passed),
                ("deploy", row.deploy_verified),
                ("accepted", row.prod_accepted),
            )
            if enabled
        )
        lines.append(f"- completion {row.work_thread_key}: {row.completion} {badges}".rstrip())
    return "\n".join(lines)


def _completion_lines(view: DiagnosticCardView) -> list[str]:
    lines: list[str] = []
    for row in view.completion:
        badges = " · ".join(
            name
            for name, enabled in (
                ("证据", row.evidence_backed),
                ("测试", row.tests_passed),
                ("部署", row.deploy_verified),
                ("验收", row.prod_accepted),
            )
            if enabled
        ) or "无证据"
        lines.append(
            f"**{row.work_thread_key}**\n{row.completion} · {badges}"
        )
    return lines


def render_diagnostic_feishu_card(
    view: DiagnosticCardView,
    *,
    actions: tuple[DiagnosticAction, ...] = (),
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**模式** {view.mode}　**读取** {'可用' if view.read_ready else '阻塞'}　"
                f"**写入** {'可用' if view.write_ready else '关闭'}"
            ),
        }
    ]
    for check in view.checks[:8]:
        marker = "✅" if check.status == "pass" else "⚠" if check.status == "warn" else "✕"
        required = " · required" if check.required else ""
        elements.append(
            {"tag": "markdown", "content": f"{marker} **{_safe(check.id, 48)}**{required}\n{check.summary}"}
        )
    if view.completion:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": "**完成证据**"})
        elements.extend(
            {"tag": "markdown", "content": line} for line in _completion_lines(view)
        )
    allowed_actions = tuple(action for action in actions if action.authorized)[:3]
    if allowed_actions:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": _safe(action.label, 48)},
                        "type": "primary" if index == 0 else "default",
                        "value": {"capability_id": action.callback_id},
                    }
                    for index, action in enumerate(allowed_actions)
                ],
            }
        )
    card = {
        "config": {"wide_screen_mode": False, "enable_forward": False},
        "header": {
            "template": _STATUS_COLOR.get(view.status, "grey"),
            "title": {
                "tag": "plain_text",
                "content": f"OS Tag · {view.platform} connector",
            },
        },
        "elements": elements[:20],
    }
    if len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()) > 24_000:
        raise ValueError("Feishu diagnostic card exceeds safe encoded size")
    return card


def render_diagnostic_dingtalk_card(
    view: DiagnosticCardView,
    *,
    actions: tuple[DiagnosticAction, ...] = (),
    action_base_url: str = "",
) -> dict[str, Any]:
    allowed_actions = tuple(action for action in actions if action.authorized)[:3]
    if allowed_actions:
        parsed = urlparse(action_base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("DingTalk diagnostic action base URL must use HTTPS")
    label = _STATUS_LABEL.get(view.status, "UNKNOWN")
    text = render_diagnostic_text(view)
    payload = {
        "msgtype": "actionCard",
        "actionCard": {
            "title": f"[{label}] OS Tag · {view.platform} connector",
            "text": text,
            "btnOrientation": "0",
            "btns": [
                {
                    "title": _safe(action.label, 48),
                    "actionURL": f"{action_base_url.rstrip('/')}/{action.callback_id}",
                }
                for action in allowed_actions
            ],
        },
    }
    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) > 20_000:
        raise ValueError("DingTalk diagnostic card exceeds safe encoded size")
    return payload
