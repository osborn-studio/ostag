from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from ostag.connectors.requirements import FeishuBaseReadConnector
from ostag.core.identity import Principal
from ostag.config import Settings
from ostag.core.schemas import ActionReceipt
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.server import HumanCredential, SecurityContract, ServerAuth, create_app
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore


def test_separate_outbox_owner_is_rejected(tmp_path: Path) -> None:
    store = MonitorStore(tmp_path / "outbox.sqlite3")
    with pytest.raises(ValueError, match="scheduler owns outbox"):
        create_app(
            service=WorkMonitorService(store, IdentityStore(store.path)),
            projector=Projector(store),
            outbox=object(),
        )


def _event(
    sequence: int = 1, *, task_id: str = "task-1", project: str = "ostag"
) -> dict[str, object]:
    return {
        "event_id": f"event-{sequence}",
        "instance_id": "runner-1",
        "sequence": sequence,
        "kind": "task.created",
        "occurred_at": "2026-07-13T00:00:00+00:00",
        "task": {
            "task_id": task_id,
            "title": f"Ship {task_id}",
            "project": project,
            "status": "todo",
            "agent": "codex",
            "progress": 0,
            "duration_seconds": 0,
            "summary": "",
            "error_class": None,
            "evidence_ref": None,
            "version": 1,
        },
        "per_task_capabilities": {},
    }


def _instance_headers(
    body: bytes,
    instance: str = "runner-1",
    path: str = "/v1/events",
    nonce: str = "nonce-1",
    timestamp: int | None = None,
) -> dict[str, str]:
    timestamp_text = str(timestamp if timestamp is not None else int(datetime.now(UTC).timestamp()))
    canonical = "\n".join(
        ("v1", "POST", path, instance, timestamp_text, nonce, hashlib.sha256(body).hexdigest())
    ).encode()
    signature = hmac.new(b"runner-secret", canonical, hashlib.sha256).hexdigest()
    return {
        "X-OStag-Instance": instance,
        "X-OStag-Timestamp": timestamp_text,
        "X-OStag-Nonce": nonce,
        "X-OStag-Signature": signature,
        "content-type": "application/json",
    }


def _human_headers(**extra: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer human-secret",
        "X-OStag-Platform": "feishu",
        "X-OStag-Actor": "alice",
        "X-OStag-Destination": "alice-chat",
        "X-OStag-Chat-Type": "private",
    } | extra


def _app(tmp_path: Path, **kwargs: object):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    identities = IdentityStore(store.path, enrollment_secret="enroll")
    destination = Destination("feishu", "alice-chat", "private")
    identities.bind_private(
        owner=Principal("feishu", "alice"), destination=destination, local_admin=True
    )
    service = WorkMonitorService(store, identities)
    app = create_app(
        service=service,
        projector=Projector(store),
        auth=ServerAuth(
            {"runner-1": "runner-secret"},
            human_credentials={
                "human-secret": HumanCredential(Principal("feishu", "alice"), destination)
            },
            admin_token="admin-secret",
        ),
        **kwargs,
    )
    return app, store


