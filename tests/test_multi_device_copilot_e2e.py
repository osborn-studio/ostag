import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ostag.copilot_relay.models import RelayEnvelope
from ostag.copilot_relay.osstack_port import OsstackSession
from ostag.copilot_relay.policy import RelayPolicy
from ostag.copilot_relay.reporter import CopilotReporter
from ostag.copilot_relay.service import CopilotRelayService
from ostag.copilot_relay.status_board import OsstackStatusBoardService
from ostag.copilot_relay.store import RelayStore
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.reporter import TransportResponse
from ostag.work_monitor.store import MonitorStore


NOW = datetime(2026, 7, 16, 1, 0, tzinfo=UTC)
OWNER = Principal("feishu", "owner")
SESSION_ID = "same-session"


def _session(device: str, runtime: str, agent: str, title: str) -> RelayEnvelope:
    return RelayEnvelope.from_dict({
        "schema_version": 2,
        "message_id": f"session-{device}",
        "binding": {
            "instance_id": device, "runtime_id": runtime, "task_id": f"task-{device}",
            "run_id": f"run-{device}", "session_id": SESSION_ID, "turn_id": "turn-1",
            "session_version": 1,
        },
        "sequence": 1, "created_at": NOW.isoformat().replace("+00:00", "Z"),
        "expires_at": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "sender": {"platform": "osstack", "actor_id": "reporter"},
        "kind": "session.upsert",
        "payload": {
            "agent_id": agent, "title": title, "state": "idle",
            "resume_supported": True, "latest_summary": "等待指令",
        },
        "correlation_id": None, "causation_id": None,
    })


class Port:
    def __init__(self, runtime: str, agent: str):
        self.runtime = runtime
        self.agent = agent
        self.received: list[str] = []

    def list_sessions(self):
        return (OsstackSession(
            SESSION_ID, "task", "run", self.agent, "idle", True, True, 1,
            NOW.timestamp(), self.runtime,
        ),)

    def runtime_inventory(self):
        return ({
            "runtime_id": self.runtime,
            "agent_type": "codex" if self.agent == "codex-cli" else "claude-code",
            "state": "online", "session_count": 1,
        },)

    def send(self, envelope):
        self.received.append(str(envelope.payload["text"]))
        return {"state": "applied", "session_version": 2, "events": []}

    def snapshot(self, session_id):
        raise AssertionError("snapshot not used in this acceptance")

    def create_task(self, confirmation, proposal):
        raise AssertionError("task creation not used in this acceptance")


class CentralMailbox:
    def __init__(self, relay: RelayStore, device_id: str):
        self.relay = relay
        self.device_id = device_id
        self.polls = 0

    def request(self, method, url, body, headers):
        if url.endswith("/messages/poll"):
            self.polls += 1
            messages = self.relay.claim_messages(
                direction="outbound", owner=f"{self.device_id}-{self.polls}",
                now=NOW.isoformat().replace("+00:00", "Z"),
                lease_until=(NOW + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                instance_id=self.device_id,
            )
            return TransportResponse(200, json.dumps({
                "messages": [message.envelope.to_dict() for message in messages]
            }).encode())
        return TransportResponse(200, b'{"accepted":true}')


class Identity:
    def __init__(self, device_id: str):
        self.device_id = device_id

    def sign(self, body: bytes) -> str:
        return "test-signature"


@pytest.mark.asyncio
async def test_two_devices_same_session_id_never_cross_routes_and_recover_once(
    tmp_path: Path,
) -> None:
    monitor = MonitorStore(tmp_path / "central.sqlite3")
    relay = RelayStore(monitor)
    identities = IdentityStore(monitor.path)
    identities.bind_private(
        owner=OWNER,
        destination=Destination("feishu", "owner-private", "private"),
        local_admin=True,
    )
    devices = (
        ("device_macbook", "MacBook Pro", "codex-runtime", "codex", "codex-cli", "Codex 测试"),
        ("device_macmini", "Mac mini", "claude-runtime", "claude-code", "claude-code", "Claude 灰度"),
    )
    for device, name, *_ in devices:
        identities.bind_instance(OWNER, device, display_name=name, bound_at=NOW.isoformat())
    with monitor.transaction() as db:
        for device, name, runtime, agent_type, _, _ in devices:
            db.execute(
                "INSERT INTO monitor_devices(device_id,principal_platform,actor_id,display_name,public_key,paired_at,last_seen_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (device, "feishu", "owner", name, "test-key", NOW.isoformat(), NOW.isoformat()),
            )
            db.execute(
                "INSERT INTO monitor_device_runtimes VALUES (?,?,?,?,?,?)",
                (device, runtime, agent_type, "online", 1, NOW.isoformat()),
            )
    for device, _, runtime, _, agent, title in devices:
        relay.upsert_session(_session(device, runtime, agent, title))

    board = OsstackStatusBoardService(
        relay, identities, owner=OWNER, clock=lambda: NOW
    ).build(OWNER)
    assert board.device_count == 2
    assert {item.display_name for item in board.devices} == {"MacBook Pro", "Mac mini"}
    projection_latency_seconds = max(
        (NOW - datetime.fromisoformat(item.last_seen_at)).total_seconds()
        for item in board.devices
    )
    assert projection_latency_seconds < 10

    service = CopilotRelayService(relay, RelayPolicy(OWNER), clock=lambda: NOW)
    service.select_session(
        OWNER, "device_macbook", SESSION_ID, runtime_id="codex-runtime",
        selected_at=NOW.isoformat(),
    )
    service.handle_text(OWNER, "继续 Codex 测试", event_id="codex-command")
    service.select_session(
        OWNER, "device_macmini", SESSION_ID, runtime_id="claude-runtime",
        selected_at=NOW.isoformat(),
    )
    service.handle_text(OWNER, "采用 Claude 灰度方案", event_id="claude-command")

    ports = {
        "device_macbook": Port("codex-runtime", "codex-cli"),
        "device_macmini": Port("claude-runtime", "claude-code"),
    }
    reporters = {
        device: CopilotReporter(
            instance_id=device, secret="", central_url="https://central.invalid",
            spool_path=tmp_path / f"{device}.sqlite3", port=ports[device],
            transport=CentralMailbox(relay, device), clock=lambda: NOW,
            device_identity=Identity(device),
        )
        for device in ports
    }
    for reporter in reporters.values():
        assert await reporter.poll_inbound_once() == 1
        assert await reporter.apply_inbound_once() == 1

    assert ports["device_macbook"].received == ["继续 Codex 测试"]
    assert ports["device_macmini"].received == ["采用 Claude 灰度方案"]

    service.handle_text(OWNER, "断线期间第一条", event_id="offline-1")
    service.handle_text(OWNER, "断线期间第二条", event_id="offline-2")
    macmini = reporters["device_macmini"]
    assert await macmini.poll_inbound_once() == 2
    assert await macmini.apply_inbound_once() == 2
    assert await macmini.apply_inbound_once() == 0
    assert ports["device_macmini"].received[-2:] == ["断线期间第一条", "断线期间第二条"]
    assert ports["device_macbook"].received == ["继续 Codex 测试"]
