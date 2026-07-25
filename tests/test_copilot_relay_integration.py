from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
import secrets
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from ostag.copilot_relay.osstack_port import OsstackSession
from ostag.copilot_relay.policy import RelayPolicy
from ostag.copilot_relay.reporter import CopilotReporter
from ostag.copilot_relay.service import CopilotRelayService
from ostag.copilot_relay.store import RelayStore
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.reporter import TransportResponse
from ostag.work_monitor.server import HumanCredential, ServerAuth, create_app
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore


OWNER = Principal("feishu", "owner")
NOW = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)


class LocalSessionPort:
    def __init__(self) -> None:
        self.sessions = (
            OsstackSession(
                "session-1", "task-1", "run-1", "codex-cli", "idle", True, True, 4, 1.0,
                "codex-cli",
            ),
        )
        self.applied: list[tuple[str, str]] = []

    def list_sessions(self):
        return self.sessions

    def snapshot(self, session_id):
        return {
            "session_id": session_id, "task_id": "task-1", "run_id": "run-1",
            "agent_id": "codex-cli", "runtime_id": "codex-cli", "state": "idle", "version": 4,
            "turn_id": "turn-4", "assistant_text": "等待负责人确认测试范围",
            "decision": None, "updated_at": 1.0,
        }

    def runtime_inventory(self):
        return ()

    def send(self, envelope):
        self.applied.append((envelope.message_id, envelope.binding.session_id))
        return {"state": "duplicate" if len(self.applied) > 1 else "applied", "session_version": 5, "events": []}

    def create_task(self, confirmation, proposal):
        raise AssertionError("not used")


class ClientTransport:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.offline = False

    def request(self, method, url, body, headers):
        if self.offline:
            raise OSError("central unavailable")
        response = self.client.request(method, urlsplit(url).path, content=body, headers=headers)
        return TransportResponse(response.status_code, response.content)


def _central(tmp_path, secret: str):
    store = MonitorStore(tmp_path / "central.db")
    identities = IdentityStore(store.path)
    destination = Destination("feishu", "owner-chat", "private")
    identities.bind_private(owner=OWNER, destination=destination, local_admin=True)
    service = WorkMonitorService(store, identities)
    now = [NOW]
    app = create_app(
        service=service,
        projector=Projector(store),
        auth=ServerAuth(
            {"macmini": secret},
            human_credentials={"human": HumanCredential(OWNER, destination)},
            clock=lambda: now[0].timestamp(),
    ),
    )
    return app, store, now


@pytest.mark.asyncio
async def test_two_host_same_session_instruction_receipt_and_restart_recovery(tmp_path) -> None:
    secret = os.environ.get("OSTAG_SMOKE_RELAY_SECRET") or secrets.token_urlsafe(32)
    app, central_store, central_now = _central(tmp_path, secret)
    port = LocalSessionPort()
    with TestClient(app) as client:
        transport = ClientTransport(client)
        reporter = CopilotReporter(
            instance_id="macmini", secret=secret, central_url="https://relay.test",
            spool_path=tmp_path / "local.db", port=port, transport=transport, clock=lambda: NOW,
        )

        assert await reporter.discover_sessions_once() == 1
        assert await reporter.capture_changed_snapshots_once() == 2
        transport.offline = True
        assert await reporter.flush_outbound_once() == 0
        assert len(reporter.pending_outbound()) == 2
        transport.offline = False

        later = [NOW + timedelta(minutes=15)]
        central_now[0] = later[0]
        reporter.clock = lambda: later[0]
        assert await reporter.flush_outbound_once() == 2
        relay = RelayStore(central_store)
        assert relay.get_session("macmini", "session-1") is not None

        service = CopilotRelayService(relay, RelayPolicy(OWNER), clock=lambda: later[0])
        service.select_session(OWNER, "macmini", "session-1", selected_at=later[0].isoformat())
        queued = service.handle_text(OWNER, "改为先做回归测试", event_id="feishu-1")
        assert queued.outbound is not None

        assert await reporter.poll_inbound_once() == 1
        assert await reporter.apply_inbound_once() == 1
        assert port.applied == [(queued.outbound.message_id, "session-1")]
        assert await reporter.flush_receipts_once() == 1
        assert await reporter.flush_outbound_once() == 1
        with central_store.connect() as db:
            message_ids = [
                row[0] for row in db.execute(
                    "SELECT message_id FROM relay_messages WHERE direction='inbound'"
                ).fetchall()
            ]
        assert any(
            envelope is not None and envelope.kind == "delivery.receipt"
            for envelope in (relay.envelope_for_message(message_id) for message_id in message_ids)
        )

    restarted_app = create_app(
        service=WorkMonitorService(central_store, IdentityStore(central_store.path)),
        projector=Projector(central_store),
        auth=ServerAuth({"macmini": secret}, clock=lambda: later[0].timestamp()),
    )
    with TestClient(restarted_app) as restarted_client:
        restarted = CopilotReporter(
            instance_id="macmini", secret=secret, central_url="https://relay.test",
            spool_path=tmp_path / "local.db", port=port,
            transport=ClientTransport(restarted_client), clock=lambda: later[0],
        )
        assert restarted.pending_outbound() == []
        assert await restarted.poll_inbound_once() == 0
    assert port.applied == [("relay_feishu-1", "session-1")]
