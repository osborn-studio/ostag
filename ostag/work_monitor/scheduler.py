"""Clock-driven critical alerts, Beijing digests, and durable delivery."""

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

from .identity import IdentityStore
from .store import MonitorStore

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
    ) -> None:
        self.store, self.identities, self.transports = store, identities, transports
        self.clock = clock or (lambda: datetime.now(UTC))
        self.jitter = jitter or (lambda maximum: random.uniform(0, maximum))
        self.degraded_seconds, self.offline_seconds = degraded_seconds, offline_seconds
        self.morning_hour, self.evening_hour, self.retry_cap = morning_hour, evening_hour, retry_cap
        self.poll_seconds = poll_seconds
        self.owner = uuid.uuid4().hex
        self.ledger = RunLedger(store.path.with_name("run_ledger.jsonl"), clock=self.clock)

    def wake(self) -> None:
        """Compatibility hook: producers only wake; they never drain."""

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

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
        if local.minute < 60:
            window = (
                "morning"
                if local.hour == self.morning_hour
                else "evening"
                if local.hour == self.evening_hour
                else ""
            )
            if window:
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
                    key = f"{local.date()}:{window}:{sub['destination_platform']}:{sub['destination_id']}:{sub['subscription_id']}"
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
        now = self.clock()
        lease_until = (now + timedelta(seconds=30)).isoformat()
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM notification_outbox WHERE state='pending' AND delivered_at IS NULL AND delivery_id<>'' "
                "AND (next_attempt_at='' OR next_attempt_at<=?) AND (lease_until='' OR lease_until<=?) ORDER BY rowid LIMIT 1",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return False
            claimed = db.execute(
                "UPDATE notification_outbox SET lease_owner=?,lease_until=? WHERE notification_id=? AND (lease_until='' OR lease_until<=?)",
                (self.owner, lease_until, row["notification_id"], now.isoformat()),
            ).rowcount
            if not claimed:
                return False
        item = dict(row)
        platform, destination_id, subscription_id = item["destination_key"].split(":", 2)
        # Re-check authorization at delivery time; revocation must win over queued work.
        active = next(
            (s for s in self._subscriptions() if str(s["subscription_id"]) == subscription_id), None
        )
        if active is None or platform not in self.transports:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE notification_outbox SET state='suppressed',lease_owner='',lease_until='' WHERE notification_id=?",
                    (item["notification_id"],),
                )
            return True
        payload = json.loads(item["payload"])
        text = ("延迟通知\n" if item["delayed"] else "") + json.dumps(payload, ensure_ascii=False)
        try:
            await self.transports[platform].send_message(
                destination_id, text, delivery_id=item["delivery_id"], chat_type=active["chat_type"]
            )
        except Exception as exc:
            attempts = int(item["attempts"]) + 1
            status = getattr(exc, "status_code", 500)
            terminal = attempts >= self.retry_cap or not (status == 429 or status >= 500)
            delay = (1, 2, 4, 8, 16)[min(attempts - 1, 4)]
            next_at = now + timedelta(minutes=delay, seconds=self.jitter(delay * 6))
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE notification_outbox SET attempts=?,state=?,next_attempt_at=?,lease_owner='',lease_until='' WHERE notification_id=?",
                    (
                        attempts,
                        "dead_letter" if terminal else "pending",
                        next_at.isoformat(),
                        item["notification_id"],
                    ),
                )
            self.ledger.record_notification(
                item["delivery_id"],
                "deadletter" if terminal else "fail",
                {"error": str(exc), "payload": payload},
            )
            return True
        with self.store.transaction() as db:
            db.execute(
                "UPDATE notification_outbox SET delivered_at=?,state='delivered',lease_owner='',lease_until='' WHERE notification_id=?",
                (now.isoformat(), item["notification_id"]),
            )
            db.execute(
                "UPDATE monitor_subscriptions SET delivery_cursor=delivery_cursor+1 WHERE rowid=?",
                (subscription_id,),
            )
            db.execute(
                "UPDATE notification_outbox SET state='pending',attempts=0,delayed=1,"
                "next_attempt_at=?,lease_owner='',lease_until='' WHERE state='dead_letter' "
                "AND destination_key LIKE ?",
                (now.isoformat(), f"{platform}:%"),
            )
        self.ledger.record_notification(item["delivery_id"], "deliver", {"payload": payload})
        return True

    def replay_dead_letters(self) -> int:
        with self.store.transaction() as db:
            result = db.execute(
                "UPDATE notification_outbox SET state='pending',attempts=0,delayed=1,next_attempt_at=?,lease_owner='',lease_until='' WHERE state='dead_letter'",
                (self.clock().isoformat(),),
            )
        return result.rowcount
