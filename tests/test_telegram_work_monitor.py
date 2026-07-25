from __future__ import annotations

import json
from pathlib import Path

import pytest
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
from ostag.work_monitor.transport import TransportError


def test_telegram_business_error_preserves_retry_after() -> None:
    client = TelegramBotClient(
        "placeholder",
        transport=lambda method, payload: {
            "ok": False,
            "error_code": 429,
            "parameters": {"retry_after": 17},
        },
    )
    with pytest.raises(TransportError) as exc:
        client.send_message("1", "hello")
    assert exc.value.status_code == 429
    assert exc.value.retry_after_seconds == 17
    assert exc.value.transient is True


def _service(tmp_path: Path) -> WorkMonitorService:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    identities = IdentityStore(store.path)
    identities.bind_private(
        owner=Principal("telegram", "7"),
        destination=Destination("telegram", "7", "private"),
        local_admin=True,
    )
    return WorkMonitorService(store, identities)


def _seed_task(service: WorkMonitorService, capabilities: tuple[str, ...]) -> None:
    with service.store.connect() as db:
        db.execute("INSERT INTO instances(instance_id,last_sequence) VALUES ('runner-1',1)")
        db.execute(
            "INSERT INTO tasks(instance_id,task_id,title,project,status,agent,progress,"
            "duration_seconds,summary,error_class,evidence_ref,occurred_at,version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "runner-1",
                "task-1",
                "Ship <safe>",
                "ostag",
                "running",
                "codex",
                10,
                1,
                "",
                None,
                None,
                "2026-07-13T00:00:00+00:00",
                1,
            ),
        )
        payload = json.dumps({"per_task_capabilities": {"task-1": list(capabilities)}})
        db.execute(
            "INSERT INTO events(event_id,instance_id,sequence,kind,occurred_at,payload) "
            "VALUES (?,?,?,?,?,?)",
            (
                "heartbeat",
                "runner-1",
                1,
                "instance.heartbeat",
                "2026-07-13T00:00:00+00:00",
                payload,
            ),
        )


async def test_message_and_callback_normalize_verified_identity() -> None:
    adapter = TelegramAdapter(Settings(telegram_bot_token="token"))
    message = await adapter.normalize_event(
        {
            "update_id": 10,
            "message": {
                "message_id": 4,
                "from": {"id": 7},
                "chat": {"id": 8, "type": "supergroup"},
                "text": "/board",
            },
        }
    )
    assert message.principal == Principal("telegram", "7")
    assert message.chat_type == "group" and message.destination_id == "8"
    callback = await adapter.normalize_event(
        {
            "update_id": 11,
            "callback_query": {
                "id": "cb",
                "from": {"id": 7},
                "data": "opaque",
                "message": {"message_id": 5, "chat": {"id": 8, "type": "supergroup"}},
            },
            "actor": "attacker",
            "chat_type": "private",
        }
    )
    assert callback.sender_id == "7" and callback.chat_type == "group"
    assert callback.text == "opaque"


