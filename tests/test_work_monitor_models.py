from dataclasses import FrozenInstanceError

import pytest

from ostag.work_monitor.models import MonitorEvent, RemoteCommand, TaskSnapshot


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
