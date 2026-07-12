"""Clock-driven critical alerts, Beijing digests, and durable delivery.

Digest catch-up is deliberately bounded to today's due 09:00/19:00 Beijing
windows (at most two). A 10:00 start catches 09:00; a 20:00 start catches both,
with persistent per-target window keys preventing duplicates across restarts.
Older days are never replayed, avoiding restart notification storms.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ostag.core.run_ledger import RunLedger
from ostag.core.identity import Principal

from .identity import Destination, IdentityStore
from .store import MonitorStore
from .transport import TransportError

BEIJING = ZoneInfo("Asia/Shanghai")
CRITICAL = {"blocked", "failed", "approval_required"}


class WorkMonitorScheduler:
    """The sole lifespan-owned scheduler and notification outbox worker."""

    def __init__(
        self,
        store: MonitorStore,
        identities: IdentityStore,
        transports: dict[str, Any],
        *,
        clock: Callable[[], datetime] | None = None,
        jitter: Callable[[float], float] | None = None,
        degraded_seconds: int = 120,
        offline_seconds: int = 600,
        morning_hour: int = 9,
        evening_hour: int = 19,
        retry_cap: int = 5,
        poll_seconds: float = 5,
        lease_seconds: float = 30,
        recovery_limit: int = 10,
        recovery_cooldown_seconds: int = 60,
    ) -> None:
        self.store, self.identities, self.transports = store, identities, transports
        self.clock = clock or (lambda: datetime.now(UTC))
        self.jitter = jitter or (lambda maximum: random.uniform(0, maximum))
        self.degraded_seconds, self.offline_seconds = degraded_seconds, offline_seconds
        self.morning_hour, self.evening_hour, self.retry_cap = morning_hour, evening_hour, retry_cap
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.recovery_limit = recovery_limit
        self.recovery_cooldown_seconds = recovery_cooldown_seconds
        self.owner = uuid.uuid4().hex
        self.ledger = RunLedger(store.path.with_name("run_ledger.jsonl"), clock=self.clock)

    def wake(self) -> None:
        """Compatibility hook: producers only wake; they never drain."""

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        try:
            while not stop.is_set():
                await self.tick()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
        finally:
            with self.store.transaction() as db:
                db.execute("DELETE FROM monitor_scheduler_owners WHERE owner_id=?", (self.owner,))

    async def run(self, stop: asyncio.Event) -> None:
        await self.run_forever(stop)

    def _subscriptions(self) -> list[dict[str, Any]]:
        with self.identities._connect() as db:
            rows = db.execute(
                "SELECT s.rowid subscription_id,s.*,b.chat_type,b.scope FROM monitor_subscriptions s "
                "JOIN monitor_bindings b ON b.platform=s.destination_platform AND b.destination_id=s.destination_id "
                "WHERE s.revoked=0 AND b.revoked=0"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _target_matches(
        subscription: dict[str, Any], instance_id: str, task_id: str = "", project: str = ""
    ) -> bool:
        scope = subscription.get("scope", "")
        if scope.startswith("instance:") and scope != f"instance:{instance_id}":
            return False
        if scope.startswith("task:") and scope != f"task:{task_id}":
            return False
        if scope.startswith("project:") and scope != f"project:{project}":
            return False
        return (not subscription["instance_id"] or subscription["instance_id"] == instance_id) and (
            not subscription["task_id"] or subscription["task_id"] == task_id
        )

    def _enqueue(
        self, subscription: dict[str, Any], event_id: str, payload: dict[str, Any]
    ) -> None:
        destination_key = f"{subscription['destination_platform']}:{subscription['destination_id']}:{subscription['subscription_id']}"
        delivery_id = hashlib.sha256(f"{event_id}:{destination_key}".encode()).hexdigest()[:32]
        with self.store.transaction() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO notification_outbox(notification_id,event_id,destination_key,payload,delivery_id,next_attempt_at) VALUES (?,?,?,?,?,?)",
                (
                    f"notification_{delivery_id}",
                    event_id,
                    destination_key,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    delivery_id,
                    self.clock().isoformat(),
                ),
            ).rowcount
        if inserted:
            self.ledger.record_notification(delivery_id, "enqueue", {"payload": payload})

    async def tick(self, *, drain: bool = True) -> None:
        now = self.clock()
        owner_lease = now + timedelta(seconds=max(30.0, self.poll_seconds * 3))
        with self.store.transaction() as db:
            db.execute("DELETE FROM monitor_scheduler_owners WHERE lease_until<=?", (now.isoformat(),))
            db.execute(
                "INSERT INTO monitor_scheduler_owners(owner_id,lease_until,heartbeat_at) "
                "VALUES (?,?,?) ON CONFLICT(owner_id) DO UPDATE SET "
                "lease_until=excluded.lease_until,heartbeat_at=excluded.heartbeat_at",
                (self.owner, owner_lease.isoformat(), now.isoformat()),
            )
        subscriptions = self._subscriptions()
        with self.store.connect() as db:
            tasks = [dict(r) for r in db.execute("SELECT * FROM tasks")]
            instances = [dict(r) for r in db.execute("SELECT * FROM instances")]
        for task in tasks:
            if task["status"] not in CRITICAL:
                continue
            event_id = f"task:{task['instance_id']}:{task['task_id']}:{task['status']}:{task['occurred_at']}"
            for sub in subscriptions:
                if self._target_matches(sub, task["instance_id"], task["task_id"], task["project"]):
                    self._enqueue(
                        sub,
                        event_id,
                        {
                            "kind": f"task.{task['status']}",
                            "task_id": task["task_id"],
                            "title": task["title"],
                        },
                    )
        for instance in instances:
            if not instance["last_seen_at"]:
                continue
            age = (now - datetime.fromisoformat(instance["last_seen_at"])).total_seconds()
            health = (
                "offline"
                if age >= self.offline_seconds
                else "degraded"
                if age >= self.degraded_seconds
                else ""
            )
            if health:
                event_id = f"health:{instance['instance_id']}:{health}:{instance['last_seen_at']}"
                for sub in subscriptions:
                    if self._target_matches(sub, instance["instance_id"]):
                        self._enqueue(
                            sub,
                            event_id,
                            {"kind": f"instance.{health}", "instance_id": instance["instance_id"]},
                        )
        local = now.astimezone(BEIJING)
        # Bounded catch-up: only today's due windows, at most morning+evening.
        # This catches a 10:00/20:00 restart without creating a multi-day storm.
        due: list[tuple[datetime, str]] = []
        for hour, name in ((self.morning_hour, "morning"), (self.evening_hour, "evening")):
            candidate = datetime.combine(local.date(), datetime.min.time(), BEIJING).replace(
                hour=hour
            )
            if candidate <= local:
                due.append((candidate, name))
        for window_at, window in sorted(due)[-2:]:
            for sub in subscriptions:
                scoped = [
                    task
                    for task in tasks
                    if self._target_matches(
                        sub, task["instance_id"], task["task_id"], task["project"]
                    )
                ]
                statuses = (
                    ["running", "blocked", "approval_required", "queued"]
                    if window == "morning"
                    else ["completed", "failed", "blocked", "next_attention"]
                )
                sections = {
                    status: [
                        {"task_id": task["task_id"], "title": task["title"]}
                        for task in scoped
                        if (
                            task["status"] == status
                            or (
                                status == "next_attention"
                                and task["status"] in {"approval_required", "queued", "running"}
                            )
                        )
                    ]
                    for status in statuses
                }
                key = f"{window_at.date()}:{window}:{sub['destination_platform']}:{sub['destination_id']}:{sub['subscription_id']}"
                with self.store.transaction() as db:
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO monitor_schedule_windows VALUES (?,?)",
                        (key, now.isoformat()),
                    ).rowcount
                if inserted:
                    self._enqueue(
                        sub, f"digest:{key}", {"kind": f"digest.{window}", "sections": sections}
                    )
        if drain:
            while await self.drain_once():
                pass

    async def drain_once(self) -> bool:
        self.reconcile_audits()
        legacy = self._suppress_and_audit_legacy()
        now = self.clock()
        lease_until = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM notification_outbox WHERE state='pending' AND delivered_at IS NULL AND delivery_id<>'' "
                "AND (next_attempt_at='' OR next_attempt_at<=?) AND (lease_until='' OR lease_until<=?) ORDER BY rowid LIMIT 1",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return bool(legacy)
            generation = int(row["lease_generation"]) + 1
            claimed = db.execute(
                "UPDATE notification_outbox SET lease_owner=?,lease_until=?,lease_generation=? WHERE notification_id=? AND lease_generation=? AND (lease_until='' OR lease_until<=?)",
                (
                    self.owner,
                    lease_until,
                    generation,
                    row["notification_id"],
                    row["lease_generation"],
                    now.isoformat(),
                ),
            ).rowcount
            if not claimed:
                return False
        item = dict(row)
        platform, destination_id, subscription_id = item["destination_key"].split(":", 2)
        payload = json.loads(item["payload"])
        # Re-check authorization at delivery time; revocation must win over queued work.
        if subscription_id == "binding":
            try:
                destination = Destination(platform, destination_id, str(payload["chat_type"]))
                recipient = Principal.from_dict(payload["recipient"])
                active = (
                    {"chat_type": destination.chat_type}
                    if self.identities.binding(destination) is not None
                    and self.identities.allows(recipient, destination, "write")
                    else None
                )
            except (KeyError, ValueError):
                active = None
        else:
            active = next(
                (s for s in self._subscriptions() if str(s["subscription_id"]) == subscription_id),
                None,
            )
        if active is None or platform not in self.transports:
            with self.store.transaction() as db:
                updated = db.execute(
                    "UPDATE notification_outbox SET state='suppressed',lease_owner='',lease_until='' WHERE notification_id=? AND lease_owner=? AND lease_generation=?",
                    (item["notification_id"], self.owner, generation),
                )
            return bool(updated)
        text = ("延迟通知\n" if item["delayed"] else "") + json.dumps(payload, ensure_ascii=False)
        renew_stop = asyncio.Event()
        renewer = asyncio.create_task(
            self._renew_lease(item["notification_id"], generation, renew_stop)
        )
        try:
            await asyncio.wait_for(
                self.transports[platform].send_message(
                    destination_id,
                    text,
                    delivery_id=item["delivery_id"],
                    chat_type=active["chat_type"],
                ),
                timeout=self.lease_seconds * 0.8,
            )
        except Exception as exc:
            renew_stop.set()
            await renewer
            attempts = int(item["attempts"]) + 1
            raw_status = getattr(exc, "status_code", 0)
            error = (
                exc
                if isinstance(exc, TransportError)
                else TransportError(
                    str(exc),
                    status_code=raw_status,
                    transient=isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                    or raw_status == 429
                    or raw_status >= 500,
                )
            )
            status = error.status_code
            transient = error.transient or status == 429 or status >= 500
            terminal = attempts >= self.retry_cap or not transient
            delay = (1, 2, 4, 8, 16)[min(attempts - 1, 4)]
            wait_seconds = delay * 60 + self.jitter(delay * 6)
            if status == 429 and error.retry_after_seconds is not None:
                wait_seconds = max(wait_seconds, error.retry_after_seconds)
            next_at = now + timedelta(seconds=wait_seconds)
            with self.store.transaction() as db:
                updated = db.execute(
                    "UPDATE notification_outbox SET attempts=?,state=?,next_attempt_at=?,lease_owner='',lease_until='',failure_transient=?,deadlettered_at=? WHERE notification_id=? AND lease_owner=? AND lease_generation=?",
                    (
                        attempts,
                        "dead_letter" if terminal else "pending",
                        next_at.isoformat(),
                        int(transient),
                        now.isoformat() if terminal else "",
                        item["notification_id"],
                        self.owner,
                        generation,
                    ),
                ).rowcount
            if not updated:
                return False
            self.ledger.record_notification(
                item["delivery_id"],
                "deadletter" if terminal else "fail",
                {"error": str(exc), "payload": payload},
            )
            return True
        renew_stop.set()
        await renewer
        with self.store.transaction() as db:
            updated = db.execute(
                "UPDATE notification_outbox SET delivered_at=?,state='delivered',lease_owner='',lease_until='',audit_pending=1 WHERE notification_id=? AND lease_owner=? AND lease_generation=?",
                (self.clock().isoformat(), item["notification_id"], self.owner, generation),
            ).rowcount
            if not updated:
                return False
            if subscription_id != "binding":
                db.execute(
                    "UPDATE monitor_subscriptions SET delivery_cursor=delivery_cursor+1 WHERE rowid=?",
                    (subscription_id,),
                )
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT notification_id FROM notification_outbox WHERE state='dead_letter' AND failure_transient=1 "
                    "AND destination_key=? AND deadlettered_at<=? ORDER BY deadlettered_at LIMIT ?",
                    (
                        item["destination_key"],
                        (
                            self.clock() - timedelta(seconds=self.recovery_cooldown_seconds)
                        ).isoformat(),
                        self.recovery_limit,
                    ),
                )
            ]
            db.executemany(
                "UPDATE notification_outbox SET state='pending',attempts=0,delayed=1,next_attempt_at=?,deadlettered_at='' WHERE notification_id=?",
                [(self.clock().isoformat(), value) for value in ids],
            )
        self.reconcile_audits()
        return True

    async def _renew_lease(
        self, notification_id: str, generation: int, stop: asyncio.Event
    ) -> None:
        interval = max(0.01, self.lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break
            except TimeoutError:
                with self.store.transaction() as db:
                    updated = db.execute(
                        "UPDATE notification_outbox SET lease_until=? WHERE notification_id=? AND lease_owner=? AND lease_generation=? AND state='pending'",
                        (
                            (self.clock() + timedelta(seconds=self.lease_seconds)).isoformat(),
                            notification_id,
                            self.owner,
                            generation,
                        ),
                    ).rowcount
                if not updated:
                    break

    def reconcile_audits(self, *, limit: int = 100) -> int:
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT notification_id,delivery_id,payload FROM notification_outbox WHERE audit_pending=1 AND ledger_synced=0 ORDER BY rowid LIMIT ?",
                (limit,),
            ).fetchall()
        synced = 0
        for row in rows:
            try:
                self.ledger.record_notification(
                    row["delivery_id"], "deliver", {"payload": json.loads(row["payload"])}
                )
            except Exception:  # noqa: BLE001 - durable row remains pending for reconciliation
                continue
            with self.store.transaction() as db:
                synced += db.execute(
                    "UPDATE notification_outbox SET audit_pending=0,ledger_synced=1 WHERE notification_id=? AND state='delivered'",
                    (row["notification_id"],),
                ).rowcount
        return synced

    def _suppress_and_audit_legacy(self) -> int:
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO legacy_outbox_audit(notification_id) "
                "SELECT notification_id FROM notification_outbox WHERE delivery_id=''"
            )
            db.execute(
                "UPDATE notification_outbox SET state='suppressed' "
                "WHERE delivery_id='' AND state='pending'"
            )
            rows = db.execute(
                "SELECT a.notification_id,o.event_id,o.destination_key,o.payload "
                "FROM legacy_outbox_audit a JOIN notification_outbox o USING(notification_id) "
                "WHERE a.audited=0"
            ).fetchall()
        for row in rows:
            self.ledger.record_notification(
                str(row["notification_id"]),
                "suppress",
                {
                    "reason": "legacy notification lacks delivery_id",
                    "event_id": row["event_id"],
                    "destination_key": row["destination_key"],
                    "payload": json.loads(str(row["payload"])),
                },
            )
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE legacy_outbox_audit SET audited=1 WHERE notification_id=?",
                    (row["notification_id"],),
                )
        return len(rows)

    def replay_dead_letters(self, *, destination_key: str, limit: int) -> int:
        if not destination_key or limit <= 0:
            raise ValueError("destination_key and positive limit are required")
        with self.store.transaction() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT notification_id FROM notification_outbox WHERE state='dead_letter' AND failure_transient=1 AND destination_key=? ORDER BY deadlettered_at LIMIT ?",
                    (destination_key, limit),
                )
            ]
            db.executemany(
                "UPDATE notification_outbox SET state='pending',attempts=0,delayed=1,next_attempt_at=?,deadlettered_at='' WHERE notification_id=?",
                [(self.clock().isoformat(), value) for value in ids],
            )
        return len(ids)
