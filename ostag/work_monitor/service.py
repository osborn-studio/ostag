"""Authorized work-monitor queries used by every chat adapter."""

from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
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
    if name is None and "看板" in cleaned and any(
        intent in cleaned for intent in ("汇报", "报告", "展示", "显示", "进展", "状态")
    ):
        return MonitorCommand("board")
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

    def poll_commands(
        self, instance_id: str, *, lease_seconds: int = 30
    ) -> tuple[dict[str, Any], ...]:
        """Claim commands for an already-authenticated instance."""
        claimed_at = self.clock()
        return self.store.claim_pending_commands(
            instance_id,
            claimed_at=claimed_at.isoformat(),
            lease_until=(claimed_at + timedelta(seconds=lease_seconds)).isoformat(),
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

    def _owned_command(
        self, db: sqlite3.Connection, receipt: ActionReceipt, actor_id: str
    ) -> dict[str, Any]:
        """The durable command this exact receipt owns, read under ``db``.

        Every reuse path — a replayed ``applying`` apply and the
        ``ready_to_apply`` recovery of a lost publication alike — must resolve
        the one command whose stored row and frozen payload still agree with
        the receipt binding: action, instance/task target, expected version and
        lifecycle, and the requesting actor. A missing, corrupt, or rebound row
        is never adopted, and a row the instance already acknowledged is no
        longer reusable, so the caller fails closed instead of publishing.

        The caller reads this inside the ``commands`` write transaction that
        :meth:`acknowledge_control` also takes, so the decision cannot race an
        acknowledgement.
        """
        row = db.execute(
            "SELECT command_id,instance_id,task_id,status,payload FROM commands WHERE action_id=?",
            (receipt.action_id,),
        ).fetchone()
        if row is None:
            raise SchemaError(
                "command_not_found",
                f"Action {receipt.action_id} has no reserved command of its own",
                "Propose the control again from the current task state.",
            )
        try:
            stored = json.loads(str(row["payload"]))
        except ValueError:
            stored = None
        command: dict[str, Any] = stored if isinstance(stored, dict) else {}
        payload = receipt.apply_payload
        bindings = {
            "command_id": str(row["command_id"]),
            "instance_id": str(payload.get("instance_id", "")),
            "task_id": str(payload.get("task_id", "")),
            "action": receipt.mutation_type,
            "expected_version": payload.get("expected_version"),
            "expected_lifecycle": payload.get("expected_lifecycle"),
            "requested_by": actor_id,
        }
        owned = (
            bool(command)
            and (str(row["instance_id"]), str(row["task_id"]))
            == (bindings["instance_id"], bindings["task_id"])
            and all(command.get(key) == value for key, value in bindings.items())
            and bool(command.get("issued_at"))
            and bool(command.get("expires_at"))
        )
        if not owned:
            raise SchemaError(
                "command_not_found",
                f"Reserved command for {receipt.action_id} is corrupt or bound elsewhere",
                "Propose the control again from the current task state.",
            )
        if str(row["status"]) != "pending":
            raise SchemaError(
                "command_replay",
                f"Command {row['command_id']} was already acknowledged",
                "Read the acknowledged receipt instead of applying again.",
            )
        return command

    @staticmethod
    def _state_moved(receipt: ActionReceipt, state: str) -> SchemaError:
        return SchemaError(
            "invalid_transition",
            f"Action {receipt.action_id} moved to state {state!r} before this apply",
            f"Read the current receipt state with: status {receipt.run_id}",
        )

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
        actor_id = f"{principal.platform}:{principal.actor_id}"
        if receipt.state == "applying":
            # This exact apply was already published. Hand back durable ledger
            # truth: no second command, no new ledger event, and no answer at
            # all unless this receipt still owns its reserved command and the
            # action has not moved on since it was read.
            with self.store.transaction() as db:
                self._owned_command(db, receipt, actor_id)
                latest = self.ledger.get_receipt(receipt.action_id)
                if latest.state != "applying":
                    raise self._state_moved(receipt, latest.state)
            return latest
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
            "requested_by": actor_id,
            "issued_at": issued.isoformat(),
            "expires_at": (issued + timedelta(minutes=5)).isoformat(),
        }
        # Reserve first and commit: the reservation has to outlive a failed
        # ledger publication so the retry recovers this command instead of
        # issuing a second one.
        with self.store.transaction() as db:
            reserved = db.execute(
                "SELECT 1 FROM commands WHERE action_id=?", (receipt.action_id,)
            ).fetchone()
            if reserved is None:
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
        # Publish while holding the commands write lock, so the reservation is
        # revalidated against acknowledgement state and a delayed caller can
        # never append 'applying' after this action already reached a terminal
        # state, nor publish a second event for an apply another caller landed.
        with self.store.transaction() as db:
            reserved_command = self._owned_command(db, receipt, actor_id)
            latest = self.ledger.get_receipt(receipt.action_id)
            if latest.state == applying.state:
                return latest
            if latest.state != receipt.state:
                raise self._state_moved(receipt, latest.state)
            self.ledger.record_receipt_state(
                applying,
                previous_state=receipt.state,
                actor_id=str(reserved_command["requested_by"]),
                detail=json.dumps(
                    {"command_id": str(reserved_command["command_id"])}, sort_keys=True
                ),
            )
        return applying

    def acknowledge_control(
        self, instance_id: str, command_id: str, result: dict[str, Any]
    ) -> ActionReceipt:
        canonical = self._canonical_ack(result)
        canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        ack_hash = hashlib.sha256(canonical_json.encode()).hexdigest()
        should_sync = False
        enqueued_notifications: list[tuple[str, dict[str, Any]]] = []
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
                destination = receipt.apply_payload.get("destination", {})
                platform = str(destination.get("platform", ""))
                destination_id = str(destination.get("destination_id", ""))
                chat_type = str(destination.get("chat_type", ""))
                recipient = receipt.principal
                notify = bool(
                    platform
                    and destination_id
                    and chat_type in {"private", "group"}
                    and recipient is not None
                    and self.identities.allows(
                        recipient, Destination(platform, destination_id, chat_type), "write"
                    )
                )
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
                if notify:
                    assert recipient is not None
                    event_id = f"control_ack:{command_id}:{ack_hash}"
                    notification_payload = {
                        "kind": "task.control_ack",
                        "task_id": command["task_id"],
                        "action": command["action"],
                        "result": canonical,
                        "recipient": recipient.to_dict(),
                        "recipient_scope": destination.get("scope", ""),
                        "chat_type": chat_type,
                    }
                    subscriptions = db.execute(
                        "SELECT s.rowid,s.destination_platform,s.destination_id "
                        "FROM monitor_subscriptions s JOIN monitor_bindings b ON "
                        "b.platform=s.destination_platform AND b.destination_id=s.destination_id "
                        "WHERE s.revoked=0 AND b.revoked=0 "
                        "AND (s.task_id='' OR s.task_id=?) AND (s.instance_id='' OR s.instance_id=?)",
                        (command["task_id"], instance_id),
                    ).fetchall()
                    targets = [
                        f"{row['destination_platform']}:{row['destination_id']}:{row['rowid']}"
                        for row in subscriptions
                    ] or [f"{platform}:{destination_id}:binding"]
                    for destination_key in targets:
                        payload_to_store = (
                            notification_payload
                            if destination_key.endswith(":binding")
                            else {
                                key: value
                                for key, value in notification_payload.items()
                                if key not in {"recipient", "chat_type"}
                            }
                        )
                        delivery_id = hashlib.sha256(
                            f"{event_id}:{destination_key}".encode()
                        ).hexdigest()[:32]
                        inserted = db.execute(
                            "INSERT OR IGNORE INTO notification_outbox(notification_id,event_id,destination_key,payload,delivery_id,next_attempt_at) "
                            "VALUES (?,?,?,?,?,?)",
                            (
                                f"notification_{delivery_id}",
                                event_id,
                                destination_key,
                                json.dumps(payload_to_store, sort_keys=True),
                                delivery_id,
                                self.clock().isoformat(),
                            ),
                        ).rowcount
                        if inserted:
                            enqueued_notifications.append((delivery_id, payload_to_store))
                should_sync = True
        for enqueued_notification in enqueued_notifications:
            self.ledger.record_notification(
                enqueued_notification[0], "enqueue", {"payload": enqueued_notification[1]}
            )
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
