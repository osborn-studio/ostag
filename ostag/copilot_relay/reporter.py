"""Durable store-and-forward loop between local osstack and central OStag."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from ostag.work_monitor.reporter import (
    ReporterTransport,
    TransportResponse,
    UrlTransport,
)

from .models import RelayEnvelope, SessionBinding
from .osstack_port import OsstackSession, OsstackSessionPort


class SigningIdentity(Protocol):
    device_id: str

    def sign(self, body: bytes) -> str: ...


class CopilotReporter:
    def __init__(
        self,
        *,
        instance_id: str,
        secret: str,
        central_url: str,
        spool_path: str | Path,
        port: OsstackSessionPort,
        transport: ReporterTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        device_identity: SigningIdentity | None = None,
        gateway_version: str = "0.2.0",
        heartbeat_interval_seconds: int = 5,
    ) -> None:
        if not instance_id or (not secret and device_identity is None):
            raise ValueError("relay identity credentials are required")
        if device_identity is not None and device_identity.device_id != instance_id:
            raise ValueError("relay instance_id must match device identity")
        local_http = central_url.startswith(
            ("http://127.0.0.1", "http://localhost", "http://[::1]")
        )
        if central_url and not central_url.startswith("https://") and not local_http:
            raise ValueError("relay central URL must use HTTPS outside localhost")
        self.instance_id = instance_id
        self._secret = secret
        self._device_identity = device_identity
        self.gateway_version = gateway_version
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.central_url = central_url.rstrip("/")
        self.spool_path = Path(spool_path)
        self.port = port
        self.transport = transport or UrlTransport()
        self.clock = clock or (lambda: datetime.now(UTC))
        self._after_local_apply: Callable[[], None] = lambda: None
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()
        self.spool_path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.spool_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _migrate(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS relay_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS relay_local_sessions(
                    runtime_id TEXT NOT NULL,session_id TEXT NOT NULL,payload TEXT NOT NULL,
                    exported_version INTEGER NOT NULL DEFAULT 0,snapshot_hash TEXT NOT NULL DEFAULT '',
                    exported_decision_id TEXT NOT NULL DEFAULT '',PRIMARY KEY(runtime_id,session_id));
                CREATE TABLE IF NOT EXISTS relay_outbound(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,message_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS relay_inbound(
                    message_id TEXT PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
                    applied_at TEXT NOT NULL DEFAULT '',acked_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '');
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(relay_inbound)")}
            if "acked_at" not in columns:
                db.execute(
                    "ALTER TABLE relay_inbound ADD COLUMN acked_at TEXT NOT NULL DEFAULT ''"
                )
            session_columns = {
                row[1] for row in db.execute("PRAGMA table_info(relay_local_sessions)")
            }
            if "exported_decision_id" not in session_columns:
                db.execute(
                    "ALTER TABLE relay_local_sessions ADD COLUMN "
                    "exported_decision_id TEXT NOT NULL DEFAULT ''"
                )
                session_columns.add("exported_decision_id")
            if "runtime_id" not in session_columns:
                db.executescript(
                    "ALTER TABLE relay_local_sessions RENAME TO relay_local_sessions_v1;"
                    "CREATE TABLE relay_local_sessions(runtime_id TEXT NOT NULL,session_id TEXT NOT NULL,payload TEXT NOT NULL,exported_version INTEGER NOT NULL DEFAULT 0,snapshot_hash TEXT NOT NULL DEFAULT '',exported_decision_id TEXT NOT NULL DEFAULT '',PRIMARY KEY(runtime_id,session_id));"
                    "INSERT INTO relay_local_sessions(runtime_id,session_id,payload,exported_version,snapshot_hash,exported_decision_id) SELECT 'legacy',session_id,payload,exported_version,snapshot_hash,exported_decision_id FROM relay_local_sessions_v1;"
                    "DROP TABLE relay_local_sessions_v1;"
                )
                session_columns.add("runtime_id")

    @staticmethod
    def _dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _meta(self, key: str, default: str = "") -> str:
        with self._connect() as db:
            row = db.execute("SELECT value FROM relay_meta WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    @staticmethod
    def _set_meta(db: sqlite3.Connection, key: str, value: object) -> None:
        db.execute(
            "INSERT INTO relay_meta(key,value) VALUES(?,?) ON CONFLICT(key) "
            "DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def _utc(self, value: datetime | None = None) -> str:
        return (value or self.clock()).astimezone(UTC).isoformat().replace("+00:00", "Z")

    async def discover_sessions_once(self) -> int:
        found = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for session in self.port.list_sessions():
                payload = self._dump(session.__dict__)
                row = db.execute(
                    "SELECT payload FROM relay_local_sessions WHERE runtime_id=? AND session_id=?",
                    (session.runtime_id, session.session_id),
                ).fetchone()
                if row is None:
                    db.execute(
                        "INSERT INTO relay_local_sessions(runtime_id,session_id,payload) VALUES(?,?,?)",
                        (session.runtime_id, session.session_id, payload),
                    )
                    found += 1
                elif row["payload"] != payload:
                    db.execute(
                        "UPDATE relay_local_sessions SET payload=? WHERE runtime_id=? AND session_id=?",
                        (payload, session.runtime_id, session.session_id),
                    )
        return found

    def _next_sequence(self, db: sqlite3.Connection) -> int:
        row = db.execute(
            "SELECT value FROM relay_meta WHERE key='next_outbound_sequence'"
        ).fetchone()
        sequence = 1 if row is None else int(row["value"])
        self._set_meta(db, "next_outbound_sequence", sequence + 1)
        return sequence

    def _make_envelope(
        self,
        db: sqlite3.Connection,
        binding: SessionBinding,
        kind: str,
        payload: dict[str, object],
        message_id: str,
        *,
        causation_id: str | None = None,
    ) -> RelayEnvelope:
        now = self.clock()
        schema_version = 2 if self._device_identity is not None else 1
        return RelayEnvelope.from_dict(
            {
                "schema_version": schema_version,
                "message_id": message_id,
                "binding": binding.to_dict(schema_version=schema_version),
                "sequence": self._next_sequence(db),
                "created_at": self._utc(now),
                "expires_at": self._utc(now + timedelta(days=1)),
                "sender": {"platform": "osstack", "actor_id": self.instance_id},
                "kind": kind,
                "payload": payload,
                "correlation_id": None,
                "causation_id": causation_id,
            }
        )

    @staticmethod
    def _insert_outbound(db: sqlite3.Connection, envelope: RelayEnvelope) -> bool:
        result = db.execute(
            "INSERT OR IGNORE INTO relay_outbound(sequence,message_id,payload) VALUES(?,?,?)",
            (envelope.sequence, envelope.message_id, CopilotReporter._dump(envelope.to_dict())),
        )
        return result.rowcount == 1

    async def capture_changed_snapshots_once(self) -> int:
        created = 0
        with self._connect() as db:
            rows = db.execute("SELECT * FROM relay_local_sessions ORDER BY session_id").fetchall()
        for row in rows:
            local = OsstackSession(**json.loads(row["payload"]))
            snapshot = self.port.snapshot(local.session_id)
            snapshot_hash = hashlib.sha256(self._dump(snapshot).encode()).hexdigest()
            decision = snapshot["decision"]
            decision_id = "" if decision is None else str(decision["decision_id"])
            base_changed = not (
                int(row["exported_version"]) >= local.version
                and row["snapshot_hash"] == snapshot_hash
            )
            decision_changed = row["exported_decision_id"] != decision_id
            if not base_changed and not decision_changed:
                continue
            binding = SessionBinding(
                self.instance_id, local.task_id, local.run_id, local.session_id,
                str(snapshot["turn_id"]), local.version, local.runtime_id,
            )
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if base_changed:
                    upsert = self._make_envelope(
                        db, binding, "session.upsert",
                        {
                            "agent_id": local.agent_id, "title": local.task_id,
                            "state": local.state,
                            "resume_supported": local.resume_supported,
                            "latest_summary": str(snapshot["assistant_text"])[:1_000],
                        },
                        f"session_{local.session_id}_v{local.version}",
                    )
                    created += int(self._insert_outbound(db, upsert))
                    snap = self._make_envelope(
                        db, binding, "assistant.snapshot",
                        {
                            "assistant_text": snapshot["assistant_text"],
                            "redaction_count": 0,
                            "truncated": False,
                        },
                        f"snapshot_{local.session_id}_v{local.version}_{snapshot_hash[:12]}",
                    )
                    created += int(self._insert_outbound(db, snap))
                if decision is not None and decision_changed:
                    request = self._make_envelope(
                        db,
                        binding,
                        "decision.request",
                        {
                            "decision_id": decision["decision_id"],
                            "question": decision["question"],
                            "choices": decision["choices"],
                            "recommended_choice_id": None,
                            "blocking": True,
                            "safe_work": [],
                        },
                        f"decision_{decision['decision_id']}_v{local.version}",
                    )
                    created += int(self._insert_outbound(db, request))
                db.execute(
                    "UPDATE relay_local_sessions SET exported_version=?,snapshot_hash=?,"
                    "exported_decision_id=? WHERE runtime_id=? AND session_id=?",
                    (local.version, snapshot_hash, decision_id, local.runtime_id, local.session_id),
                )
        return created

    def pending_outbound(self) -> list[RelayEnvelope]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM relay_outbound WHERE state='pending' ORDER BY sequence"
            ).fetchall()
        return [RelayEnvelope.from_dict(json.loads(row["payload"])) for row in rows]

    def _headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(self.clock().timestamp()))
        nonce = uuid.uuid4().hex
        version = "v2" if self._device_identity is not None else "v1"
        canonical = "\n".join(
            (version, method.upper(), path, self.instance_id, timestamp, nonce,
             hashlib.sha256(body).hexdigest())
        ).encode()
        if self._device_identity is not None:
            return {
                "Content-Type": "application/json",
                "X-OStag-Device": self.instance_id,
                "X-OStag-Timestamp": timestamp,
                "X-OStag-Nonce": nonce,
                "X-OStag-Signature": self._device_identity.sign(canonical),
            }
        return {
            "Content-Type": "application/json",
            "X-OStag-Instance": self.instance_id,
            "X-OStag-Timestamp": timestamp,
            "X-OStag-Nonce": nonce,
            "X-OStag-Signature": hmac.new(
                self._secret.encode(), canonical, hashlib.sha256
            ).hexdigest(),
        }

    async def heartbeat_once(self) -> int:
        if self._device_identity is None:
            return 0
        last = self._meta("last_heartbeat_at")
        if last:
            previous = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if (self.clock() - previous).total_seconds() < self.heartbeat_interval_seconds:
                return 0
        runtimes = self.port.runtime_inventory()
        if len(runtimes) > 100:
            raise ValueError("runtime inventory exceeds heartbeat limit")
        body = self._dump(
            {
                "device_id": self.instance_id,
                "gateway_version": self.gateway_version,
                "runtimes": list(runtimes),
            }
        ).encode()
        try:
            response = self._request("/v1/copilot/device/heartbeat", body)
        except OSError:
            return 0
        if not 200 <= response.status < 300:
            return 0
        with self._connect() as db:
            self._set_meta(db, "last_heartbeat_at", self._utc())
        return 1

    def _request(self, path: str, body: bytes) -> TransportResponse:
        return self.transport.request(
            "POST", f"{self.central_url}{path}", body, self._headers("POST", path, body)
        )

    async def flush_outbound_once(self) -> int:
        pending = self.pending_outbound()
        if not pending:
            return 0
        next_attempt = self._meta("next_attempt_at")
        if next_attempt and self.clock() < datetime.fromisoformat(next_attempt.replace("Z", "+00:00")):
            return 0
        body = self._dump({"events": [item.to_dict() for item in pending]}).encode()
        try:
            response = self._request("/v1/copilot/events", body)
        except OSError:
            response = TransportResponse(599, b"")
        if response.status == 409:
            with self._connect() as db:
                self._set_meta(db, "reconciliation_required", 1)
            return 0
        if not 200 <= response.status < 300:
            self._record_failure()
            return 0
        try:
            payload = json.loads(response.body)
            ack = payload["ack_sequence"]
            sequences = {item.sequence for item in pending}
            if (
                isinstance(ack, bool)
                or not isinstance(ack, int)
                or ack < 0
                or ack > pending[-1].sequence
                or (ack != 0 and ack not in sequences)
            ):
                raise ValueError
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            self._record_failure()
            return 0
        with self._connect() as db:
            deleted = db.execute(
                "DELETE FROM relay_outbound WHERE state='pending' AND sequence<=?", (ack,)
            ).rowcount
            self._set_meta(db, "failure_count", 0)
            self._set_meta(db, "next_attempt_at", "")
        return deleted

    def _record_failure(self) -> None:
        failures = int(self._meta("failure_count", "0")) + 1
        with self._connect() as db:
            self._set_meta(db, "failure_count", failures)
            self._set_meta(db, "next_attempt_at", self._utc(self.clock() + timedelta(seconds=min(300, 2 ** failures))))
            db.execute("UPDATE relay_outbound SET attempts=attempts+1 WHERE state='pending'")

    def reconciliation_required(self) -> bool:
        return self._meta("reconciliation_required", "0") == "1"

    async def poll_inbound_once(self) -> int:
        body = b"{}"
        try:
            response = self._request("/v1/copilot/messages/poll", body)
        except OSError:
            return 0
        if not 200 <= response.status < 300:
            return 0
        try:
            root = json.loads(response.body)
            if not isinstance(root, dict) or set(root) != {"messages"} or not isinstance(root["messages"], list):
                raise ValueError
            messages = [RelayEnvelope.from_dict(item) for item in root["messages"]]
        except (json.JSONDecodeError, ValueError, TypeError):
            return 0
        added = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for message in messages:
                if message.binding.instance_id != self.instance_id:
                    continue
                existing = db.execute(
                    "SELECT payload FROM relay_inbound WHERE message_id=?",
                    (message.message_id,),
                ).fetchone()
                encoded = self._dump(message.to_dict())
                if existing is not None:
                    if existing["payload"] != encoded:
                        self._set_meta(db, "inbound_conflict", message.message_id)
                    continue
                result = db.execute(
                    "INSERT OR IGNORE INTO relay_inbound(message_id,payload) VALUES(?,?)",
                    (message.message_id, encoded),
                )
                added += int(result.rowcount == 1)
        return added

    async def apply_inbound_once(self) -> int:
        sessions = {
            (session.runtime_id, session.session_id): session
            for session in self.port.list_sessions()
        }
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM relay_inbound WHERE state='pending' ORDER BY rowid"
            ).fetchall()
        applied = 0
        for row in rows:
            message = RelayEnvelope.from_dict(json.loads(row["payload"]))
            expires = datetime.fromisoformat(message.expires_at.replace("Z", "+00:00"))
            if self.clock() >= expires:
                self._finish_inbound(message, "expired", "instruction expired before local apply")
                continue
            session = sessions.get((message.binding.runtime_id, message.binding.session_id))
            if message.binding.runtime_id == "legacy":
                candidates = [
                    candidate
                    for (runtime_id, session_id), candidate in sessions.items()
                    if session_id == message.binding.session_id and runtime_id != "legacy"
                ]
                if len(candidates) == 1:
                    session = candidates[0]
                    message = replace(
                        message,
                        binding=replace(message.binding, runtime_id=session.runtime_id),
                    )
            if session is None or session.version != message.binding.session_version:
                self._finish_inbound(
                    message, "stale", "exact runtime session is unavailable"
                )
                continue
            if message.kind in {"instruction.send", "decision.answer"}:
                result = self.port.send(message)
                if result["state"] not in {"applied", "duplicate"}:
                    continue
                self._after_local_apply()
                self._finish_inbound(message, "applied", "original session accepted the instruction")
                applied += 1
            elif message.kind == "assistant.snapshot":
                if message.payload["assistant_text"] != "":
                    continue
                snapshot = self.port.snapshot(message.binding.session_id)
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    response = self._make_envelope(
                        db,
                        SessionBinding(
                            self.instance_id,
                            str(snapshot["task_id"]),
                            str(snapshot["run_id"]),
                            str(snapshot["session_id"]),
                            str(snapshot["turn_id"]),
                            int(snapshot["version"]),
                            str(snapshot["runtime_id"]),
                        ),
                        "assistant.snapshot",
                        {
                            "assistant_text": snapshot["assistant_text"],
                            "redaction_count": 0,
                            "truncated": False,
                        },
                        f"snapshot_response_{message.message_id}",
                        causation_id=message.message_id,
                    )
                    self._insert_outbound(db, response)
                self._finish_inbound(message, "applied", "safe assistant snapshot captured")
                applied += 1
            elif message.kind == "task.create.proposal":
                with self._connect() as db:
                    db.execute(
                        "UPDATE relay_inbound SET state='staged' WHERE message_id=? AND state='pending'",
                        (message.message_id,),
                    )
            elif message.kind == "task.create.confirm":
                proposal_id = message.payload["proposal_id"]
                with self._connect() as db:
                    staged = db.execute(
                        "SELECT * FROM relay_inbound WHERE state='staged' ORDER BY rowid"
                    ).fetchall()
                proposal = next(
                    (
                        candidate
                        for candidate in (
                            RelayEnvelope.from_dict(json.loads(item["payload"])) for item in staged
                        )
                        if candidate.kind == "task.create.proposal"
                        and candidate.payload["proposal_id"] == proposal_id
                    ),
                    None,
                )
                if proposal is None:
                    continue
                if proposal.sender != message.sender or proposal.binding != message.binding:
                    continue
                self.port.create_task(message, proposal)
                self._after_local_apply()
                with self._connect() as db:
                    db.execute(
                        "UPDATE relay_inbound SET state='applied',applied_at=? WHERE message_id=?",
                        (self._utc(), proposal.message_id),
                    )
                self._finish_inbound(message, "applied", "confirmed osstack task created")
                applied += 1
            elif message.kind == "delivery.receipt":
                with self._connect() as db:
                    updated = db.execute(
                        "UPDATE relay_inbound SET state='applied',applied_at=? "
                        "WHERE message_id=? AND state='pending'",
                        (self._utc(), message.message_id),
                    ).rowcount
                applied += int(updated == 1)
        return applied

    def _finish_inbound(self, message: RelayEnvelope, state: str, summary: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE relay_inbound SET state=?,applied_at=? WHERE message_id=? AND state='pending'",
                (state, self._utc(), message.message_id),
            )
            if result.rowcount != 1:
                return
            receipt = self._make_envelope(
                db, message.binding, "delivery.receipt",
                {
                    "related_message_id": message.message_id,
                    "state": state,
                    "summary": summary,
                    "error_code": None if state == "applied" else state,
                },
                f"receipt_{message.message_id}_{state}",
                causation_id=message.message_id,
            )
            self._insert_outbound(db, receipt)

    async def flush_receipts_once(self) -> int:
        with self._connect() as db:
            rows = db.execute(
                "SELECT message_id,payload FROM relay_inbound "
                "WHERE state IN ('applied','expired','stale') AND acked_at='' ORDER BY rowid LIMIT 100"
            ).fetchall()
        if not rows:
            return 0
        acks = [
            {
                "message_id": str(row["message_id"]),
                "payload_hash": hashlib.sha256(str(row["payload"]).encode()).hexdigest(),
            }
            for row in rows
        ]
        body = self._dump({"acks": acks}).encode()
        try:
            response = self._request("/v1/copilot/messages/ack", body)
        except OSError:
            return 0
        if not 200 <= response.status < 300:
            return 0
        try:
            result = json.loads(response.body)
            acked = result["acked"]
            if not isinstance(acked, list) or any(not isinstance(item, str) for item in acked):
                raise ValueError
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return 0
        allowed = {str(row["message_id"]) for row in rows}
        if not set(acked) <= allowed:
            return 0
        with self._connect() as db:
            for message_id in acked:
                db.execute(
                    "UPDATE relay_inbound SET acked_at=? WHERE message_id=? AND acked_at=''",
                    (self._utc(), message_id),
                )
        return len(acked)
