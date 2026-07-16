import json
from datetime import UTC, datetime, timedelta

import pytest

from ostag.copilot_relay.feishu_routes import CopilotFeishuController
from ostag.copilot_relay.models import RelayEnvelope
from ostag.copilot_relay.policy import RelayPolicy
from ostag.copilot_relay.scheduler import CopilotRelayScheduler
from ostag.copilot_relay.service import CopilotRelayService
from ostag.copilot_relay.store import RelayStore
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.scheduler import WorkMonitorScheduler
from ostag.work_monitor.store import MonitorStore


OWNER = Principal("feishu", "owner")
NOW = datetime(2026, 7, 14, 1, 10, tzinfo=UTC)


def _envelope(kind: str, message_id: str, *, version: int = 1, expires: str = "2026-07-14T02:00:00Z"):
    payloads = {
        "session.upsert": {
            "agent_id": "codex-cli", "title": "Relay", "state": "running",
            "resume_supported": True, "latest_summary": f"阶段 {version}",
        },
        "decision.request": {
            "decision_id": f"decision-{message_id}", "question": "采用哪种方案？",
            "choices": [{"choice_id": "a", "label": "方案 A"}],
            "recommended_choice_id": "a", "blocking": True, "safe_work": ["继续检查"],
        },
        "assistant.snapshot": {
            "assistant_text": "这是经过过滤的最新回复。", "redaction_count": 0,
            "truncated": False,
        },
        "delivery.receipt": {
            "related_message_id": "relay-user-instruction", "state": "applied",
            "summary": "原会话已应用这条指令", "error_code": None,
        },
    }
    return RelayEnvelope.from_dict(
        {
            "schema_version": 1, "message_id": message_id,
            "binding": {
                "instance_id": "macmini", "task_id": "task-1", "run_id": "run-1",
                "session_id": "session-1", "turn_id": f"turn-{version}",
                "session_version": version,
            },
            "sequence": version, "created_at": "2026-07-14T01:00:00Z",
            "expires_at": expires, "sender": {"platform": "osstack", "actor_id": "reporter"},
            "kind": kind, "payload": payloads[kind], "correlation_id": None,
            "causation_id": None,
        }
    )


def _setup(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    identities = IdentityStore(store.path)
    destination = Destination("feishu", "owner-chat", "private")
    identities.bind_private(owner=OWNER, destination=destination, local_admin=True)
    relay = RelayStore(store)
    session = _envelope("session.upsert", "session-upsert")
    relay.enqueue(session, direction="inbound")
    relay.upsert_session(session)
    controller = CopilotFeishuController(
        CopilotRelayService(relay, RelayPolicy(OWNER), clock=lambda: NOW),
        clock=lambda: NOW,
    )
    return store, identities, relay, controller


def test_decision_is_planned_once_and_stage_summaries_are_coalesced(tmp_path) -> None:
    store, identities, relay, _ = _setup(tmp_path)
    decision = _envelope("decision.request", "decision-message")
    relay.enqueue(decision, direction="inbound")
    relay.record_decision(decision)
    planner = CopilotRelayScheduler(store, identities, clock=lambda: NOW)

    assert planner.plan_once() >= 1
    assert planner.plan_once() == 0
    with store.connect() as db:
        payloads = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM notification_outbox WHERE payload LIKE '%relay.%'"
        )]
    assert sum(item["kind"] == "relay.decision" for item in payloads) == 1
    assert sum(item["kind"] == "relay.stage" for item in payloads) == 0


def test_only_terminal_lifecycle_states_are_pushed_once(tmp_path) -> None:
    store, identities, relay, _ = _setup(tmp_path)
    for version, state in ((2, "starting"), (3, "running"), (4, "completed")):
        envelope = _envelope("session.upsert", f"stage-{state}", version=version)
        raw = envelope.to_dict()
        raw["payload"]["state"] = state
        relay.enqueue(RelayEnvelope.from_dict(raw), direction="inbound")

    planner = CopilotRelayScheduler(store, identities, clock=lambda: NOW)
    assert planner.plan_once() == 1
    assert planner.plan_once() == 0
    with store.connect() as db:
        payloads = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM notification_outbox WHERE payload LIKE '%relay.stage%'"
        )]
    assert len(payloads) == 1
    assert payloads[0]["relay_message_id"] == "stage-completed"


def test_reconnect_emits_one_unresolved_decision_reminder(tmp_path) -> None:
    store, identities, relay, _ = _setup(tmp_path)
    decision = _envelope("decision.request", "decision-reconnect")
    relay.enqueue(decision, direction="inbound")
    relay.record_decision(decision)
    identities.bind_instance(
        OWNER, "macmini", display_name="Mac mini", bound_at=NOW.isoformat()
    )
    with store.transaction() as db:
        db.execute(
            "INSERT INTO monitor_devices(device_id,principal_platform,actor_id,display_name,public_key,paired_at,last_seen_at) "
            "VALUES ('macmini','feishu','owner','Mac mini','test-key',?,'')",
            (NOW.isoformat(),),
        )
    planner = CopilotRelayScheduler(store, identities, clock=lambda: NOW)
    planner.plan_once()
    with store.transaction() as db:
        db.execute(
            "UPDATE monitor_devices SET last_seen_at=? WHERE device_id='macmini'",
            (NOW.isoformat(),),
        )

    assert planner.plan_once() == 1
    assert planner.plan_once() == 0
    with store.connect() as db:
        reminders = db.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_id LIKE 'relay-reconnect:%'"
        ).fetchone()[0]
    assert reminders == 1


