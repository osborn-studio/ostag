"""Validated local command execution for OStag and osstack tasks."""

from __future__ import annotations

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
    status: str
    version: int
    attempt_id: str = ""
    attempt_order: int = 0
    terminal: str = ""


class OStagControlPort(Protocol):
    def capabilities(self) -> frozenset[str]: ...
    def get_task(self, task_id: str) -> ControlTask: ...
    def execute(self, action: str, task_id: str) -> None: ...


class OsstackControlPort(OStagControlPort, Protocol):
    """The bridge must call osstack's versioned task/cockpit controls, never aliases."""


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
        self.instance_id = instance_id
        self.ostag = ostag
        self.osstack = osstack
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.poll_attempts = poll_attempts

    def _port(self) -> OStagControlPort | None:
        return self.ostag or self.osstack

    def capabilities(self) -> frozenset[str]:
        port = self._port()
        return frozenset() if port is None else frozenset(port.capabilities()) & SAFE_ACTIONS

    def execute(
        self,
        command: RemoteCommand,
        *,
        expected_version: int | None = None,
        authoritative_status: str | None = None,
    ) -> dict[str, object]:
        port = self._port()
        if port is None or command.action not in self.capabilities():
            return {"ok": False, "code": "UNSUPPORTED", "action": command.action}
        expires = datetime.fromisoformat(command.expires_at.replace("Z", "+00:00"))
        if self.clock() >= expires:
            return {"ok": False, "code": "EXPIRED"}
        task = port.get_task(command.task_id)
        if task.instance_id != self.instance_id or command.instance_id != self.instance_id:
            return {"ok": False, "code": "INSTANCE_MISMATCH"}
        expected_status = authoritative_status or command.expected_status
        if task.status != expected_status:
            if command.action == "retry" and task.status != "failed":
                return {"ok": False, "code": "RETRY_REQUIRES_FAILED"}
            return {"ok": False, "code": "STATUS_MISMATCH"}
        if expected_version is not None and task.version != expected_version:
            return {"ok": False, "code": "VERSION_MISMATCH"}
        if command.action == "retry" and task.status != "failed":
            return {"ok": False, "code": "RETRY_REQUIRES_FAILED"}
        port.execute(command.action, command.task_id)
        if command.action == "retry":
            updated = port.get_task(command.task_id)
            changed = (
                updated.attempt_id != task.attempt_id or updated.attempt_order > task.attempt_order
            )
            if not changed:
                return {"ok": False, "code": "RETRY_NOT_VERIFIED"}
            return {"ok": True, "status": updated.status, "version": updated.version}
        if command.action == "cancel":
            for _ in range(self.poll_attempts):
                updated = port.get_task(command.task_id)
                if updated.terminal == "cancelled":
                    return {"ok": True, "status": "cancelled", "version": updated.version}
                self.sleep(0.2)
            return {"ok": False, "code": "CANCEL_NOT_CONFIRMED"}
        updated = port.get_task(command.task_id)
        return {"ok": True, "status": updated.status, "version": updated.version}
