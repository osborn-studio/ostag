"""The single authenticated Feishu HTTP callback route."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError

from .feishu_card_security import VerifiedFeishuCallback
from .identity import Destination, IdentityError
from .service import TaskNotFound, WorkMonitorService


def issue_feishu_callback(
    service: WorkMonitorService, principal: Principal, destination: Destination, *,
    action: str, task_id: str, control_action: str = "", now: int | None = None,
) -> str:
    """Persist a compact one-use callback capability bound to identity and destination."""
    callback_id = "f_" + secrets.token_urlsafe(18)
    service.store.create_control_callback(
        callback_id, action=action, task_id=task_id,
        destination_id=destination.destination_id, chat_type=destination.chat_type,
        principal_platform=principal.platform, actor_id=principal.actor_id,
        expires_at=(now if now is not None else int(time.time())) + 300,
        control_action=control_action,
    )
    return callback_id


def create_feishu_card_router(
    service: WorkMonitorService | Callable[[], WorkMonitorService],
    *,
    copilot: object | Callable[[], object | None] | None = None,
    briefs: object | None = None,
) -> APIRouter:
    router = APIRouter()

    def active() -> WorkMonitorService:
        return service() if callable(service) else service

    def active_copilot() -> Any | None:
        return copilot() if callable(copilot) else copilot

    def create_callback(
        principal: Principal, destination: Destination, *, action: str, task_id: str,
        control_action: str = "",
    ) -> str:
        return issue_feishu_callback(
            active(), principal, destination, action=action, task_id=task_id,
            control_action=control_action,
        )

    def raw_card(card: dict[str, object]) -> dict[str, object]:
        """Wrap card JSON for the current card.action.trigger response protocol."""
        return {"type": "raw", "data": card}

    def failure_card(message: str) -> dict[str, object]:
        return {"toast": {"type": "error", "content": message},
                "card": raw_card(
                    {"elements": [{"tag": "markdown", "content": message}]}
                )}

    @router.post("/webhooks/feishu/card-action")
    async def card_action(request: Request) -> dict[str, object]:
        verified = getattr(request.state, 'feishu_callback', None)
        if type(verified) is not VerifiedFeishuCallback:
            raise HTTPException(401, 'verified Feishu callback required')
        if verified.kind == "challenge":
            challenge = verified.payload.get("challenge")
            if challenge is None:
                challenge = verified.payload["event"]["challenge"]
            return {"challenge": challenge}
        assert verified.principal is not None and verified.destination is not None
        value = verified.payload["event"]["action"]["value"]
        if set(value) != {"callback_id"} or not isinstance(value["callback_id"], str):
            return failure_card("操作无效")
        if value['callback_id'].startswith('br_'):
            # FeishuCardSecurity is the mandatory authentication boundary. Never
            # use action-supplied actor, tenant, chat, revision or message fields.
            from ostag.briefs.callbacks import BriefCallbackController

            try:
                if type(briefs) is not BriefCallbackController:
                    raise ValueError('unavailable')
                header = verified.payload['header']
                event = verified.payload['event']
                tenant = header.get('tenant_key')
                if (type(tenant) is not str or not tenant
                        or event['operator'].get('tenant_key', tenant) != tenant):
                    raise ValueError('unavailable')
                card = briefs.handle_callback(
                    value['callback_id'], principal=verified.principal,
                    destination=verified.destination, tenant_id=tenant,
                    app_id=header['app_id'],
                    message_id=event['context'].get('open_message_id'),
                )
                return {'card': raw_card(card)}
            except Exception:
                # A br_ capability never falls through to monitor/relay controls.
                return {'toast': {'type': 'error', 'content': 'Brief preview unavailable'}}
        try:
            relay = active_copilot()
            if value["callback_id"].startswith("cr_") and relay is not None:
                response = relay.handle_callback(
                    value["callback_id"],
                    principal=verified.principal,
                    destination=verified.destination,
                )
                toast_type = (
                    "warning" if "过期" in response.text else "success"
                )
                result: dict[str, object] = {
                    "toast": {"type": toast_type, "content": response.text}
                }
                if response.card is not None:
                    result["card"] = raw_card(response.card)
                return result
            mapped = active().store.consume_control_callback(
                value["callback_id"], principal_platform=verified.principal.platform,
                actor_id=verified.principal.actor_id,
                destination_id=verified.destination.destination_id,
                chat_type="", now=int(time.time()),
            )
            if mapped is None:
                return failure_card("操作已过期或已使用")
            destination = Destination(
                verified.destination.platform, mapped["destination_id"], mapped["chat_type"]
            )
            action, reference = mapped["action"], mapped["task_id"]
            if action == "confirm":
                control = mapped["control_action"]
                if control == "cancel":
                    binding = active().identities.binding(destination)
                    if destination.chat_type != "private" or binding is None \
                            or binding.role != "admin":
                        raise IdentityError("cancel confirmation requires private admin reauth")
                active().approve_control(verified.principal, destination, reference)
                active().apply_control(verified.principal, destination, reference)
                return {"toast": {"type": "success", "content": "操作已确认并下发"}}
            if action not in {"pause", "resume", "retry", "cancel"}:
                raise ValueError("invalid callback action")
            if action == "cancel" and destination.chat_type != "private":
                raise IdentityError("cancel controls require private admin reauth")
            receipt = active().propose_control(
                verified.principal, destination, task_id=reference, action=action
            )
            confirm_id = create_callback(
                verified.principal, destination, action="confirm",
                task_id=receipt.action_id, control_action=action,
            )
            return {
                "toast": {"type": "success", "content": "请再次确认"},
                "card": raw_card(
                    {
                        "elements": [
                            {
                                "tag": "action",
                                "actions": [
                                    {
                                        "tag": "button",
                                        "text": {
                                            "tag": "plain_text",
                                            "content": "确认",
                                        },
                                        "value": {"callback_id": confirm_id},
                                    }
                                ],
                            }
                        ]
                    }
                ),
            }
        except (IdentityError, PermissionError):
            return failure_card("没有权限")
        except (TaskNotFound, ValueError, SchemaError):
            return failure_card("操作失败")

    return router
