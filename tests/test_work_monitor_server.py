from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from ostag.core.identity import Principal
from ostag.core.schemas import ActionReceipt
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.server import ServerAuth, create_app
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore


def _event(sequence: int = 1, *, task_id: str = "task-1", project: str = "ostag") -> dict[str, object]:
    return {
        "event_id": f"event-{sequence}", "instance_id": "runner-1", "sequence": sequence,
        "kind": "task.created", "occurred_at": "2026-07-13T00:00:00+00:00",
        "task": {"task_id": task_id, "title": f"Ship {task_id}", "project": project,
                 "status": "todo", "agent": "codex", "progress": 0,
                 "duration_seconds": 0, "summary": "", "error_class": None,
                 "evidence_ref": None, "version": 1},
        "per_task_capabilities": {},
    }


def _instance_headers(body: bytes, instance: str = "runner-1") -> dict[str, str]:
    timestamp = str(int(datetime.now(UTC).timestamp()))
    signature = hmac.new(b"runner-secret", timestamp.encode() + b"\n" + body, hashlib.sha256).hexdigest()
    return {"X-OStag-Instance": instance, "X-OStag-Timestamp": timestamp,
            "X-OStag-Signature": signature, "content-type": "application/json"}


def _human_headers(**extra: str) -> dict[str, str]:
    return {"Authorization": "Bearer human-secret", "X-OStag-Platform": "feishu",
            "X-OStag-Actor": "alice", "X-OStag-Destination": "alice-chat",
            "X-OStag-Chat-Type": "private"} | extra


def _app(tmp_path: Path, **kwargs: object):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    identities = IdentityStore(store.path, enrollment_secret="enroll")
    destination = Destination("feishu", "alice-chat", "private")
    identities.bind_private(owner=Principal("feishu", "alice"), destination=destination,
                            local_admin=True)
    service = WorkMonitorService(store, identities)
    app = create_app(service=service, projector=Projector(store),
                     auth=ServerAuth({"runner-1": "runner-secret"}, human_token="human-secret",
                                     admin_token="admin-secret"), **kwargs)
    return app, store


