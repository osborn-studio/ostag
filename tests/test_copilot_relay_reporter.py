import hashlib
import hmac
import json
import sqlite3
import stat
from datetime import UTC, datetime, timedelta

import pytest

from ostag.copilot_relay.models import RelayEnvelope
from ostag.copilot_relay.osstack_port import OsstackSession
from ostag.copilot_relay.osstack_port import OsstackPortError
from ostag.copilot_relay.reporter import CopilotReporter
from ostag.work_monitor.reporter import TransportResponse


NOW = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)


class FakePort:
    def __init__(self) -> None:
        self.sessions = (
            OsstackSession(
                "sess_1", "task_1", "run_1", "codex-cli", "idle", True, True, 4, 1.0,
                "codex-cli",
            ),
        )
        self.sent: list[str] = []
        self.send_results = ["applied"]
        self.created_tasks = []

    def list_sessions(self):
        return self.sessions

    def snapshot(self, session_id):
        return {
            "session_id": session_id, "task_id": "task_1", "run_id": "run_1",
            "agent_id": "codex-cli", "state": "idle", "version": 4,
            "runtime_id": "codex-cli",
            "turn_id": "turn_4", "assistant_text": "请选择方案", "decision": None,
            "updated_at": 1.0,
        }

    def runtime_inventory(self):
        return (
            {
                "runtime_id": "codex-cli", "agent_type": "codex",
                "state": "online", "session_count": 1,
            },
        )

    def send(self, envelope):
        self.sent.append(envelope.message_id)
        return {"state": self.send_results.pop(0), "session_version": 5, "events": []}

    def create_task(self, confirmation, proposal):
        self.created_tasks.append((confirmation.message_id, proposal.payload["proposal_id"]))


class FakeTransport:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []

    def request(self, method, url, body, headers):
        self.requests.append((method, url, body, headers))
        if not self.responses:
            raise OSError("offline")
        return self.responses.pop(0)


def _instruction(
    *, message_id="in_1", version=4, created="2026-07-14T01:00:00Z",
    expires="2026-07-14T02:00:00Z"
):
    return RelayEnvelope.from_dict(
        {
            "schema_version": 2, "message_id": message_id,
            "binding": {
                "instance_id": "macmini", "task_id": "task_1", "run_id": "run_1",
                "runtime_id": "codex-cli",
                "session_id": "sess_1", "turn_id": "turn_4", "session_version": version,
            },
            "sequence": 1, "created_at": created, "expires_at": expires,
            "sender": {"platform": "feishu", "actor_id": "owner"},
            "kind": "instruction.send", "payload": {"text": "继续"},
            "correlation_id": None, "causation_id": None,
        }
    )


def _reporter(tmp_path, *, transport=None, port=None, clock=lambda: NOW):
    return CopilotReporter(
        instance_id="macmini", secret="relay-secret", central_url="https://relay.example",
        spool_path=tmp_path / "relay.sqlite3", port=port or FakePort(),
        transport=transport or FakeTransport(), clock=clock,
    )


def test_spool_database_is_owner_only(tmp_path) -> None:
    reporter = _reporter(tmp_path)

    assert stat.S_IMODE(reporter.spool_path.stat().st_mode) == 0o600


def test_spool_migration_adds_durable_decision_export_cursor(tmp_path) -> None:
    path = tmp_path / "relay.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE relay_local_sessions("
            "session_id TEXT PRIMARY KEY,payload TEXT NOT NULL,"
            "exported_version INTEGER NOT NULL DEFAULT 0,"
            "snapshot_hash TEXT NOT NULL DEFAULT '')"
        )

    reporter = _reporter(tmp_path)

    with reporter._connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(relay_local_sessions)")}
    assert "exported_decision_id" in columns


@pytest.mark.asyncio
async def test_discovery_snapshot_spools_in_order_and_survives_restart(tmp_path) -> None:
    reporter = _reporter(tmp_path)
    assert await reporter.discover_sessions_once() == 1
    assert await reporter.capture_changed_snapshots_once() == 2
    pending = reporter.pending_outbound()
    assert [item.kind for item in pending] == ["session.upsert", "assistant.snapshot"]
    assert [item.sequence for item in pending] == [1, 2]

    restarted = _reporter(tmp_path)
    assert [item.message_id for item in restarted.pending_outbound()] == [
        item.message_id for item in pending
    ]
    assert await restarted.capture_changed_snapshots_once() == 0


