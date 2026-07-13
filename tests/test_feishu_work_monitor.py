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
from ostag.config import Settings, load_settings
from ostag.core.event import NormalizedEvent
from ostag.work_monitor.feishu_card_security import feishu_signature
from ostag.work_monitor.feishu_cards import MAX_CARD_BYTES, render_feishu_card
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
from ostag.work_monitor.transport import TransportError


@pytest.mark.asyncio
async def test_feishu_http_200_business_error_is_typed(monkeypatch) -> None:
    settings = Settings(dry_run=False, feishu_app_id="id", feishu_app_secret="secret")
    adapter = FeishuAdapter(settings)
    monkeypatch.setattr(adapter, "_tenant_access_token", lambda: None)

    async def token():
        return "token"

    monkeypatch.setattr(adapter, "_tenant_access_token", token)
    monkeypatch.setattr("ostag.adapters.feishu._urlopen_json", lambda request: {"code": 230001})
    with pytest.raises(TransportError) as exc:
        await adapter.send_message("chat", "hello", delivery_id="stable")
    assert exc.value.status_code == 400
    assert exc.value.transient is False


def test_feishu_cards_restore_operations_minimalist_hierarchy() -> None:
    board = MobileBoardView.from_tasks(
        [
            {"task_id": "task-1", "title": "Ship", "status": "blocked", "progress": 40},
            {"task_id": "task-2", "title": "Verify", "status": "running", "progress": 70},
            {"task_id": "task-3", "title": "Done", "status": "done", "progress": 100},
        ]
    )
    card = render_feishu_card(board)
    encoded = json.dumps(card, ensure_ascii=False)

    assert card["config"]["wide_screen_mode"] is False
    assert card["header"]["template"] == "blue"
    assert card["header"]["title"]["content"] == "OStag Control · 今日看板"
    assert any(element["tag"] == "column_set" for element in card["elements"])
    assert "需要你处理" in encoded
    assert "运行中的任务" in encoded
    assert "六阶段概览" in encoded
    assert "🔴" in encoded and "🟢" in encoded and "🔵" in encoded
    assert len(encoded.encode()) <= MAX_CARD_BYTES


def test_feishu_task_detail_only_exposes_governed_callbacks() -> None:
    detail = render_feishu_card(
        TaskDetailView.from_task(
            {"task_id": "task-1", "title": "Ship", "status": "running", "progress": 40},
            capabilities=("pause", "cancel"),
        ), callback_factory=lambda action, task: f"f_{action}",
    )
    assert [element["tag"] for element in detail["elements"]] == ["markdown", "action", "action"]
    assert detail["elements"][1]["actions"][0]["value"] == {"callback_id": "f_pause"}


def test_feishu_cards_escape_untrusted_markdown_and_bound_elements() -> None:
    view = MobileBoardView(
        cards=tuple(
            {
                "task_id": f"task-{index}",
                "title": "<script>*boom*</script>",
                "status": "blocked",
                "progress": 1,
            }
            for index in range(30)
        ),
        page=1,
        page_size=30,
        total=30,
    )
    card = render_feishu_card(view)
    assert len(card["elements"]) <= 20
    encoded = json.dumps(card, ensure_ascii=False)
    assert "<script>" not in encoded and "&lt;script&gt;" in encoded and "boom" in encoded
    assert len(encoded.encode()) <= MAX_CARD_BYTES


@pytest.mark.asyncio
async def test_ws_private_board_card_is_sent_to_sender_open_id(
    monkeypatch, tmp_path: Path
) -> None:
    sent = []

    class Service:
        def board(self, principal, destination, page=1):
            return MobileBoardView.from_tasks(
                [{"task_id": "task-1", "title": "Ship", "status": "running"}]
            )

    class Runner:
        work_monitor = Service()

    adapter = FeishuAdapter(load_settings(None, audit_log=tmp_path / "audit.log"))

    async def capture(*args, **kwargs):
        sent.append((args, kwargs))

    monkeypatch.setattr(adapter, "send_card", capture)
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt-private-board",
        chat_id="oc_private",
        message_id="msg-private",
        sender_id="ou_user",
        text="用看板形式汇报目前进展",
        mention_bot=True,
        chat_type="private",
        destination_id="oc_private",
    )

    assert await adapter._handle_monitor_card(event, Runner()) is True
    assert sent[0][0][0] == "ou_user"
    assert sent[0][1]["chat_type"] == "private"


