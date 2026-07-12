"""Authenticated Telegram webhook for work-monitor messages and callbacks."""

from __future__ import annotations

import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request

from ostag.adapters.telegram import TelegramAdapter, TelegramBotClient
from ostag.core.event import NormalizedEvent
from ostag.core.identity import Principal

from .identity import Destination, IdentityError
from .service import AmbiguousTask, TaskNotFound, WorkMonitorService, parse_monitor_command
from .telegram_views import render_telegram_view


@dataclass(frozen=True)
class VerifiedTelegramUpdate:
    update_id: int
    event: NormalizedEvent
    callback_query_id: str = ""


class TelegramWebhookSecurity:
    """Authenticate before bounded parsing, authorization, and replay persistence."""

    def __init__(
        self, *, secret: str, service: Callable[[], WorkMonitorService],
        adapter: TelegramAdapter, group_allowlist: tuple[str, ...] = (),
        max_body_bytes: int = 1_048_576,
    ) -> None:
        if not secret:
            raise ValueError("Telegram webhook secret is required")
        self._secret = secret
        self._service = service
        self._adapter = adapter
        self._groups = frozenset(group_allowlist)
        self._limit = max_body_bytes

    async def __call__(self, request: Request) -> VerifiedTelegramUpdate:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied, self._secret):
            raise HTTPException(401, "invalid Telegram webhook secret")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > self._limit:
                raise HTTPException(413, "Telegram update too large")
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("update must be an object")
            event = await self._adapter.normalize_event(payload)
            update_id = payload["update_id"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, "invalid Telegram update") from exc
        if event.chat_type == "group" and event.destination_id not in self._groups:
            raise HTTPException(403, "Telegram group is not allowlisted")
        if not self._service().store.consume_telegram_update(update_id):
            raise HTTPException(409, "replayed Telegram update")
        callback = payload.get("callback_query")
        callback_id = str(callback.get("id", "")) if isinstance(callback, dict) else ""
        verified = VerifiedTelegramUpdate(update_id, event, callback_id)
        request.state.telegram_update = verified
        return verified


def create_telegram_router(
    service: WorkMonitorService | Callable[[], WorkMonitorService], client: TelegramBotClient,
    *, admin_ids: tuple[str, ...] = (),
) -> APIRouter:
    router = APIRouter()
    admins = frozenset(admin_ids)

    def active() -> WorkMonitorService:
        return service() if callable(service) else service

    def callback_factory(
        principal: Principal, destination: Destination,
    ) -> Callable[[str, str], str]:
        def create(action: str, task_id: str, *, control_action: str = "") -> str:
            callback_id = "w_" + secrets.token_urlsafe(18)
            active().store.create_telegram_callback(
                callback_id, action=action, task_id=task_id,
                destination_id=destination.destination_id, chat_type=destination.chat_type,
                principal_platform=principal.platform, actor_id=principal.actor_id,
                expires_at=int(time.time()) + 300,
                control_action=control_action,
            )
            return callback_id
        return create

    def require_private_admin(event: NormalizedEvent, destination: Destination) -> None:
        binding = active().identities.binding(destination)
        if event.chat_type != "private" or binding is None or binding.role != "admin" \
                or (admins and event.sender_id not in admins):
            raise IdentityError("cancel confirmation requires a private admin chat")

    def send_view(event: NormalizedEvent, view: object) -> None:
        destination = Destination("telegram", event.destination_id, event.chat_type)
        text, markup = render_telegram_view(
            view, callback_factory=callback_factory(event.principal, destination)  # type: ignore[arg-type]
        )
        client.send_message(event.destination_id, text, reply_markup=markup)

    @router.post("/webhooks/telegram")
    async def telegram_webhook(request: Request) -> dict[str, bool]:
        verified: VerifiedTelegramUpdate = request.state.telegram_update
        event = verified.event
        principal = event.principal
        destination = Destination("telegram", event.destination_id, event.chat_type)
        if verified.callback_query_id:
            answer = "操作无效"
            try:
                mapped = active().store.consume_telegram_callback(
                    event.text, principal_platform=principal.platform, actor_id=principal.actor_id,
                    destination_id=event.destination_id, chat_type=event.chat_type,
                    now=int(time.time()),
                )
                if mapped is None:
                    return {"ok": True}
                action, reference = mapped["action"], mapped["task_id"]
                if action == "page":
                    send_view(event, active().board(principal, destination, page=int(reference)))
                    answer = "已翻页"
                elif action == "confirm":
                    original_action = mapped["control_action"]
                    if original_action not in {"pause", "resume", "retry", "cancel"}:
                        raise ValueError("confirmed control action is missing or invalid")
                    if original_action == "cancel":
                        require_private_admin(event, destination)
                    active().approve_control(principal, destination, reference)
                    active().apply_control(principal, destination, reference)
                    answer = "操作已确认并下发"
                else:
                    if action == "cancel":
                        require_private_admin(event, destination)
                    receipt = active().propose_control(
                        principal, destination, task_id=reference, action=action
                    )
                    answer = f"已提交审批 {receipt.action_id}"
                    confirm_id = callback_factory(principal, destination)(
                        "confirm", receipt.action_id, control_action=action
                    )
                    client.send_message(event.destination_id, "请再次确认操作", reply_markup={
                        "inline_keyboard": [[{"text": "确认", "callback_data": confirm_id}]]
                    })
            except (IdentityError, PermissionError):
                answer = "没有权限"
            except (TaskNotFound, AmbiguousTask, ValueError):
                answer = "操作失败"
            finally:
                client.answer_callback(verified.callback_query_id, answer)
            return {"ok": True}
        try:
            command = parse_monitor_command(event.text)
            if command is None:
                client.send_message(event.destination_id, "可用命令：/board /task /instances /watch")
            elif command.name == "board":
                page = int(command.argument or "1")
                send_view(event, active().board(principal, destination, page=page))
            elif command.name == "task":
                send_view(event, active().task(principal, destination, command.argument))
            elif command.name == "instances":
                instances = active().instances(principal, destination)
                text = "<b>实例</b>\n" + "\n".join(
                    f"<code>{html_escape(item['instance_id'])}</code>" for item in instances
                )
                client.send_message(event.destination_id, text)
            elif command.name == "watch":
                message = active().watch(principal, destination, task_id=command.argument)
                client.send_message(event.destination_id, html_escape(message))
        except (IdentityError, PermissionError):
            client.send_message(event.destination_id, "没有权限")
        except (TaskNotFound, AmbiguousTask, ValueError):
            client.send_message(event.destination_id, "请求无效")
        return {"ok": True}

    return router


def html_escape(value: object) -> str:
    import html
    return html.escape(str(value), quote=True)
