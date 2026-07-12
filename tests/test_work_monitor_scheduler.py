import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.scheduler import WorkMonitorScheduler
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore


class Sender:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.sent = []

    async def send_message(self, destination_id, text, *, delivery_id, chat_type="group"):
        response = self.responses.pop(0) if self.responses else 200
        if response >= 400:
            error = RuntimeError(str(response))
            error.status_code = response
            raise error
        self.sent.append((destination_id, text, delivery_id, chat_type))


def setup(tmp_path: Path):
    store = MonitorStore(tmp_path / "monitor.db")
    identities = IdentityStore(store.path)
    owner = Principal("feishu", "u1")
    destination = Destination("feishu", "chat1", "private")
    identities.bind_private(owner=owner, destination=destination, local_admin=True)
    identities.subscribe(owner, destination)
    return store, identities


def seed(store, *, status="running", occurred=None, seen=None):
    occurred = occurred or datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
    seen = seen or occurred
    with store.transaction() as db:
        db.execute("INSERT OR REPLACE INTO instances VALUES (?,?,?)", ("i1", 1, seen.isoformat()))
        db.execute(
            "INSERT OR REPLACE INTO tasks(instance_id,task_id,title,project,status,agent,progress,duration_seconds,summary,error_class,evidence_ref,occurred_at,version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "i1",
                "t1",
                "build",
                "p",
                status,
                "a",
                30,
                10,
                "safe",
                None,
                None,
                occurred.isoformat(),
                1,
            ),
        )


@pytest.mark.asyncio
async def test_critical_immediate_completion_digest_and_health_thresholds(tmp_path):
    store, identities = setup(tmp_path)
    now = [datetime(2026, 7, 13, 3, 0, tzinfo=UTC)]
    seed(store, status="blocked", seen=now[0] - timedelta(minutes=3))
    scheduler = WorkMonitorScheduler(
        store, identities, {"feishu": Sender()}, clock=lambda: now[0], jitter=lambda _: 0
    )
    await scheduler.tick(drain=False)
    with store.connect() as db:
        kinds = [
            json.loads(r[0])["kind"] for r in db.execute("SELECT payload FROM notification_outbox")
        ]
    assert "task.blocked" in kinds
    assert "instance.degraded" in kinds
    now[0] += timedelta(minutes=8)
    await scheduler.tick(drain=False)
    with store.connect() as db:
        kinds = [
            json.loads(r[0])["kind"] for r in db.execute("SELECT payload FROM notification_outbox")
        ]
    assert "instance.offline" in kinds
    seed(store, status="completed", occurred=now[0])
    await scheduler.tick(drain=False)
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM notification_outbox WHERE payload LIKE '%task.completed%'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_beijing_windows_once_across_restart_and_cursor_per_target(tmp_path):
    store, identities = setup(tmp_path)
    seed(store, status="approval_required")
    now = datetime(2026, 7, 13, 1, 0, tzinfo=UTC)
    first = WorkMonitorScheduler(store, identities, {"feishu": Sender()}, clock=lambda: now)
    await first.tick(drain=False)
    second = WorkMonitorScheduler(store, identities, {"feishu": Sender()}, clock=lambda: now)
    await second.tick(drain=False)
    with store.connect() as db:
        morning = db.execute(
            "SELECT payload FROM notification_outbox WHERE payload LIKE '%digest.morning%'"
        ).fetchall()
    assert len(morning) == 1
    sections = json.loads(morning[0][0])["sections"]
    assert {"running", "blocked", "approval_required", "queued"}.issubset(sections)
    assert sections["approval_required"] == [{"task_id": "t1", "title": "build"}]
    sender = Sender()
    delivery = WorkMonitorScheduler(store, identities, {"feishu": sender}, clock=lambda: now)
    while await delivery.drain_once():
        pass
    with identities._connect() as db:
        assert db.execute("SELECT delivery_cursor FROM monitor_subscriptions").fetchone()[0] == len(
            sender.sent
        )


