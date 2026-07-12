"""SQLite persistence for the OStag work monitor."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .models import MonitorEvent, TASK_STATUSES


SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(instance_id, sequence)
);
CREATE TABLE IF NOT EXISTS pending_events (
    instance_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    PRIMARY KEY(instance_id, sequence)
);
CREATE TABLE IF NOT EXISTS tasks (
    instance_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL,
    project TEXT NOT NULL,
    status TEXT NOT NULL,
    agent TEXT NOT NULL,
    progress INTEGER NOT NULL,
    duration_seconds INTEGER NOT NULL,
    summary TEXT NOT NULL,
    error_class TEXT,
    evidence_ref TEXT,
    version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    PRIMARY KEY(instance_id, task_id)
);
CREATE TABLE IF NOT EXISTS subscriptions (
    platform TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    instance_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    delivery_cursor INTEGER NOT NULL DEFAULT 0,
    UNIQUE(platform, destination_id, instance_id, task_id)
);
CREATE TABLE IF NOT EXISTS notification_outbox (
    notification_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    destination_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(event_id, destination_key)
);
CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    consumed_at TEXT
);
"""


@dataclass(frozen=True)
class StoredTask:
    instance_id: str
    task_id: str
    status: str
    version: int


class MonitorStore:
    """Owns schema creation and small read-only monitor queries."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def last_sequence(self, instance_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT last_sequence FROM instances WHERE instance_id = ?", (instance_id,)
            ).fetchone()
        return 0 if row is None else int(row["last_sequence"])

    def pending_sequences(self, instance_id: str) -> tuple[int, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT sequence FROM pending_events WHERE instance_id = ? ORDER BY sequence",
                (instance_id,),
            ).fetchall()
        return tuple(int(row["sequence"]) for row in rows)

    def get_task(self, instance_id: str, task_id: str) -> StoredTask | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT instance_id, task_id, status, version FROM tasks "
                "WHERE instance_id = ? AND task_id = ?",
                (instance_id, task_id),
            ).fetchone()
        return None if row is None else StoredTask(**dict(row))

    def stage_counts(self, instance_id: str | None = None) -> dict[str, int]:
        parameters: tuple[str, ...] = ()
        where = ""
        if instance_id is not None:
            where = " WHERE instance_id = ?"
            parameters = (instance_id,)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT status, COUNT(*) AS count FROM tasks{where} GROUP BY status",  # noqa: S608
                parameters,
            ).fetchall()
        observed = {str(row["status"]): int(row["count"]) for row in rows}
        return {status: observed.get(status, 0) for status in sorted(TASK_STATUSES)}

    def event_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()
        assert row is not None
        return int(row["count"])


def encode_event(event: MonitorEvent) -> str:
    return json.dumps(event.to_dict(), separators=(",", ":"), sort_keys=True)


def decode_event(payload: str) -> MonitorEvent:
    return MonitorEvent.from_dict(json.loads(payload))