def test_webhook_auth_replay_group_allowlist_and_callback_ack(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(method: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append((method, payload))
        return {"ok": True, "result": {}}

    service = _service(tmp_path)
    settings = Settings(
        telegram_bot_token="token",
        telegram_webhook_secret="secret",
        telegram_webhook_url="https://example.test/webhooks/telegram",
        telegram_group_allowlist=("-100",),
        monitor_server_db=service.store.path,
    )
    app = create_app(
        settings=settings,
        service=service,
        projector=Projector(service.store),
        telegram_client=TelegramBotClient("token", transport=transport),
    )
    update = {
        "update_id": 22,
        "message": {
            "message_id": 1,
            "from": {"id": 7},
            "chat": {"id": 7, "type": "private"},
            "text": "/board",
        },
    }
    with TestClient(app) as client:
        assert client.post("/webhooks/telegram", json=update).status_code == 401
        headers = {"X-Telegram-Bot-Api-Secret-Token": "secret"}
        assert client.post("/webhooks/telegram", json=update, headers=headers).status_code == 200
        calls_after_first_delivery = list(calls)
        assert client.post("/webhooks/telegram", json=update, headers=headers).status_code == 200
        assert calls == calls_after_first_delivery
        denied = {
            "update_id": 23,
            "message": {
                "message_id": 2,
                "from": {"id": 7},
                "chat": {"id": -200, "type": "group"},
                "text": "/board",
            },
        }
        assert client.post("/webhooks/telegram", json=denied, headers=headers).status_code == 403
        callback = {
            "update_id": 24,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": 7},
                "data": "unknown",
                "message": {"message_id": 3, "chat": {"id": 7, "type": "private"}},
            },
        }
        assert client.post("/webhooks/telegram", json=callback, headers=headers).status_code == 200
    assert ("answerCallbackQuery", {"callback_query_id": "cb-1", "text": "操作无效"}) in calls


def test_client_url_is_not_exposed_and_payload_is_json() -> None:
    seen = []
    client = TelegramBotClient(
        "very-secret",
        transport=lambda method, payload: (
            seen.append((method, payload)) or {"ok": True, "result": {}}
        ),
    )
    client.send_message("7", "hello", reply_markup={"inline_keyboard": []})
    assert seen == [
        (
            "sendMessage",
            {
                "chat_id": "7",
                "text": "hello",
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": []},
            },
        )
    ]
    assert "very-secret" not in repr(client)


def test_production_telegram_configuration_fails_closed() -> None:
    try:
        Settings(env="production", platform="telegram").validate()
    except ValueError as exc:
        assert "Telegram" in str(exc)
    else:
        raise AssertionError("incomplete Telegram production configuration accepted")
    with pytest.raises(ValueError, match="Telegram"):
        create_app(settings=Settings(env="production", platform="telegram"))
    with pytest.raises(ValueError, match="HTTPS"):
        create_app(
            settings=Settings(
                env="production",
                platform="telegram",
                telegram_bot_token="token",
                telegram_webhook_secret="secret",
                telegram_webhook_url="http://unsafe.test/hook",
            )
        )


@pytest.mark.asyncio
async def test_normalization_rejects_bool_ids_and_container_callback_data() -> None:
    adapter = TelegramAdapter(Settings(telegram_bot_token="token"))
    with pytest.raises(ValueError):
        await adapter.normalize_event(
            {
                "update_id": True,
                "message": {
                    "message_id": 1,
                    "from": {"id": 7},
                    "chat": {"id": 7, "type": "private"},
                    "text": "/board",
                },
            }
        )
    with pytest.raises(ValueError):
        await adapter.normalize_event(
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cb",
                    "from": {"id": 7},
                    "data": {"action": "pause"},
                    "message": {"message_id": 1, "chat": {"id": 7, "type": "private"}},
                },
            }
        )


def test_telegram_html_is_escaped_and_callback_is_opaque_and_compact() -> None:
    view = TaskDetailView.from_task(
        {"task_id": "t<&", "title": "<script>&", "status": "running", "progress": 5},
        capabilities=("pause",),
    )
    text, markup = render_telegram_view(view, callback_factory=lambda action, task: "w_safe")
    assert "<script>" not in text and "&lt;script&gt;&amp;" in text
    assert markup == {"inline_keyboard": [[{"text": "PAUSE", "callback_data": "w_safe"}]]}
    assert len("w_safe".encode()) <= 64


