"""Relay notification planning inside the existing Work Monitor scheduler."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ostag.core.identity import Principal
from ostag.work_monitor.identity import IdentityStore
from ostag.work_monitor.store import MonitorStore

from .models import RelayEnvelope
from .store import RelayStore


class CopilotRelayScheduler:
    """Plans relay cards; WorkMonitorScheduler remains the sole delivery owner."""

    def __init__(
        self,
        store: MonitorStore,
        identities: IdentityStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.identities = identities
        self.clock = clock
        self.relay = RelayStore(store)

    def _owners(self) -> tuple[tuple[Principal, str, str, str], ...]:
        with self.identities._connect() as db:
            rows = db.execute(
                "SELECT platform,destination_id,chat_type,owner_platform,owner_actor_id "
                "FROM monitor_bindings WHERE revoked=0 AND chat_type='private' "
                "AND role IN ('owner','admin') ORDER BY platform,destination_id"
            ).fetchall()
        return tuple(
            (
                Principal(str(row["owner_platform"]), str(row["owner_actor_id"])),
                str(row["platform"]),
                str(row["destination_id"]),
                str(row["chat_type"]),
            )
            for row in rows
        )

    def _recipients(
        self, instance_id: str
    ) -> tuple[tuple[Principal, str, str, str], ...]:
        with self.store.connect() as db:
            owner = db.execute(
                "SELECT principal_platform,actor_id FROM monitor_devices "
                "WHERE device_id=? AND revoked_at=''",
                (instance_id,),
            ).fetchone()
            if owner is None:
                owner = db.execute(
                    "SELECT principal_platform,actor_id FROM monitor_instance_owners "
                    "WHERE instance_id=? AND revoked=0",
                    (instance_id,),
                ).fetchone()
            if owner is None:
                return self._owners()
            rows = db.execute(
                "SELECT platform,destination_id,chat_type FROM monitor_bindings "
                "WHERE owner_platform=? AND owner_actor_id=? AND revoked=0 "
                "AND chat_type='private' ORDER BY destination_id",
                (owner["principal_platform"], owner["actor_id"]),
            ).fetchall()
        principal = Principal(str(owner["principal_platform"]), str(owner["actor_id"]))
        return tuple(
            (
                principal,
                str(row["platform"]),
                str(row["destination_id"]),
                str(row["chat_type"]),
            )
            for row in rows
        )

    def _enqueue(
        self,
        *,
        event_id: str,
        relay_message_id: str,
        kind: str,
        principal: Principal,
        platform: str,
        destination_id: str,
        chat_type: str,
        coalesce: bool = False,
    ) -> int:
        payload = json.dumps(
            {
                "kind": kind,
                "relay_message_id": relay_message_id,
                "recipient": principal.to_dict(),
                "chat_type": chat_type,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        destination_key = f"{platform}:{destination_id}:binding"
        delivery_id = f"relay:{event_id}:{destination_id}"
        now = self.clock().isoformat()
        with self.store.transaction() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO notification_outbox(notification_id,event_id,"
                "destination_key,payload,delivery_id,next_attempt_at) VALUES (?,?,?,?,?,?)",
                (f"relay_{event_id}_{destination_id}", event_id, destination_key, payload, delivery_id, now),
            ).rowcount
            if inserted or not coalesce:
                return inserted
            row = db.execute(
                "SELECT notification_id,payload,state FROM notification_outbox "
                "WHERE event_id=? AND destination_key=?",
                (event_id, destination_key),
            ).fetchone()
            if row is None or row["state"] != "pending" or row["payload"] == payload:
                return 0
            db.execute(
                "UPDATE notification_outbox SET payload=?,delivery_id=?,next_attempt_at=? "
                "WHERE notification_id=? AND state='pending'",
                (payload, delivery_id, now, row["notification_id"]),
            )
            return 1

    def _expire_decisions(self) -> int:
        now = self.clock().astimezone(UTC)
        now_text = now.isoformat().replace("+00:00", "Z")
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT d.decision_id,m.payload_json FROM relay_decisions d "
                "JOIN relay_messages m ON m.kind='decision.request' "
                "AND json_extract(m.payload_json,'$.payload.decision_id')=d.decision_id "
                "WHERE d.state='pending' AND d.expires_at<=?",
                (now_text,),
            ).fetchall()
            ids = [str(row["decision_id"]) for row in rows]
            db.executemany(
                "UPDATE relay_decisions SET state='timed_out',answered_at=? "
                "WHERE decision_id=? AND state='pending'",
                [(now_text, decision_id) for decision_id in ids],
            )
        for row in rows:
            source = RelayEnvelope.from_dict(json.loads(row["payload_json"]))
            decision_id = str(row["decision_id"])
            timeout = RelayEnvelope.from_dict(
                {
                    "schema_version": 1,
                    "message_id": f"timeout_{decision_id}",
                    "binding": source.binding.to_dict(),
                    "sequence": 1,
                    "created_at": now_text,
                    "expires_at": (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
                    "sender": Principal("feishu", "ostag-system").to_dict(),
                    "kind": "delivery.receipt",
                    "payload": {
                        "related_message_id": source.message_id,
                        "state": "expired",
                        "summary": "decision timed out without an answer",
                        "error_code": "decision_timeout",
                    },
                    "correlation_id": source.message_id,
                    "causation_id": source.message_id,
                }
            )
            self.relay.enqueue(timeout, direction="outbound")
        return len(rows)

    def _record_connection_transitions(self) -> tuple[str, ...]:
        now = self.clock().astimezone(UTC)
        with self.store.transaction() as db:
            devices = db.execute(
                "SELECT device_id,last_seen_at FROM monitor_devices WHERE revoked_at=''"
            ).fetchall()
            reconnected: list[str] = []
            for device in devices:
                device_id = str(device["device_id"])
                last_seen = str(device["last_seen_at"] or "")
                if not last_seen:
                    current = "offline"
                else:
                    seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                    age = max(0.0, (now - seen).total_seconds())
                    current = "online" if age < 30 else "degraded" if age < 120 else "offline"
                previous = db.execute(
                    "SELECT connection_state FROM relay_device_notification_state "
                    "WHERE device_id=?",
                    (device_id,),
                ).fetchone()
                if previous is not None and previous["connection_state"] != "online" \
                        and current == "online":
                    reconnected.append(device_id)
                db.execute(
                    "INSERT INTO relay_device_notification_state(device_id,connection_state,last_seen_at,updated_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
                    "connection_state=excluded.connection_state,last_seen_at=excluded.last_seen_at,"
                    "updated_at=excluded.updated_at",
                    (device_id, current, last_seen, now.isoformat()),
                )
        return tuple(reconnected)

    def plan_once(self) -> int:
        changed = self._expire_decisions()
        reconnected = self._record_connection_transitions()
        with self.store.connect() as db:
            decisions = db.execute(
                "SELECT m.payload_json FROM relay_messages m JOIN relay_decisions d "
                "ON json_extract(m.payload_json,'$.payload.decision_id')=d.decision_id "
                "WHERE m.direction='inbound' AND m.kind='decision.request' AND d.state='pending' "
                "ORDER BY m.created_at,m.message_id"
            ).fetchall()
            stages = db.execute(
                "SELECT payload_json FROM relay_messages WHERE direction='inbound' "
                "AND kind='session.upsert' ORDER BY created_at,message_id"
            ).fetchall()
            snapshots = db.execute(
                "SELECT payload_json FROM relay_messages WHERE direction='inbound' "
                "AND kind='assistant.snapshot' "
                "AND json_extract(payload_json,'$.payload.assistant_text')<>'' "
                "ORDER BY created_at,message_id"
            ).fetchall()
            receipts = db.execute(
                "SELECT payload_json FROM relay_messages WHERE direction='inbound' "
                "AND kind='delivery.receipt' ORDER BY created_at,message_id"
            ).fetchall()
        for row in decisions:
            envelope = RelayEnvelope.from_dict(json.loads(row["payload_json"]))
            for principal, platform, destination_id, chat_type in self._recipients(
                envelope.binding.instance_id
            ):
                changed += self._enqueue(
                    event_id=f"relay-decision:{envelope.message_id}",
                    relay_message_id=envelope.message_id,
                    kind="relay.decision",
                    principal=principal,
                    platform=platform,
                    destination_id=destination_id,
                    chat_type=chat_type,
                )
        for row in stages:
            envelope = RelayEnvelope.from_dict(json.loads(row["payload_json"]))
            state = str(envelope.payload["state"])
            if state not in {"completed", "failed"}:
                continue
            for principal, platform, destination_id, chat_type in self._recipients(
                envelope.binding.instance_id
            ):
                changed += self._enqueue(
                    event_id=(
                        f"relay-stage:{envelope.binding.instance_id}:"
                        f"{envelope.binding.session_id}:{state}"
                    ),
                    relay_message_id=envelope.message_id,
                    kind="relay.stage",
                    principal=principal,
                    platform=platform,
                    destination_id=destination_id,
                    chat_type=chat_type,
                    coalesce=True,
                )
        for row in snapshots:
            envelope = RelayEnvelope.from_dict(json.loads(row["payload_json"]))
            for principal, platform, destination_id, chat_type in self._recipients(
                envelope.binding.instance_id
            ):
                changed += self._enqueue(
                    event_id=f"relay-snapshot:{envelope.message_id}",
                    relay_message_id=envelope.message_id,
                    kind="relay.snapshot",
                    principal=principal,
                    platform=platform,
                    destination_id=destination_id,
                    chat_type=chat_type,
                )
        for row in receipts:
            envelope = RelayEnvelope.from_dict(json.loads(row["payload_json"]))
            for principal, platform, destination_id, chat_type in self._recipients(
                envelope.binding.instance_id
            ):
                changed += self._enqueue(
                    event_id=f"relay-receipt:{envelope.message_id}",
                    relay_message_id=envelope.message_id,
                    kind="relay.receipt",
                    principal=principal,
                    platform=platform,
                    destination_id=destination_id,
                    chat_type=chat_type,
                )
        for device_id in reconnected:
            for row in decisions:
                envelope = RelayEnvelope.from_dict(json.loads(row["payload_json"]))
                if envelope.binding.instance_id != device_id:
                    continue
                for principal, platform, destination_id, chat_type in self._recipients(
                    device_id
                ):
                    changed += self._enqueue(
                        event_id=f"relay-reconnect:{device_id}:{envelope.message_id}",
                        relay_message_id=envelope.message_id,
                        kind="relay.decision",
                        principal=principal,
                        platform=platform,
                        destination_id=destination_id,
                        chat_type=chat_type,
                    )
        return changed
