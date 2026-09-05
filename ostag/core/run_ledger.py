"""Durable JSONL run ledger.

Every OS Tag run appends immutable :class:`LedgerEvent` records to a local
JSONL file. The ledger is the audit source of truth: run status can be
reconstructed from it, artifacts can be listed from it, and it can be exported
as stable JSON for humans and agents. All string payload values pass through
secret redaction before they touch disk.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ostag.core.event import NormalizedEvent
from ostag.core.identity import Principal
from ostag.core.safety import redact_secrets
from ostag.core.schemas import (
    ActionReceipt,
    ApprovalDecision,
    Artifact,
    LedgerEvent,
    Run,
    RunActor,
    RunIntent,
    RunSource,
    SchemaError,
    new_id,
)


def _redact_deep(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _redact_deep(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deep(item) for item in value]
    return value


class RunLedger:
    def __init__(self, path: Path, *, clock: Any | None = None) -> None:
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))

    def append(self, event: LedgerEvent) -> LedgerEvent:
        record = event.to_dict()
        record["payload"] = _redact_deep(record.get("payload", {}))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return event

    def record_notification(
        self, delivery_id: str, action: str, payload: dict[str, Any]
    ) -> LedgerEvent:
        """Audit scheduler enqueue/delivery outcomes with normal deep redaction."""
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=f"notification:{delivery_id}",
                event_type=f"notification_{action}",
                payload=payload,
            )
        )

    def open_run(self, event: NormalizedEvent, *, executor_id: str = "") -> Run:
        """Create a Run for an inbound event and record it in the ledger."""
        run = Run(
            run_id=new_id("run"),
            source=RunSource(
                platform=event.platform,
                channel_id=event.chat_id,
                thread_id=event.thread_id or "",
                message_id=event.message_id,
            ),
            actor=RunActor(actor_id=event.sender_id),
            intent=RunIntent(raw_text=event.text),
            status="admitted",
            executor_id=executor_id,
        )
        self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run.run_id,
                event_type="run_created",
                payload={"run": run.to_dict()},
            )
        )
        return run

    def record_status(self, run_id: str, status: str, *, detail: str = "") -> LedgerEvent:
        payload: dict[str, Any] = {"status": status}
        if detail:
            payload["detail"] = detail
        event_type = (
            "run_finished"
            if status in ("succeeded", "failed", "cancelled")
            else "run_status_changed"
        )
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run_id,
                event_type=event_type,
                payload=payload,
            )
        )

    def record_artifact(self, artifact: Artifact) -> LedgerEvent:
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=artifact.run_id,
                event_type="artifact_recorded",
                payload={"artifact": artifact.to_dict()},
            )
        )

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def events(self) -> list[dict[str, Any]]:
        """Return every ledger record, in append order."""
        return self._read_all()

    def export_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return every ledger record for a run, in append order."""
        return [record for record in self._read_all() if record.get("run_id") == run_id]

    def run_status(self, run_id: str) -> dict[str, Any]:
        """Reconstruct the latest run snapshot from ledger events."""
        records = self.export_run(run_id)
        if not records:
            raise SchemaError(
                code="run_not_found",
                message=f"No ledger events found for run_id={run_id!r}",
                recovery_hint=(
                    "Check the run id with 'ostag ledger export' or confirm the ledger path "
                    "(OSTAG_RUN_LEDGER_PATH)."
                ),
            )
        snapshot: dict[str, Any] = {}
        for record in records:
            payload = record.get("payload", {})
            if record.get("event_type") == "run_created" and "run" in payload:
                snapshot = dict(payload["run"])
            elif record.get("event_type") == "context_packet_built":
                packet = payload.get("context_packet", {})
                if packet.get("context_packet_id"):
                    snapshot["context_packet_id"] = packet["context_packet_id"]
            elif "status" in payload:
                snapshot["status"] = payload["status"]
                snapshot["updated_at"] = record.get("created_at", snapshot.get("updated_at"))
                if payload.get("detail"):
                    snapshot["last_detail"] = payload["detail"]
        snapshot["run_id"] = run_id
        snapshot["event_count"] = len(records)
        return snapshot

    def schedule_continuation(
        self,
        run_id: str,
        *,
        due_at: str,
        kind: str,
        reason: str,
        chat_id: str = "",
        platform: str = "",
        action_id: str = "",
    ) -> str:
        """Record a durable scheduled continuation; returns its id."""
        continuation_id = new_id("cont")
        payload: dict[str, Any] = {
            "continuation_id": continuation_id,
            "due_at": due_at,
            "kind": kind,
            "reason": reason,
        }
        if chat_id:
            payload["chat_id"] = chat_id
        if platform:
            payload["platform"] = platform
        if action_id:
            payload["action_id"] = action_id
        self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run_id,
                event_type="continuation_scheduled",
                payload=payload,
            )
        )
        return continuation_id

    def record_continuation_done(
        self, continuation_id: str, run_id: str, *, fired: bool, result: str
    ) -> LedgerEvent:
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run_id,
                event_type="continuation_fired" if fired else "continuation_cancelled",
                payload={"continuation_id": continuation_id, "result": result[:500]},
            )
        )

    def list_continuations(self, *, pending_only: bool = True) -> list[dict[str, Any]]:
        """Continuations in schedule order; pending ones exclude fired/cancelled."""
        scheduled: dict[str, dict[str, Any]] = {}
        done: set[str] = set()
        for record in self._read_all():
            payload = record.get("payload", {})
            continuation_id = payload.get("continuation_id", "")
            if not continuation_id:
                continue
            if record.get("event_type") == "continuation_scheduled":
                scheduled[continuation_id] = dict(payload) | {"run_id": record.get("run_id", "")}
            elif record.get("event_type") in ("continuation_fired", "continuation_cancelled"):
                done.add(continuation_id)
        items = [
            entry
            for continuation_id, entry in scheduled.items()
            if not pending_only or continuation_id not in done
        ]
        items.sort(key=lambda entry: entry.get("due_at", ""))
        return items

    def due_continuations(self, now_iso: str) -> list[dict[str, Any]]:
        return [
            entry
            for entry in self.list_continuations(pending_only=True)
            if entry.get("due_at", "") <= now_iso
        ]

    def get_context_packet(self, run_id: str) -> dict[str, Any]:
        """Latest context packet recorded for a run, including its audit hash."""
        packets = [
            record["payload"]
            for record in self.export_run(run_id)
            if record.get("event_type") == "context_packet_built"
            and isinstance(record.get("payload", {}).get("context_packet"), dict)
        ]
        if not packets:
            raise SchemaError(
                code="no_context_packet",
                message=f"No context packet recorded for run_id={run_id!r}",
                recovery_hint=(
                    "Context packets are recorded for runs that reach the workflow/backend "
                    "stage; check the run with 'ostag ledger export'."
                ),
            )
        return packets[-1]

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return [
            record["payload"]["artifact"]
            for record in self.export_run(run_id)
            if record.get("event_type") == "artifact_recorded"
            and isinstance(record.get("payload", {}).get("artifact"), dict)
        ]

    def record_receipt(
        self,
        receipt: ActionReceipt,
        *,
        chat_id: str = "",
        platform: str = "",
        proposer_id: str = "",
    ) -> LedgerEvent:
        payload: dict[str, Any] = {
            "receipt": receipt.to_dict(),
            "chat_id": chat_id,
            "platform": platform,
        }
        if proposer_id:
            payload["proposer_id"] = proposer_id
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=receipt.run_id,
                event_type="action_proposed",
                payload=payload,
                created_at=self.clock().isoformat(),
            )
        )

    def record_receipt_state(
        self, receipt: ActionReceipt, *, previous_state: str, actor_id: str = "", detail: str = ""
    ) -> LedgerEvent:
        payload: dict[str, Any] = {
            "action_id": receipt.action_id,
            "from_state": previous_state,
            "to_state": receipt.state,
        }
        if actor_id:
            payload["actor_id"] = actor_id
        if detail:
            payload["detail"] = detail
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=receipt.run_id,
                event_type="action_state_changed",
                payload=payload,
                created_at=self.clock().isoformat(),
            )
        )

    def record_approval(self, decision: ApprovalDecision) -> LedgerEvent:
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=decision.run_id or decision.action_id,
                event_type="approval_recorded",
                payload={"decision": decision.to_dict()},
            )
        )

    def receipt_proposer(self, action_id: str) -> str:
        """Actor who proposed the action; empty for pre-tracking receipts."""
        entry = self._fold_receipts().get(action_id)
        return entry.get("proposer_id", "") if entry else ""

    def receipt_for_command(self, command_id: str) -> ActionReceipt:
        """Resolve the receipt bound to an emitted command audit event."""
        action_id = ""
        for record in self._read_all():
            if record.get("event_type") != "action_state_changed":
                continue
            detail = record.get("payload", {}).get("detail", "")
            try:
                parsed = json.loads(detail)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict) and parsed.get("command_id") == command_id:
                action_id = str(record.get("payload", {}).get("action_id", ""))
        if not action_id:
            raise SchemaError(
                "command_not_found",
                f"No receipt is bound to command {command_id!r}",
                "Reject the acknowledgement and poll for an issued command.",
            )
        return self.get_receipt(action_id)

    def list_approvers(self, action_id: str) -> tuple[str, ...]:
        """Distinct actor ids that recorded an 'approve' decision for an action."""
        approvers: list[str] = []
        for record in self._read_all():
            if record.get("event_type") != "approval_recorded":
                continue
            decision = record.get("payload", {}).get("decision", {})
            if (
                decision.get("action_id") == action_id
                and decision.get("decision") == "approve"
                and decision.get("actor_id")
                and decision["actor_id"] not in approvers
            ):
                approvers.append(decision["actor_id"])
        return tuple(approvers)

    def record_cost(
        self,
        run_id: str,
        *,
        chat_id: str,
        platform: str,
        credits: int,
        backend: str,
        date: str,
    ) -> LedgerEvent:
        """Charge a run's estimated cost, attributed to a channel and day."""
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run_id,
                event_type="cost_recorded",
                payload={
                    "chat_id": chat_id,
                    "platform": platform,
                    "credits": int(credits),
                    "backend": backend,
                    "date": date,
                },
            )
        )

    def cost_today(self, chat_id: str, date: str) -> int:
        """Total credits charged to a channel on a given day (UTC date string)."""
        total = 0
        for record in self._read_all():
            if record.get("event_type") != "cost_recorded":
                continue
            payload = record.get("payload", {})
            if payload.get("chat_id") == chat_id and payload.get("date") == date:
                total += int(payload.get("credits", 0))
        return total

    def record_security_event(self, run_id: str, reason: str) -> LedgerEvent:
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run_id,
                event_type="security_event",
                payload={"reason": reason},
            )
        )

    def _fold_receipts(self) -> dict[str, dict[str, Any]]:
        """Reconstruct latest receipt payloads from proposed/state-changed events."""
        entries: dict[str, dict[str, Any]] = {}
        for record in self._read_all():
            event_type = record.get("event_type")
            payload = record.get("payload", {})
            if event_type == "action_proposed" and isinstance(payload.get("receipt"), dict):
                action_id = payload["receipt"].get("action_id")
                if action_id:
                    receipt_payload = dict(payload["receipt"])
                    # Legacy migration is allowed only when the enclosing event
                    # supplies one unambiguous platform + proposer pair. Never
                    # infer a platform from a bare actor id.
                    if "principal" not in receipt_payload:
                        platform = payload.get("platform", "")
                        proposer_id = payload.get("proposer_id", "")
                        if platform and proposer_id:
                            try:
                                receipt_payload["principal"] = Principal(
                                    str(platform), str(proposer_id)
                                ).to_dict()
                            except ValueError:
                                receipt_payload["legacy_identity"] = "unbound"
                        else:
                            receipt_payload["legacy_identity"] = "unbound"
                    previous = entries.get(action_id)
                    if previous is not None:
                        old_principal = previous["receipt"].get("principal")
                        new_principal = receipt_payload.get("principal")
                        if old_principal != new_principal:
                            receipt_payload.pop("principal", None)
                            receipt_payload["legacy_identity"] = "unbound"
                    entries[action_id] = {
                        "receipt": receipt_payload,
                        "chat_id": payload.get("chat_id", ""),
                        "platform": payload.get("platform", ""),
                        "proposer_id": payload.get("proposer_id", ""),
                    }
            elif event_type == "action_state_changed":
                entry = entries.get(payload.get("action_id", ""))
                if entry and payload.get("to_state"):
                    entry["receipt"]["state"] = payload["to_state"]
                    entry["receipt"]["updated_at"] = record.get(
                        "created_at", entry["receipt"].get("updated_at")
                    )
        return entries

    def get_receipt(
        self,
        action_id: str,
        *,
        chat_id: str | None = None,
        platform: str | None = None,
    ) -> ActionReceipt:
        entry = self._fold_receipts().get(action_id)
        if entry is None:
            raise SchemaError(
                code="action_not_found",
                message=f"No receipt found for action_id={action_id!r}",
                recovery_hint=(
                    "List pending receipts with 'ostag receipts list --json' "
                    "or check the run with 'ostag ledger export'."
                ),
            )
        if (chat_id is not None and entry["chat_id"] != chat_id) or (
            platform is not None and entry["platform"] != platform
        ):
            raise SchemaError(
                code="receipt_scope_mismatch",
                message=f"Action {action_id!r} does not belong to this source channel",
                recovery_hint="Run the command in the channel where the receipt was proposed.",
            )
        return ActionReceipt.from_dict(entry["receipt"])

    def list_receipts(
        self,
        *,
        chat_id: str | None = None,
        platform: str | None = None,
        states: tuple[str, ...] | None = None,
    ) -> list[ActionReceipt]:
        """Receipts in proposal order, optionally filtered by chat and states."""
        receipts: list[ActionReceipt] = []
        for entry in self._fold_receipts().values():
            if chat_id is not None and entry["chat_id"] != chat_id:
                continue
            if platform is not None and entry["platform"] != platform:
                continue
            receipt = ActionReceipt.from_dict(entry["receipt"])
            if states is not None and receipt.state not in states:
                continue
            receipts.append(receipt)
        receipts.sort(key=lambda item: item.created_at)
        return receipts

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent run snapshots, newest first."""
        run_ids: list[str] = []
        for record in self._read_all():
            run_id = record.get("run_id")
            if run_id and run_id not in run_ids:
                run_ids.append(run_id)
        return [self.run_status(run_id) for run_id in reversed(run_ids[-limit:])]