def test_core_routes_authenticate_and_apply_scope(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    event = _event()
    body = json.dumps({"events": [event]}, separators=(",", ":")).encode()
    with TestClient(app) as client:
        assert client.post("/v1/events", content=body, headers=_instance_headers(body)).json()["ack_sequence"] == 1
        assert client.get("/v1/board", headers=_human_headers()).json()["cards"][0]["task_id"] == "task-1"
        assert client.get("/v1/tasks/task-1", headers=_human_headers()).status_code == 200
        assert client.get("/v1/instances", headers=_human_headers()).json()[0]["instance_id"] == "runner-1"
        response = client.post("/v1/subscriptions", headers=_human_headers(), json={"task_id": "task-1"})
        assert response.status_code == 200


def test_group_scope_allows_matching_tasks_and_rejects_cross_scope_access(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    identities = IdentityStore(store.path)
    identities.enroll_group(
        Principal("feishu", "alice"), Destination("feishu", "project-chat", "group"),
        scope="project:ostag", local_admin=True,
    )
    headers = _human_headers(**{"X-OStag-Destination": "project-chat", "X-OStag-Chat-Type": "group"})
    with TestClient(app) as client:
        for event in (_event(1), _event(2, task_id="task-2", project="secret")):
            body = json.dumps({"events": [event]}, separators=(",", ":")).encode()
            assert client.post("/v1/events", content=body, headers=_instance_headers(body)).status_code == 200
        assert [card["task_id"] for card in client.get("/v1/board", headers=headers).json()["cards"]] == ["task-1"]
        assert client.get("/v1/tasks/task-1", headers=headers).status_code == 200
        assert client.get("/v1/tasks/task-2", headers=headers).status_code == 404
        assert client.post("/v1/subscriptions", headers=headers, json={"task_id": "task-1"}).status_code == 200
        assert client.post("/v1/subscriptions", headers=headers, json={"task_id": "task-2"}).status_code == 403


def test_instance_identity_is_derived_from_hmac_not_payload(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    body = b'{"instance_id":"other"}'
    with TestClient(app) as client:
        assert client.post("/v1/commands/poll", content=body, headers=_instance_headers(body)).status_code == 422
        assert client.post("/v1/events", json={"events": [_event()]}, headers={}).status_code == 401


def test_command_poll_is_single_consumer_and_ack_is_instance_scoped(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    command = {"command_id": "cmd-1", "instance_id": "runner-1", "task_id": "task-1",
               "action": "cancel", "expected_version": 1, "expected_lifecycle": "running",
               "requested_by": "feishu:alice", "issued_at": "2026-07-13T00:00:00+00:00",
               "expires_at": "2026-07-14T00:00:00+00:00"}
    ledger = WorkMonitorService(store, IdentityStore(store.path)).ledger
    ledger.record_receipt(ActionReceipt(action_id="action-1", run_id="run-1", title="Cancel",
                          target_system="work_monitor", target_object="runner-1/task-1",
                          mutation_type="cancel", state="applying"))
    with store.connect() as db:
        db.execute("INSERT INTO commands(command_id,instance_id,task_id,status,payload,action_id) VALUES (?,?,?,?,?,?)",
                   ("cmd-1", "runner-1", "task-1", "pending", json.dumps(command), "action-1"))
    body = b"{}"
    with TestClient(app) as client:
        first = client.post("/v1/commands/poll", content=body, headers=_instance_headers(body))
        second = client.post("/v1/commands/poll", content=body, headers=_instance_headers(body))
        assert [item["command_id"] for item in first.json()["commands"]] == ["cmd-1"]
        assert second.json() == {"commands": []}
        ack_body = json.dumps({"result": {"ok": False, "code": "COMMAND_REJECTED"}}, separators=(",", ":")).encode()
        assert client.post("/v1/commands/cmd-1/ack", content=ack_body,
                           headers=_instance_headers(ack_body)).status_code == 200


def test_store_claims_pending_commands_once_with_caller_clock(tmp_path: Path) -> None:
    store = MonitorStore(tmp_path / "claim.sqlite3")
    with store.connect() as db:
        db.execute("INSERT INTO commands(command_id,instance_id,task_id,status,payload) VALUES (?,?,?,?,?)",
                   ("cmd-1", "runner-1", "task-1", "pending", '{"command_id":"cmd-1"}'))
    fixed = datetime.fromisoformat("2026-07-13T01:02:03+00:00")
    service = WorkMonitorService(store, IdentityStore(store.path), clock=lambda: fixed)
    claimed = service.poll_commands("runner-1")
    assert claimed == ({"command_id": "cmd-1"},)
    assert service.poll_commands("runner-1") == ()
    with store.connect() as db:
        assert db.execute("SELECT consumed_at FROM commands").fetchone()["consumed_at"] == "2026-07-13T01:02:03+00:00"


def test_health_router_hook_and_no_placeholder_webhooks(tmp_path: Path) -> None:
    router = APIRouter()
    router.add_api_route("/extension", lambda: {"ok": True})
    app, _ = _app(tmp_path, routers=[router])
    paths = {route.path for route in app.routes}
    assert "/webhooks/feishu" not in paths and "/webhooks/telegram" not in paths
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/extension").json() == {"ok": True}


def test_authentication_happens_before_domain_call(tmp_path: Path) -> None:
    store = MonitorStore(tmp_path / "spy.sqlite3")
    identities = IdentityStore(store.path)

    class SpyService(WorkMonitorService):
        calls = 0

        def board(self, *args: object, **kwargs: object):
            self.calls += 1
            raise AssertionError("domain called before authentication")

    spy = SpyService(store, identities)
    app = create_app(service=spy, projector=Projector(store),
                     auth=ServerAuth({"runner-1": "runner-secret"}, human_token="human-secret"))
    with TestClient(app) as client:
        assert client.get("/v1/board").status_code == 401
    assert spy.calls == 0


def _request_with_token(token: str) -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_human_and_admin_credentials_are_not_interchangeable() -> None:
    auth = ServerAuth({}, human_token="human-secret", admin_token="admin-secret")
    auth.admin(_request_with_token("admin-secret"))
    for token in ("human-secret", "wrong"):
        try:
            auth.admin(_request_with_token(token))
        except HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("human token authenticated as admin")
    try:
        auth.human(_request_with_token("admin-secret"))
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("admin token authenticated as human")


class RestartingOwner:
    def __init__(self) -> None:
        self.starts = 0
        self.stopped = threading.Event()

    async def run(self, stop: object) -> None:
        self.starts += 1
        if self.starts == 1:
            raise RuntimeError("crash")
        while not stop.is_set():
            await stop.wait()
        self.stopped.set()


def test_lifespan_supervises_restart_and_graceful_shutdown(tmp_path: Path) -> None:
    owner = RestartingOwner()
    app, _ = _app(tmp_path, feishu_ws=owner, supervisor_backoff=(0.001, 0.002))
    with TestClient(app):
        deadline = time.monotonic() + 1
        while owner.starts < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert owner.starts >= 2
    assert owner.stopped.wait(1)
