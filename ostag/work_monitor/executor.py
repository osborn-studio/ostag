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


@dataclass(frozen=True)
class OsstackTaskReference:
    run_id: str
    workspace: str
    state_path: str
    version: int = 1
    lifecycle: str = "running"
    attempt_id: str = ""
    attempt_order: int = 0


class OsstackCliControlPort:
    """Fail-closed argv-only adapter over osstack's authoritative CLI."""

    def __init__(
        self,
        *,
        instance_id: str,
        binary: str = "osstack",
        runner: Runner | None = None,
        references: dict[str, OsstackTaskReference] | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.binary = binary
        self.runner = runner or self._run
        self.references = references or {}
        self._verified_attempts: dict[str, tuple[str, int]] = {}

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
        try:
            result = self.runner([self.binary, "version"])
        except (OSError, subprocess.SubprocessError):
            return frozenset()
        if (
            result.returncode
            or not result.stdout.strip().startswith("0.8.1")
            or not self.references
        ):
            return frozenset()
        actions = {"cancel"}
        if any(ref.workspace and ref.state_path for ref in self.references.values()):
            actions.add("retry")
        return frozenset(actions)

    def _reference(self, task_id: str) -> OsstackTaskReference:
        try:
            return self.references[task_id]
        except KeyError as exc:
            raise RuntimeError(f"missing osstack task reference: {task_id}") from exc

    def get_task(self, task_id: str) -> ControlTask:
        reference = self._reference(task_id)
        payload = self._json([self.binary, "task", "status", reference.run_id])
        if not payload:
            raise RuntimeError("osstack status unavailable or invalid")
        attempt = payload.get("attempt", {})
        attempt = attempt if isinstance(attempt, dict) else {}
        cached_attempt = self._verified_attempts.get(
            task_id, (reference.attempt_id, reference.attempt_order)
        )
        task_status = str(payload.get("task_status", ""))
        lifecycle = str(payload.get("lifecycle", payload.get("status", "")))
        if not lifecycle:
            lifecycle = task_status or reference.lifecycle
        if payload.get("active") is True:
            lifecycle = "running"
        elif str(payload.get("outcome", "")) in {"failed", "cancelled"}:
            lifecycle = str(payload["outcome"])
        return ControlTask(
            task_id=str(payload.get("task_id", task_id)),
            instance_id=str(payload.get("instance_id", self.instance_id)),
            lifecycle=lifecycle,
            version=int(payload.get("version", reference.version)),
            attempt_id=str(attempt.get("id", payload.get("attempt_id", cached_attempt[0]))),
            attempt_order=int(
                attempt.get("order", payload.get("attempt_order", cached_attempt[1]))
            ),
            terminal=str(payload.get("terminal", "")),
        )

    def execute(self, action: str, task_id: str) -> None:
        reference = self._reference(task_id)
        commands = {
            "cancel": [self.binary, "task", "cancel", reference.run_id],
            "retry": [
                self.binary,
                "cockpit",
                "retry",
                task_id,
                "--workspace",
                reference.workspace,
                "--state",
                reference.state_path,
                "--json",
            ],
        }
        if action not in self.capabilities() or action not in commands:
            raise RuntimeError(f"unsupported osstack action: {action}")
        result = self.runner(commands[action])
        if result.returncode:
            raise RuntimeError(f"osstack {action} failed")
        if action == "retry":
            try:
                payload = json.loads(result.stdout)
                retry_task = payload["retry_task"]
                retry_id = str(retry_task["task_id"])
            except (json.JSONDecodeError, KeyError, TypeError):
                raise RuntimeError("osstack retry result was not verifiable") from None
            current = self._verified_attempts.get(
                task_id, (reference.attempt_id, reference.attempt_order)
            )
            self._verified_attempts[task_id] = (retry_id, current[1] + 1)


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
        try:
            return frozenset() if port is None else frozenset(port.capabilities()) & SAFE_ACTIONS
        except (RuntimeError, OSError, ValueError):
            return frozenset()

    def execute(self, command: RemoteCommand) -> dict[str, object]:
        port = self._port()
        # This query is intentionally repeated here; a rendered capability is stale authority.
        try:
            supported = (
                frozenset() if port is None else frozenset(port.capabilities()) & SAFE_ACTIONS
            )
        except (RuntimeError, OSError, ValueError):
            supported = frozenset()
        if port is None or command.action not in supported:
            return {"ok": False, "code": "UNSUPPORTED", "action": command.action}
        if self.clock() >= datetime.fromisoformat(command.expires_at.replace("Z", "+00:00")):
            return {"ok": False, "code": "EXPIRED"}
        try:
            task = port.get_task(command.task_id)
        except (RuntimeError, OSError, ValueError):
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
                try:
                    updated = port.get_task(command.task_id)
                except (RuntimeError, OSError, ValueError):
                    return {"ok": False, "code": "RETRY_NOT_CONFIRMED"}
                if (
                    updated.attempt_id != task.attempt_id
                    or updated.attempt_order > task.attempt_order
                ):
                    return {"ok": True, "status": updated.lifecycle, "version": updated.version}
                self.sleep(0.2)
            return {"ok": False, "code": "RETRY_NOT_VERIFIED"}
        if command.action == "cancel":
            for _ in range(self.poll_attempts):
                try:
                    updated = port.get_task(command.task_id)
                except (RuntimeError, OSError, ValueError):
                    return {"ok": False, "code": "CANCEL_NOT_CONFIRMED"}
                if updated.lifecycle == "cancelled" or updated.terminal == "cancelled":
                    return {"ok": True, "status": "cancelled", "version": updated.version}
                self.sleep(0.2)
            return {"ok": False, "code": "CANCEL_NOT_CONFIRMED"}
        try:
            updated = port.get_task(command.task_id)
        except (RuntimeError, OSError, ValueError):
            return {"ok": False, "code": "STATUS_UNAVAILABLE"}
        return {"ok": True, "status": updated.lifecycle, "version": updated.version}
