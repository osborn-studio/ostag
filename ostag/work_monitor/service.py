"""Authorized work-monitor queries used by every chat adapter."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any

from ostag.core.event import NormalizedEvent
from ostag.core.identity import Principal

from .identity import Destination, IdentityError, IdentityStore
from .rendering import MobileBoardView, TaskDetailView
from .store import MonitorStore


class TaskNotFound(LookupError):
    pass


class AmbiguousTask(LookupError):
    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        super().__init__("task reference is ambiguous")
        self.candidates = candidates


@dataclass(frozen=True)
class MonitorCommand:
    name: str
    argument: str = ""


def parse_monitor_command(text: str) -> MonitorCommand | None:
    cleaned = text.strip()
    aliases = {
        "看板": "board",
        "任务": "task",
        "实例": "instances",
        "订阅": "watch",
        "/board": "board",
        "/task": "task",
        "/instances": "instances",
        "/watch": "watch",
    }
    try:
        parts = shlex.split(cleaned)
    except ValueError:
        return None
    if not parts:
        return None
    name = aliases.get(parts[0].lower()) or aliases.get(parts[0])
    if name is None:
        return None
    return MonitorCommand(name, " ".join(parts[1:]))


class WorkMonitorService:
    def __init__(self, store: MonitorStore, identities: IdentityStore) -> None:
        self.store = store
        self.identities = identities

    def _require(self, principal: Principal, destination: Destination, capability: str) -> None:
        if not self.identities.allows(principal, destination, capability):
            raise IdentityError(f"{capability} permission required")

    def _tasks(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT instance_id,task_id,title,project,status,agent,progress,"
                "duration_seconds,summary,error_class,evidence_ref,version,occurred_at "
                "FROM tasks"
            ).fetchall()
        return [dict(row) for row in rows]

    def _scoped_tasks(self, destination: Destination) -> list[dict[str, Any]]:
        binding = self.identities.binding(destination)
        if binding is None:
            raise IdentityError("destination is not enrolled")
        tasks = self._tasks()
        scope = binding.scope or "all"
        if scope == "all":
            return tasks
        kind, separator, value = scope.partition(":")
        if not separator or kind not in {"project", "instance", "task"} or not value:
            raise IdentityError("invalid destination scope")
        key = {"project": "project", "instance": "instance_id", "task": "task_id"}[kind]
        scoped = [task for task in tasks if str(task[key]) == value]
        if kind == "instance":
            with self.store.connect() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM instances WHERE instance_id=?", (value,)
                ).fetchone()
            if exists is None:
                raise IdentityError("destination scope target does not exist")
            return scoped
        if not scoped:
            raise IdentityError("destination scope target does not exist")
        return scoped

    def _scope(self, destination: Destination) -> tuple[str, str]:
        binding = self.identities.binding(destination)
        if binding is None:
            raise IdentityError("destination is not enrolled")
        scope = binding.scope or "all"
        if scope == "all":
            return "all", ""
        kind, separator, value = scope.partition(":")
        if not separator or kind not in {"project", "instance", "task"} or not value:
            raise IdentityError("invalid destination scope")
        return kind, value

    def board(
        self,
        principal: Principal,
        destination: Destination,
        *,
        page: int = 1,
        page_size: int = 6,
    ) -> MobileBoardView:
        self._require(principal, destination, "read")
        return MobileBoardView.from_tasks(
            self._scoped_tasks(destination), page=page, page_size=page_size
        )

    def task(
        self, principal: Principal, destination: Destination, reference: str
    ) -> TaskDetailView:
        self._require(principal, destination, "read")
        needle = reference.strip().casefold()
        candidates = [
            task
            for task in self._scoped_tasks(destination)
            if needle
            and (
                needle in str(task["task_id"]).casefold() or needle in str(task["title"]).casefold()
            )
        ]
        if not candidates:
            raise TaskNotFound(reference)
        exact = [task for task in candidates if str(task["task_id"]).casefold() == needle]
        if len(exact) == 1:
            candidates = exact
        if len(candidates) > 1:
            raise AmbiguousTask(candidates)
        task = candidates[0]
        return TaskDetailView.from_task(task, capabilities=self._capabilities(task))

    def _capabilities(self, task: dict[str, Any]) -> tuple[str, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM events WHERE instance_id=? AND kind='instance.heartbeat' "
                "ORDER BY sequence DESC",
                (task["instance_id"],),
            ).fetchall()
        for row in rows:
            payload = json.loads(str(row["payload"]))
            actions = payload.get("per_task_capabilities", {}).get(task["task_id"])
            if actions is not None:
                return tuple(str(action) for action in actions)
        return ()

    def instances(self, principal: Principal, destination: Destination) -> list[dict[str, Any]]:
        self._require(principal, destination, "read")
        kind, value = self._scope(destination)
        scoped_tasks = self._scoped_tasks(destination)
        allowed_instances = {task["instance_id"] for task in scoped_tasks}
        if kind == "instance":
            allowed_instances.add(value)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT instance_id,last_sequence,last_seen_at FROM instances ORDER BY instance_id"
            ).fetchall()
        if kind == "all":
            return [dict(row) for row in rows]
        return [dict(row) for row in rows if row["instance_id"] in allowed_instances]

    def watch(
        self,
        principal: Principal,
        destination: Destination,
        *,
        instance_id: str = "",
        task_id: str = "",
    ) -> str:
        self._require(principal, destination, "propose")
        scoped = self._scoped_tasks(destination)
        if task_id and not any(task["task_id"] == task_id for task in scoped):
            raise IdentityError("task is outside destination scope or does not exist")
        if instance_id:
            kind, value = self._scope(destination)
            with self.store.connect() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM instances WHERE instance_id=?", (instance_id,)
                ).fetchone()
            if exists is None:
                raise IdentityError("instance is outside destination scope or does not exist")
            if kind == "all":
                pass
            elif kind == "instance" and instance_id == value:
                pass
            else:
                raise IdentityError("instance is outside destination scope or does not exist")
        self.identities.subscribe(principal, destination, instance_id=instance_id, task_id=task_id)
        target = task_id or instance_id or "全部任务"
        return f"已订阅 {target}"

    def handle_event(self, event: NormalizedEvent) -> str:
        command = parse_monitor_command(event.text)
        if command is None:
            raise ValueError("not a monitor command")
        principal = event.principal
        destination = Destination(event.platform, event.destination_id, event.chat_type)
        if command.name == "board":
            page = int(command.argument or "1")
            return self.board(principal, destination, page=page).to_text()
        if command.name == "task":
            try:
                return self.task(principal, destination, command.argument).to_text()
            except AmbiguousTask as exc:
                choices = "\n".join(
                    f"- {task['task_id']}: {task['title']}" for task in exc.candidates[:8]
                )
                return f"找到多个任务，请发送完整任务 ID：\n{choices}"
            except TaskNotFound:
                return "未找到该任务。"
        if command.name == "instances":
            rows = self.instances(principal, destination)
            return "实例\n" + "\n".join(
                f"- {row['instance_id']} · {row['last_seen_at'] or '未上线'}" for row in rows
            )
        if command.name == "watch":
            return self.watch(principal, destination, task_id=command.argument)
        raise AssertionError(command.name)
