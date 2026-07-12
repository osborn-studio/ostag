from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from ostag.core.identity import Principal
from ostag.work_monitor.feishu_cards import render_feishu_card
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.server import ServerAuth, create_app
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


def test_deterministic_two_instance_control_and_notification_loop(tmp_path: Path) -> None:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    identities = IdentityStore(store.path, enrollment_secret="fixture-enroll")
    principal = Principal("feishu", "fixture-owner")
    destination = Destination("feishu", "fixture-private", "private")
    identities.bind_private(owner=principal, destination=destination, local_admin=True)
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
        assert render_feishu_card(board)["header"]["title"]["content"] == "OStag 工作看板"
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

    with store.connect() as connection:
        notification = connection.execute(
            "SELECT state,payload FROM notification_outbox WHERE event_id LIKE 'control_ack:%'"
        ).fetchone()
    assert notification is not None and notification["state"] == "pending"
    assert json.loads(notification["payload"])["result"]["status"] == "paused"
