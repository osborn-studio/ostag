from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from ostag.adapters.feishu import FeishuAdapter
from ostag.config import load_settings
from ostag.work_monitor.feishu_card_security import feishu_signature
from ostag.work_monitor.feishu_cards import render_feishu_card
from ostag.work_monitor.identity import IdentityStore
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.rendering import MobileBoardView, TaskDetailView
from ostag.work_monitor.server import (
    SecurityContract,
    ServerAuth,
    create_app,
    register_feishu_card_router,
)
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore


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


def test_production_composition_registers_cards_only_when_fully_configured(tmp_path: Path) -> None:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    service = WorkMonitorService(store, IdentityStore(store.path))
    base = dict(service=service, projector=Projector(store), auth=ServerAuth({}))
    absent = create_app(
        settings=load_settings(None, audit_log=tmp_path / "audit.log"), **base
    )
    configured = load_settings(
        None, audit_log=tmp_path / "audit.log", feishu_app_id="app",
        feishu_app_secret="secret", feishu_encrypt_key="encrypt",
        feishu_verification_token="verify", feishu_use_websocket=False,
    )
    present = create_app(settings=configured, **base)
    challenge = json.dumps(
        {"challenge": "verified", "token": "verify", "type": "url_verification"},
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    headers = {
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": "composition-challenge",
        "X-Lark-Signature": feishu_signature(
            timestamp, "composition-challenge", "encrypt", challenge
        ),
        "Content-Type": "application/json",
    }
    with TestClient(absent) as client:
        assert client.post("/webhooks/feishu/card-action", content=challenge).status_code == 404
    with TestClient(present) as client:
        assert client.post(
            "/webhooks/feishu/card-action", content=challenge,
            headers=headers | {"X-Lark-Signature": "bad"},
        ).status_code == 401
        assert client.post(
            "/webhooks/feishu/card-action", content=challenge, headers=headers
        ).json() == {"challenge": "verified"}
        assert client.post("/webhooks/feishu", content=challenge, headers=headers).status_code == 404


def test_production_composition_supervises_auto_ws_restart_and_shutdown(
    monkeypatch, tmp_path: Path
) -> None:
    starts = 0
    stopped = threading.Event()

    def fake_start(self, runner):
        nonlocal starts
        starts += 1
        if starts == 1:
            raise RuntimeError("crash")
        while not self._ws_stop.wait(0.005):
            pass
        stopped.set()

    monkeypatch.setattr("ostag.adapters.feishu.FeishuAdapter._start_blocking", fake_start)
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    settings = load_settings(
        None, audit_log=tmp_path / "audit.log", feishu_app_id="app",
        feishu_app_secret="secret", monitor_server_db=store.path,
    )
    app = create_app(
        settings=settings, service=WorkMonitorService(store, IdentityStore(store.path)),
        projector=Projector(store), auth=ServerAuth({}), supervisor_backoff=(0.001, 0.002),
    )
    with TestClient(app):
        deadline = time.monotonic() + 1
        while starts < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert starts >= 2
    assert stopped.wait(1)


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
