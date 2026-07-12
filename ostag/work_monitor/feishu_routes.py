"""The single authenticated Feishu HTTP callback route."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request

from ostag.core.schemas import SchemaError

from .feishu_card_security import VerifiedFeishuCallback
from .identity import IdentityError
from .service import TaskNotFound, WorkMonitorService


def create_feishu_card_router(
    service: WorkMonitorService | Callable[[], WorkMonitorService],
) -> APIRouter:
    router = APIRouter()

    @router.post("/webhooks/feishu/card-action")
    async def card_action(request: Request) -> dict[str, object]:
        verified: VerifiedFeishuCallback = request.state.feishu_callback
        if verified.kind == "challenge":
            return {"challenge": verified.payload["challenge"]}
        assert verified.principal is not None and verified.destination is not None
        value = verified.payload["event"]["action"]["value"]
        if not {"command", "task_id"} <= set(value) or not set(value) <= {
            "command", "task_id", "actor", "chat_type"
        }:
            raise HTTPException(422, "invalid card action value")
        command, task_id = value["command"], value["task_id"]
        if command not in {"pause", "resume", "retry", "cancel"}:
            raise HTTPException(422, "unsupported card action")
        if not isinstance(task_id, str) or not task_id:
            raise HTTPException(422, "task_id is required")
        try:
            active_service = service() if callable(service) else service
            receipt = active_service.propose_control(
                verified.principal, verified.destination, task_id=task_id, action=command
            )
            return {"toast": {"type": "success", "content": "操作已提交审批"},
                    "action_id": receipt.action_id}
        except (IdentityError, PermissionError) as exc:
            raise HTTPException(403, str(exc)) from exc
        except TaskNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, SchemaError) as exc:
            raise HTTPException(422, str(exc)) from exc

    return router
