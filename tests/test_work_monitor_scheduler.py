import asyncio
import contextlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ostag.config import Settings
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.scheduler import WorkMonitorScheduler
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore
from ostag.work_monitor.transport import TransportError
from ostag.work_monitor import store as store_module


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


@pytest.mark.asyncio
async def test_scheduler_publishes_runtime_owner_lease_for_doctor(tmp_path: Path) -> None:
    store, identities = setup(tmp_path)
    now = datetime(2026, 7, 13, 3, 0, tzinfo=UTC)
    scheduler = WorkMonitorScheduler(store, identities, {}, clock=lambda: now)

    await scheduler.tick(drain=False)

    with store.connect() as db:
        owner = db.execute(
            "SELECT owner_id,lease_until FROM monitor_scheduler_owners"
        ).fetchone()
    assert owner["owner_id"] == scheduler.owner
    assert datetime.fromisoformat(owner["lease_until"]) > now


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
    with store.transaction() as db:
        db.execute("DELETE FROM notification_outbox WHERE payload LIKE '%digest.%'")
    await asyncio.gather(a.drain_once(), b.drain_once())
    for minutes in (1, 2, 4, 8):
        now[0] += timedelta(minutes=minutes)
        await a.drain_once()
    with store.connect() as db:
        row = db.execute("SELECT state,attempts FROM notification_outbox").fetchone()
    assert tuple(row) == ("dead_letter", 5)
    sender.responses = []
    assert a.replay_dead_letters(destination_key="feishu:chat1:1", limit=1) == 1
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
            "INSERT INTO notification_outbox(notification_id,event_id,destination_key,payload,delivery_id,state,next_attempt_at,failure_transient,deadlettered_at) VALUES (?,?,?,?,?,'dead_letter',?,1,?)",
            (
                "old",
                "old-event",
                "feishu:chat1:1",
                '{"kind":"old"}',
                "old-delivery",
                now.isoformat(),
                (now - timedelta(minutes=2)).isoformat(),
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


async def _await_real_lease_renewal(
    store, owner: str, after: datetime, delivery: asyncio.Task, *, timeout: float = 5.0
) -> datetime:
    """Block until ``owner``'s heartbeat actually writes a lease beyond ``after``.

    Observes the real ``notification_outbox`` row the running scheduler renews,
    so the fencing assertion runs only once a genuine renewal by that owner has
    been persisted, instead of assuming it landed inside an arbitrary sleep
    window. Bounded by ``timeout``, and fails fast if the delivery it is
    waiting on already ended.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        with store.connect() as db:
            row = db.execute(
                "SELECT lease_until FROM notification_outbox "
                "WHERE lease_owner=? AND state='pending' ORDER BY rowid LIMIT 1",
                (owner,),
            ).fetchone()
        if row is not None and row["lease_until"]:
            renewed = datetime.fromisoformat(row["lease_until"])
            if renewed > after:
                return renewed
        if delivery.done():
            delivery.result()
            raise AssertionError("delivery finished before the lease was renewed")
        if loop.time() >= deadline:
            raise AssertionError(f"owner {owner} did not renew the delivery lease")
        await asyncio.sleep(0.002)


@pytest.mark.asyncio
async def test_lease_renewal_fences_second_owner_and_increments_cursor_once(tmp_path):
    store, identities = setup(tmp_path)
    now = [datetime(2026, 7, 13, 4, 0, tzinfo=UTC)]
    seed(store, status="failed", seen=now[0])
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingSender(Sender):
        async def send_message(self, *args, **kwargs):
            entered.set()
            await release.wait()
            await super().send_message(*args, **kwargs)

    sender = BlockingSender()
    first = WorkMonitorScheduler(
        store, identities, {"feishu": sender}, clock=lambda: now[0], lease_seconds=0.15
    )
    await first.tick(drain=False)
    with store.transaction() as db:
        db.execute("DELETE FROM notification_outbox WHERE payload LIKE '%digest.%'")
    second = WorkMonitorScheduler(
        store, identities, {"feishu": sender}, clock=lambda: now[0], lease_seconds=0.15
    )
    delivery = asyncio.create_task(first.drain_once())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        now[0] += timedelta(minutes=1)
        # The original claim lease has now expired on the shared clock; only a
        # real heartbeat renewal can still fence the competing owner out.
        renewed = await _await_real_lease_renewal(store, first.owner, now[0], delivery)
        assert renewed > now[0]
        assert await second.drain_once() is False
        release.set()
        assert await delivery is True
    finally:
        release.set()
        delivery.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await delivery
    assert len(sender.sent) == 1
    with identities._connect() as db:
        assert db.execute("SELECT delivery_cursor FROM monitor_subscriptions").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_deliver_audit_reconciles_without_resending(tmp_path, monkeypatch):
    store, identities = setup(tmp_path)
    seed(store, status="failed", seen=datetime.now(UTC))
    sender = Sender()
    scheduler = WorkMonitorScheduler(store, identities, {"feishu": sender})
    await scheduler.tick(drain=False)
    with store.transaction() as db:
        db.execute("DELETE FROM notification_outbox WHERE payload LIKE '%digest.%'")
    original = scheduler.ledger.record_notification
    monkeypatch.setattr(
        scheduler.ledger,
        "record_notification",
        lambda *a, **k: (_ for _ in ()).throw(OSError("down")),
    )
    assert await scheduler.drain_once() is True
    assert len(sender.sent) == 1
    monkeypatch.setattr(scheduler.ledger, "record_notification", original)
    assert await scheduler.drain_once() is False
    assert len(sender.sent) == 1
    with store.connect() as db:
        assert tuple(
            db.execute("SELECT state,ledger_synced FROM notification_outbox").fetchone()
        ) == ("delivered", 1)


@pytest.mark.asyncio
async def test_retry_after_and_permanent_errors_are_classified(tmp_path):
    store, identities = setup(tmp_path)
    now = [datetime(2026, 7, 13, 4, 0, tzinfo=UTC)]
    seed(store, status="failed", seen=now[0])

    class ErrorSender(Sender):
        async def send_message(self, *args, **kwargs):
            raise TransportError(
                "limited", status_code=429, retry_after_seconds=600, transient=True
            )

    scheduler = WorkMonitorScheduler(
        store, identities, {"feishu": ErrorSender()}, clock=lambda: now[0], jitter=lambda _: 0
    )
    await scheduler.tick(drain=False)
    with store.transaction() as db:
        db.execute("DELETE FROM notification_outbox WHERE payload LIKE '%digest.%'")
    await scheduler.drain_once()
    with store.connect() as db:
        row = db.execute("SELECT state,next_attempt_at FROM notification_outbox").fetchone()
    assert row[0] == "pending"
    assert datetime.fromisoformat(row[1]) == now[0] + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_digest_catches_up_at_ten_and_twenty_with_bounded_windows(tmp_path):
    store, identities = setup(tmp_path)
    seed(store)
    now = [datetime(2026, 7, 13, 2, 0, tzinfo=UTC)]  # 10:00 Beijing
    scheduler = WorkMonitorScheduler(store, identities, {"feishu": Sender()}, clock=lambda: now[0])
    await scheduler.tick(drain=False)
    now[0] = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)  # 20:00 Beijing
    await scheduler.tick(drain=False)
    await WorkMonitorScheduler(store, identities, {"feishu": Sender()}, clock=lambda: now[0]).tick(
        drain=False
    )
    with store.connect() as db:
        digests = [
            json.loads(row[0])["kind"]
            for row in db.execute(
                "SELECT payload FROM notification_outbox WHERE payload LIKE '%digest.%'"
            )
        ]
    assert digests.count("digest.morning") == 1
    assert digests.count("digest.evening") == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"monitor_morning_digest_hour": 24},
        {"monitor_morning_digest_hour": 9, "monitor_evening_digest_hour": 9},
        {"monitor_degraded_seconds": 600, "monitor_offline_seconds": 120},
        {"monitor_notification_retry_cap": 0},
    ],
)
def test_scheduler_settings_validation(overrides):
    with pytest.raises(ValueError):
        Settings(**overrides).validate()


def test_notification_migration_partial_failure_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    MonitorStore(path)
    original_version = len(store_module.MIGRATIONS)
    broken = store_module.Migration(
        statements=(
            "CREATE TABLE migration_must_rollback(value INTEGER)",
            "THIS IS INVALID SQL",
        )
    )
    monkeypatch.setattr(store_module, "MIGRATIONS", (*store_module.MIGRATIONS, broken))
    with pytest.raises(sqlite3.OperationalError):
        MonitorStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == original_version
        assert (
            db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='migration_must_rollback'"
            ).fetchone()[0]
            == 0
        )
