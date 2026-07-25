"""Identity-bound Feishu interaction controller for Copilot Relay."""

from __future__ import annotations

import json
import secrets
from uuid import uuid4
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ostag.core.event import NormalizedEvent
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination

from .feishu_cards import (
    MAX_CARD_BYTES,
    render_current_task_card,
    render_device_detail_card,
    render_expired_status_recovery_card,
    render_osstack_status_card,
    render_task_proposal_card,
    safe_plain_text,
)
from .service import CopilotRelayService, RelayResult, RelayRoutingError
from .status_board import (
    BoardScope,
    OsstackStatusBoardService,
    format_osstack_status_text,
)
from .status_intent import classify_osstack_status_intent


STATUS_CALLBACK_TTL_SECONDS = 86_400


@dataclass(frozen=True)
class FeishuRelayResponse:
    text: str
    card: dict[str, Any] | None = None
    relay_message_id: str | None = None


class CopilotFeishuController:
    """Routes normalized Feishu events without trusting text or callback payload IDs."""

    def __init__(
        self,
        service: CopilotRelayService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        write_enabled: bool = True,
        canary_session_ids: frozenset[str] = frozenset(),
        status_service: OsstackStatusBoardService | None = None,
        status_board_enabled: bool = False,
        route_explainer=None,
    ) -> None:
        self.service = service
        self.store = service.store
        self.clock = clock
        self.write_enabled = write_enabled
        self.canary_session_ids = canary_session_ids
        self.status_service = status_service
        self.status_board_enabled = status_board_enabled
        # Phase 5: optional read-only explanation layer. When absent, every
        # call goes straight to the service — behavior is identical either
        # way (proven by the corpus equivalence tests).
        self.route_explainer = route_explainer

    # -- Phase 5 routing indirection (read-only explanations) -----------------

    def _decision_stamp(self, event_id: str | None, suffix: str = "") -> tuple[str, str]:
        identifier = event_id or uuid4().hex
        return f"rd_{identifier}{suffix}", self.clock().isoformat()

    def _routed_handle_text(
        self, principal, text, *, quote_message_id=None, event_id=None, suffix=""
    ) -> RelayResult:
        if self.route_explainer is None:
            return self.service.handle_text(
                principal, text, quote_message_id=quote_message_id, event_id=event_id
            )
        decision_id, decided_at = self._decision_stamp(event_id, suffix)
        result, _ = self.route_explainer.handle_text(
            principal, text, quote_message_id=quote_message_id, event_id=event_id,
            decision_id=decision_id, decided_at=decided_at,
        )
        return result

    def _routed_answer_decision(
        self, principal, decision_message_id, answer, *, event_id=None
    ) -> RelayResult:
        # answer_decision is handle_text with a quoted decision message.
        return self._routed_handle_text(
            principal, answer, quote_message_id=decision_message_id, event_id=event_id
        )

    def _routed_snapshot(self, principal, *, event_id=None) -> RelayResult:
        if self.route_explainer is None:
            return self.service.request_snapshot(principal, event_id=event_id)
        decision_id, decided_at = self._decision_stamp(event_id, "_snapshot")
        result, _ = self.route_explainer.request_snapshot(
            principal, event_id=event_id, decision_id=decision_id, decided_at=decided_at
        )
        return result

    def _routed_confirm(self, principal, proposal, *, confirmed_at, event_id=None) -> RelayResult:
        if self.route_explainer is None:
            return self.service.confirm_task_proposal(
                principal, proposal, confirmed_at=confirmed_at
            )
        decision_id, decided_at = self._decision_stamp(event_id, "_confirm")
        result, _ = self.route_explainer.confirm_task_proposal(
            principal, proposal, confirmed_at=confirmed_at,
            decision_id=decision_id, decided_at=decided_at,
        )
        return result

    @staticmethod
    def callback_value(callback_id: str) -> dict[str, str]:
        return {"callback_id": callback_id}

    def issue_callback(
        self,
        principal: Principal,
        destination: Destination,
        *,
        action: str,
        reference: str,
        ttl_seconds: int = STATUS_CALLBACK_TTL_SECONDS,
    ) -> str:
        callback_id = "cr_" + secrets.token_urlsafe(18)
        self.store.create_callback(
            callback_id,
            action=action,
            reference_id=reference,
            platform=principal.platform,
            actor_id=principal.actor_id,
            destination_id=destination.destination_id,
            chat_type=destination.chat_type,
            expires_at=int(self.clock().timestamp()) + ttl_seconds,
        )
        return callback_id

    def record_delivery(
        self,
        *,
        platform_message_id: str,
        relay_message_id: str,
        principal: Principal,
        destination: Destination,
    ) -> bool:
        return self.store.bind_platform_message(
            platform=destination.platform,
            platform_message_id=platform_message_id,
            relay_message_id=relay_message_id,
            destination_id=destination.destination_id,
            actor_id=principal.actor_id,
            created_at=self.clock().isoformat().replace("+00:00", "Z"),
        )

    def handle_event(self, event: NormalizedEvent) -> FeishuRelayResponse | None:
        principal = Principal("feishu", event.sender_id)
        destination = Destination("feishu", event.destination_id, event.chat_type)
        text = event.text.strip()
        is_command = text in {"/任务", "任务", "/最新回复", "最新回复", "/切换任务", "切换任务"}
        if event.chat_type != "private":
            if is_command or text:
                return FeishuRelayResponse("Copilot 写操作仅支持已绑定的负责人私聊。")
            return None
        status_intent = classify_osstack_status_intent(text)
        if (
            self.status_board_enabled
            and self.status_service is not None
            and status_intent in {"status_query", "mixed"}
        ):
            return self._status_board(
                principal,
                destination,
                scope="personal",
                mixed=status_intent == "mixed",
            )
        if not self.write_enabled and text not in {"/任务", "任务", "/切换任务", "切换任务"}:
            return FeishuRelayResponse("Copilot Relay 当前处于只读灰度，远程写入尚未启用。")
        if text not in {"/任务", "任务", "/切换任务", "切换任务"}:
            current = self.store.current_session(principal.platform, principal.actor_id)
            if current is not None and not self._session_write_allowed(current.session_id):
                return FeishuRelayResponse("当前任务不在写入灰度范围，请切换到灰度任务。")
        if text in {"/任务", "任务"}:
            current = self.store.current_session(principal.platform, principal.actor_id)
            if current is None:
                return self._picker(principal, destination)
            return FeishuRelayResponse(
                "当前任务", self._current_card(current, principal, destination)
            )
        if text in {"/切换任务", "切换任务"}:
            return self._picker(principal, destination)
        try:
            if text in {"/最新回复", "最新回复"}:
                result = self._routed_snapshot(principal, event_id=event.event_id)
                return FeishuRelayResponse(
                    "已请求最新回复，收到本地安全快照后会发送给你。",
                    relay_message_id=result.outbound.message_id if result.outbound else None,
                )
            quote_relay_id = None
            if event.quote_message_id:
                quote_relay_id = self.store.relay_message_for_platform(
                    "feishu", event.quote_message_id, destination_id=event.destination_id
                )
                if quote_relay_id is None:
                    return FeishuRelayResponse("引用消息不是有效的 Copilot 决策，请查看当前任务。")
            result = self._routed_handle_text(
                principal,
                text,
                quote_message_id=quote_relay_id,
                event_id=event.event_id,
            )
            return self._result(result, principal, destination)
        except PermissionError:
            return FeishuRelayResponse("没有权限操作这个任务。")
        except RelayRoutingError as exc:
            if "stale decision" in str(exc):
                return FeishuRelayResponse("这条决策已过期，请查看当前任务的最新问题。")
            if "no current session" in str(exc):
                candidates = self._accessible_sessions(principal)
                if len(candidates) == 1:
                    target = candidates[0]
                    self.service.select_session(
                        principal,
                        target.instance_id,
                        target.session_id,
                        runtime_id=target.runtime_id,
                        selected_at=self.clock().isoformat().replace("+00:00", "Z"),
                    )
                    result = self._routed_handle_text(
                        principal, text, event_id=event.event_id, suffix="_r2"
                    )
                    return self._result(result, principal, destination)
                return self._picker(principal, destination)
            return FeishuRelayResponse("请求目标无效，请重新选择任务。")

    def handle_callback(
        self,
        callback_id: str,
        *,
        principal: Principal,
        destination: Destination,
    ) -> FeishuRelayResponse:
        mapped = self.store.consume_callback(
            callback_id,
            platform=principal.platform,
            actor_id=principal.actor_id,
            destination_id=destination.destination_id,
            chat_type=destination.chat_type,
            now=int(self.clock().timestamp()),
        )
        if mapped is None or mapped.get("error") == "identity_mismatch":
            return FeishuRelayResponse("没有权限或操作目标不匹配。")
        if mapped.get("error") == "expired":
            if mapped.get("action") in {
                "status_refresh", "status_personal", "status_team", "status_recover",
                "device_detail", "device_sessions",
            }:
                def recovery_factory(action: str, reference: str) -> str:
                    return self.issue_callback(
                        principal,
                        destination,
                        action=action,
                        reference=reference,
                        ttl_seconds=STATUS_CALLBACK_TTL_SECONDS,
                    )

                return FeishuRelayResponse(
                    "原操作已过期，请获取最新看板。",
                    render_expired_status_recovery_card(
                        callback_factory=recovery_factory
                    ),
                )
            return FeishuRelayResponse("操作已过期或已使用。")
        action, reference = mapped["action"], mapped["reference_id"]
        try:
            if action in {"status_refresh", "status_personal", "status_team"}:
                scope: BoardScope = (
                    "team"
                    if action == "status_team"
                    or (action == "status_refresh" and reference == "team")
                    else "personal"
                )
                board = self._status_board(
                    principal, destination, scope=scope, mixed=False
                )
                toast = {
                    "status_refresh": "看板已刷新",
                    "status_personal": "已切换到我的设备",
                    "status_team": "已切换到团队总览",
                }[action]
                return FeishuRelayResponse(toast, board.card)
            if action == "status_recover":
                scope: BoardScope = "team" if reference == "team" else "personal"
                board = self._status_board(
                    principal, destination, scope=scope, mixed=False
                )
                return FeishuRelayResponse("已获取最新看板", board.card)
            if action in {"device_detail", "device_sessions"}:
                target = json.loads(reference)
                if not isinstance(target, dict) or set(target) != {"scope", "device_id"}:
                    raise ValueError("invalid device target")
                return self._device_detail(
                    principal,
                    destination,
                    scope=target["scope"],
                    device_id=target["device_id"],
                )
            if action == "current":
                current = self.store.current_session(
                    principal.platform, principal.actor_id
                )
                if current is None:
                    return self._picker(principal, destination)
                return FeishuRelayResponse(
                    "当前会话", self._current_card(current, principal, destination)
                )
            if action == "switch_picker":
                return self._picker(principal, destination)
            if not self.write_enabled:
                return FeishuRelayResponse(
                    "Copilot Relay 当前处于只读灰度，远程写入尚未启用。"
                )
            if action == "device_continue":
                target = json.loads(reference)
                required = {
                    "instance_id", "runtime_id", "session_id", "turn_id",
                    "session_version",
                }
                if not isinstance(target, dict) or set(target) != required:
                    raise ValueError("invalid session target")
                session = self.store.get_session(
                    target["instance_id"], target["session_id"], target["runtime_id"]
                )
                if (
                    session is None
                    or session.last_turn_id != target["turn_id"]
                    or session.session_version != target["session_version"]
                    or not self.service.policy.can_access(principal, session.instance_id)
                ):
                    raise RelayRoutingError("session target changed")
                selected = self.service.select_session(
                    principal,
                    session.instance_id,
                    session.session_id,
                    runtime_id=session.runtime_id,
                    selected_at=self.clock().isoformat().replace("+00:00", "Z"),
                )
                return FeishuRelayResponse(
                    "已切换到该会话。",
                    self._current_card(selected, principal, destination),
                )
            if action == "switch":
                if reference.startswith("{"):
                    target = json.loads(reference)
                    if not isinstance(target, dict) or set(target) != {
                        "instance_id", "runtime_id", "session_id", "turn_id",
                        "session_version",
                    }:
                        raise ValueError("invalid switch target")
                    current = self.store.get_session(
                        target["instance_id"], target["session_id"], target["runtime_id"]
                    )
                    if (
                        current is None
                        or current.last_turn_id != target["turn_id"]
                        or current.session_version != target["session_version"]
                    ):
                        raise RelayRoutingError("switch target changed")
                    instance_id = current.instance_id
                    runtime_id = current.runtime_id
                    session_id = current.session_id
                else:
                    instance_id, session_id = reference.split("\0", 1)
                    runtime_id = "legacy"
                session = self.service.select_session(
                    principal,
                    instance_id,
                    session_id,
                    runtime_id=runtime_id,
                    selected_at=self.clock().isoformat().replace("+00:00", "Z"),
                )
                return FeishuRelayResponse(
                    "已切换当前任务。", self._current_card(session, principal, destination)
                )
            if action == "snapshot":
                result = self._routed_snapshot(
                    principal, event_id=f"callback_{callback_id}"
                )
                return FeishuRelayResponse(
                    "已请求最新回复，收到后会发送给你。",
                    relay_message_id=result.outbound.message_id if result.outbound else None,
                )
            if action == "decision":
                message_id, choice_id = reference.split("\0", 1)
                source = self.store.envelope_for_message(message_id)
                if source is None or source.kind != "decision.request":
                    raise RelayRoutingError("decision not found")
                label = next(
                    str(choice["label"])
                    for choice in source.payload["choices"]
                    if choice["choice_id"] == choice_id
                )
                result = self._routed_answer_decision(
                    principal, message_id, label, event_id=f"callback_{callback_id}"
                )
                return self._result(result, principal, destination)
            if action == "decision_answer":
                target = json.loads(reference)
                required = {
                    "message_id", "instance_id", "runtime_id", "session_id",
                    "turn_id", "session_version", "choice_id",
                }
                if not isinstance(target, dict) or set(target) != required:
                    raise ValueError("invalid decision target")
                source = self.store.envelope_for_message(target["message_id"])
                session = self.store.get_session(
                    target["instance_id"], target["session_id"], target["runtime_id"]
                )
                if (
                    source is None
                    or source.kind != "decision.request"
                    or source.binding.instance_id != target["instance_id"]
                    or source.binding.runtime_id != target["runtime_id"]
                    or source.binding.session_id != target["session_id"]
                    or source.binding.turn_id != target["turn_id"]
                    or source.binding.session_version != target["session_version"]
                    or session is None
                    or session.last_turn_id != target["turn_id"]
                    or session.session_version != target["session_version"]
                    or not self.service.policy.can_access(principal, session.instance_id)
                ):
                    raise RelayRoutingError("decision target changed")
                label = next(
                    str(choice["label"])
                    for choice in source.payload["choices"]
                    if choice["choice_id"] == target["choice_id"]
                )
                result = self._routed_answer_decision(
                    principal,
                    source.message_id,
                    label,
                    event_id=f"callback_{callback_id}",
                )
                return self._result(result, principal, destination)
            if action == "confirm":
                proposal = self.store.proposal_envelope(reference)
                if proposal is None:
                    raise RelayRoutingError("proposal not found")
                result = self._routed_confirm(
                    principal,
                    proposal,
                    confirmed_at=self.clock().isoformat().replace("+00:00", "Z"),
                    event_id=f"callback_{callback_id}",
                )
                return FeishuRelayResponse(
                    "新任务已确认，等待本地 osstack 创建。",
                    relay_message_id=result.outbound.message_id if result.outbound else None,
                )
            if action == "cancel":
                return FeishuRelayResponse("已取消创建新任务。")
        except (PermissionError, RelayRoutingError, StopIteration, ValueError):
            return FeishuRelayResponse("操作目标已变化，请刷新当前任务。")
        return FeishuRelayResponse("操作无效。")

    def _status_board(
        self,
        principal: Principal,
        destination: Destination,
        *,
        scope: BoardScope,
        mixed: bool,
    ) -> FeishuRelayResponse:
        if self.status_service is None:
            return FeishuRelayResponse("osstack 状态服务当前不可用，请稍后重试。")
        try:
            view = self.status_service.build(principal, scope=scope)
        except PermissionError:
            return FeishuRelayResponse("没有权限查看团队 osstack 状态。")

        def factory(action: str, reference: str) -> str:
            stored_reference = reference
            if action == "device_detail":
                stored_reference = json.dumps(
                    {"scope": view.scope, "device_id": reference},
                    separators=(",", ":"),
                )
            return self.issue_callback(
                principal,
                destination,
                action=action,
                reference=stored_reference,
                ttl_seconds=STATUS_CALLBACK_TTL_SECONDS,
            )

        card = render_osstack_status_card(
            view,
            can_view_team=principal == self.service.policy.owner,
            callback_factory=factory,
        )
        fallback = "osstack 实时状态\n" + format_osstack_status_text(view)
        if mixed:
            fallback += "\n请把修改指令单独发送；本次未下发任何指令。"
        return FeishuRelayResponse(fallback, card)

    def _device_detail(
        self,
        principal: Principal,
        destination: Destination,
        *,
        scope: BoardScope,
        device_id: str,
    ) -> FeishuRelayResponse:
        if self.status_service is None or not isinstance(device_id, str):
            raise ValueError("status service is unavailable")
        view = self.status_service.build(principal, scope=scope)
        device = next(
            (item for item in view.devices if item.device_id == device_id), None
        )
        if device is None:
            raise PermissionError("device is outside authorized status scope")

        def factory(action: str, reference: str) -> str:
            stored_reference = reference
            if action == "device_continue":
                matches = tuple(
                    session for session in device.sessions
                    if session.session_id == reference
                )
                if len(matches) != 1:
                    raise ValueError("session target is ambiguous")
                session = matches[0]
                current = self.store.get_session(
                    session.instance_id, session.session_id, session.runtime_id
                )
                if current is None:
                    raise ValueError("session target no longer exists")
                stored_reference = json.dumps(
                    {
                        "instance_id": session.instance_id,
                        "runtime_id": session.runtime_id,
                        "session_id": session.session_id,
                        "turn_id": current.last_turn_id,
                        "session_version": current.session_version,
                    },
                    separators=(",", ":"),
                )
            elif action == "status_recover":
                stored_reference = scope
            return self.issue_callback(
                principal,
                destination,
                action=action,
                reference=stored_reference,
                ttl_seconds=STATUS_CALLBACK_TTL_SECONDS,
            )

        return FeishuRelayResponse(
            "设备详情",
            render_device_detail_card(device, callback_factory=factory),
        )

    def _result(
        self,
        result: RelayResult,
        principal: Principal,
        destination: Destination,
    ) -> FeishuRelayResponse:
        message_id = result.outbound.message_id if result.outbound else None
        if result.state == "requires_approval":
            return FeishuRelayResponse("该指令涉及高风险操作，必须先通过受治理审批。")
        if result.state == "requires_confirmation" and result.outbound is not None:
            session = self.store.get_session(
                result.outbound.binding.instance_id, result.outbound.binding.session_id
            )
            assert session is not None
            def factory(action: str, ref: str) -> str:
                return self.issue_callback(
                    principal, destination, action=action, reference=ref
                )
            return FeishuRelayResponse(
                "请确认新任务。",
                render_task_proposal_card(
                    session, result.outbound,
                    can_act=self._can_act(principal, session.session_id),
                    callback_factory=factory,
                ),
                message_id,
            )
        source = self.store.envelope_for_message(message_id) if message_id else None
        prefix = "回答已排队" if source and source.kind == "decision.answer" else "指令已排队"
        return FeishuRelayResponse(f"{prefix}，等待本地会话接收。", relay_message_id=message_id)

    def _picker(
        self, principal: Principal, destination: Destination
    ) -> FeishuRelayResponse:
        sessions = self._accessible_sessions(principal)
        if not sessions:
            return FeishuRelayResponse("当前没有可访问的 osstack 会话。")
        elements: list[dict[str, Any]] = [
            {"tag": "div", "text": {"tag": "plain_text", "content": "请选择明确的目标任务"}}
        ]
        for session in sessions[:10]:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": safe_plain_text(
                            f"{session.title}\n{session.instance_id} · {session.agent_id} · {session.state}"
                        ),
                    },
                }
            )
            if self._can_act(principal, session.session_id):
                callback_id = self.issue_callback(
                    principal,
                    destination,
                    action="switch",
                    reference=json.dumps(
                        {
                            "instance_id": session.instance_id,
                            "runtime_id": session.runtime_id,
                            "session_id": session.session_id,
                            "turn_id": session.last_turn_id,
                            "session_version": session.session_version,
                        },
                        separators=(",", ":"),
                    ),
                )
                elements.append(
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "设为当前任务"},
                                "type": "primary",
                                "value": {"callback_id": callback_id},
                            }
                        ],
                    }
                )
        card = {
            "config": {"wide_screen_mode": False, "enable_forward": False},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": "OStag Copilot · 切换任务"},
            },
            "elements": elements,
        }
        if len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()) >= MAX_CARD_BYTES:
            raise ValueError("Feishu task picker exceeds safe encoded size")
        return FeishuRelayResponse("请选择任务。", card)

    def _accessible_sessions(self, principal: Principal) -> tuple[Any, ...]:
        return tuple(
            item
            for item in self.store.list_sessions()
            if self.service.policy.can_access(principal, item.instance_id)
            and item.state not in {"completed", "failed", "cancelled"}
            and self._session_write_allowed(item.session_id)
        )

    def _current_card(
        self, session: Any, principal: Principal, destination: Destination
    ) -> dict[str, Any]:
        def factory(action: str, ref: str) -> str:
            return self.issue_callback(
                principal, destination, action=action, reference=ref
            )
        return render_current_task_card(
            session,
            can_act=self._can_act(principal, session.session_id),
            callback_factory=factory,
        )

    def _session_write_allowed(self, session_id: str) -> bool:
        return not self.canary_session_ids or session_id in self.canary_session_ids

    def _can_act(self, principal: Principal, session_id: str) -> bool:
        return (
            self.write_enabled
            and principal == self.service.policy.owner
            and self._session_write_allowed(session_id)
        )