@pytest.mark.asyncio
async def test_ws_monitor_task_uses_production_card_helper_and_persists_opaque_button(
    monkeypatch, tmp_path: Path
) -> None:
    mappings = []
    sent = []
    handled = []

    class Store:
        def create_control_callback(self, callback_id, **mapping):
            mappings.append((callback_id, mapping))

    class Identities:
        def allows(self, principal, destination, capability):
            return capability == "write"

    class Service:
        store = Store()
        identities = Identities()

        def handle_event(self, event):
            handled.append(event.text)
            return "fallback task"

        def task(self, principal, destination, reference):
            return TaskDetailView.from_task(
                {"task_id": reference, "title": "Ship", "status": "running", "progress": 20},
                capabilities=("pause",),
            )

    class Runner:
        work_monitor = Service()

    config = load_settings(None, audit_log=tmp_path / "audit.log", dry_run=False,
                           feishu_app_id="app", feishu_app_secret="secret")
    adapter = FeishuAdapter(config)

    async def capture(*args, **kwargs):
        sent.append((args, kwargs))

    monkeypatch.setattr(adapter, "send_card", capture)
    event = NormalizedEvent(
        platform="feishu", event_id="evt-1", chat_id="oc_chat", message_id="msg-1",
        sender_id="ou_user", text="/task task-1", mention_bot=True, chat_type="group",
        destination_id="oc_chat",
    )
    assert await adapter._handle_monitor_card(event, Runner()) is True
    value = sent[0][0][1]["elements"][1]["actions"][0]["value"]
    assert set(value) == {"callback_id"} and value["callback_id"].startswith("f_")
    assert mappings[0][1]["task_id"] == "task-1"
    assert sent[0][1]["delivery_id"] == "monitor-evt-1"
    assert handled == []


@pytest.mark.asyncio
async def test_ws_rich_dispatch_leaves_watch_to_runner_once_and_replies_for_missing_task(
    monkeypatch, tmp_path: Path
) -> None:
    calls = []

    class Service:
        def handle_event(self, event):
            calls.append(event.text)
            return "未找到该任务。"

        def task(self, principal, destination, reference):
            raise LookupError(reference)

    class Runner:
        work_monitor = Service()

        async def handle_event(self, event):
            return self.work_monitor.handle_event(event)

    adapter = FeishuAdapter(load_settings(None, audit_log=tmp_path / "audit.log"))
    replies = []

    async def reply(event, text):
        replies.append(text)

    monkeypatch.setattr(adapter, "reply", reply)
    watch = NormalizedEvent(
        platform="feishu", event_id="watch", chat_id="oc", message_id="m1",
        sender_id="ou", text="/watch task-1", mention_bot=True, chat_type="group",
        destination_id="oc",
    )
    assert await adapter._handle_monitor_card(watch, Runner()) is False
    await Runner().handle_event(watch)
    assert calls == ["/watch task-1"]
    missing = NormalizedEvent(
        platform="feishu", event_id="missing", chat_id="oc", message_id="m2",
        sender_id="ou", text="/task missing", mention_bot=True, chat_type="group",
        destination_id="oc",
    )
    assert await adapter._handle_monitor_card(missing, Runner()) is True
    assert calls[-1] == "/task missing" and replies == ["未找到该任务。"]


def test_feishu_route_is_absent_until_strict_registration() -> None:
    app = FastAPI()
    with TestClient(app) as client:
        assert client.post("/webhooks/feishu/card-action").status_code == 404
    router = APIRouter()
    router.add_api_route("/webhooks/feishu/card-action", lambda: {"ok": True}, methods=["POST"])
    register_feishu_card_router(
        app, router, security=SecurityContract("verified_platform", lambda: None)
    )
    with TestClient(app) as client:
        assert client.post("/webhooks/feishu/card-action").json() == {"ok": True}
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
    absent = create_app(settings=load_settings(None, audit_log=tmp_path / "audit.log"), **base)
    configured = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        feishu_app_id="app",
        feishu_app_secret="secret",
        feishu_encrypt_key="encrypt",
        feishu_verification_token="verify",
        feishu_use_websocket=False,
    )
    present = create_app(settings=configured, **base)
    challenge = json.dumps(
        {
            "schema": "2.0",
            "header": {
                "event_type": "url_verification",
                "token": "verify",
                "app_id": "app",
            },
            "event": {"challenge": "verified"},
        },
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
        assert (
            client.post(
                "/webhooks/feishu/card-action",
                content=challenge,
                headers=headers | {"X-Lark-Signature": "bad"},
            ).status_code
            == 401
        )
        assert client.post(
            "/webhooks/feishu/card-action", content=challenge, headers=headers
        ).json() == {"challenge": "verified"}
        assert (
            client.post("/webhooks/feishu", content=challenge, headers=headers).status_code == 404
        )


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
        None,
        audit_log=tmp_path / "audit.log",
        feishu_app_id="app",
        feishu_app_secret="secret",
        monitor_server_db=store.path,
    )
    app = create_app(
        settings=settings,
        service=WorkMonitorService(store, IdentityStore(store.path)),
        projector=Projector(store),
        auth=ServerAuth({}),
        supervisor_backoff=(0.001, 0.002),
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
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        dry_run=False,
        feishu_app_id="app",
        feishu_app_secret="secret",
    )
    adapter = FeishuAdapter(config)
    monkeypatch.setattr(adapter, "_tenant_access_token", lambda: asyncio.sleep(0, result="token"))

    async def fake_to_thread(func, req):
        requests.append(req)
        if json.loads(req.data)["msg_type"] == "interactive":
            raise RuntimeError("card unsupported")
        return {"ok": True}

    monkeypatch.setattr("ostag.adapters.feishu.asyncio.to_thread", fake_to_thread)
    await adapter.send_message("ou_user", "private", chat_type="private", delivery_id="private-1")
    await adapter.send_message("oc_group", "group", chat_type="group", delivery_id="group-1")
    await adapter.send_card(
        "ou_user", {"elements": []}, "fallback", chat_type="private", delivery_id="card-1"
    )
    urls = [request.full_url for request in requests]
    assert "receive_id_type=open_id" in urls[0]
    assert "receive_id_type=chat_id" in urls[1]
    assert json.loads(requests[-1].data)["msg_type"] == "text"