def test_expired_decision_times_out_without_guess_and_notifies_local_session(tmp_path) -> None:
    store, identities, relay, _ = _setup(tmp_path)
    expired = _envelope(
        "decision.request", "expired-decision", expires="2026-07-14T01:09:59Z"
    )
    relay.enqueue(expired, direction="inbound")
    relay.record_decision(expired)

    CopilotRelayScheduler(store, identities, clock=lambda: NOW).plan_once()

    with store.connect() as db:
        assert db.execute(
            "SELECT state FROM relay_decisions WHERE decision_id=?",
            (expired.payload["decision_id"],),
        ).fetchone()[0] == "timed_out"
    timeout = relay.envelope_for_message(f"timeout_{expired.payload['decision_id']}")
    assert timeout is not None and timeout.kind == "delivery.receipt"
    assert timeout.payload["state"] == "expired"
    assert timeout.payload["error_code"] == "decision_timeout"


class CardSender:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.cards = []

    async def send_card(self, destination_id, card, fallback, *, delivery_id, chat_type):
        response = self.responses.pop(0) if self.responses else 200
        if response >= 400:
            error = RuntimeError(str(response))
            error.status_code = response
            raise error
        self.cards.append((destination_id, card, delivery_id, chat_type))
        return f"platform-{delivery_id}"

    async def send_message(self, *args, **kwargs):
        raise AssertionError("relay notifications must use cards")


@pytest.mark.asyncio
async def test_work_monitor_owner_delivers_relay_card_and_persists_quote_binding(tmp_path) -> None:
    store, identities, relay, controller = _setup(tmp_path)
    decision = _envelope("decision.request", "decision-deliver")
    relay.enqueue(decision, direction="inbound")
    relay.record_decision(decision)
    sender = CardSender()
    scheduler = WorkMonitorScheduler(
        store, identities, {"feishu": sender}, clock=lambda: NOW,
        copilot=controller, jitter=lambda _: 0,
    )

    await scheduler.tick()

    assert len([item for item in sender.cards if "等待决策" in str(item[1])]) == 1
    rendered = str(sender.cards[-1][1])
    assert "本次回复将发送到：macmini · Codex · Relay" in rendered
    callback = next(
        action["value"]["callback_id"]
        for element in sender.cards[-1][1]["elements"]
        if element["tag"] == "action"
        for action in element["actions"]
    )
    answered = controller.handle_callback(
        callback,
        principal=OWNER,
        destination=Destination("feishu", "owner-chat", "private"),
    )
    assert answered.text.startswith("回答已排队")
    platform_id = sender.cards[-1][0]
    with store.connect() as db:
        row = db.execute(
            "SELECT relay_message_id FROM relay_platform_messages WHERE relay_message_id=?",
            (decision.message_id,),
        ).fetchone()
    assert row[0] == decision.message_id
    assert platform_id == "owner"


@pytest.mark.asyncio
async def test_requested_assistant_snapshot_is_delivered_as_bounded_card(tmp_path) -> None:
    store, identities, relay, controller = _setup(tmp_path)
    snapshot = _envelope("assistant.snapshot", "snapshot-response", version=2)
    relay.enqueue(snapshot, direction="inbound")
    sender = CardSender()
    scheduler = WorkMonitorScheduler(
        store, identities, {"feishu": sender}, clock=lambda: NOW,
        copilot=controller, jitter=lambda _: 0,
    )

    await scheduler.tick()

    assert any("这是经过过滤的最新回复" in str(card) for _, card, _, _ in sender.cards)


@pytest.mark.asyncio
async def test_applied_receipt_is_reported_without_claiming_code_success(tmp_path) -> None:
    store, identities, relay, controller = _setup(tmp_path)
    receipt = _envelope("delivery.receipt", "receipt-applied", version=2)
    relay.enqueue(receipt, direction="inbound")
    sender = CardSender()
    scheduler = WorkMonitorScheduler(
        store, identities, {"feishu": sender}, clock=lambda: NOW,
        copilot=controller, jitter=lambda _: 0,
    )

    await scheduler.tick()

    rendered = " ".join(str(card) for _, card, _, _ in sender.cards)
    assert "原会话已应用这条指令" in rendered
    assert "代码已完成" not in rendered


@pytest.mark.asyncio
async def test_relay_delivery_uses_existing_retry_deadletter_and_scheduler_owner(tmp_path) -> None:
    store, identities, relay, controller = _setup(tmp_path)
    decision = _envelope("decision.request", "decision-retry")
    relay.enqueue(decision, direction="inbound")
    relay.record_decision(decision)
    now = [NOW]
    sender = CardSender([500, 500, 500, 500, 500])
    scheduler = WorkMonitorScheduler(
        store, identities, {"feishu": sender}, clock=lambda: now[0],
        copilot=controller, jitter=lambda _: 0, retry_cap=5,
    )
    await scheduler.tick(drain=False)
    standby = WorkMonitorScheduler(
        store, identities, {"feishu": sender}, clock=lambda: now[0],
        copilot=controller, jitter=lambda _: 0, retry_cap=5,
    )
    await standby.tick(drain=False)
    for minutes in (0, 1, 2, 4, 8):
        if minutes:
            now[0] += timedelta(minutes=minutes)
        await scheduler.drain_once()
    with store.connect() as db:
        row = db.execute(
            "SELECT state,attempts,delivery_id FROM notification_outbox "
            "WHERE payload LIKE '%relay.decision%'"
        ).fetchone()
        owners = db.execute("SELECT count(*) FROM monitor_scheduler_owners").fetchone()[0]
    assert tuple(row[:2]) == ("dead_letter", 5)
    assert row["delivery_id"].startswith("relay:relay-decision:decision-retry:")
    assert owners == 1
