from __future__ import annotations

import sqlite3

from ostag.work_monitor.models import MonitorEvent, TaskSnapshot
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.store import MonitorStore


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
    } <= tables
    assert any(index[2] for index in indexes)