@pytest.mark.asyncio
async def test_competing_workers_claim_once_and_retry_deadletter_recovery(tmp_path):
    store, identities = setup(tmp_path)
    now = [datetime(2026, 7, 13, 4, 0, tzinfo=UTC)]
    seed(store, status="failed", seen=now[0])
    sender = Sender([500, 500, 500, 500, 500])
    a = WorkMonitorScheduler(
        store, identities, {"feishu": sender}, clock=lambda: now[0], jitter=lambda _: 0
    )
    b = WorkMonitorScheduler(
        store, identities, {"feishu": sender}, clock=lambda: now[0], jitter=lambda _: 0
    )
    await a.tick(drain=False)
    await asyncio.gather(a.drain_once(), b.drain_once())
    for minutes in (1, 2, 4, 8):
        now[0] += timedelta(minutes=minutes)
        await a.drain_once()
    with store.connect() as db:
        row = db.execute("SELECT state,attempts FROM notification_outbox").fetchone()
    assert tuple(row) == ("dead_letter", 5)
    sender.responses = []
    assert a.replay_dead_letters() == 1
    await a.drain_once()
    assert "延迟通知" in sender.sent[0][1]
    assert sender.sent[0][2]


@pytest.mark.asyncio
async def test_successful_platform_delivery_replays_earlier_dead_letters(tmp_path):
    store, identities = setup(tmp_path)
    now = datetime(2026, 7, 13, 4, 0, tzinfo=UTC)
    scheduler = WorkMonitorScheduler(store, identities, {"feishu": Sender()}, clock=lambda: now)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO notification_outbox(notification_id,event_id,destination_key,payload,delivery_id,state,next_attempt_at) VALUES (?,?,?,?,?,'dead_letter',?)",
            (
                "old",
                "old-event",
                "feishu:chat1:1",
                '{"kind":"old"}',
                "old-delivery",
                now.isoformat(),
            ),
        )
    subscription = scheduler._subscriptions()[0]
    scheduler._enqueue(subscription, "recovery-probe", {"kind": "probe"})
    await scheduler.drain_once()
    with store.connect() as db:
        old = db.execute(
            "SELECT state,delayed FROM notification_outbox WHERE notification_id='old'"
        ).fetchone()
    assert tuple(old) == ("pending", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["feishu", "telegram"])
async def test_control_ack_delivers_once_to_bound_recipient_without_fake_subscription(
    tmp_path, platform
):
    store = MonitorStore(tmp_path / "control.db")
    identities = IdentityStore(store.path)
    owner = Principal(platform, "owner")
    destination = Destination(platform, "chat", "private")
    identities.bind_private(owner=owner, destination=destination, local_admin=True)
    now = datetime.now(UTC)
    with store.transaction() as db:
        db.execute("INSERT INTO instances VALUES (?,?,?)", ("i", 1, now.isoformat()))
        db.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("i", "t", "ship", "p", "running", "a", 1, 1, "ok", None, None, now.isoformat(), 1),
        )
        db.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?)",
            (
                "hb",
                "i",
                1,
                "instance.heartbeat",
                now.isoformat(),
                json.dumps({"per_task_capabilities": {"t": ["pause"]}}),
            ),
        )
    service = WorkMonitorService(store, identities)
    receipt = service.propose_control(owner, destination, task_id="t", action="pause")
    service.approve_control(owner, destination, receipt.action_id)
    service.apply_control(owner, destination, receipt.action_id)
    with store.connect() as db:
        command_id = db.execute("SELECT command_id FROM commands").fetchone()[0]
    result = {"ok": True, "code": "OK", "status": "paused", "version": 1}
    service.acknowledge_control("i", command_id, result)
    service.acknowledge_control("i", command_id, result)
    sender = Sender()
    scheduler = WorkMonitorScheduler(store, identities, {platform: sender})
    while await scheduler.drain_once():
        pass
    assert len(sender.sent) == 1
    with identities._connect() as db:
        assert db.execute("SELECT count(*) FROM monitor_subscriptions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_legacy_outbox_is_suppressed_and_audited(tmp_path):
    store, identities = setup(tmp_path)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO notification_outbox(notification_id,event_id,destination_key,payload) VALUES ('legacy','old','control','{}')"
        )
    scheduler = WorkMonitorScheduler(store, identities, {"feishu": Sender()})
    await scheduler.drain_once()
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT state FROM notification_outbox WHERE notification_id='legacy'"
            ).fetchone()[0]
            == "suppressed"
        )
    assert any(r["event_type"] == "notification_suppress" for r in scheduler.ledger._read_all())
