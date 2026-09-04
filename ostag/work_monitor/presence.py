"""Read-only Agent Presence derived from authorized monitor facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal


AgentPresenceState = Literal[
    "setup_required",
    "offline",
    "available",
    "queued",
    "working",
    "needs_attention",
]

PRESENCE_STATES: tuple[AgentPresenceState, ...] = (
    "setup_required",
    "needs_attention",
    "offline",
    "working",
    "queued",
    "available",
)
_FRESHNESS_STATES = frozenset({"fresh", "stale", "offline", "unknown"})
_PRIORITY = {state: len(PRESENCE_STATES) - index for index, state in enumerate(PRESENCE_STATES)}


@dataclass(frozen=True)
class AgentPresence:
    instance_id: str
    state: AgentPresenceState
    reason_code: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "state": self.state,
            "reasonCode": self.reason_code,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AgentPresenceSummary:
    state: AgentPresenceState
    reason_code: str
    reason: str
    counts: dict[AgentPresenceState, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reasonCode": self.reason_code,
            "reason": self.reason,
            "counts": {state: self.counts[state] for state in PRESENCE_STATES},
        }


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _counts(values: Mapping[str, int], field: str) -> dict[str, int]:
    if not isinstance(values, Mapping):
        raise ValueError(f"{field} must be a mapping")
    output: dict[str, int] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field} keys must be non-empty strings")
        output[key.casefold()] = _count(value, f"{field}.{key}")
    return output


def _presence(
    instance_id: str,
    state: AgentPresenceState,
    reason_code: str,
    reason: str,
) -> AgentPresence:
    return AgentPresence(instance_id, state, reason_code, reason)


def derive_agent_presence(
    *,
    instance_id: str,
    configured: bool,
    freshness_state: str,
    task_counts: Mapping[str, int],
    session_counts: Mapping[str, int],
    pending_decisions: int,
    active_escalations: int,
) -> AgentPresence:
    """Derive one time-bounded presence view without changing monitor truth."""
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("instance_id must be a non-empty string")
    if type(configured) is not bool:
        raise ValueError("configured must be a boolean")
    if freshness_state not in _FRESHNESS_STATES:
        raise ValueError("freshness_state is invalid")
    tasks = _counts(task_counts, "task_counts")
    sessions = _counts(session_counts, "session_counts")
    decisions = _count(pending_decisions, "pending_decisions")
    escalations = _count(active_escalations, "active_escalations")

    if not configured:
        return _presence(
            instance_id,
            "setup_required",
            "instance_missing",
            "The authorized OStag instance has no registered monitor projection.",
        )
    if decisions:
        return _presence(
            instance_id,
            "needs_attention",
            "decision_pending",
            "A human decision is pending for this instance.",
        )
    if escalations:
        return _presence(
            instance_id,
            "needs_attention",
            "escalation_active",
            "A human escalation is open for this instance.",
        )
    if tasks.get("blocked", 0) or sessions.get("blocked", 0):
        return _presence(
            instance_id,
            "needs_attention",
            "work_blocked",
            "Projected work is blocked and needs attention.",
        )
    queued = bool(sum(tasks.get(state, 0) for state in ("triage", "todo", "ready")))
    running = bool(tasks.get("running", 0) or sessions.get("running", 0))
    if freshness_state != "fresh" and queued:
        return _presence(
            instance_id,
            "queued",
            "durable_work_queued",
            "Work is durably queued for this instance.",
        )
    if freshness_state != "fresh":
        return _presence(
            instance_id,
            "offline",
            "heartbeat_not_fresh",
            "The instance has no fresh monitor heartbeat.",
        )
    if running:
        return _presence(
            instance_id,
            "working",
            "active_work",
            "Fresh monitor evidence reports active work.",
        )
    if queued:
        return _presence(
            instance_id,
            "queued",
            "durable_work_queued",
            "Work is durably queued for this instance.",
        )
    return _presence(
        instance_id,
        "available",
        "ready_for_work",
        "The instance is fresh and has no active or queued work.",
    )


def summarize_agent_presence(
    agents: Sequence[AgentPresence],
) -> AgentPresenceSummary:
    counts = {state: 0 for state in PRESENCE_STATES}
    for agent in agents:
        if not isinstance(agent, AgentPresence):
            raise ValueError("agents must contain AgentPresence values")
        counts[agent.state] += 1
    if not agents:
        return AgentPresenceSummary(
            state="setup_required",
            reason_code="no_authorized_instances",
            reason="No authorized OStag instance is configured.",
            counts=counts,
        )
    primary = sorted(agents, key=lambda item: (-_PRIORITY[item.state], item.instance_id))[0]
    if len(agents) == 1:
        return AgentPresenceSummary(
            state=primary.state,
            reason_code=primary.reason_code,
            reason=primary.reason,
            counts=counts,
        )
    return AgentPresenceSummary(
        state=primary.state,
        reason_code=f"aggregate_{primary.state}",
        reason=(
            f"{len(agents)} authorized instances; highest-priority state is "
            f"{primary.state}."
        ),
        counts=counts,
    )
