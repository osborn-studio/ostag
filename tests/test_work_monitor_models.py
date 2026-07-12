from dataclasses import FrozenInstanceError

import pytest

from ostag.work_monitor.models import EVENT_KINDS, MonitorEvent, RemoteCommand, TaskSnapshot


def _task_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_id": "task-42",
        "title": "Ship monitor",
        "project": "ostag",
        "status": "blocked",
        "agent": "codex",
        "progress": 35,
        "duration_seconds": 90,
        "summary": "Waiting for cloud credentials",
        "error_class": "external_dependency",
        "evidence_ref": "run:42",
    }
    payload.update(overrides)
    return payload


def test_task_blocked_event_round_trips_and_models_are_frozen() -> None:
    event = MonitorEvent.from_dict(
        {
            "event_id": "evt-42",
            "instance_id": "macbook-pro",
            "sequence": 7,
            "kind": "task.blocked",
            "occurred_at": "2026-07-12T01:00:00Z",
            "task": _task_payload(),
        }
    )

    assert event.kind == "task.blocked"
    assert event.task.status == "blocked"
    assert event.to_dict()["task"] == _task_payload()
    with pytest.raises(FrozenInstanceError):
        event.sequence = 8  # type: ignore[misc]


def test_all_models_are_frozen() -> None:
    task = TaskSnapshot.from_dict(_task_payload())
    command = RemoteCommand.from_dict(
        {
            "command_id": "cmd-1",
            "task_id": "task-42",
            "instance_id": "macbook-pro",
            "action": "pause",
            "expected_status": "running",
            "requested_by": "ou_owner",
            "issued_at": "2026-07-12T01:00:00Z",
            "expires_at": "2026-07-12T01:05:00Z",
        }
    )

    with pytest.raises(FrozenInstanceError):
        task.status = "done"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        command.action = "cancel"  # type: ignore[misc]


def test_monitor_event_rejects_unknown_raw_log_key() -> None:
    with pytest.raises(ValueError, match="unknown keys.*raw_log"):
        MonitorEvent.from_dict(
            {
                "event_id": "evt-42",
                "instance_id": "macbook-pro",
                "sequence": 7,
                "kind": "task.progress",
                "occurred_at": "2026-07-12T01:00:00Z",
                "task": _task_payload(status="running"),
                "raw_log": "Bearer top-secret",
            }
        )


def test_task_snapshot_and_remote_command_reject_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown keys.*source"):
        TaskSnapshot.from_dict({**_task_payload(), "source": "private"})

    with pytest.raises(ValueError, match="unknown keys.*raw_log"):
        RemoteCommand.from_dict(
            {
                "command_id": "cmd-1",
                "task_id": "task-42",
                "instance_id": "macbook-pro",
                "action": "pause",
                "expected_status": "running",
                "requested_by": "ou_owner",
                "issued_at": "2026-07-12T01:00:00Z",
                "expires_at": "2026-07-12T01:05:00Z",
                "raw_log": "private",
            }
        )


def test_remote_command_requires_expires_at() -> None:
    with pytest.raises(ValueError, match="missing keys.*expires_at"):
        RemoteCommand.from_dict(
            {
                "command_id": "cmd-1",
                "task_id": "task-42",
                "instance_id": "macbook-pro",
                "action": "pause",
                "expected_status": "running",
                "requested_by": "ou_owner",
                "issued_at": "2026-07-12T01:00:00Z",
            }
        )


@pytest.mark.parametrize("sequence", [0, -1, True])
def test_event_sequence_must_be_a_positive_integer(sequence: object) -> None:
    with pytest.raises(ValueError, match="sequence"):
        MonitorEvent.from_dict(
            {
                "event_id": "evt-42",
                "instance_id": "macbook-pro",
                "sequence": sequence,
                "kind": "task.progress",
                "occurred_at": "2026-07-12T01:00:00Z",
                "task": _task_payload(status="running"),
            }
        )


def test_task_summary_is_limited_to_500_characters() -> None:
    with pytest.raises(ValueError, match="summary"):
        TaskSnapshot.from_dict(_task_payload(summary="x" * 501))


@pytest.mark.parametrize("status", ["triage", "todo", "ready", "running", "blocked", "done"])
def test_task_and_command_accept_each_kanban_status(status: str) -> None:
    assert TaskSnapshot.from_dict(_task_payload(status=status)).status == status
    command = RemoteCommand.from_dict(
        {
            "command_id": "cmd-1",
            "task_id": "task-42",
            "instance_id": "macbook-pro",
            "action": "pause",
            "expected_status": status,
            "requested_by": "ou_owner",
            "issued_at": "2026-07-12T01:00:00Z",
            "expires_at": "2026-07-12T01:05:00Z",
        }
    )
    assert command.expected_status == status


@pytest.mark.parametrize("status", ["queued", "awaiting_approval", "completed", "failed"])
def test_task_and_command_reject_non_contract_status(status: str) -> None:
    with pytest.raises(ValueError, match="status"):
        TaskSnapshot.from_dict(_task_payload(status=status))
    with pytest.raises(ValueError, match="expected_status"):
        RemoteCommand.from_dict(
            {
                "command_id": "cmd-1",
                "task_id": "task-42",
                "instance_id": "macbook-pro",
                "action": "pause",
                "expected_status": status,
                "requested_by": "ou_owner",
                "issued_at": "2026-07-12T01:00:00Z",
                "expires_at": "2026-07-12T01:05:00Z",
            }
        )


def test_event_kind_contract_is_complete_and_rejects_unknown_kind() -> None:
    expected_kinds = {
        "task.created",
        "task.started",
        "task.progress",
        "task.blocked",
        "task.awaiting_approval",
        "task.completed",
        "task.failed",
        "task.cancelled",
        "instance.heartbeat",
    }
    assert EVENT_KINDS == expected_kinds

    for kind in expected_kinds:
        event = MonitorEvent.from_dict(
            {
                "event_id": f"evt-{kind}",
                "instance_id": "macbook-pro",
                "sequence": 7,
                "kind": kind,
                "occurred_at": "2026-07-12T01:00:00Z",
                "task": None if kind == "instance.heartbeat" else _task_payload(),
            }
        )
        assert event.kind == kind

    with pytest.raises(ValueError, match="kind"):
        MonitorEvent.from_dict(
            {
                "event_id": "evt-42",
                "instance_id": "macbook-pro",
                "sequence": 7,
                "kind": "task.raw_log",
                "occurred_at": "2026-07-12T01:00:00Z",
                "task": _task_payload(status="running"),
            }
        )
