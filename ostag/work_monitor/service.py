"""Authorized work-monitor queries used by every chat adapter."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ostag.core.event import NormalizedEvent
from ostag.core.identity import Principal
from ostag.core.receipts import ensure_fresh_confirmation, transition_receipt
from ostag.core.run_ledger import RunLedger
from ostag.core.safety import redact_secrets
from ostag.core.schemas import ActionReceipt, SchemaError, new_id

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
    def __init__(
        self,
        store: MonitorStore,
        identities: IdentityStore,
        *,
        ledger: RunLedger | None = None,
        clock: Any | None = None,
    ) -> None:
        self.store = store
        self.identities = identities
        self.ledger = ledger or RunLedger(store.path.with_name("run_ledger.jsonl"))
        self.clock = clock or (lambda: datetime.now(UTC))
        self.ledger.clock = self.clock

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

    def poll_commands(self, instance_id: str) -> tuple[dict[str, Any], ...]:
        """Claim commands for an already-authenticated instance."""
        return self.store.claim_pending_commands(
            instance_id, claimed_at=self.clock().isoformat()
        )

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

    def _control_task(
        self, principal: Principal, destination: Destination, task_id: str
    ) -> dict[str, Any]:
        self._require(principal, destination, "write")
        matches = [task for task in self._scoped_tasks(destination) if task["task_id"] == task_id]
        if len(matches) != 1:
            raise TaskNotFound(task_id)
        return matches[0]

    def propose_control(
        self,
        principal: Principal,
        destination: Destination,
        *,
        task_id: str,
        action: str,
    ) -> ActionReceipt:
        if action not in {"pause", "resume", "retry", "cancel"}:
            raise SchemaError(
                "unsupported_control",
                f"Unsupported control {action!r}",
                "Use pause, resume, retry, or cancel.",
            )
        if destination.chat_type != "private" and action == "cancel":
            raise IdentityError("cancel controls require a private destination")
        task = self._control_task(principal, destination, task_id)
        if action not in self._capabilities(task):
            raise SchemaError(
                "unsupported_control",
                f"Task does not advertise {action}",
                "Refresh task capabilities before retrying.",
            )
        now = self.clock().isoformat()
        receipt = ActionReceipt(
            action_id=new_id("action"),
            run_id=f"run_monitor_{new_id('ctl').removeprefix('ctl_')}",
            title=f"{action.title()} {task['title']}",
            target_system="work_monitor",
            target_object=f"{task['instance_id']}/{task_id}",
            mutation_type=action,
            principal=principal,
            summary=f"{action} task {task_id} on instance {task['instance_id']}",
            risk="high" if action == "cancel" else "medium",
            required_permissions=("write",),
            state="needs_approval",
            apply_payload={
                "instance_id": task["instance_id"],
                "task_id": task_id,
                "expected_version": task["version"],
                "expected_lifecycle": task["status"],
                "destination": {
                    "platform": destination.platform,
                    "destination_id": destination.destination_id,
                    "chat_type": destination.chat_type,
                    "scope": ":".join(part for part in self._scope(destination) if part),
                },
            },
            created_at=now,
            updated_at=now,
        )
        self.ledger.record_receipt(
            receipt,
            chat_id=destination.destination_id,
            platform=principal.platform,
            proposer_id=principal.actor_id,
        )
        return receipt

    def _bound_receipt(
        self, principal: Principal, destination: Destination, action_id: str
    ) -> ActionReceipt:
        self._require(principal, destination, "write")
        receipt = self.ledger.get_receipt(action_id)
        if receipt.principal is None:
            raise IdentityError("legacy receipt principal is unbound")
        if receipt.principal != principal:
            raise IdentityError("receipt principal does not match requester principal")
        expected_destination = {
            "platform": destination.platform,
            "destination_id": destination.destination_id,
            "chat_type": destination.chat_type,
            "scope": ":".join(part for part in self._scope(destination) if part),
        }
        if receipt.apply_payload.get("destination") != expected_destination:
            raise IdentityError(
                "receipt destination is unbound or does not match requester destination"
            )
        ensure_fresh_confirmation(receipt, now=self.clock())
        return receipt

    @staticmethod
    def _canonical_ack(result: dict[str, Any]) -> dict[str, bool | str | int]:
        allowed = {"ok", "status", "code", "version"}
        if not isinstance(result, dict) or not set(result) <= allowed or "ok" not in result:
            raise SchemaError(
                "invalid_ack",
                "ACK contains unsupported fields",
                "Send only ok, status, code, and version scalars.",
            )
        if type(result["ok"]) is not bool:
            raise SchemaError("invalid_ack", "ACK ok must be a boolean", "Send a JSON boolean.")
        canonical: dict[str, bool | str | int] = {"ok": result["ok"]}
        for key in ("status", "code"):
            value = result.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value or len(value) > 64:
                raise SchemaError(
                    "invalid_ack",
                    f"ACK {key} must be a 1-64 character string",
                    "Send a bounded scalar string.",
                )
            canonical[key] = redact_secrets(value)
        if "version" in result:
            version = result["version"]
            if type(version) is not int or version < 0:
                raise SchemaError(
                    "invalid_ack",
                    "ACK version must be a non-negative integer",
                    "Send an integer version, never a boolean or container.",
                )
            canonical["version"] = version
        return canonical

    def approve_control(
        self, principal: Principal, destination: Destination, action_id: str
    ) -> ActionReceipt:
        receipt = self._bound_receipt(principal, destination, action_id)
        approved = transition_receipt(receipt, "approve")
        self.ledger.record_receipt_state(
            approved,
            previous_state=receipt.state,
            actor_id=f"{principal.platform}:{principal.actor_id}",
        )
        return approved

    def apply_control(
        self, principal: Principal, destination: Destination, action_id: str
    ) -> ActionReceipt:
        receipt = self._bound_receipt(principal, destination, action_id)
        payload = receipt.apply_payload
        task = self.store.get_task(
            str(payload.get("instance_id", "")), str(payload.get("task_id", ""))
        )
        if (
            task is None
            or task.version != payload.get("expected_version")
            or task.status != payload.get("expected_lifecycle")
        ):
            raise SchemaError(
                "stale_control",
                "Task state changed after proposal",
                "Propose the control again from the current task state.",
            )
        applying = transition_receipt(receipt, "apply")
        issued = self.clock()
        command_id = new_id("cmd")
        command = {
            "command_id": command_id,
            "task_id": task.task_id,
            "instance_id": task.instance_id,
            "action": receipt.mutation_type,
            "expected_version": task.version,
            "expected_lifecycle": task.status,
            "requested_by": f"{principal.platform}:{principal.actor_id}",
            "issued_at": issued.isoformat(),
            "expires_at": (issued + timedelta(minutes=5)).isoformat(),
        }
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT command_id,payload FROM commands WHERE action_id=?", (receipt.action_id,)
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO commands(command_id,instance_id,task_id,status,payload,action_id) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        command_id,
                        task.instance_id,
                        task.task_id,
                        "pending",
                        json.dumps(command, sort_keys=True),
                        receipt.action_id,
                    ),
                )
            else:
                command_id = str(existing["command_id"])
                command = json.loads(str(existing["payload"]))
        self.ledger.record_receipt_state(
            applying,
            previous_state=receipt.state,
            actor_id=command["requested_by"],
            detail=json.dumps({"command_id": command_id}, sort_keys=True),
        )
        return applying

    def acknowledge_control(
        self, instance_id: str, command_id: str, result: dict[str, Any]
    ) -> ActionReceipt:
        canonical = self._canonical_ack(result)
        canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        ack_hash = hashlib.sha256(canonical_json.encode()).hexdigest()
        should_sync = False
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT status,payload,action_id,ack_payload,ack_hash,ledger_synced "
                "FROM commands WHERE command_id=? AND instance_id=?",
                (command_id, instance_id),
            ).fetchone()
            if row is None:
                raise SchemaError(
                    "command_not_found",
                    "Unknown or cross-instance command",
                    "Poll commands for the authenticated instance.",
                )
            command = json.loads(row["payload"])
            receipt = self.ledger.get_receipt(str(row["action_id"]))
            if row["status"] == "acked":
                if row["ack_hash"] != ack_hash:
                    raise SchemaError(
                        "conflicting_ack",
                        "ACK payload conflicts with the persisted result",
                        "Retry only the equivalent canonical ACK.",
                    )
                persisted = json.loads(str(row["ack_payload"]))
                terminal = receipt.with_state(str(persisted["terminal_state"]))
                should_sync = not bool(row["ledger_synced"])
            elif row["status"] != "pending":
                raise SchemaError(
                    "command_replay",
                    "Command is not acknowledgeable",
                    "Poll current command state.",
                )
            else:
                statuses = {
                    "pause": {"paused"},
                    "resume": {"running", "resumed"},
                    "retry": {"queued", "running", "retried"},
                    "cancel": {"cancelled"},
                }
                version = canonical.get("version")
                success = (
                    canonical["ok"] is True
                    and str(canonical.get("code", "")).upper()
                    in {"OK", "APPLIED", "SUCCESS", "EXECUTED"}
                    and str(canonical.get("status", "")).lower()
                    in statuses.get(str(command["action"]), set())
                    and type(version) is int
                    and version >= int(command["expected_version"])
                )
                terminal = receipt.with_state("applied" if success else "failed")
                persisted = {"terminal_state": terminal.state, "result": canonical}
                db.execute(
                    "UPDATE commands SET status='acked',consumed_at=?,ack_payload=?,ack_hash=? "
                    "WHERE command_id=? AND status='pending'",
                    (
                        self.clock().isoformat(),
                        json.dumps(persisted, sort_keys=True),
                        ack_hash,
                        command_id,
                    ),
                )
                db.execute(
                    "INSERT OR IGNORE INTO notification_outbox(notification_id,event_id,destination_key,payload) "
                    "VALUES (?,?,?,?)",
                    (
                        new_id("notification"),
                        command_id,
                        "control",
                        json.dumps(
                            {
                                "kind": "task.control_ack",
                                "task_id": command["task_id"],
                                "action": command["action"],
                                "result": canonical,
                            },
                            sort_keys=True,
                        ),
                    ),
                )
                should_sync = True
        if should_sync:
            self.ledger.record_receipt_state(
                terminal, previous_state=receipt.state, detail=canonical_json
            )
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE commands SET ledger_synced=1 WHERE command_id=? AND ack_hash=?",
                    (command_id, ack_hash),
                )
        return terminal

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
