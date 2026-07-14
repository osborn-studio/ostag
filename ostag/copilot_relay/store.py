"""Transactional central registry and mailbox for Copilot Relay."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass

from ostag.work_monitor.store import MonitorStore

from .models import RelayEnvelope, SessionBinding


class StaleSessionError(ValueError):
    """Raised when an older session projection attempts to replace newer truth."""


class MessageConflictError(ValueError):
    """Raised when a stable message ID is reused for different content."""


@dataclass(frozen=True)
class RelaySession:
    instance_id: str
    session_id: str
    task_id: str
    run_id: str
    agent_id: str
    title: str
    state: str
    session_version: int
    last_turn_id: str
    latest_summary: str
    updated_at: str


@dataclass(frozen=True)
class LeasedMessage:
    message_id: str
    direction: str
    kind: str
    binding: SessionBinding
    attempts: int
    envelope: RelayEnvelope


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    return value


def _canonical(value: object) -> str:
    return json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


class RelayStore:
    """Uses the existing MonitorStore as the sole schema and connection owner."""

    def __init__(self, monitor_store: MonitorStore) -> None:
        self.monitor_store = monitor_store

    def enqueue(self, envelope: RelayEnvelope, *, direction: str) -> bool:
        if direction not in {"inbound", "outbound"}:
            raise ValueError("direction must be inbound or outbound")
        binding_json = _canonical(envelope.binding.to_dict())
        payload_json = _canonical(envelope.to_dict())
        with self.monitor_store.transaction() as connection:
            result = connection.execute(
                "INSERT OR IGNORE INTO relay_messages("
                "message_id,direction,kind,binding_json,payload_json,state,next_attempt_at,created_at"
                ") VALUES (?,?,?,?,?,'pending',?,?)",
                (
                    envelope.message_id,
                    direction,
                    envelope.kind,
                    binding_json,
                    payload_json,
                    envelope.created_at,
                    envelope.created_at,
                ),
            )
            if result.rowcount == 1:
                return True
            existing = connection.execute(
                "SELECT direction,kind,binding_json,payload_json FROM relay_messages "
                "WHERE message_id=?",
                (envelope.message_id,),
            ).fetchone()
            if existing is None or (
                existing["direction"] != direction
                or existing["kind"] != envelope.kind
                or existing["binding_json"] != binding_json
                or existing["payload_json"] != payload_json
            ):
                raise MessageConflictError(f"message_id collision: {envelope.message_id}")
        return False

    def message_count(self) -> int:
        with self.monitor_store.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM relay_messages").fetchone()
        return int(row["count"])

    def instance_cursor(self, instance_id: str) -> int:
        with self.monitor_store.connect() as connection:
            row = connection.execute(
                "SELECT last_sequence FROM relay_instance_cursors WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
        return 0 if row is None else int(row["last_sequence"])

    def advance_instance_cursor(
        self, instance_id: str, expected: int, new_value: int, *, updated_at: str
    ) -> bool:
        with self.monitor_store.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO relay_instance_cursors(instance_id,last_sequence,updated_at) "
                "VALUES (?,0,?)",
                (instance_id, updated_at),
            )
            result = connection.execute(
                "UPDATE relay_instance_cursors SET last_sequence=?,updated_at=? "
                "WHERE instance_id=? AND last_sequence=?",
                (new_value, updated_at, instance_id, expected),
            )
        return result.rowcount == 1

    def binding_for_message(self, message_id: str) -> SessionBinding | None:
        with self.monitor_store.connect() as connection:
            row = connection.execute(
                "SELECT binding_json FROM relay_messages WHERE message_id=?", (message_id,)
            ).fetchone()
        return None if row is None else SessionBinding.from_dict(json.loads(row["binding_json"]))

    def envelope_for_message(self, message_id: str) -> RelayEnvelope | None:
        with self.monitor_store.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM relay_messages WHERE message_id=?", (message_id,)
            ).fetchone()
        return None if row is None else RelayEnvelope.from_dict(json.loads(row["payload_json"]))

    def claim_messages(
        self,
        *,
        direction: str,
        owner: str,
        now: str,
        lease_until: str,
        limit: int = 100,
        instance_id: str | None = None,
    ) -> tuple[LeasedMessage, ...]:
        if direction not in {"inbound", "outbound"}:
            raise ValueError("direction must be inbound or outbound")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self.monitor_store.transaction() as connection:
            instance_clause = (
                " AND json_extract(binding_json,'$.instance_id')=?" if instance_id else ""
            )
            parameters: tuple[object, ...] = (direction, now, now)
            if instance_id:
                parameters += (instance_id,)
            parameters += (limit,)
            rows = connection.execute(
                "SELECT message_id FROM relay_messages WHERE direction=? "
                "AND state IN ('pending','leased') AND next_attempt_at<=? "
                "AND (state='pending' OR lease_until<=?)"
                + instance_clause
                + " ORDER BY created_at,message_id LIMIT ?",
                parameters,
            ).fetchall()
            ids = [row["message_id"] for row in rows]
            for message_id in ids:
                connection.execute(
                    "UPDATE relay_messages SET state='leased',attempts=attempts+1,"
                    "lease_owner=?,lease_until=? WHERE message_id=?",
                    (owner, lease_until, message_id),
                )
            leased = [
                connection.execute(
                    "SELECT * FROM relay_messages WHERE message_id=?", (message_id,)
                ).fetchone()
                for message_id in ids
            ]
        return tuple(
            LeasedMessage(
                message_id=row["message_id"],
                direction=row["direction"],
                kind=row["kind"],
                binding=SessionBinding.from_dict(json.loads(row["binding_json"])),
                attempts=int(row["attempts"]),
                envelope=RelayEnvelope.from_dict(json.loads(row["payload_json"])),
            )
            for row in leased
        )

    def mark_delivered(self, message_id: str, *, owner: str, delivered_at: str) -> bool:
        with self.monitor_store.transaction() as connection:
            result = connection.execute(
                "UPDATE relay_messages SET state='delivered',delivered_at=?,lease_owner='',"
                "lease_until='' WHERE message_id=? AND state='leased' AND lease_owner=?",
                (delivered_at, message_id, owner),
            )
        return result.rowcount == 1

    def upsert_session(self, envelope: RelayEnvelope) -> bool:
        if envelope.kind != "session.upsert":
            raise ValueError("session upsert requires a session.upsert envelope")
        binding = envelope.binding
        payload = envelope.payload
        values = (
            binding.task_id,
            binding.run_id,
            payload["agent_id"],
            payload["title"],
            payload["state"],
            binding.session_version,
            binding.turn_id,
            payload["latest_summary"],
            envelope.created_at,
        )
        with self.monitor_store.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM relay_sessions WHERE instance_id=? AND session_id=?",
                (binding.instance_id, binding.session_id),
            ).fetchone()
            if current is None:
                connection.execute(
                    "INSERT INTO relay_sessions(instance_id,session_id,task_id,run_id,agent_id,"
                    "title,state,session_version,last_turn_id,latest_summary,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (binding.instance_id, binding.session_id, *values),
                )
                return True
            current_version = int(current["session_version"])
            if binding.session_version < current_version:
                raise StaleSessionError(
                    f"session version {binding.session_version} is older than {current_version}"
                )
            if binding.session_version == current_version:
                projected = {
                    "task_id": binding.task_id,
                    "run_id": binding.run_id,
                    "agent_id": payload["agent_id"],
                    "title": payload["title"],
                    "state": payload["state"],
                    "last_turn_id": binding.turn_id,
                    "latest_summary": payload["latest_summary"],
                }
                if any(current[key] != value for key, value in projected.items()):
                    raise StaleSessionError("equal session version contains conflicting state")
                return False
            result = connection.execute(
                "UPDATE relay_sessions SET task_id=?,run_id=?,agent_id=?,title=?,state=?,"
                "session_version=?,last_turn_id=?,latest_summary=?,updated_at=? "
                "WHERE instance_id=? AND session_id=? AND session_version=?",
                (*values, binding.instance_id, binding.session_id, current_version),
            )
            if result.rowcount != 1:
                raise StaleSessionError("session changed concurrently")
        return True

    def get_session(self, instance_id: str, session_id: str) -> RelaySession | None:
        with self.monitor_store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM relay_sessions WHERE instance_id=? AND session_id=?",
                (instance_id, session_id),
            ).fetchone()
        return None if row is None else RelaySession(**dict(row))

    def list_sessions(self, instance_ids: frozenset[str] | None = None) -> tuple[RelaySession, ...]:
        with self.monitor_store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM relay_sessions ORDER BY updated_at DESC,instance_id,session_id"
            ).fetchall()
        sessions = tuple(RelaySession(**dict(row)) for row in rows)
        return sessions if instance_ids is None else tuple(
            session for session in sessions if session.instance_id in instance_ids
        )

    def latest_snapshot(self, instance_id: str, session_id: str) -> RelayEnvelope | None:
        with self.monitor_store.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM relay_messages WHERE direction='inbound' "
                "AND kind='assistant.snapshot' ORDER BY created_at DESC,rowid DESC"
            ).fetchall()
        for row in rows:
            envelope = RelayEnvelope.from_dict(json.loads(row["payload_json"]))
            if (
                envelope.binding.instance_id == instance_id
                and envelope.binding.session_id == session_id
                and envelope.payload["assistant_text"] != ""
            ):
                return envelope
        return None

    def bind_platform_message(
        self,
        *,
        platform: str,
        platform_message_id: str,
        relay_message_id: str,
        destination_id: str,
        actor_id: str,
        created_at: str,
    ) -> bool:
        if self.envelope_for_message(relay_message_id) is None:
            raise ValueError("cannot bind an unknown relay message")
        with self.monitor_store.transaction() as connection:
            result = connection.execute(
                "INSERT OR IGNORE INTO relay_platform_messages(platform,platform_message_id,"
                "relay_message_id,destination_id,actor_id,created_at) VALUES (?,?,?,?,?,?)",
                (platform, platform_message_id, relay_message_id, destination_id, actor_id, created_at),
            )
            if result.rowcount == 1:
                return True
            row = connection.execute(
                "SELECT relay_message_id,destination_id,actor_id FROM relay_platform_messages "
                "WHERE platform=? AND platform_message_id=?",
                (platform, platform_message_id),
            ).fetchone()
            if row is None or tuple(row) != (relay_message_id, destination_id, actor_id):
                raise MessageConflictError("platform message binding collision")
        return False

    def relay_message_for_platform(
        self, platform: str, platform_message_id: str, *, destination_id: str
    ) -> str | None:
        with self.monitor_store.connect() as connection:
            row = connection.execute(
                "SELECT relay_message_id FROM relay_platform_messages WHERE platform=? "
                "AND platform_message_id=? AND destination_id=?",
                (platform, platform_message_id, destination_id),
            ).fetchone()
        return None if row is None else str(row["relay_message_id"])

    def create_callback(
        self,
        callback_id: str,
        *,
        action: str,
        reference_id: str,
        platform: str,
        actor_id: str,
        destination_id: str,
        chat_type: str,
        expires_at: int,
    ) -> None:
        with self.monitor_store.transaction() as connection:
            connection.execute(
                "INSERT INTO relay_callbacks(callback_id,action,reference_id,platform,actor_id,"
                "destination_id,chat_type,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    callback_id, action, reference_id, platform, actor_id,
                    destination_id, chat_type, expires_at,
                ),
            )

    def consume_callback(
        self,
        callback_id: str,
        *,
        platform: str,
        actor_id: str,
        destination_id: str,
        chat_type: str,
        now: int,
    ) -> dict[str, str] | None:
        with self.monitor_store.transaction() as connection:
            row = connection.execute(
                "SELECT action,reference_id,platform,actor_id,destination_id,chat_type,"
                "expires_at,consumed_at FROM relay_callbacks WHERE callback_id=?",
                (callback_id,),
            ).fetchone()
            if row is None:
                return None
            if (
                row["platform"] != platform
                or row["actor_id"] != actor_id
                or row["destination_id"] != destination_id
                or row["chat_type"] != chat_type
            ):
                return {"error": "identity_mismatch"}
            if row["consumed_at"] is not None or int(row["expires_at"]) < now:
                return {"error": "expired"}
            result = connection.execute(
                "UPDATE relay_callbacks SET consumed_at=? WHERE callback_id=? AND consumed_at IS NULL",
                (now, callback_id),
            )
            if result.rowcount != 1:
                return None
        return {"action": str(row["action"]), "reference_id": str(row["reference_id"])}

    def acknowledge_messages(
        self,
        acknowledgements: tuple[tuple[str, str], ...],
        *,
        owner: str,
        delivered_at: str,
    ) -> tuple[str, ...]:
        """Atomically validate and acknowledge an instance's leased messages."""
        if not acknowledgements:
            return ()
        if len({message_id for message_id, _ in acknowledgements}) != len(acknowledgements):
            raise MessageConflictError("duplicate message_id in acknowledgement batch")
        with self.monitor_store.transaction() as connection:
            rows = {}
            for message_id, _ in acknowledgements:
                row = connection.execute(
                    "SELECT payload_json,state,lease_owner FROM relay_messages WHERE message_id=? "
                    "AND direction='outbound'",
                    (message_id,),
                ).fetchone()
                if row is None or row["state"] != "leased" or row["lease_owner"] != owner:
                    raise MessageConflictError("message is not leased to authenticated instance")
                rows[message_id] = row
            for message_id, supplied_hash in acknowledgements:
                expected = hashlib.sha256(str(rows[message_id]["payload_json"]).encode()).hexdigest()
                if not hmac.compare_digest(supplied_hash, expected):
                    raise MessageConflictError("ack payload hash mismatch")
            for message_id, _ in acknowledgements:
                connection.execute(
                    "UPDATE relay_messages SET state='delivered',delivered_at=?,lease_owner='',"
                    "lease_until='' WHERE message_id=?",
                    (delivered_at, message_id),
                )
        return tuple(message_id for message_id, _ in acknowledgements)

    def acknowledge_message(
        self, message_id: str, *, owner: str, payload_hash: str, delivered_at: str
    ) -> bool:
        try:
            return bool(
                self.acknowledge_messages(
                    ((message_id, payload_hash),), owner=owner, delivered_at=delivered_at
                )
            )
        except MessageConflictError as exc:
            if str(exc) == "message is not leased to authenticated instance":
                return False
            raise

    def select_current_task(
        self,
        platform: str,
        actor_id: str,
        instance_id: str,
        session_id: str,
        *,
        selected_at: str,
    ) -> None:
        if self.get_session(instance_id, session_id) is None:
            raise ValueError("cannot select an unknown relay session")
        with self.monitor_store.transaction() as connection:
            connection.execute(
                "INSERT INTO relay_current_tasks(platform,actor_id,instance_id,session_id,selected_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(platform,actor_id) DO UPDATE SET "
                "instance_id=excluded.instance_id,session_id=excluded.session_id,"
                "selected_at=excluded.selected_at",
                (platform, actor_id, instance_id, session_id, selected_at),
            )

    def current_session(self, platform: str, actor_id: str) -> RelaySession | None:
        with self.monitor_store.connect() as connection:
            row = connection.execute(
                "SELECT s.* FROM relay_current_tasks c JOIN relay_sessions s "
                "ON s.instance_id=c.instance_id AND s.session_id=c.session_id "
                "WHERE c.platform=? AND c.actor_id=?",
                (platform, actor_id),
            ).fetchone()
        return None if row is None else RelaySession(**dict(row))

    def record_decision(self, envelope: RelayEnvelope) -> bool:
        if envelope.kind != "decision.request":
            raise ValueError("decision record requires decision.request")
        payload = envelope.payload
        binding = envelope.binding
        with self.monitor_store.transaction() as connection:
            result = connection.execute(
                "INSERT OR IGNORE INTO relay_decisions(decision_id,instance_id,session_id,turn_id,"
                "session_version,question,choices_json,state,expires_at) VALUES (?,?,?,?,?,?,?,'pending',?)",
                (
                    payload["decision_id"],
                    binding.instance_id,
                    binding.session_id,
                    binding.turn_id,
                    binding.session_version,
                    payload["question"],
                    _canonical(payload["choices"]),
                    envelope.expires_at,
                ),
            )
        return result.rowcount == 1

    def answer_decision(self, envelope: RelayEnvelope, *, answered_at: str) -> bool:
        if envelope.kind != "decision.answer":
            raise ValueError("decision answer requires decision.answer")
        binding = envelope.binding
        with self.monitor_store.transaction() as connection:
            result = connection.execute(
                "UPDATE relay_decisions SET state='answered',answered_at=? WHERE decision_id=? "
                "AND instance_id=? AND session_id=? AND turn_id=? AND session_version=? "
                "AND state='pending' AND expires_at>?",
                (
                    answered_at,
                    envelope.payload["decision_id"],
                    binding.instance_id,
                    binding.session_id,
                    binding.turn_id,
                    binding.session_version,
                    answered_at,
                ),
            )
        return result.rowcount == 1

    def record_proposal(self, envelope: RelayEnvelope) -> bool:
        if envelope.kind != "task.create.proposal":
            raise ValueError("proposal record requires task.create.proposal")
        with self.monitor_store.transaction() as connection:
            result = connection.execute(
                "INSERT OR IGNORE INTO relay_task_proposals(proposal_id,platform,actor_id,"
                "payload_json,state,expires_at) VALUES (?,?,?,?,'proposed',?)",
                (
                    envelope.payload["proposal_id"],
                    envelope.sender.platform,
                    envelope.sender.actor_id,
                    _canonical(envelope.to_dict()),
                    envelope.expires_at,
                ),
            )
        return result.rowcount == 1

    def proposal_envelope(self, proposal_id: str) -> RelayEnvelope | None:
        with self.monitor_store.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM relay_task_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        return None if row is None else RelayEnvelope.from_dict(json.loads(row["payload_json"]))

    def confirm_proposal(self, envelope: RelayEnvelope, *, confirmed_at: str) -> bool:
        if envelope.kind != "task.create.confirm":
            raise ValueError("proposal confirmation requires task.create.confirm")
        with self.monitor_store.transaction() as connection:
            result = connection.execute(
                "UPDATE relay_task_proposals SET state='confirmed',confirmed_at=? "
                "WHERE proposal_id=? AND platform=? AND actor_id=? AND state='proposed' "
                "AND expires_at>?",
                (
                    confirmed_at,
                    envelope.payload["proposal_id"],
                    envelope.sender.platform,
                    envelope.sender.actor_id,
                    confirmed_at,
                ),
            )
        return result.rowcount == 1