def test_governed_callback_propose_then_confirm_applies_exactly_once(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(method: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append((method, payload))
        return {"ok": True, "result": {}}

    service = _service(tmp_path)
    _seed_task(service, ("pause",))
    settings = Settings(
        telegram_bot_token="token",
        telegram_webhook_secret="secret",
        telegram_webhook_url="https://example.test/webhooks/telegram",
        monitor_server_db=service.store.path,
    )
    app = create_app(
        settings=settings,
        service=service,
        projector=Projector(service.store),
        telegram_client=TelegramBotClient("token", transport=transport),
    )
    headers = {"X-Telegram-Bot-Api-Secret-Token": "secret"}

    def callback(update_id: int, callback_id: str) -> dict[str, object]:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"q-{update_id}",
                "from": {"id": 7},
                "data": callback_id,
                "message": {"message_id": update_id, "chat": {"id": 7, "type": "private"}},
            },
        }

    with TestClient(app) as client:
        message = {
            "update_id": 30,
            "message": {
                "message_id": 1,
                "from": {"id": 7},
                "chat": {"id": 7, "type": "private"},
                "text": "/task task-1",
            },
        }
        assert client.post("/webhooks/telegram", json=message, headers=headers).status_code == 200
        markup = calls[-1][1]["reply_markup"]
        action_id = markup["inline_keyboard"][0][0]["callback_data"]  # type: ignore[index]
        assert (
            client.post(
                "/webhooks/telegram", json=callback(31, action_id), headers=headers
            ).status_code
            == 200
        )
        with service.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
        confirm_id = next(
            payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]  # type: ignore[index]
            for method, payload in reversed(calls)
            if method == "sendMessage" and payload.get("text") == "请再次确认操作"
        )
        assert (
            client.post(
                "/webhooks/telegram", json=callback(32, confirm_id), headers=headers
            ).status_code
            == 200
        )
        with service.store.connect() as db:
            commands = db.execute("SELECT action_id,status FROM commands").fetchall()
        assert len(commands) == 1 and commands[0]["status"] == "pending"
        assert service.ledger.get_receipt(commands[0]["action_id"]).state == "applying"
        assert len(service.poll_commands("runner-1")) == 1
        assert (
            client.post(
                "/webhooks/telegram", json=callback(33, confirm_id), headers=headers
            ).status_code
            == 200
        )
        service.store.create_telegram_callback(
            "w_expired",
            action="pause",
            task_id="task-1",
            destination_id="7",
            chat_type="private",
            principal_platform="telegram",
            actor_id="7",
            expires_at=1,
        )
        assert (
            client.post(
                "/webhooks/telegram", json=callback(34, "w_expired"), headers=headers
            ).status_code
            == 200
        )
        with service.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1


def test_cancel_group_is_rejected_but_private_admin_can_confirm(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(method: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append((method, payload))
        return {"ok": True, "result": {}}

    service = _service(tmp_path)
    _seed_task(service, ("cancel",))
    group = Destination("telegram", "-100", "group")
    service.identities.enroll_group(
        Principal("telegram", "7"), group, scope="all", local_admin=True
    )
    settings = Settings(
        telegram_bot_token="token",
        telegram_webhook_secret="secret",
        telegram_webhook_url="https://example.test/webhooks/telegram",
        telegram_group_allowlist=("-100",),
        telegram_admin_ids=("7",),
        monitor_server_db=service.store.path,
    )
    app = create_app(
        settings=settings,
        service=service,
        projector=Projector(service.store),
        telegram_client=TelegramBotClient("token", transport=transport),
    )
    headers = {"X-Telegram-Bot-Api-Secret-Token": "secret"}

    def callback(update_id: int, data: str, chat_id: int, chat_type: str) -> dict[str, object]:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"q-{update_id}",
                "from": {"id": 7},
                "data": data,
                "message": {"message_id": update_id, "chat": {"id": chat_id, "type": chat_type}},
            },
        }

    service.store.create_telegram_callback(
        "w_group",
        action="cancel",
        task_id="task-1",
        destination_id="-100",
        chat_type="group",
        principal_platform="telegram",
        actor_id="7",
        expires_at=2**31,
    )
    service.store.create_telegram_callback(
        "w_private",
        action="cancel",
        task_id="task-1",
        destination_id="7",
        chat_type="private",
        principal_platform="telegram",
        actor_id="7",
        expires_at=2**31,
    )
    with TestClient(app) as client:
        assert (
            client.post(
                "/webhooks/telegram", json=callback(40, "w_group", -100, "group"), headers=headers
            ).status_code
            == 200
        )
        with service.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
        assert (
            client.post(
                "/webhooks/telegram", json=callback(41, "w_private", 7, "private"), headers=headers
            ).status_code
            == 200
        )
        confirm_id = next(
            payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]  # type: ignore[index]
            for method, payload in reversed(calls)
            if method == "sendMessage" and payload.get("text") == "请再次确认操作"
        )
        with service.store.connect() as db:
            receipt_id = db.execute(
                "SELECT task_id FROM telegram_callbacks WHERE callback_id=?", (confirm_id,)
            ).fetchone()[0]
        private = Destination("telegram", "7", "private")
        service.identities.revoke(Principal("telegram", "7"), private, local_admin=True)
        assert (
            client.post(
                "/webhooks/telegram", json=callback(42, confirm_id, 7, "private"), headers=headers
            ).status_code
            == 200
        )
        assert service.ledger.get_receipt(receipt_id).state == "needs_approval"
        with service.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
        service.identities.bind_private(
            owner=Principal("telegram", "7"), destination=private, local_admin=True
        )
        assert (
            client.post(
                "/webhooks/telegram", json=callback(43, confirm_id, 7, "private"), headers=headers
            ).status_code
            == 200
        )
        with service.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0

        service.store.create_telegram_callback(
            "w_private2",
            action="cancel",
            task_id="task-1",
            destination_id="7",
            chat_type="private",
            principal_platform="telegram",
            actor_id="7",
            expires_at=2**31,
            control_action="cancel",
        )
        assert (
            client.post(
                "/webhooks/telegram", json=callback(44, "w_private2", 7, "private"), headers=headers
            ).status_code
            == 200
        )
        confirm2 = next(
            payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]  # type: ignore[index]
            for method, payload in reversed(calls)
            if method == "sendMessage" and payload.get("text") == "请再次确认操作"
        )
        assert (
            client.post(
                "/webhooks/telegram", json=callback(45, confirm2, 7, "private"), headers=headers
            ).status_code
            == 200
        )
        with service.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1


