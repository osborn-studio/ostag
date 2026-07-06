"""Audit export: a consolidated, filterable view over the run ledger.

Answers the admin question "what did OS Tag do, for whom, at what cost?" by
folding the append-only ledger into a single bundle — run outcomes, receipt
states, approval decisions, spend, and security events — optionally scoped by
date range, platform, and channel. Everything is already secret-redacted at
ledger-write time, so the export is safe to hand to auditors as-is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ostag.config import Settings
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import prune


def _date_of(iso_timestamp: str) -> str:
    return iso_timestamp[:10]


def _in_range(created_at: str, since: str | None, until: str | None) -> bool:
    day = _date_of(created_at)
    if since and day < since:
        return False
    if until and day > until:
        return False
    return True


class AuditExporter:
    def __init__(self, config: Settings, ledger: RunLedger) -> None:
        self.config = config
        self.ledger = ledger

    def export(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        platform: str | None = None,
        chat_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        events = self.ledger.events()

        # First pass: reconstruct run snapshots and per-run cost in one sweep.
        snapshots: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        cost_by_run: dict[str, int] = {}
        for record in events:
            run_id = record.get("run_id", "")
            event_type = record.get("event_type")
            payload = record.get("payload", {})
            if event_type == "run_created" and isinstance(payload.get("run"), dict):
                snapshots[run_id] = dict(payload["run"])
                order.append(run_id)
            elif event_type == "context_packet_built":
                packet = payload.get("context_packet", {})
                if run_id in snapshots and packet.get("context_packet_id"):
                    snapshots[run_id]["context_packet_id"] = packet["context_packet_id"]
            elif "status" in payload and run_id in snapshots:
                snapshots[run_id]["status"] = payload["status"]
                snapshots[run_id]["updated_at"] = record.get(
                    "created_at", snapshots[run_id].get("updated_at")
                )
            if event_type == "cost_recorded":
                cost_by_run[run_id] = cost_by_run.get(run_id, 0) + int(
                    payload.get("credits", 0)
                )

        # Filter runs by date range, platform, and channel.
        included: set[str] = set()
        included_runs: list[dict[str, Any]] = []
        for run_id in order:
            snapshot = snapshots[run_id]
            if not _in_range(snapshot.get("created_at", ""), since, until):
                continue
            source = snapshot.get("source", {})
            if platform and source.get("platform") != platform:
                continue
            if chat_id and source.get("channel_id") != chat_id:
                continue
            enriched = dict(snapshot)
            enriched["cost_credits"] = cost_by_run.get(run_id, 0)
            included.add(run_id)
            included_runs.append(enriched)

        # Second pass: aggregate events belonging to the included runs.
        runs_by_status: dict[str, int] = {}
        receipt_state: dict[str, str] = {}
        approvals = {"approve": 0, "reject": 0}
        security_events: list[dict[str, Any]] = []
        ambient_decisions = 0
        for record in events:
            run_id = record.get("run_id", "")
            if run_id not in included:
                continue
            event_type = record.get("event_type")
            payload = record.get("payload", {})
            if event_type == "action_proposed" and isinstance(payload.get("receipt"), dict):
                action_id = payload["receipt"].get("action_id", "")
                if action_id:
                    receipt_state[action_id] = payload["receipt"].get("state", "")
            elif event_type == "action_state_changed" and payload.get("action_id"):
                if payload.get("to_state"):
                    receipt_state[payload["action_id"]] = payload["to_state"]
            elif event_type == "approval_recorded":
                decision = payload.get("decision", {}).get("decision")
                if decision in approvals:
                    approvals[decision] += 1
            elif event_type == "security_event":
                security_events.append(
                    {
                        "run_id": run_id,
                        "reason": payload.get("reason", ""),
                        "created_at": record.get("created_at", ""),
                    }
                )
            elif event_type == "ambient_decision":
                ambient_decisions += 1

        for run in included_runs:
            status = run.get("status", "")
            runs_by_status[status] = runs_by_status.get(status, 0) + 1
        receipts_by_state: dict[str, int] = {}
        for state in receipt_state.values():
            receipts_by_state[state] = receipts_by_state.get(state, 0) + 1

        moment = now or datetime.now(UTC)
        return {
            "generated_at": moment.isoformat(timespec="seconds"),
            "filters": prune(
                {
                    "since": since,
                    "until": until,
                    "platform": platform,
                    "chat_id": chat_id,
                }
            ),
            "summary": {
                "runs": len(included_runs),
                "runs_by_status": runs_by_status,
                "cost_credits": sum(cost_by_run.get(run_id, 0) for run_id in included),
                "receipts": len(receipt_state),
                "receipts_by_state": receipts_by_state,
                "approvals": approvals,
                "security_events": len(security_events),
                "ambient_decisions": ambient_decisions,
            },
            "runs": included_runs,
            "security_events": security_events,
        }