def test_core_routes_authenticate_and_apply_scope(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    event = _event()
    body = json.dumps({"events": [event]}, separators=(",", ":")).encode()
    with TestClient(app) as client:
        assert (
            client.post("/v1/events", content=body, headers=_instance_headers(body)).json()[
                "ack_sequence"
            ]
            == 1
        )
        assert (
            client.get("/v1/board", headers=_human_headers()).json()["cards"][0]["task_id"]
            == "task-1"
        )
        assert client.get("/v1/tasks/task-1", headers=_human_headers()).status_code == 200
        assert (
            client.get("/v1/instances", headers=_human_headers()).json()[0]["instance_id"]
            == "runner-1"
        )
        response = client.post(
            "/v1/subscriptions", headers=_human_headers(), json={"task_id": "task-1"}
        )
        assert response.status_code == 200


def test_group_scope_allows_matching_tasks_and_rejects_cross_scope_access(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    identities = IdentityStore(store.path)
    identities.enroll_group(
        Principal("feishu", "alice"),
        Destination("feishu", "project-chat", "group"),
        scope="project:ostag",
        local_admin=True,
    )
    group = Destination("feishu", "project-chat", "group")
    app = create_app(
        service=WorkMonitorService(store, identities),
        projector=Projector(store),
        auth=ServerAuth(
            {"runner-1": "runner-secret"},
            human_credentials={
                "scope-secret": HumanCredential(Principal("feishu", "alice"), group)
            },
        ),
    )
    headers = _human_headers(
        **{
            "Authorization": "Bearer scope-secret",
            "X-OStag-Destination": "forged-chat",
            "X-OStag-Chat-Type": "private",
        }
    )
    with TestClient(app) as client:
        for event in (_event(1), _event(2, task_id="task-2", project="secret")):
            body = json.dumps({"events": [event]}, separators=(",", ":")).encode()
            assert (
                client.post(
                    "/v1/events",
                    content=body,
                    headers=_instance_headers(body, nonce=f"nonce-{event['sequence']}"),
                ).status_code
                == 200
            )
        assert [
            card["task_id"] for card in client.get("/v1/board", headers=headers).json()["cards"]
        ] == ["task-1"]
        assert client.get("/v1/tasks/task-1", headers=headers).status_code == 200
        assert client.get("/v1/tasks/task-2", headers=headers).status_code == 404
        assert (
            client.post(
                "/v1/subscriptions", headers=headers, json={"task_id": "task-1"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/subscriptions", headers=headers, json={"task_id": "task-2"}
            ).status_code
            == 403
        )


def test_instance_identity_is_derived_from_hmac_not_payload(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    body = b'{"instance_id":"other"}'
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/commands/poll",
                content=body,
                headers=_instance_headers(body, path="/v1/commands/poll"),
            ).status_code
            == 422
        )
        assert client.post("/v1/events", json={"events": [_event()]}, headers={}).status_code == 401


def test_command_poll_is_single_consumer_and_ack_is_instance_scoped(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    command = {
        "command_id": "cmd-1",
        "instance_id": "runner-1",
        "task_id": "task-1",
        "action": "cancel",
        "expected_version": 1,
        "expected_lifecycle": "running",
        "requested_by": "feishu:alice",
        "issued_at": "2026-07-13T00:00:00+00:00",
        "expires_at": "2099-07-14T00:00:00+00:00",
    }
    ledger = WorkMonitorService(store, IdentityStore(store.path)).ledger
    ledger.record_receipt(
        ActionReceipt(
            action_id="action-1",
            run_id="run-1",
            title="Cancel",
            target_system="work_monitor",
            target_object="runner-1/task-1",
            mutation_type="cancel",
            state="applying",
        )
    )
    with store.connect() as db:
        db.execute(
            "INSERT INTO commands(command_id,instance_id,task_id,status,payload,action_id) VALUES (?,?,?,?,?,?)",
            ("cmd-1", "runner-1", "task-1", "pending", json.dumps(command), "action-1"),
        )
    body = b"{}"
    with TestClient(app) as client:
        first = client.post(
            "/v1/commands/poll",
            content=body,
            headers=_instance_headers(body, path="/v1/commands/poll", nonce="poll-1"),
        )
        second = client.post(
            "/v1/commands/poll",
            content=body,
            headers=_instance_headers(body, path="/v1/commands/poll", nonce="poll-2"),
        )
        assert [item["command_id"] for item in first.json()["commands"]] == ["cmd-1"]
        assert second.json() == {"commands": []}
        ack_body = json.dumps(
            {"result": {"ok": False, "code": "COMMAND_REJECTED"}}, separators=(",", ":")
        ).encode()
        assert (
            client.post(
                "/v1/commands/cmd-1/ack",
                content=ack_body,
                headers=_instance_headers(ack_body, path="/v1/commands/cmd-1/ack", nonce="ack-1"),
            ).status_code
            == 200
        )


def test_store_claims_pending_commands_once_with_caller_clock(tmp_path: Path) -> None:
    store = MonitorStore(tmp_path / "claim.sqlite3")
    with store.connect() as db:
        db.execute(
            "INSERT INTO commands(command_id,instance_id,task_id,status,payload) VALUES (?,?,?,?,?)",
            (
                "cmd-1",
                "runner-1",
                "task-1",
                "pending",
                '{"command_id":"cmd-1","expires_at":"2026-07-14T00:00:00+00:00"}',
            ),
        )
    fixed = datetime.fromisoformat("2026-07-13T01:02:03+00:00")
    service = WorkMonitorService(store, IdentityStore(store.path), clock=lambda: fixed)
    claimed = service.poll_commands("runner-1")
    assert claimed == ({"command_id": "cmd-1", "expires_at": "2026-07-14T00:00:00+00:00"},)
    assert service.poll_commands("runner-1") == ()
    with store.connect() as db:
        assert (
            db.execute("SELECT consumed_at FROM commands").fetchone()["consumed_at"]
            == "2026-07-13T01:02:03+00:00"
        )


def test_v4_database_migrates_command_leases_and_nonce_cache(tmp_path: Path) -> None:
    path = tmp_path / "v4.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE commands (command_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, task_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, consumed_at TEXT, action_id TEXT NOT NULL DEFAULT '', ack_payload TEXT NOT NULL DEFAULT '', ack_hash TEXT NOT NULL DEFAULT '', ledger_synced INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute("PRAGMA user_version=4")
    store = MonitorStore(path)
    with store.connect() as db:
        assert "lease_until" in {row[1] for row in db.execute("PRAGMA table_info(commands)")}
        assert db.execute("SELECT name FROM sqlite_master WHERE name='instance_nonces'").fetchone()


def test_command_lease_can_be_reclaimed_but_expired_command_is_never_delivered(
    tmp_path: Path,
) -> None:
    store = MonitorStore(tmp_path / "lease.sqlite3")
    now = [datetime.fromisoformat("2026-07-13T01:00:00+00:00")]
    with store.connect() as db:
        for command_id, expires_at in (
            ("live", "2026-07-14T00:00:00+00:00"),
            ("expired", "2026-07-13T00:00:00+00:00"),
        ):
            payload = json.dumps({"command_id": command_id, "expires_at": expires_at})
            db.execute(
                "INSERT INTO commands(command_id,instance_id,task_id,status,payload) VALUES (?,?,?,?,?)",
                (command_id, "runner-1", "task-1", "pending", payload),
            )
    service = WorkMonitorService(store, IdentityStore(store.path), clock=lambda: now[0])
    assert [item["command_id"] for item in service.poll_commands("runner-1")] == ["live"]
    assert service.poll_commands("runner-1") == ()
    now[0] += timedelta(seconds=31)
    assert [item["command_id"] for item in service.poll_commands("runner-1")] == ["live"]


def test_health_router_hook_and_no_placeholder_webhooks(tmp_path: Path) -> None:
    router = APIRouter()
    calls = []
    endpoint_calls = []

    def extension() -> dict[str, bool]:
        endpoint_calls.append(1)
        return {"ok": True}

    router.add_api_route("/extension", extension)

    def authenticated(request: Request) -> None:
        calls.append(1)
        if request.headers.get("authorization") != "Bearer extension-secret":
            raise HTTPException(401, "unauthorized")

    contract = SecurityContract("authenticated_dependency", authenticated)
    app, _ = _app(tmp_path, routers=[(router, contract)])
    paths = set(app.openapi()["paths"])
    assert "/webhooks/feishu" not in paths and "/webhooks/telegram" not in paths
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/extension").status_code == 401
        assert client.get(
            "/extension", headers={"Authorization": "Bearer extension-secret"}
        ).json() == {"ok": True}
    assert len(calls) == 2
    assert len(endpoint_calls) == 1


def test_router_hook_rejects_missing_security_contract(tmp_path: Path) -> None:
    for registration in (APIRouter(), (APIRouter(), "verified_platform")):
        try:
            _app(tmp_path, routers=[registration])
        except ValueError as exc:
            assert "security contract" in str(exc)
        else:
            raise AssertionError("router without callable security was registered")


def test_instance_signature_binds_path_and_nonce_replay_is_rejected(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    body = json.dumps({"events": [_event()]}, separators=(",", ":")).encode()
    headers = _instance_headers(body, nonce="one-shot")
    with TestClient(app) as client:
        assert client.post("/v1/events", content=body, headers=headers).status_code == 200
        assert client.post("/v1/events", content=body, headers=headers).status_code == 401
        wrong_path = _instance_headers(body, path="/v1/commands/poll", nonce="wrong-path")
        assert client.post("/v1/events", content=body, headers=wrong_path).status_code == 401


def test_future_timestamp_nonce_remains_replay_blocked_for_entire_signed_window(
    tmp_path: Path,
) -> None:
    now = [1_000]
    store = MonitorStore(tmp_path / "future-nonce.sqlite3")
    identities = IdentityStore(store.path)
    auth = ServerAuth({"runner-1": "runner-secret"}, clock=lambda: now[0])
    app = create_app(
        service=WorkMonitorService(store, identities), projector=Projector(store), auth=auth
    )
    body = json.dumps({"events": [_event()]}, separators=(",", ":")).encode()
    headers = _instance_headers(body, nonce="future", timestamp=1_299)
    with TestClient(app) as client:
        assert client.post("/v1/events", content=body, headers=headers).status_code == 200
        now[0] = 1_301
        assert client.post("/v1/events", content=body, headers=headers).status_code == 401
        now[0] = 1_600
        fresh = _instance_headers(body, nonce="future", timestamp=1_600)
        assert client.post("/v1/events", content=body, headers=fresh).status_code == 200


def test_instance_body_limit_rejects_before_authentication(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, settings=None)
    with TestClient(app) as client:
        response = client.post("/v1/events", content=b"x" * 1_048_577)
    assert response.status_code == 413

    def chunks():
        yield b"x" * 700_000
        yield b"y" * 700_000

    with TestClient(app) as client:
        chunked = client.post("/v1/events", content=chunks())
    assert chunked.status_code == 413


def test_authentication_happens_before_domain_call(tmp_path: Path) -> None:
    store = MonitorStore(tmp_path / "spy.sqlite3")
    identities = IdentityStore(store.path)

    class SpyService(WorkMonitorService):
        calls = 0

        def board(self, *args: object, **kwargs: object):
            self.calls += 1
            raise AssertionError("domain called before authentication")

    spy = SpyService(store, identities)
    credential = HumanCredential(
        Principal("feishu", "alice"), Destination("feishu", "chat", "private")
    )
    app = create_app(
        service=spy,
        projector=Projector(store),
        auth=ServerAuth(
            {"runner-1": "runner-secret"}, human_credentials={"human-secret": credential}
        ),
    )
    with TestClient(app) as client:
        assert client.get("/v1/board").status_code == 401
    assert spy.calls == 0


def _request_with_token(token: str) -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_human_and_admin_credentials_are_not_interchangeable() -> None:
    credential = HumanCredential(
        Principal("feishu", "alice"), Destination("feishu", "chat", "private")
    )
    auth = ServerAuth(
        {}, human_credentials={"human-secret": credential}, admin_token="admin-secret"
    )
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


def test_auto_feishu_owner_uses_central_service_and_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners = []

    class FeishuOwner:
        def __init__(self, config: Settings, *, runner=None):
            self.runner = runner
            owners.append(self)

        async def run(self, stop) -> None:
            await stop.wait()

    monkeypatch.setattr("ostag.adapters.feishu.FeishuAdapter", FeishuOwner)
    database = tmp_path / "central.sqlite3"
    app = create_app(
        settings=Settings(
            monitor_server_db=database,
            feishu_use_websocket=True,
            feishu_app_id="cli_test_id",
            feishu_app_secret="test-secret",
            monitor_scheduler_enabled=False,
        )
    )

    with TestClient(app):
        assert len(owners) == 1
        service = owners[0].runner.work_monitor
        assert service.store.path == database
        assert service.identities.path == database


def test_auto_feishu_owner_wires_scoped_status_board_from_central_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners = []

    class FeishuOwner:
        def __init__(self, config: Settings, *, runner=None):
            self.runner = runner
            owners.append(self)

        async def run(self, stop) -> None:
            await stop.wait()

    monkeypatch.setattr("ostag.adapters.feishu.FeishuAdapter", FeishuOwner)
    database = tmp_path / "central.sqlite3"
    identities = IdentityStore(database)
    owner = Principal("feishu", "alice")
    destination = Destination("feishu", "alice-chat", "private")
    identities.bind_private(owner=owner, destination=destination, local_admin=True)
    identities.bind_instance(
        owner,
        instance_id="macmini",
        display_name="Mac mini",
        bound_at="2026-07-15T00:00:00+00:00",
    )
    settings = Settings(
        monitor_server_db=database,
        monitor_human_credentials=(
            ("human-secret", "feishu", "alice", "alice-chat", "private"),
        ),
        feishu_use_websocket=True,
        feishu_app_id="cli_test_id",
        feishu_app_secret="test-secret",
        monitor_scheduler_enabled=False,
        monitor_offline_seconds=321,
        copilot_status_board_enabled=True,
    )

    with TestClient(create_app(settings=settings)):
        controller = owners[0].runner.copilot_relay
        assert controller.status_board_enabled is True
        assert controller.status_service.store.monitor_store.path == database
        assert controller.status_service.identities.path == database
        assert controller.status_service.offline_seconds == 321
        assert controller.service.policy.can_access(owner, "macmini") is True


def test_lifespan_owns_daily_boards_on_the_central_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners = []

    class DailyOwner:
        def __init__(self, store, profiles, **kwargs):
            self.store = store
            self.profiles = profiles
            self.kwargs = kwargs
            owners.append(self)

        async def run(self, stop) -> None:
            await stop.wait()

    monkeypatch.setattr(
        "ostag.work_monitor.daily_digest_scheduler.DailyTaskBoardScheduler", DailyOwner
    )
    settings = Settings(
        monitor_server_db=tmp_path / "central.sqlite3",
        feishu_use_websocket=False,
        monitor_scheduler_enabled=False,
        daily_task_boards_enabled=True,
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        dingtalk_client_id="ding_test",
        dingtalk_client_secret="secret",
        dingtalk_robot_webhook="https://oapi.dingtalk.com/robot/send?access_token=test",
        daily_task_board_feishu_chat_id="oc_feishu",
        daily_task_board_feishu_base_token="base_feishu",
        daily_task_board_feishu_table_id="tbl_feishu",
        daily_task_board_feishu_table_url="https://example.test/feishu",
        daily_task_board_dingtalk_chat_id="cid_dingtalk",
        daily_task_board_dingtalk_base_token="base_dingtalk",
        daily_task_board_dingtalk_table_id="tbl_dingtalk",
        daily_task_board_dingtalk_table_url="https://example.test/dingtalk",
    )
    app = create_app(settings=settings)

    with TestClient(app):
        assert len(owners) == 1
        assert owners[0].store.path == settings.monitor_server_db
        assert {profile.platform for profile in owners[0].profiles} == {
            "feishu", "dingtalk"
        }
        assert owners[0].kwargs["morning_hour"] == 9
        assert owners[0].kwargs["evening_hour"] == 18


def test_lifespan_adds_noon_requirement_board_to_the_single_daily_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners = []

    class DailyOwner:
        def __init__(self, store, profiles, **kwargs):
            self.profiles = profiles
            self.kwargs = kwargs
            owners.append(self)

        async def run(self, stop) -> None:
            await stop.wait()

    monkeypatch.setattr(
        "ostag.work_monitor.daily_digest_scheduler.DailyTaskBoardScheduler", DailyOwner
    )
    settings = Settings(
        monitor_server_db=tmp_path / "central.sqlite3",
        feishu_use_websocket=False,
        monitor_scheduler_enabled=False,
        ambient_daily_board_enabled=True,
        ambient_chat_ids=("oc_fde",),
        ambient_daily_board_base_url="https://example.test/fde",
        auto_base_default_base_token="base_fde",
        auto_base_default_table_id="tbl_fde",
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
    )

    with TestClient(create_app(settings=settings)):
        assert len(owners) == 1
        profiles = owners[0].kwargs["requirement_profiles"]
        assert len(profiles) == 1
        assert profiles[0].destination_id == "oc_fde"
        assert isinstance(profiles[0].connector, FeishuBaseReadConnector)
        assert owners[0].kwargs["requirement_hour"] == 12
