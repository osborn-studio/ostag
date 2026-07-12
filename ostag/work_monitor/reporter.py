"""Durable outbound reporter for OStag and osstack work events."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from .models import MonitorEvent, RemoteCommand
from .redaction import sanitize_task_input


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: bytes


class ReporterTransport(Protocol):
    def request(
        self, method: str, url: str, body: bytes, headers: dict[str, str]
    ) -> TransportResponse: ...


class UrlTransport:
    def request(
        self, method: str, url: str, body: bytes, headers: dict[str, str]
    ) -> TransportResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                return TransportResponse(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return TransportResponse(exc.code, exc.read())


class Reporter:
    def __init__(
        self,
        *,
        instance_id: str,
        secret: str,
        central_url: str,
        ledger_path: str | Path,
        spool_path: str | Path,
        transport: ReporterTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        interval_seconds: int = 30,
        command_poll_seconds: int = 10,
        capabilities: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        if not instance_id:
            raise ValueError("instance_id is required")
        self.instance_id = instance_id
        self._secret = secret
        self.central_url = central_url.rstrip("/")
        self.ledger_path = Path(ledger_path)
        self.spool_path = Path(spool_path)
        self.transport = transport or UrlTransport()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.interval_seconds = interval_seconds
        self.command_poll_seconds = command_poll_seconds
        self.capabilities = capabilities or (lambda: frozenset())
        self._before_spool_commit: Callable[[], None] = lambda: None
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.spool_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS spool (
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_history (
                    event_id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_metadata (
                    task_id TEXT PRIMARY KEY, title TEXT NOT NULL, agent TEXT NOT NULL,
                    project TEXT NOT NULL, version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS command_state (
                    command_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    command_payload TEXT NOT NULL, result_payload TEXT
                );
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO event_history(event_id,payload) "
                "SELECT event_id,payload FROM spool"
            )

    def _state(self, key: str, default: str = "") -> str:
        with self._connect() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    @staticmethod
    def _set_state(db: sqlite3.Connection, key: str, value: object) -> None:
        db.execute(
            "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def enqueue(self, payload: dict[str, object]) -> MonitorEvent:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            event = self._enqueue_in_transaction(db, payload)
            db.commit()
        return event

    def _enqueue_in_transaction(
        self, db: sqlite3.Connection, payload: dict[str, object]
    ) -> MonitorEvent:
        existing = db.execute(
            "SELECT payload FROM event_history WHERE event_id=?", (str(payload["event_id"]),)
        ).fetchone()
        if existing is not None:
            return MonitorEvent.from_dict(json.loads(existing["payload"]))
        row = db.execute("SELECT value FROM state WHERE key='next_sequence'").fetchone()
        sequence = 1 if row is None else int(row["value"])
        raw_task = payload.get("task")
        task = None if raw_task is None else sanitize_task_input(dict(raw_task))  # type: ignore[arg-type]
        event = MonitorEvent(
            event_id=str(payload["event_id"]),
            instance_id=self.instance_id,
            sequence=sequence,
            kind=str(payload["kind"]),
            occurred_at=str(payload["occurred_at"]),
            task=task,
        )
        db.execute(
            "INSERT OR IGNORE INTO spool(sequence,event_id,payload) VALUES(?,?,?)",
            (
                sequence,
                event.event_id,
                json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True),
            ),
        )
        db.execute(
            "INSERT INTO event_history(event_id,payload) VALUES(?,?)",
            (event.event_id, json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)),
        )
        self._set_state(db, "next_sequence", sequence + 1)
        return event

    def pending_events(self) -> list[MonitorEvent]:
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM spool ORDER BY sequence").fetchall()
        return [MonitorEvent.from_dict(json.loads(row["payload"])) for row in rows]

    def tail_ledger_once(self) -> int:
        if not self.ledger_path.exists():
            return 0
        stat = self.ledger_path.stat()
        inode = str(stat.st_ino)
        old_inode = self._state("ledger_inode")
        offset = int(self._state("ledger_offset", "0")) if old_inode == inode else 0
        count = 0
        with self.ledger_path.open("rb") as handle:
            handle.seek(offset)
            while True:
                start = handle.tell()
                line = handle.readline()
                if not line or not line.endswith(b"\n"):
                    break
                record = json.loads(line)
                mapped = self._map_ledger_record(record)
                end = handle.tell()
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if mapped is not None:
                        mapped = self._apply_task_metadata(db, mapped)
                        self._enqueue_in_transaction(db, mapped)
                        count += 1
                    self._before_spool_commit()
                    self._set_state(db, "ledger_inode", inode)
                    self._set_state(db, "ledger_offset", end)
                    self._set_state(db, "ledger_event_id", record.get("event_id", ""))
                    db.commit()
                offset = end
                if end <= start:  # pragma: no cover - defensive
                    break
        return count

    def _apply_task_metadata(
        self, db: sqlite3.Connection, mapped: dict[str, object]
    ) -> dict[str, object]:
        task = dict(mapped["task"])  # type: ignore[arg-type]
        task_id = str(task["task_id"])
        row = db.execute("SELECT * FROM task_metadata WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            version = 1
            title, agent, project = str(task["title"]), str(task["agent"]), str(task["project"])
            db.execute(
                "INSERT INTO task_metadata VALUES(?,?,?,?,?)",
                (task_id, title, agent, project, version),
            )
        else:
            version = int(row["version"]) + 1
            title, agent, project = str(row["title"]), str(row["agent"]), str(row["project"])
            db.execute("UPDATE task_metadata SET version=? WHERE task_id=?", (version, task_id))
        task.update({"title": title, "agent": agent, "project": project, "version": version})
        return {**mapped, "task": task}

    def _map_ledger_record(self, record: dict[str, object]) -> dict[str, object] | None:
        event_type = str(record.get("event_type", ""))
        payload = dict(record.get("payload", {}))  # type: ignore[arg-type]
        run = dict(payload.get("run", {})) if isinstance(payload.get("run"), dict) else {}
        status = str(payload.get("status", ""))
        mapping = {
            "run_created": "task.created",
            "action_proposed": "task.awaiting_approval",
            "action_state_changed": "task.progress",
            "run_status_changed": {
                "running": "task.started",
                "blocked": "task.blocked",
                "waiting_approval": "task.awaiting_approval",
            }.get(status, "task.progress"),
            "run_finished": {
                "succeeded": "task.completed",
                "failed": "task.failed",
                "cancelled": "task.cancelled",
            }.get(status),
        }
        kind = mapping.get(event_type)
        if not kind:
            return None
        intent = run.get("intent", {}) if isinstance(run.get("intent"), dict) else {}
        summary_source = payload.get("detail") or payload.get("summary")
        receipt = payload.get("receipt", {}) if isinstance(payload.get("receipt"), dict) else {}
        summary_source = summary_source or receipt.get("summary") or status
        title = intent.get("raw_text") or f"OStag run {record.get('run_id', '')}"
        task_status = {
            "task.created": "todo",
            "task.started": "running",
            "task.progress": "running",
            "task.blocked": "blocked",
            "task.awaiting_approval": "blocked",
            "task.completed": "done",
            "task.failed": "blocked",
            "task.cancelled": "done",
        }[kind]
        return {
            "event_id": f"monitor-{record.get('event_id', '')}",
            "kind": kind,
            "occurred_at": str(record.get("created_at", self.clock().isoformat())),
            "task": {
                "task_id": str(record.get("run_id", "")),
                "title": str(title),
                "project": "ostag",
                "status": task_status,
                "agent": str(run.get("executor_id") or "ostag"),
                "progress": 100 if task_status == "done" else (10 if task_status == "todo" else 50),
                "duration_seconds": 0,
                "summary": str(summary_source or ""),
                "error_class": "execution_failed" if kind == "task.failed" else None,
                "evidence_ref": f"ledger:{record.get('event_id', '')}",
                "version": 1,
            },
        }

    def heartbeat(self) -> MonitorEvent:
        now = self.clock().isoformat()
        return self.enqueue(
            {
                "event_id": f"heartbeat-{self.instance_id}-{uuid.uuid4().hex}",
                "kind": "instance.heartbeat",
                "occurred_at": now,
                "task": None,
            }
        )

    def _headers(self, body: bytes) -> dict[str, str]:
        timestamp = str(int(self.clock().timestamp()))
        signature = hmac.new(
            self._secret.encode(), timestamp.encode() + b"\n" + body, hashlib.sha256
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-OStag-Instance": self.instance_id,
            "X-OStag-Timestamp": timestamp,
            "X-OStag-Signature": signature,
        }

    def flush_once(self) -> int:
        if not self.central_url or not self._secret:
            return 0
        next_attempt = self._state("next_attempt_at")
        if next_attempt and self.clock() < datetime.fromisoformat(next_attempt):
            return 0
        events = self.pending_events()
        if not events:
            return 0
        body = json.dumps(
            {
                "events": [event.to_dict() for event in events],
                "supported_actions": sorted(self.capabilities()),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        try:
            response = self.transport.request(
                "POST", f"{self.central_url}/v1/events", body, self._headers(body)
            )
        except OSError:
            response = TransportResponse(599, b"")
        if not 200 <= response.status < 300:
            self._record_failure()
            return 0
        try:
            result = json.loads(response.body or b"{}")
            ack = result["ack_sequence"]
            if isinstance(ack, bool) or not isinstance(ack, int):
                raise ValueError("invalid ack")
            sequences = [event.sequence for event in events]
            if ack < 0 or ack > max(sequences) or (ack and ack not in sequences):
                raise ValueError("ack outside sent contiguous range")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            self._record_failure()
            return 0
        with self._connect() as db:
            deleted = db.execute("DELETE FROM spool WHERE sequence<=?", (ack,)).rowcount
            self._set_state(db, "failure_count", 0)
            self._set_state(db, "next_attempt_at", "")
        return deleted

    def _record_failure(self) -> None:
        failures = int(self._state("failure_count", "0")) + 1
        delay = min(300, 2 ** min(failures, 8))
        with self._connect() as db:
            self._set_state(db, "failure_count", failures)
            self._set_state(
                db, "next_attempt_at", (self.clock() + timedelta(seconds=delay)).isoformat()
            )

    def poll_commands_once(self, execute: Callable[[RemoteCommand], dict[str, object]]) -> int:
        if not self.central_url or not self._secret:
            return 0
        empty = b"{}"
        response = self.transport.request(
            "GET", f"{self.central_url}/v1/commands/{self.instance_id}", empty, self._headers(empty)
        )
        if not 200 <= response.status < 300:
            return 0
        try:
            commands = json.loads(response.body or b"{}").get("commands", [])
        except (json.JSONDecodeError, AttributeError):
            return 0
        handled = 0
        for raw in commands:
            command = RemoteCommand.from_dict(raw)
            with self._connect() as db:
                row = db.execute(
                    "SELECT state,result_payload FROM command_state WHERE command_id=?",
                    (command.command_id,),
                ).fetchone()
                if row is None:
                    db.execute(
                        "INSERT INTO command_state VALUES(?,?,?,NULL)",
                        (
                            command.command_id,
                            "received",
                            json.dumps(command.to_dict(), sort_keys=True),
                        ),
                    )
                    state, stored = "received", None
                else:
                    state, stored = str(row["state"]), row["result_payload"]
            if state == "acked":
                continue
            if state == "executed":
                result = json.loads(stored)
            else:
                result = execute(command)
                with self._connect() as db:
                    db.execute(
                        "UPDATE command_state SET state='executed',result_payload=? WHERE command_id=?",
                        (json.dumps(result, sort_keys=True), command.command_id),
                    )
            body = json.dumps(
                {"command_id": command.command_id, "result": result}, sort_keys=True
            ).encode()
            ack_response = self.transport.request(
                "POST", f"{self.central_url}/v1/commands/ack", body, self._headers(body)
            )
            if 200 <= ack_response.status < 300:
                with self._connect() as db:
                    db.execute(
                        "UPDATE command_state SET state='acked' WHERE command_id=?",
                        (command.command_id,),
                    )
                handled += 1
        return handled

    def command_states(self) -> dict[str, str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT command_id,state FROM command_state ORDER BY command_id"
            ).fetchall()
        return {str(row["command_id"]): str(row["state"]) for row in rows}

    def run_once(
        self, execute: Callable[[RemoteCommand], dict[str, object]] | None = None
    ) -> dict[str, object]:
        now = self.clock()
        report_due = self._due("last_report_at", self.interval_seconds, now)
        poll_due = self._due("last_command_poll_at", self.command_poll_seconds, now)
        tailed = self.tail_ledger_once() if report_due else 0
        if report_due:
            self.heartbeat()
            flushed = self.flush_once()
            self._save_time("last_report_at", now)
        else:
            flushed = 0
        polled = False
        handled = 0
        if poll_due and execute is not None:
            handled = self.poll_commands_once(execute)
            self._save_time("last_command_poll_at", now)
            polled = True
        return {
            "tailed": tailed,
            "flushed": flushed,
            "heartbeat": report_due,
            "polled": polled,
            "handled": handled,
            **self.status(),
        }

    def _due(self, key: str, seconds: int, now: datetime) -> bool:
        previous = self._state(key)
        return not previous or now >= datetime.fromisoformat(previous) + timedelta(seconds=seconds)

    def _save_time(self, key: str, value: datetime) -> None:
        with self._connect() as db:
            self._set_state(db, key, value.isoformat())

    def start(self, execute: Callable[[RemoteCommand], dict[str, object]] | None = None) -> None:
        while True:
            self.run_once(execute)
            self.sleep(min(self.interval_seconds, self.command_poll_seconds))

    def status(self) -> dict[str, object]:
        return {
            "enabled": bool(self.central_url and self._secret),
            "instance_id": self.instance_id,
            "pending_events": len(self.pending_events()),
            "ledger_inode": self._state("ledger_inode"),
            "ledger_offset": int(self._state("ledger_offset", "0")),
            "last_ledger_event_id": self._state("ledger_event_id"),
            "failure_count": int(self._state("failure_count", "0")),
            "next_attempt_at": self._state("next_attempt_at"),
            "next_sequence": int(self._state("next_sequence", "1")),
            "command_states": self.command_states(),
        }
