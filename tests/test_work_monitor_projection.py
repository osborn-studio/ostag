from __future__ import annotations

import sqlite3

import pytest

from ostag.work_monitor.models import MonitorEvent, TaskSnapshot
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.store import MonitorStore
from ostag.work_monitor import store as store_module


def event(
    instance: str,
    sequence: int,
    task_id: str,
    status: str,
    *,
    task_version: int | None = None,
    occurred_at: str | None = None,
) -> MonitorEvent:
    return MonitorEvent(
        event_id=f"{instance}-{sequence}",
        instance_id=instance,
        sequence=sequence,
        kind="task.completed" if status == "done" else "task.progress",
        occurred_at=occurred_at or f"2026-07-12T00:00:0{sequence}Z",
        task=TaskSnapshot(
            task_id=task_id,
            title=f"Task {task_id}",
            project="ostag",
            status=status,
            agent="codex",
            progress=100 if status == "done" else sequence * 10,
            duration_seconds=sequence,
            summary="safe summary",
            error_class=None,
            evidence_ref=None,
            version=sequence if task_version is None else task_version,
        ),
    )


def test_duplicate_instance_sequence_is_applied_once(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    projector = Projector(store)

    first = projector.apply(event("mac", 1, "task-1", "running"))
    duplicate = projector.apply(event("mac", 1, "task-1", "done"))

    assert first.applied_sequences == (1,)
    assert duplicate.duplicate is True
    assert store.get_task("mac", "task-1").status == "running"
    assert store.event_count() == 1


def test_gap_is_buffered_then_consecutive_events_are_drained(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    projector = Projector(store)
    projector.apply(event("mac", 1, "task-1", "todo"))

    gap = projector.apply(event("mac", 3, "task-1", "done"))
    assert gap.buffered is True
    assert store.pending_sequences("mac") == (3,)

    drained = projector.apply(event("mac", 2, "task-1", "running"))

    assert drained.applied_sequences == (2, 3)
    assert store.pending_sequences("mac") == ()
    assert store.last_sequence("mac") == 3
    assert store.get_task("mac", "task-1").status == "done"


def test_event_cannot_mutate_same_task_id_on_another_instance(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    projector = Projector(store)

    projector.apply(event("mac", 1, "shared-task", "running"))
    projector.apply(event("server", 1, "shared-task", "done"))

    assert store.get_task("mac", "shared-task").status == "running"
    assert store.get_task("server", "shared-task").status == "done"


def test_six_stage_counts_and_older_task_version_cannot_overwrite(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    projector = Projector(store)
    statuses = ("triage", "todo", "ready", "running", "blocked", "done")
    for sequence, status in enumerate(statuses, start=1):
        projector.apply(
            event(
                "mac",
                sequence,
                f"task-{sequence}",
                status,
                task_version=7 if sequence == 6 else sequence,
            )
        )

    # A later instance event with a later timestamp may still carry an older task version.
    projector.apply(
        event(
            "mac",
            7,
            "task-6",
            "running",
            task_version=6,
            occurred_at="2026-07-12T00:00:09Z",
        )
    )

    assert store.stage_counts() == {status: 1 for status in statuses}
    assert store.get_task("mac", "task-6").status == "done"


def test_equal_task_version_is_idempotent_even_with_later_timestamp(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    projector = Projector(store)
    projector.apply(event("mac", 1, "task-1", "running", task_version=7))

    projector.apply(
        event(
            "mac",
            2,
            "task-1",
            "done",
            task_version=7,
            occurred_at="2026-07-12T00:00:09Z",
        )
    )

    assert store.get_task("mac", "task-1").status == "running"
    assert store.get_task("mac", "task-1").version == 7


def test_schema_contains_all_monitor_tables_and_unique_instance_sequence(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    with sqlite3.connect(store.path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = connection.execute("PRAGMA index_list(events)").fetchall()

    assert {
        "instances",
        "events",
        "pending_events",
        "tasks",
        "subscriptions",
        "notification_outbox",
        "commands",
        "relay_sessions",
        "relay_messages",
        "relay_decisions",
        "relay_current_tasks",
        "relay_task_proposals",
    } <= tables
    assert any(index[2] for index in indexes)


def test_v1_database_without_task_version_is_upgraded_and_projectable(tmp_path):
    path = tmp_path / "monitor.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE instances (
                instance_id TEXT PRIMARY KEY,
                last_sequence INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT
            );
            CREATE TABLE tasks (
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
                occurred_at TEXT NOT NULL,
                PRIMARY KEY(instance_id, task_id)
            );
            PRAGMA user_version = 1;
            """
        )

    store = MonitorStore(path)
    Projector(store).apply(event("mac", 1, "task-1", "running", task_version=7))

    assert store.get_task("mac", "task-1").version == 7
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert "version" in columns
    assert version == len(store_module.MIGRATIONS)


def test_repeated_initialize_is_idempotent(tmp_path):
    path = tmp_path / "monitor.db"
    MonitorStore(path)
    MonitorStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == len(
            store_module.MIGRATIONS
        )


def test_failed_migration_rolls_back_schema_and_version(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    MonitorStore(path)
    original_version = len(store_module.MIGRATIONS)
    broken = store_module.Migration(
        statements=("CREATE TABLE migration_probe(value TEXT)", "INVALID SQL"),
    )
    monkeypatch.setattr(store_module, "MIGRATIONS", (*store_module.MIGRATIONS, broken))

    with pytest.raises(sqlite3.OperationalError):
        MonitorStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == original_version
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'migration_probe'"
        ).fetchone() is None


def heartbeat(instance: str, sequence: int, occurred_at: str) -> MonitorEvent:
    return MonitorEvent(
        event_id=f"heartbeat-{instance}-{sequence}-{occurred_at}",
        instance_id=instance,
        sequence=sequence,
        kind="instance.heartbeat",
        occurred_at=occurred_at,
        task=None,
    )


def test_gap_and_drain_keep_latest_received_instance_liveness(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    projector = Projector(store)
    projector.apply(heartbeat("mac", 1, "2026-07-12T00:00:01Z"))
    projector.apply(heartbeat("mac", 3, "2026-07-12T00:00:03Z"))

    assert store.last_seen_at("mac") == "2026-07-12T00:00:03Z"

    projector.apply(heartbeat("mac", 2, "2026-07-12T00:00:02Z"))
    assert store.last_sequence("mac") == 3
    assert store.last_seen_at("mac") == "2026-07-12T00:00:03Z"


def test_duplicate_heartbeat_refreshes_liveness_without_moving_cursor(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    projector = Projector(store)
    projector.apply(heartbeat("mac", 1, "2026-07-12T00:00:01Z"))

    result = projector.apply(heartbeat("mac", 1, "2026-07-12T00:00:04Z"))

    assert result.duplicate is True
    assert store.last_sequence("mac") == 1
    assert store.last_seen_at("mac") == "2026-07-12T00:00:04Z"


def test_out_of_order_older_timestamp_never_regresses_liveness(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    projector = Projector(store)
    projector.apply(heartbeat("mac", 1, "2026-07-12T00:00:05Z"))
    projector.apply(heartbeat("mac", 3, "2026-07-12T00:00:03Z"))
    projector.apply(heartbeat("mac", 2, "2026-07-12T00:00:02Z"))

    assert store.last_seen_at("mac") == "2026-07-12T00:00:05Z"
