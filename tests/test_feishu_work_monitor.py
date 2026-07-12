from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI

from ostag.adapters.feishu import FeishuAdapter
from ostag.config import load_settings
from ostag.work_monitor.feishu_cards import render_feishu_card
from ostag.work_monitor.rendering import MobileBoardView, TaskDetailView
from ostag.work_monitor.server import SecurityContract, register_feishu_card_router


def test_feishu_cards_are_vertical_mobile_high_contrast() -> None:
    board = MobileBoardView.from_tasks([
        {"task_id": "task-1", "title": "Ship", "status": "blocked", "progress": 40}
    ])
    card = render_feishu_card(board)
    assert card["config"]["wide_screen_mode"] is False
    assert card["header"]["template"] == "indigo"
    assert [element["tag"] for element in card["elements"]] == ["markdown", "hr"]
    detail = render_feishu_card(TaskDetailView.from_task(
        {"task_id": "task-1", "title": "Ship", "status": "running", "progress": 40},
        capabilities=("pause", "cancel"),
    ))
    assert [element["tag"] for element in detail["elements"]] == ["markdown", "action", "action"]


def test_feishu_route_is_absent_until_strict_registration() -> None:
    app = FastAPI()
    assert "/webhooks/feishu/card-action" not in {route.path for route in app.routes}
    router = APIRouter()
    router.add_api_route("/webhooks/feishu/card-action", lambda: {"ok": True}, methods=["POST"])
    register_feishu_card_router(
        app, router, security=SecurityContract("verified_platform", lambda: None)
    )
    assert "/webhooks/feishu/card-action" in {route.path for route in app.routes}
    extra = APIRouter()
    extra.add_api_route("/webhooks/feishu", lambda: {}, methods=["POST"])
    with pytest.raises(ValueError):
        register_feishu_card_router(
            FastAPI(), extra, security=SecurityContract("verified_platform", lambda: None)
        )


@pytest.mark.asyncio
async def test_proactive_private_uses_open_id_group_uses_chat_id_and_card_falls_back(
    monkeypatch, tmp_path: Path
) -> None:
    requests = []
    config = load_settings(None, audit_log=tmp_path / "audit.log", dry_run=False,
                           feishu_app_id="app", feishu_app_secret="secret")
    adapter = FeishuAdapter(config)
    monkeypatch.setattr(adapter, "_tenant_access_token", lambda: asyncio.sleep(0, result="token"))

    async def fake_to_thread(func, req):
        requests.append(req)
        if json.loads(req.data)["msg_type"] == "interactive":
            raise RuntimeError("card unsupported")
        return {"ok": True}

    monkeypatch.setattr("ostag.adapters.feishu.asyncio.to_thread", fake_to_thread)
    await adapter.send_message("ou_user", "private", chat_type="private")
    await adapter.send_message("oc_group", "group", chat_type="group")
    await adapter.send_card("ou_user", {"elements": []}, "fallback", chat_type="private")
    urls = [request.full_url for request in requests]
    assert "receive_id_type=open_id" in urls[0]
    assert "receive_id_type=chat_id" in urls[1]
    assert json.loads(requests[-1].data)["msg_type"] == "text"


@pytest.mark.asyncio
async def test_ws_callback_uses_supervised_loop_without_per_message_thread(tmp_path: Path) -> None:
    handled = asyncio.Event()

    class Runner:
        async def handle_event(self, event):
            handled.set()
            return ""

    config = load_settings(None, audit_log=tmp_path / "audit.log", dry_run=True)
    adapter = FeishuAdapter(config, runner=Runner())
    adapter._loop = asyncio.get_running_loop()
    adapter._run_live_event_handler({
        "header": {"event_id": "evt"},
        "event": {"message": {"message_id": "msg", "chat_id": "oc", "content": "{\"text\":\"hi\"}"},
                  "sender": {"sender_id": {"open_id": "ou"}}},
    }, adapter.runner)
    await asyncio.wait_for(handled.wait(), timeout=1)
    await adapter.stop()


@pytest.mark.asyncio
async def test_ws_sdk_bridge_disconnects_on_graceful_stop(tmp_path: Path) -> None:
    states: list[str] = []

    class Client:
        async def _connect(self):
            states.append("connected")

        async def _ping_loop(self):
            await asyncio.Event().wait()

        async def _disconnect(self):
            states.append("disconnected")

    config = load_settings(None, audit_log=tmp_path / "audit.log", dry_run=True)
    adapter = FeishuAdapter(config)
    serving = asyncio.create_task(adapter._serve_sdk_client(Client()))
    await asyncio.sleep(0)
    adapter._ws_stop.set()
    await asyncio.wait_for(serving, timeout=1)
    assert states == ["connected", "disconnected"]
