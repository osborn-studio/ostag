from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ostag.adapters.telegram import TelegramAdapter, TelegramBotClient
from ostag.config import Settings
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.server import create_app
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore
from ostag.work_monitor.rendering import TaskDetailView
from ostag.work_monitor.telegram_views import render_telegram_view


def _service(tmp_path: Path) -> WorkMonitorService:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    identities = IdentityStore(store.path)
    identities.bind_private(
        owner=Principal("telegram", "7"),
        destination=Destination("telegram", "7", "private"),
        local_admin=True,
    )
    return WorkMonitorService(store, identities)


async def test_message_and_callback_normalize_verified_identity() -> None:
    adapter = TelegramAdapter(Settings(telegram_bot_token="token"))
    message = await adapter.normalize_event({
        "update_id": 10,
        "message": {"message_id": 4, "from": {"id": 7},
                    "chat": {"id": 8, "type": "supergroup"}, "text": "/board"},
    })
    assert message.principal == Principal("telegram", "7")
    assert message.chat_type == "group" and message.destination_id == "8"
    callback = await adapter.normalize_event({
        "update_id": 11,
        "callback_query": {"id": "cb", "from": {"id": 7}, "data": "opaque",
                           "message": {"message_id": 5,
                                       "chat": {"id": 8, "type": "supergroup"}}},
        "actor": "attacker", "chat_type": "private",
    })
    assert callback.sender_id == "7" and callback.chat_type == "group"
    assert callback.text == "opaque"


def test_webhook_auth_replay_group_allowlist_and_callback_ack(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(method: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append((method, payload))
        return {"ok": True, "result": {}}

    service = _service(tmp_path)
    settings = Settings(
        telegram_bot_token="token", telegram_webhook_secret="secret",
        telegram_webhook_url="https://example.test/webhooks/telegram",
        telegram_group_allowlist=("-100",), monitor_server_db=service.store.path,
    )
    app = create_app(settings=settings, service=service, projector=Projector(service.store),
                     telegram_client=TelegramBotClient("token", transport=transport))
    update = {"update_id": 22, "message": {"message_id": 1, "from": {"id": 7},
              "chat": {"id": 7, "type": "private"}, "text": "/board"}}
    with TestClient(app) as client:
        assert client.post("/webhooks/telegram", json=update).status_code == 401
        headers = {"X-Telegram-Bot-Api-Secret-Token": "secret"}
        assert client.post("/webhooks/telegram", json=update, headers=headers).status_code == 200
        assert client.post("/webhooks/telegram", json=update, headers=headers).status_code == 409
        denied = {"update_id": 23, "message": {"message_id": 2, "from": {"id": 7},
                  "chat": {"id": -200, "type": "group"}, "text": "/board"}}
        assert client.post("/webhooks/telegram", json=denied, headers=headers).status_code == 403
        callback = {"update_id": 24, "callback_query": {"id": "cb-1", "from": {"id": 7},
                    "data": "unknown", "message": {"message_id": 3,
                    "chat": {"id": 7, "type": "private"}}}}
        assert client.post("/webhooks/telegram", json=callback, headers=headers).status_code == 200
    assert ("answerCallbackQuery", {"callback_query_id": "cb-1", "text": "操作无效"}) in calls


def test_client_url_is_not_exposed_and_payload_is_json() -> None:
    seen = []
    client = TelegramBotClient("very-secret", transport=lambda method, payload: seen.append(
        (method, payload)) or {"ok": True, "result": {}})
    client.send_message("7", "hello", reply_markup={"inline_keyboard": []})
    assert seen == [("sendMessage", {"chat_id": "7", "text": "hello", "parse_mode": "HTML",
                                     "reply_markup": {"inline_keyboard": []}})]
    assert "very-secret" not in repr(client)


def test_production_telegram_configuration_fails_closed() -> None:
    try:
        Settings(env="production", platform="telegram").validate()
    except ValueError as exc:
        assert "Telegram" in str(exc)
    else:
        raise AssertionError("incomplete Telegram production configuration accepted")


def test_telegram_html_is_escaped_and_callback_is_opaque_and_compact() -> None:
    view = TaskDetailView.from_task(
        {"task_id": "t<&", "title": "<script>&", "status": "running", "progress": 5},
        capabilities=("pause",),
    )
    text, markup = render_telegram_view(view, callback_factory=lambda action, task: "w_safe")
    assert "<script>" not in text and "&lt;script&gt;&amp;" in text
    assert markup == {"inline_keyboard": [[{"text": "PAUSE", "callback_data": "w_safe"}]]}
    assert len("w_safe".encode()) <= 64
