from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from ostag.work_monitor.executor import (
    CommandExecutor,
    ControlTask,
    OsstackCliControlPort,
    OsstackTaskReference,
    UnavailableOStagControlPort,
)
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


def command(action: str, lifecycle: str = "running", version: int = 3) -> RemoteCommand:
    return RemoteCommand(
        "cmd-1",
        "task-1",
        "mac",
        action,
        version,
        lifecycle,
        "owner",
        "2026-07-12T00:59:00Z",
        "2026-07-12T01:05:00Z",
    )


def test_signed_command_fields_are_the_only_preconditions() -> None:
    port = FakePort(ControlTask("task-1", "mac", "running", 3), frozenset({"cancel"}), [])
    executor = CommandExecutor("mac", ostag=port, clock=lambda: NOW, sleep=lambda _: None)
    assert executor.execute(command("cancel", version=2))["code"] == "VERSION_MISMATCH"
    port.task = ControlTask("task-1", "mac", "blocked", 3)
    assert executor.execute(command("cancel"))["code"] == "LIFECYCLE_MISMATCH"
    assert port.calls == []


def test_capability_is_rechecked_immediately_before_execution() -> None:
    class VanishingPort(FakePort):
        checks = 0

        def capabilities(self) -> frozenset[str]:
            self.checks += 1
            return frozenset({"cancel"}) if self.checks == 1 else frozenset()

    port = VanishingPort(ControlTask("task-1", "mac", "running", 3), frozenset(), [])
    executor = CommandExecutor("mac", ostag=port, clock=lambda: NOW)
    assert executor.capabilities() == frozenset({"cancel"})
    assert executor.execute(command("cancel"))["code"] == "UNSUPPORTED"
    assert port.calls == []


def test_first_phase_ostag_control_port_explicitly_advertises_no_actions() -> None:
    executor = CommandExecutor("mac", ostag=UnavailableOStagControlPort(), clock=lambda: NOW)
    assert executor.capabilities() == frozenset()
    assert executor.execute(command("cancel"))["code"] == "UNSUPPORTED"


def test_retry_requires_failed_and_verifies_attempt_change() -> None:
    failed = ControlTask("task-1", "mac", "failed", 3, "a1", 2)
    updated = ControlTask("task-1", "mac", "running", 4, "a2", 3)
    port = FakePort(failed, frozenset({"retry"}), [], [failed, updated])
    result = CommandExecutor("mac", osstack=port, clock=lambda: NOW).execute(
        command("retry", "failed")
    )
    assert result["status"] == "running" and port.calls == [("retry", "task-1")]
    stale = FakePort(failed, frozenset({"retry"}), [], [failed, failed])
    assert (
        CommandExecutor("mac", osstack=stale, clock=lambda: NOW).execute(
            command("retry", "failed")
        )["code"]
        == "RETRY_NOT_VERIFIED"
    )


def test_cancel_always_polls_authoritative_cancelled_terminal() -> None:
    running = ControlTask("task-1", "mac", "running", 3)
    cancelled = ControlTask("task-1", "mac", "cancelled", 4, terminal="cancelled")
    port = FakePort(running, frozenset({"cancel"}), [], [running, running, cancelled])
    result = CommandExecutor("mac", osstack=port, clock=lambda: NOW, sleep=lambda _: None).execute(
        command("cancel")
    )
    assert result["status"] == "cancelled"


def test_osstack_081_cli_port_uses_real_safe_argv_and_parses_status_fixture() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[-1] == "version":
            out = "0.8.1\n"
        elif argv[1:3] == ["task", "status"]:
            out = json.dumps(
                {
                    "run_id": "run-7",
                    "task_id": "task-1",
                    "phase": "verify",
                    "outcome": "failed",
                    "task_status": "failed",
                    "active": False,
                }
            )
        elif argv[1:3] == ["cockpit", "retry"]:
            out = json.dumps(
                {"retry_of_task_id": "task-1", "retry_task": {"task_id": "task-1-retry"}}
            )
        else:
            out = "{}"
        return subprocess.CompletedProcess(argv, 0, out, "")

    port = OsstackCliControlPort(
        instance_id="mac",
        binary="/opt/osstack",
        runner=run,
        references={
            "task-1": OsstackTaskReference(
                "run-7", "/work/project", "/work/project/LOOP-STATE.json", 8, "failed", "a8", 8
            )
        },
    )
    assert port.capabilities() == frozenset({"cancel", "retry"})
    assert port.get_task("task-1").attempt_id == "a8"
    assert calls[-1] == ["/opt/osstack", "task", "status", "run-7"]
    port.execute("cancel", "task-1")
    assert calls[-1] == ["/opt/osstack", "task", "cancel", "run-7"]
    port.execute("retry", "task-1")
    assert calls[-1] == [
        "/opt/osstack",
        "cockpit",
        "retry",
        "task-1",
        "--workspace",
        "/work/project",
        "--state",
        "/work/project/LOOP-STATE.json",
        "--json",
    ]
    assert port.get_task("task-1").attempt_id == "task-1-retry"
    assert all(isinstance(call, list) for call in calls)


def test_osstack_cli_nonzero_or_invalid_json_fails_closed() -> None:
    def bad(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "not-json", "no")

    port = OsstackCliControlPort(instance_id="mac", runner=bad)
    assert port.capabilities() == frozenset()


def test_osstack_081_never_advertises_or_maps_pause_resume() -> None:
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "0.8.1\n", "")

    port = OsstackCliControlPort(
        instance_id="mac",
        runner=run,
        references={"task-1": OsstackTaskReference("run-1", "/w", "/s")},
    )
    assert port.capabilities() == frozenset({"cancel", "retry"})
    for action in ("pause", "resume"):
        with pytest.raises(RuntimeError):
            port.execute(action, "task-1")


def test_post_action_status_exception_returns_not_confirmed_without_second_execute() -> None:
    class BrokenAfterAction(FakePort):
        reads = 0

        def get_task(self, task_id: str) -> ControlTask:
            self.reads += 1
            if self.reads > 1:
                raise RuntimeError("daemon lost")
            return self.task

    port = BrokenAfterAction(ControlTask("task-1", "mac", "running", 3), frozenset({"cancel"}), [])
    executor = CommandExecutor("mac", osstack=port, clock=lambda: NOW, sleep=lambda _: None)
    assert executor.execute(command("cancel"))["code"] == "CANCEL_NOT_CONFIRMED"
    assert port.calls == [("cancel", "task-1")]