@pytest.mark.asyncio
async def test_card_business_error_falls_back_once_with_stable_idempotency_key(
    monkeypatch, tmp_path: Path
) -> None:
    requests = []
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        dry_run=False,
        feishu_app_id="app",
        feishu_app_secret="secret",
    )
    adapter = FeishuAdapter(config)
    monkeypatch.setattr(adapter, "_tenant_access_token", lambda: asyncio.sleep(0, result="token"))

    async def fake_to_thread(func, req):
        requests.append(req)
        return {"code": 999} if json.loads(req.data)["msg_type"] == "interactive" else {"code": 0}

    monkeypatch.setattr("ostag.adapters.feishu.asyncio.to_thread", fake_to_thread)
    await adapter.send_card("oc_group", {"elements": []}, "fallback", delivery_id="delivery-1")
    assert sum(json.loads(req.data)["msg_type"] == "interactive" for req in requests) == 1
    assert "uuid=delivery-1" in requests[0].full_url
    assert "uuid=delivery-1" in requests[-1].full_url
    assert json.loads(requests[-1].data)["msg_type"] == "text"

    with pytest.raises((TypeError, ValueError)):
        await adapter.send_card("oc_group", {"elements": []}, "fallback")
    with pytest.raises((TypeError, ValueError)):
        await adapter.send_message("oc_group", "message")


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
    adapter._run_live_event_handler(
        {
            "header": {"event_id": "evt"},
            "event": {
                "message": {"message_id": "msg", "chat_id": "oc", "content": '{"text":"hi"}'},
                "sender": {"sender_id": {"open_id": "ou"}},
            },
        },
        adapter.runner,
    )
    await asyncio.wait_for(handled.wait(), timeout=1)
    await adapter.stop()


@pytest.mark.asyncio
async def test_ws_sdk_bridge_disconnects_on_graceful_stop(tmp_path: Path) -> None:
    states: list[str] = []
    handled = asyncio.Event()

    class Runner:
        async def handle_event(self, event):
            states.append("handled")
            handled.set()
            return ""

    class Client:
        async def _disconnect(self):
            states.append("disconnected")

    def public_start_bridge() -> None:
        sdk_loop = asyncio.new_event_loop()
        adapter._sdk_loop = sdk_loop
        adapter._client = Client()
        states.append("started")
        sdk_loop.call_soon_threadsafe(
            adapter._run_live_event_handler,
            {
                "header": {"event_id": "evt"},
                "event": {
                    "message": {"message_id": "msg", "chat_id": "oc", "content": '{"text":"hi"}'},
                    "sender": {"sender_id": {"open_id": "ou"}},
                },
            },
            adapter.runner,
        )
        sdk_loop.run_forever()
        sdk_loop.close()

    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        dry_run=True,
        feishu_app_id="app",
        feishu_app_secret="secret",
    )
    adapter = FeishuAdapter(config, runner=Runner())
    adapter._start_blocking = lambda runner: public_start_bridge()
    stop = asyncio.Event()
    serving = asyncio.create_task(adapter.run(stop))
    await asyncio.wait_for(handled.wait(), timeout=1)
    stop.set()
    await asyncio.wait_for(serving, timeout=2)
    assert states == ["started", "handled", "disconnected"]


@pytest.mark.asyncio
async def test_ws_bridge_immediate_stop_repeatedly_leaves_no_thread(tmp_path: Path) -> None:
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        dry_run=True,
        feishu_app_id="app",
        feishu_app_secret="secret",
    )
    before = {thread.ident for thread in threading.enumerate()}
    for _ in range(5):
        adapter = FeishuAdapter(config, runner=object())

        def public_start(runner):
            adapter._client = object()
            adapter._sdk_loop = asyncio.new_event_loop()
            adapter._bridge_ready.set()
            if adapter._stop_requested.is_set():
                adapter._sdk_loop.close()
                return
            raise AssertionError("early stop entered client.start")

        adapter._start_blocking = public_start
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(adapter.run(stop), timeout=1)
    leaked = [
        thread
        for thread in threading.enumerate()
        if thread.ident not in before and thread.name == "ostag-feishu-ws-bridge"
    ]
    assert leaked == []