@pytest.mark.parametrize(
    "broken",
    [
        {"id": "cb-missing-message", "from": {"id": 7}, "data": "opaque"},
        {
            "id": "cb-missing-from",
            "data": "opaque",
            "message": {"message_id": 1, "chat": {"id": 7, "type": "private"}},
        },
        {
            "id": "cb-bad-data",
            "from": {"id": 7},
            "data": {"bad": True},
            "message": {"message_id": 1, "chat": {"id": 7, "type": "private"}},
        },
    ],
)
def test_authenticated_pre_normalization_callback_errors_are_answered_once(
    tmp_path: Path,
    broken: dict[str, object],
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    client = TelegramBotClient(
        "token",
        transport=lambda method, payload: (
            calls.append((method, payload)) or {"ok": True, "result": {}}
        ),
    )
    service = _service(tmp_path)
    app = create_app(
        settings=Settings(
            telegram_bot_token="token",
            telegram_webhook_secret="secret",
            telegram_webhook_url="https://example.test/webhooks/telegram",
            monitor_server_db=service.store.path,
        ),
        service=service,
        projector=Projector(service.store),
        telegram_client=client,
    )
    with TestClient(app) as http:
        response = http.post(
            "/webhooks/telegram",
            json={
                "update_id": 90,
                "callback_query": broken,
            },
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        )
    assert response.status_code == 200
    answers = [(method, payload) for method, payload in calls if method == "answerCallbackQuery"]
    assert answers == [
        ("answerCallbackQuery", {"callback_query_id": broken["id"], "text": "请求无效"})
    ]


def test_telegram_content_length_is_rejected_before_body_parse(tmp_path: Path) -> None:
    service = _service(tmp_path)
    app = create_app(
        settings=Settings(
            telegram_bot_token="token",
            telegram_webhook_secret="secret",
            telegram_webhook_url="https://example.test/webhooks/telegram",
            monitor_server_db=service.store.path,
            monitor_server_max_body_bytes=8,
        ),
        service=service,
        projector=Projector(service.store),
        telegram_client=TelegramBotClient(
            "token", transport=lambda method, payload: {"ok": True, "result": {}}
        ),
    )
    secret = {"X-Telegram-Bot-Api-Secret-Token": "secret"}
    with TestClient(app) as client:
        assert (
            client.post(
                "/webhooks/telegram", content=b"{}", headers=secret | {"Content-Length": "bad"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/webhooks/telegram", content=b"{}", headers=secret | {"Content-Length": "9"}
            ).status_code
            == 413
        )