@pytest.mark.asyncio
async def test_v2_message_ids_are_stable_and_namespaced_per_device(tmp_path) -> None:
    class Identity:
        def __init__(self, device_id: str):
            self.device_id = device_id

        @staticmethod
        def sign(body: bytes) -> str:
            return "signed"

    reporters = tuple(
        CopilotReporter(
            instance_id=device_id, secret="", central_url="https://relay.example",
            spool_path=tmp_path / f"{device_id}.sqlite3", port=FakePort(),
            device_identity=Identity(device_id),  # type: ignore[arg-type]
            clock=lambda: NOW,
        )
        for device_id in ("device_macbook", "device_macmini")
    )
    for reporter in reporters:
        await reporter.discover_sessions_once()
        await reporter.capture_changed_snapshots_once()

    first = [item.message_id for item in reporters[0].pending_outbound()]
    second = [item.message_id for item in reporters[1].pending_outbound()]
    assert first != second
    assert first == [item.message_id for item in reporters[0].pending_outbound()]
    assert all("device_macbook" not in item for item in first)


@pytest.mark.asyncio
async def test_awaiting_decision_snapshot_spools_a_real_decision_request(tmp_path) -> None:
    class DecisionPort(FakePort):
        def snapshot(self, session_id):
            result = super().snapshot(session_id)
            result["state"] = "awaiting_decision"
            result["decision"] = {
                "decision_id": "decision_live",
                "question": "请选择灰度方向",
                "choices": [
                    {"choice_id": "a", "label": "继续灰度"},
                    {"choice_id": "b", "label": "只读观察"},
                ],
            }
            return result

    port = DecisionPort()
    port.sessions = (
        OsstackSession(
            "sess_1", "task_1", "run_1", "codex-cli", "awaiting_decision",
            True, True, 4, 1.0, "codex-cli",
        ),
    )
    reporter = _reporter(tmp_path, port=port)
    await reporter.discover_sessions_once()

    assert await reporter.capture_changed_snapshots_once() == 3
    decision = reporter.pending_outbound()[-1]
    assert decision.kind == "decision.request"
    assert decision.to_dict()["payload"] == {
        "decision_id": "decision_live",
        "question": "请选择灰度方向",
        "choices": [
            {"choice_id": "a", "label": "继续灰度"},
            {"choice_id": "b", "label": "只读观察"},
        ],
        "recommended_choice_id": None,
        "blocking": True,
        "safe_work": [],
    }
    with reporter._connect() as db:
        db.execute("DELETE FROM relay_outbound")
        db.execute("UPDATE relay_local_sessions SET exported_decision_id='' ")

    assert await reporter.capture_changed_snapshots_once() == 1
    assert [item.kind for item in reporter.pending_outbound()] == ["decision.request"]


@pytest.mark.asyncio
async def test_flush_has_stable_hmac_and_ack_deletes_only_sent_sequence(tmp_path) -> None:
    transport = FakeTransport([TransportResponse(200, b'{"ack_sequence":1}')])
    reporter = _reporter(tmp_path, transport=transport)
    await reporter.discover_sessions_once()
    await reporter.capture_changed_snapshots_once()

    assert await reporter.flush_outbound_once() == 1
    method, url, body, headers = transport.requests[0]
    assert method == "POST" and url.endswith("/v1/copilot/events")
    canonical = "\n".join(
        ("v1", "POST", "/v1/copilot/events", "macmini", headers["X-OStag-Timestamp"],
         headers["X-OStag-Nonce"], hashlib.sha256(body).hexdigest())
    ).encode()
    assert headers["X-OStag-Signature"] == hmac.new(
        b"relay-secret", canonical, hashlib.sha256
    ).hexdigest()
    assert [item.sequence for item in reporter.pending_outbound()] == [2]


@pytest.mark.asyncio
async def test_outbound_sequence_never_reuses_after_full_ack_and_restart(tmp_path) -> None:
    transport = FakeTransport([TransportResponse(200, b'{"ack_sequence":2}')])
    reporter = _reporter(tmp_path, transport=transport)
    await reporter.discover_sessions_once()
    await reporter.capture_changed_snapshots_once()
    assert await reporter.flush_outbound_once() == 2
    assert reporter.pending_outbound() == []

    restarted = _reporter(tmp_path)
    with restarted._connect() as db:
        envelope = restarted._make_envelope(
            db,
            _instruction().binding,
            "delivery.receipt",
            {
                "related_message_id": "x", "state": "applied", "summary": "ok",
                "error_code": None,
            },
            "receipt_after_restart",
        )
        restarted._insert_outbound(db, envelope)
    assert restarted.pending_outbound()[0].sequence == 3


@pytest.mark.asyncio
async def test_offline_retry_backoff_and_409_reconciliation_preserve_spool(tmp_path) -> None:
    current = [NOW]
    transport = FakeTransport(
        [TransportResponse(500, b""), TransportResponse(409, b'{"expected_sequence":1}')]
    )
    reporter = _reporter(tmp_path, transport=transport, clock=lambda: current[0])
    await reporter.discover_sessions_once()
    await reporter.capture_changed_snapshots_once()
    assert await reporter.flush_outbound_once() == 0
    assert len(reporter.pending_outbound()) == 2
    assert await reporter.flush_outbound_once() == 0
    current[0] += timedelta(seconds=3)
    assert await reporter.flush_outbound_once() == 0
    assert reporter.reconciliation_required() is True
    assert len(reporter.pending_outbound()) == 2


