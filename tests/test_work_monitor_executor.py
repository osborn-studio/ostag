from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ostag.work_monitor.executor import CommandExecutor, ControlTask
from ostag.work_monitor.models import RemoteCommand


NOW = datetime(2026, 7, 12, 1, 0, tzinfo=UTC)


@dataclass
class FakePort:
    task: ControlTask
    actions: frozenset[str]
    calls: list[tuple[str, str]]
    statuses: list[ControlTask] | None = None

    def capabilities(self) -> frozenset[str]:
        return self.actions

    def get_task(self, task_id: str) -> ControlTask:
        return self.statuses.pop(0) if self.statuses else self.task

    def execute(self, action: str, task_id: str) -> None:
        self.calls.append((action, task_id))


def _command(action: str, status: str = "running") -> RemoteCommand:
    return RemoteCommand(
        "cmd-1",
        "task-1",
        "mac",
        action,
        status,
        "owner",
        "2026-07-12T00:59:00Z",
        "2026-07-12T01:05:00Z",
    )


def test_capabilities_are_negotiated_and_pause_resume_unsupported() -> None:
    port = FakePort(ControlTask("task-1", "mac", "running", 3, "a1"), frozenset({"cancel"}), [])
    executor = CommandExecutor("mac", ostag=port, clock=lambda: NOW)
    assert executor.capabilities() == frozenset({"cancel"})
    result = executor.execute(_command("pause"))
    assert result["code"] == "UNSUPPORTED" and port.calls == []


def test_instance_status_version_and_expiry_mismatch_never_execute() -> None:
    port = FakePort(ControlTask("task-1", "other", "running", 3, "a1"), frozenset({"cancel"}), [])
    executor = CommandExecutor("mac", ostag=port, clock=lambda: NOW)
    assert executor.execute(_command("cancel"))["code"] == "INSTANCE_MISMATCH"
    port.task = ControlTask("task-1", "mac", "blocked", 3, "a1")
    assert executor.execute(_command("cancel"))["code"] == "STATUS_MISMATCH"
    port.task = ControlTask("task-1", "mac", "running", 3, "a1")
    assert executor.execute(_command("cancel"), expected_version=2)["code"] == "VERSION_MISMATCH"
    expired = RemoteCommand(
        "c2",
        "task-1",
        "mac",
        "cancel",
        "running",
        "u",
        "2026-07-12T00:00:00Z",
        "2026-07-12T00:30:00Z",
    )
    assert executor.execute(expired)["code"] == "EXPIRED"
    assert port.calls == []


def test_osstack_retry_only_failed_and_verifies_new_or_reordered_attempt() -> None:
    initial = ControlTask("task-1", "mac", "failed", 4, "attempt-1", attempt_order=2)
    updated = ControlTask("task-1", "mac", "running", 5, "attempt-2", attempt_order=3)
    port = FakePort(initial, frozenset({"retry"}), [], statuses=[initial, updated])
    executor = CommandExecutor("mac", osstack=port, clock=lambda: NOW)
    result = executor.execute(
        _command("retry", "blocked"), authoritative_status="failed", expected_version=4
    )
    assert result["status"] == "running" and port.calls == [("retry", "task-1")]

    not_failed = FakePort(
        ControlTask("task-1", "mac", "running", 4, "a", 2), frozenset({"retry"}), []
    )
    result = CommandExecutor("mac", osstack=not_failed, clock=lambda: NOW).execute(
        _command("retry")
    )
    assert result["code"] == "RETRY_REQUIRES_FAILED" and not_failed.calls == []


def test_retry_rejects_unverified_attempt_and_cancel_polls_authoritative_terminal() -> None:
    failed = ControlTask("task-1", "mac", "failed", 4, "a1", 2)
    stale = ControlTask("task-1", "mac", "failed", 4, "a1", 2)
    retry_port = FakePort(failed, frozenset({"retry"}), [], statuses=[failed, stale])
    result = CommandExecutor("mac", osstack=retry_port, clock=lambda: NOW).execute(
        _command("retry", "blocked"), authoritative_status="failed", expected_version=4
    )
    assert result["code"] == "RETRY_NOT_VERIFIED"

    running = ControlTask("task-1", "mac", "running", 4, "a1")
    cancelled = ControlTask("task-1", "mac", "done", 5, "a1", terminal="cancelled")
    cancel_port = FakePort(
        running, frozenset({"cancel"}), [], statuses=[running, running, cancelled]
    )
    result = CommandExecutor(
        "mac", osstack=cancel_port, clock=lambda: NOW, sleep=lambda _: None
    ).execute(_command("cancel"))
    assert result["status"] == "cancelled" and cancel_port.calls == [("cancel", "task-1")]


def test_forbidden_aliases_are_never_advertised_or_called() -> None:
    port = FakePort(
        ControlTask("task-1", "mac", "running", 1, "a"),
        frozenset({"kanban.move", "daemon.resume"}),
        [],
    )
    executor = CommandExecutor("mac", osstack=port, clock=lambda: NOW)
    assert executor.capabilities() == frozenset()
    assert executor.execute(_command("resume"))["code"] == "UNSUPPORTED"
    assert port.calls == []
