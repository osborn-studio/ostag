"""Ordered, idempotent projection of instance monitor events."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .models import MonitorEvent
from .store import MonitorStore, decode_event, encode_event


@dataclass(frozen=True)
class ApplyResult:
    """Describes what one atomic apply operation changed."""

    stored: bool
    duplicate: bool = False
    buffered: bool = False
    applied_sequences: tuple[int, ...] = ()


class Projector:
    def __init__(self, store: MonitorStore, access_snapshots=None) -> None:
        self.store = store
        # Optional Phase 3 shadow hook (AccessSnapshotStore). Called strictly
        # AFTER the projection transaction commits and wrapped in try/except:
        # it can never change what apply() stores, buffers, or returns, and it
        # sits on no routing path.
        self.access_snapshots = access_snapshots

    def apply(self, event: MonitorEvent) -> ApplyResult:
        """Store and project an event in one SQLite transaction."""
        result, projected = self._apply_txn(event)
        if self.access_snapshots is not None:
            for applied_event in projected:
                if applied_event.kind == "task.created":
                    try:
                        self.access_snapshots.capture_from_task_event(applied_event)
                    except Exception:  # noqa: BLE001 - shadow capture must never break intake
                        pass
        return result

    def _apply_txn(self, event: MonitorEvent) -> tuple[ApplyResult, tuple[MonitorEvent, ...]]:
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO instances(instance_id, last_sequence, last_seen_at) VALUES (?, 0, ?) "
                "ON CONFLICT(instance_id) DO UPDATE SET last_seen_at = "
                "CASE WHEN instances.last_seen_at IS NULL "
                "OR excluded.last_seen_at > instances.last_seen_at "
                "THEN excluded.last_seen_at ELSE instances.last_seen_at END",
                (event.instance_id, event.occurred_at),
            )
            row = connection.execute(
                "SELECT last_sequence FROM instances WHERE instance_id = ?",
                (event.instance_id,),
            ).fetchone()
            assert row is not None
            last_sequence = int(row["last_sequence"])

            if event.sequence <= last_sequence or self._already_stored(connection, event):
                return ApplyResult(stored=False, duplicate=True), ()

            payload = encode_event(event)
            inserted = connection.execute(
                "INSERT INTO events(event_id, instance_id, sequence, kind, occurred_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (
                    event.event_id,
                    event.instance_id,
                    event.sequence,
                    event.kind,
                    event.occurred_at,
                    payload,
                ),
            ).rowcount
            if inserted == 0:
                return ApplyResult(stored=False, duplicate=True), ()

            if event.sequence > last_sequence + 1:
                connection.execute(
                    "INSERT INTO pending_events(instance_id, sequence, event_id, payload) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (event.instance_id, event.sequence, event.event_id, payload),
                )
                return ApplyResult(stored=True, buffered=True), ()

            applied = [event.sequence]
            projected_events = [event]
            self._project(connection, event)
            next_sequence = event.sequence + 1
            while True:
                pending = connection.execute(
                    "SELECT payload FROM pending_events WHERE instance_id = ? AND sequence = ?",
                    (event.instance_id, next_sequence),
                ).fetchone()
                if pending is None:
                    break
                pending_event = decode_event(str(pending["payload"]))
                projected_events.append(pending_event)
                self._project(connection, pending_event)
                connection.execute(
                    "DELETE FROM pending_events WHERE instance_id = ? AND sequence = ?",
                    (event.instance_id, next_sequence),
                )
                applied.append(next_sequence)
                next_sequence += 1

            connection.execute(
                "UPDATE instances SET last_sequence = ? "
                "WHERE instance_id = ? AND last_sequence < ?",
                (applied[-1], event.instance_id, applied[-1]),
            )
            return (
                ApplyResult(stored=True, applied_sequences=tuple(applied)),
                tuple(projected_events),
            )

    @staticmethod
    def _already_stored(connection: sqlite3.Connection, event: MonitorEvent) -> bool:
        row = connection.execute(
            "SELECT 1 FROM events WHERE event_id = ? OR (instance_id = ? AND sequence = ?)",
            (event.event_id, event.instance_id, event.sequence),
        ).fetchone()
        return row is not None

    @staticmethod
    def _project(connection: sqlite3.Connection, event: MonitorEvent) -> None:
        if event.task is None:
            return
        task = event.task
        connection.execute(
            """
            INSERT INTO tasks(
                instance_id, task_id, title, project, status, agent, progress,
                duration_seconds, summary, error_class, evidence_ref, version, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(instance_id, task_id) DO UPDATE SET
                title=excluded.title, project=excluded.project, status=excluded.status,
                agent=excluded.agent, progress=excluded.progress,
                duration_seconds=excluded.duration_seconds, summary=excluded.summary,
                error_class=excluded.error_class, evidence_ref=excluded.evidence_ref,
                version=excluded.version, occurred_at=excluded.occurred_at
            WHERE excluded.version > tasks.version
            """,
            (
                event.instance_id,
                task.task_id,
                task.title,
                task.project,
                task.status,
                task.agent,
                task.progress,
                task.duration_seconds,
                task.summary,
                task.error_class,
                task.evidence_ref,
                task.version,
                event.occurred_at,
            ),
        )