@pytest.mark.asyncio
async def test_duplicate_poll_expiry_exact_version_and_apply_ack_recovery(tmp_path) -> None:
    messages = [_instruction().to_dict(), _instruction().to_dict()]
    transport = FakeTransport([TransportResponse(200, json.dumps({"messages": messages}).encode())])
    port = FakePort()
    reporter = _reporter(tmp_path, transport=transport, port=port)
    assert await reporter.poll_inbound_once() == 1
    assert await reporter.apply_inbound_once() == 1
    assert port.sent == ["in_1"]
    receipts = [item for item in reporter.pending_outbound() if item.kind == "delivery.receipt"]
    assert receipts[-1].payload["state"] == "applied"

    expired_transport = FakeTransport(
        [TransportResponse(200, json.dumps({"messages": [_instruction(
            message_id="expired", created="2026-07-14T00:00:00Z",
            expires="2026-07-14T00:59:59Z"
        ).to_dict()]}).encode())]
    )
    expired = _reporter(tmp_path, transport=expired_transport, port=port)
    assert await expired.poll_inbound_once() == 1
    assert await expired.apply_inbound_once() == 0
    assert "expired" not in port.sent


@pytest.mark.asyncio
async def test_crash_after_local_apply_replays_same_message_id_and_osstack_deduplicates(tmp_path) -> None:
    transport = FakeTransport(
        [TransportResponse(200, json.dumps({"messages": [_instruction().to_dict()]}).encode())]
    )
    port = FakePort()
    port.send_results = ["applied", "duplicate"]
    reporter = _reporter(tmp_path, transport=transport, port=port)
    await reporter.poll_inbound_once()
    reporter._after_local_apply = lambda: (_ for _ in ()).throw(RuntimeError("crash"))
    with pytest.raises(RuntimeError, match="crash"):
        await reporter.apply_inbound_once()

    restarted = _reporter(tmp_path, port=port)
    assert await restarted.apply_inbound_once() == 1
    assert port.sent == ["in_1", "in_1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("version_delta", [1, 2])
async def test_reserved_osstack_delivery_at_recovery_version_is_retried_not_stale(
    tmp_path, version_delta,
) -> None:
    transport = FakeTransport(
        [TransportResponse(200, json.dumps({"messages": [_instruction().to_dict()]}).encode())]
    )
    port = FakePort()
    original = port.sessions[0]
    port.sessions = (
        OsstackSession(
            original.session_id,
            original.task_id,
            original.run_id,
            original.agent_id,
            original.state,
            original.resume_supported,
            original.resume_ready,
            original.version + version_delta,
            original.updated_at,
            original.runtime_id,
        ),
    )
    reporter = _reporter(tmp_path, transport=transport, port=port)

    assert await reporter.poll_inbound_once() == 1
    assert await reporter.apply_inbound_once() == 1
    assert port.sent == ["in_1"]
    receipts = [item for item in reporter.pending_outbound() if item.kind == "delivery.receipt"]
    assert receipts[-1].payload["state"] == "applied"


@pytest.mark.asyncio
async def test_applied_inbound_is_acknowledged_with_stable_payload_hash(tmp_path) -> None:
    instruction = _instruction()
    transport = FakeTransport(
        [
            TransportResponse(200, json.dumps({"messages": [instruction.to_dict()]}).encode()),
            TransportResponse(200, b'{"acked":["in_1"]}'),
        ]
    )
    reporter = _reporter(tmp_path, transport=transport)
    assert await reporter.poll_inbound_once() == 1
    assert await reporter.apply_inbound_once() == 1
    assert await reporter.flush_receipts_once() == 1

    method, url, body, _ = transport.requests[-1]
    assert method == "POST" and url.endswith("/v1/copilot/messages/ack")
    ack = json.loads(body)["acks"][0]
    canonical = json.dumps(
        instruction.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert ack == {
        "message_id": "in_1",
        "payload_hash": hashlib.sha256(canonical.encode()).hexdigest(),
    }
    assert await reporter.flush_receipts_once() == 0


@pytest.mark.asyncio
async def test_device_heartbeat_is_bounded_and_sent_no_more_than_every_five_seconds(
    tmp_path,
) -> None:
    current = [NOW]

    class Identity:
        device_id = "device_macbook"

        @staticmethod
        def sign(body: bytes) -> str:
            assert body.startswith(b"v2\nPOST\n/v1/copilot/device/heartbeat")
            return "signed"

    transport = FakeTransport([TransportResponse(200, b'{"accepted":true}')] * 2)
    reporter = CopilotReporter(
        instance_id="device_macbook",
        secret="",
        central_url="https://relay.example",
        spool_path=tmp_path / "relay.sqlite3",
        port=FakePort(),
        transport=transport,
        clock=lambda: current[0],
        device_identity=Identity(),  # type: ignore[arg-type]
        gateway_version="0.2.0",
    )

    assert await reporter.heartbeat_once() == 1
    assert await reporter.heartbeat_once() == 0
    method, url, body, headers = transport.requests[0]
    assert method == "POST" and url.endswith("/v1/copilot/device/heartbeat")
    assert json.loads(body) == {
        "device_id": "device_macbook",
        "gateway_version": "0.2.0",
        "runtimes": [
            {
                "runtime_id": "codex-cli",
                "agent_type": "codex",
                "state": "online",
                "session_count": 1,
            }
        ],
    }
    assert headers["X-OStag-Device"] == "device_macbook"
    current[0] += timedelta(seconds=5)
    assert await reporter.heartbeat_once() == 1


@pytest.mark.asyncio
async def test_device_heartbeat_survives_temporary_osstack_inventory_failure(tmp_path) -> None:
    class UnavailablePort(FakePort):
        def runtime_inventory(self):
            raise OsstackPortError("osstack is temporarily unavailable")

    class Identity:
        device_id = "device_macbook"

        @staticmethod
        def sign(body: bytes) -> str:
            return "signed"

    transport = FakeTransport([TransportResponse(200, b'{"accepted":true}')])
    reporter = CopilotReporter(
        instance_id="device_macbook", secret="", central_url="https://relay.example",
        spool_path=tmp_path / "relay.sqlite3", port=UnavailablePort(),
        transport=transport, clock=lambda: NOW,
        device_identity=Identity(),  # type: ignore[arg-type]
    )

    assert await reporter.heartbeat_once() == 1
    assert json.loads(transport.requests[0][2])["runtimes"] == []
    assert await reporter.heartbeat_once() == 0


@pytest.mark.asyncio
async def test_snapshot_request_returns_safe_snapshot_to_outbound_spool(tmp_path) -> None:
    request = _instruction(message_id="snapshot_request").to_dict()
    request["kind"] = "assistant.snapshot"
    request["payload"] = {"assistant_text": "", "redaction_count": 0, "truncated": False}
    transport = FakeTransport(
        [TransportResponse(200, json.dumps({"messages": [request]}).encode())]
    )
    reporter = _reporter(tmp_path, transport=transport)
    await reporter.poll_inbound_once()
    assert await reporter.apply_inbound_once() == 1
    snapshots = [item for item in reporter.pending_outbound() if item.kind == "assistant.snapshot"]
    assert snapshots[-1].payload["assistant_text"] == "请选择方案"


@pytest.mark.asyncio
async def test_task_proposal_is_staged_until_matching_confirmation(tmp_path) -> None:
    proposal = _instruction(message_id="proposal_message").to_dict()
    proposal["kind"] = "task.create.proposal"
    proposal["payload"] = {
        "proposal_id": "proposal_1", "objective": "修复登录", "repository": "ostag",
        "workspace": "managed", "suggested_agent": "codex-cli",
        "acceptance_criteria": ["tests pass"], "sensitive_operations": [],
    }
    confirmation = _instruction(message_id="confirm_message").to_dict()
    confirmation["kind"] = "task.create.confirm"
    confirmation["payload"] = {"proposal_id": "proposal_1", "confirmed": True}
    port = FakePort()
    reporter = _reporter(
        tmp_path,
        port=port,
        transport=FakeTransport(
            [TransportResponse(200, json.dumps({"messages": [proposal, confirmation]}).encode())]
        ),
    )
    assert await reporter.poll_inbound_once() == 2
    assert await reporter.apply_inbound_once() == 1
    assert port.created_tasks == [("confirm_message", "proposal_1")]


@pytest.mark.asyncio
async def test_decision_timeout_receipt_is_consumed_without_guessing_an_answer(tmp_path) -> None:
    receipt = _instruction(message_id="timeout_decision_1").to_dict()
    receipt["kind"] = "delivery.receipt"
    receipt["payload"] = {
        "related_message_id": "decision_1", "state": "expired",
        "summary": "decision timed out without an answer", "error_code": "decision_timeout",
    }
    port = FakePort()
    reporter = _reporter(
        tmp_path,
        port=port,
        transport=FakeTransport(
            [TransportResponse(200, json.dumps({"messages": [receipt]}).encode())]
        ),
    )
    assert await reporter.poll_inbound_once() == 1
    assert await reporter.apply_inbound_once() == 1
    assert port.sent == []
    assert not any(item.kind == "delivery.receipt" for item in reporter.pending_outbound())
