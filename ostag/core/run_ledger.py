"""Durable JSONL run ledger.

Every OS Tag run appends immutable :class:`LedgerEvent` records to a local
JSONL file. The ledger is the audit source of truth: run status can be
reconstructed from it, artifacts can be listed from it, and it can be exported
as stable JSON for humans and agents. All string payload values pass through
secret redaction before they touch disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ostag.core.event import NormalizedEvent
from ostag.core.safety import redact_secrets
from ostag.core.schemas import (
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
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, event: LedgerEvent) -> LedgerEvent:
        record = event.to_dict()
        record["payload"] = _redact_deep(record.get("payload", {}))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return event

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
        event_type = "run_finished" if status in ("succeeded", "failed", "cancelled") else "run_status_changed"
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
            elif "status" in payload:
                snapshot["status"] = payload["status"]
                snapshot["updated_at"] = record.get("created_at", snapshot.get("updated_at"))
                if payload.get("detail"):
                    snapshot["last_detail"] = payload["detail"]
        snapshot["run_id"] = run_id
        snapshot["event_count"] = len(records)
        return snapshot

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return [
            record["payload"]["artifact"]
            for record in self.export_run(run_id)
            if record.get("event_type") == "artifact_recorded"
            and isinstance(record.get("payload", {}).get("artifact"), dict)
        ]

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent run snapshots, newest first."""
        run_ids: list[str] = []
        for record in self._read_all():
            run_id = record.get("run_id")
            if run_id and run_id not in run_ids:
                run_ids.append(run_id)
        return [self.run_status(run_id) for run_id in reversed(run_ids[-limit:])]
