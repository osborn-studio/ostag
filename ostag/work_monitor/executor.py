"""Validated local control ports and command execution."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Protocol

from .models import RemoteCommand

SAFE_ACTIONS = frozenset({"pause", "resume", "retry", "cancel"})


@dataclass(frozen=True)
class ControlTask:
    task_id: str
    instance_id: str
    lifecycle: str
    version: int
    attempt_id: str = ""
    attempt_order: int = 0
    terminal: str = ""

    @property
    def status(self) -> str:  # compatibility for result rendering
        return self.lifecycle


class OStagControlPort(Protocol):
    def capabilities(self) -> frozenset[str]: ...
    def get_task(self, task_id: str) -> ControlTask: ...
    def execute(self, action: str, task_id: str) -> None: ...


class OsstackControlPort(OStagControlPort, Protocol):
    pass


class UnavailableOStagControlPort:
    """Explicit first-phase OStag port: local task controls are not implemented yet."""

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    def get_task(self, task_id: str) -> ControlTask:
        raise RuntimeError("OStag task controls are unavailable")

    def execute(self, action: str, task_id: str) -> None:
        raise RuntimeError("OStag task controls are unavailable")


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class OsstackCliControlPort:
    """Fail-closed argv-only adapter over osstack's authoritative CLI."""

    def __init__(
        self, *, instance_id: str, binary: str = "osstack", runner: Runner | None = None
    ) -> None:
        self.instance_id = instance_id
        self.binary = binary
        self.runner = runner or self._run

    @staticmethod
    def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, text=True, capture_output=True, timeout=20, check=False)  # noqa: S603

    def _json(self, argv: list[str]) -> dict[str, object]:
        try:
            result = self.runner(argv)
            if result.returncode:
                return {}
            payload = json.loads(result.stdout)
            return payload if isinstance(payload, dict) else {}
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return {}

    def capabilities(self) -> frozenset[str]:
        payload = self._json([self.binary, "capabilities", "--json"])
        actions = payload.get("actions", [])
        return (
            frozenset(str(item) for item in actions) & SAFE_ACTIONS
            if isinstance(actions, list)
            else frozenset()
        )

    def get_task(self, task_id: str) -> ControlTask:
        payload = self._json([self.binary, "status", "--task", task_id, "--json"])
        if not payload:
            raise RuntimeError("osstack status unavailable or invalid")
        attempt = payload.get("attempt", {})
        attempt = attempt if isinstance(attempt, dict) else {}
        return ControlTask(
            task_id=str(payload.get("task_id", task_id)),
            instance_id=str(payload.get("instance_id", self.instance_id)),
            lifecycle=str(payload.get("lifecycle", payload.get("status", ""))),
            version=int(payload.get("version", 0)),
            attempt_id=str(attempt.get("id", payload.get("attempt_id", ""))),
            attempt_order=int(attempt.get("order", payload.get("attempt_order", 0))),
            terminal=str(payload.get("terminal", "")),
        )

    def execute(self, action: str, task_id: str) -> None:
        commands = {
            "cancel": [self.binary, "cancel", task_id, "--json"],
            "retry": [self.binary, "cockpit", "retry", task_id, "--json"],
            "pause": [self.binary, "loop", "stop", task_id, "--json"],
        }
        if action not in self.capabilities() or action not in commands:
            raise RuntimeError(f"unsupported osstack action: {action}")
        result = self.runner(commands[action])
        if result.returncode:
            raise RuntimeError(f"osstack {action} failed")


class CommandExecutor:
    def __init__(
        self,
        instance_id: str,
        *,
        ostag: OStagControlPort | None = None,
        osstack: OsstackControlPort | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_attempts: int = 5,
    ) -> None:
        self.instance_id, self.ostag, self.osstack = instance_id, ostag, osstack
        self.clock, self.sleep, self.poll_attempts = (
            clock or (lambda: datetime.now(UTC)),
            sleep,
            poll_attempts,
        )

    def _port(self) -> OStagControlPort | None:
        return self.ostag or self.osstack

    def capabilities(self) -> frozenset[str]:
        port = self._port()
        return frozenset() if port is None else frozenset(port.capabilities()) & SAFE_ACTIONS

    def execute(self, command: RemoteCommand) -> dict[str, object]:
        port = self._port()
        # This query is intentionally repeated here; a rendered capability is stale authority.
        if port is None or command.action not in (frozenset(port.capabilities()) & SAFE_ACTIONS):
            return {"ok": False, "code": "UNSUPPORTED", "action": command.action}
        if self.clock() >= datetime.fromisoformat(command.expires_at.replace("Z", "+00:00")):
            return {"ok": False, "code": "EXPIRED"}
        try:
            task = port.get_task(command.task_id)
        except RuntimeError:
            return {"ok": False, "code": "STATUS_UNAVAILABLE"}
        if task.instance_id != self.instance_id or command.instance_id != self.instance_id:
            return {"ok": False, "code": "INSTANCE_MISMATCH"}
        if task.lifecycle != command.expected_lifecycle:
            return {"ok": False, "code": "LIFECYCLE_MISMATCH"}
        if task.version != command.expected_version:
            return {"ok": False, "code": "VERSION_MISMATCH"}
        if command.action == "retry" and task.lifecycle != "failed":
            return {"ok": False, "code": "RETRY_REQUIRES_FAILED"}
        try:
            port.execute(command.action, command.task_id)
        except RuntimeError:
            return {"ok": False, "code": "EXECUTION_FAILED"}
        if command.action == "retry":
            for _ in range(self.poll_attempts):
                updated = port.get_task(command.task_id)
                if (
                    updated.attempt_id != task.attempt_id
                    or updated.attempt_order > task.attempt_order
                ):
                    return {"ok": True, "status": updated.lifecycle, "version": updated.version}
                self.sleep(0.2)
            return {"ok": False, "code": "RETRY_NOT_VERIFIED"}
        if command.action == "cancel":
            for _ in range(self.poll_attempts):
                updated = port.get_task(command.task_id)
                if updated.lifecycle == "cancelled" or updated.terminal == "cancelled":
                    return {"ok": True, "status": "cancelled", "version": updated.version}
                self.sleep(0.2)
            return {"ok": False, "code": "CANCEL_NOT_CONFIRMED"}
        updated = port.get_task(command.task_id)
        return {"ok": True, "status": updated.lifecycle, "version": updated.version}
