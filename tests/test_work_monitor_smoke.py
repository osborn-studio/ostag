from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ostag.config import Settings
from ostag.core.identity import Principal
from ostag.work_monitor.feishu_cards import render_feishu_card
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.server import ServerAuth, create_app
from ostag.work_monitor.scheduler import WorkMonitorScheduler
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore
from ostag.work_monitor.telegram_views import render_telegram_view


def _event(instance: str, sequence: int, *, heartbeat: bool = False) -> dict[str, object]:
    task_id = f"task-{instance}"
    return {
        "event_id": f"event-{instance}-{sequence}",
        "instance_id": instance,
        "sequence": sequence,
        "kind": "instance.heartbeat" if heartbeat else "task.started",
        "occurred_at": "2026-07-13T00:00:00+00:00",
        "task": None
        if heartbeat
        else {
            "task_id": task_id,
            "title": f"Deterministic task from {instance}",
            "project": "release-smoke",
            "status": "running",
            "agent": "codex",
            "progress": 20,
            "duration_seconds": 2,
            "summary": "local fixture only",
            "error_class": None,
            "evidence_ref": "fixture:smoke",
            "version": 1,
        },
        "per_task_capabilities": {task_id: ["pause"]} if heartbeat else {},
    }


def _signed(body: bytes, instance: str, secret: str, path: str, nonce: str) -> dict[str, str]:
    timestamp = str(int(datetime.now(UTC).timestamp()))
    canonical = "\n".join(
        ("v1", "POST", path, instance, timestamp, nonce, hashlib.sha256(body).hexdigest())
    ).encode()
    return {
        "X-OStag-Instance": instance,
        "X-OStag-Timestamp": timestamp,
        "X-OStag-Nonce": nonce,
        "X-OStag-Signature": hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest(),
        "content-type": "application/json",
    }


class _FakeTransport:
    def __init__(self) -> None:
        self.deliveries: list[tuple[str, str]] = []

    async def send_message(
        self, destination_id: str, text: str, *, delivery_id: str, chat_type: str = "group"
    ) -> None:
        self.deliveries.append((destination_id, delivery_id))


@pytest.mark.asyncio
async def test_deterministic_two_instance_control_and_notification_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "socket.create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("external network used")),
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("external network used")),
    )
    async def reject_httpx(*args, **kwargs):
        raise AssertionError("external network used")

    monkeypatch.setattr("httpx.AsyncClient.request", reject_httpx)
    settings = Settings(
        dry_run=True,
        monitor_enabled=False,
        feishu_app_id="",
        feishu_app_secret="",
        telegram_bot_token="",
        telegram_webhook_secret="",
        telegram_webhook_url="",
    ).validate()
    assert settings.dry_run and not settings.monitor_enabled
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    identities = IdentityStore(store.path, enrollment_secret="fixture-enroll")
    principal = Principal("feishu", "fixture-owner")
    destination = Destination("feishu", "fixture-feishu-private", "private")
    telegram_principal = Principal("telegram", "fixture-owner")
    telegram_destination = Destination("telegram", "fixture-telegram-private", "private")
    identities.bind_private(owner=principal, destination=destination, local_admin=True)
    identities.bind_private(
        owner=telegram_principal, destination=telegram_destination, local_admin=True
    )
    identities.subscribe(principal, destination, task_id="task-ostag-1")
    identities.subscribe(
        telegram_principal, telegram_destination, task_id="task-ostag-1"
    )
    service = WorkMonitorService(store, identities)
    app = create_app(
        service=service,
        projector=Projector(store),
        auth=ServerAuth({"ostag-1": "fixture-a", "osstack-1": "fixture-b"}),
    )

    with TestClient(app) as client:
        for instance, secret in (("ostag-1", "fixture-a"), ("osstack-1", "fixture-b")):
            events = [_event(instance, 1), _event(instance, 2, heartbeat=True)]
            body = json.dumps({"events": events}, separators=(",", ":")).encode()
            response = client.post(
                "/v1/events",
                content=body,
                headers=_signed(body, instance, secret, "/v1/events", f"ingest-{instance}"),
            )
            assert response.status_code == 200
            assert response.json() == {"ack_sequence": 2}

        board = service.board(principal, destination)
        assert {card["task_id"] for card in board.cards} == {
            "task-ostag-1",
            "task-osstack-1",
        }
        assert render_feishu_card(board)["header"]["title"]["content"] == "OStag Control · 今日看板"
        telegram_text, _ = render_telegram_view(board)
        assert "ostag-1" in telegram_text and "osstack-1" in telegram_text

        receipt = service.propose_control(
            principal, destination, task_id="task-ostag-1", action="pause"
        )
        assert service.approve_control(principal, destination, receipt.action_id).state == (
            "ready_to_apply"
        )
        assert service.apply_control(principal, destination, receipt.action_id).state == "applying"

        poll_body = b"{}"
        poll = client.post(
            "/v1/commands/poll",
            content=poll_body,
            headers=_signed(
                poll_body, "ostag-1", "fixture-a", "/v1/commands/poll", "poll-ostag"
            ),
        )
        command = poll.json()["commands"][0]
        ack_body = json.dumps(
            {"result": {"ok": True, "code": "OK", "status": "paused", "version": 2}},
            separators=(",", ":"),
        ).encode()
        ack_path = f"/v1/commands/{command['command_id']}/ack"
        ack = client.post(
            ack_path,
            content=ack_body,
            headers=_signed(ack_body, "ostag-1", "fixture-a", ack_path, "ack-ostag"),
        )
        assert ack.json() == {"state": "applied"}
        replay = client.post(
            ack_path,
            content=ack_body,
            headers=_signed(ack_body, "ostag-1", "fixture-a", ack_path, "ack-replay"),
        )
        assert replay.json() == {"state": "applied"}

    with store.connect() as connection:
        notifications = connection.execute(
            "SELECT state,payload,delivery_id FROM notification_outbox "
            "WHERE event_id LIKE 'control_ack:%'"
        ).fetchall()
    assert len(notifications) == 2
    assert {notification["state"] for notification in notifications} == {"pending"}
    assert {
        json.loads(notification["payload"])["result"]["status"]
        for notification in notifications
    } == {"paused"}
    stable_delivery_ids = {notification["delivery_id"] for notification in notifications}

    feishu = _FakeTransport()
    telegram = _FakeTransport()
    scheduler = WorkMonitorScheduler(
        store,
        identities,
        {"feishu": feishu, "telegram": telegram},
        jitter=lambda _: 0,
    )
    while await scheduler.drain_once():
        pass

    assert len(feishu.deliveries) == 1
    assert len(telegram.deliveries) == 1
    assert feishu.deliveries[0][0] == destination.destination_id
    assert telegram.deliveries[0][0] == telegram_destination.destination_id
    assert {feishu.deliveries[0][1], telegram.deliveries[0][1]} == stable_delivery_ids
    with store.connect() as connection:
        delivered = connection.execute(
            "SELECT state,delivery_id FROM notification_outbox WHERE event_id LIKE 'control_ack:%'"
        ).fetchall()
    assert len(delivered) == 2
    assert {tuple(row) for row in delivered} == {
        ("delivered", delivery_id) for delivery_id in stable_delivery_ids
    }
