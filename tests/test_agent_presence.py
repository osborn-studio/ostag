from __future__ import annotations

import pytest

from ostag.work_monitor.presence import (
    AgentPresence,
    derive_agent_presence,
    summarize_agent_presence,
)


def _derive(
    *,
    configured: bool = True,
    freshness: str = "fresh",
    tasks: dict[str, int] | None = None,
    sessions: dict[str, int] | None = None,
    decisions: int = 0,
    escalations: int = 0,
) -> AgentPresence:
    return derive_agent_presence(
        instance_id="instance-1",
        configured=configured,
        freshness_state=freshness,
        task_counts=tasks or {},
        session_counts=sessions or {},
        pending_decisions=decisions,
        active_escalations=escalations,
    )


@pytest.mark.parametrize(
    ("presence", "state", "reason_code"),
    (
        (_derive(configured=False, freshness="unknown"), "setup_required", "instance_missing"),
        (_derive(decisions=1), "needs_attention", "decision_pending"),
        (_derive(freshness="offline"), "offline", "heartbeat_not_fresh"),
        (_derive(tasks={"running": 1}), "working", "active_work"),
        (_derive(tasks={"ready": 1}), "queued", "durable_work_queued"),
        (_derive(), "available", "ready_for_work"),
    ),
)
def test_presence_derives_all_six_states(
    presence: AgentPresence, state: str, reason_code: str
) -> None:
    assert presence.state == state
    assert presence.reason_code == reason_code
    assert presence.to_dict() == {
        "state": state,
        "reasonCode": reason_code,
        "reason": presence.reason,
    }


def test_presence_precedence_is_fail_closed_and_keeps_offline_queue_visible() -> None:
    assert _derive(
        configured=False,
        freshness="offline",
        tasks={"running": 1, "blocked": 1},
        decisions=1,
    ).state == "setup_required"
    assert _derive(
        freshness="offline", tasks={"ready": 1, "blocked": 1}, escalations=1
    ).state == "needs_attention"
    assert _derive(freshness="offline", tasks={"ready": 1}).state == "queued"
    assert _derive(freshness="stale", sessions={"running": 1}).state == "offline"
    assert _derive(tasks={"running": 1, "ready": 1}).state == "working"


@pytest.mark.parametrize(
    "kwargs",
    (
        {"freshness_state": "surprising"},
        {"pending_decisions": -1},
        {"active_escalations": True},
        {"task_counts": {"running": -1}},
        {"session_counts": {"blocked": False}},
    ),
)
def test_presence_rejects_invalid_aggregate_facts(kwargs: dict[str, object]) -> None:
    valid: dict[str, object] = {
        "instance_id": "instance-1",
        "configured": True,
        "freshness_state": "fresh",
        "task_counts": {},
        "session_counts": {},
        "pending_decisions": 0,
        "active_escalations": 0,
    }
    valid.update(kwargs)
    with pytest.raises(ValueError):
        derive_agent_presence(**valid)  # type: ignore[arg-type]


def test_presence_summary_is_deterministic_and_uses_upstream_priority() -> None:
    available = _derive()
    queued = _derive(tasks={"todo": 1})
    attention = _derive(escalations=1)

    summary = summarize_agent_presence((available, queued, attention))

    assert summary.to_dict() == {
        "state": "needs_attention",
        "reasonCode": "aggregate_needs_attention",
        "reason": "3 authorized instances; highest-priority state is needs_attention.",
        "counts": {
            "setup_required": 0,
            "needs_attention": 1,
            "offline": 0,
            "working": 0,
            "queued": 1,
            "available": 1,
        },
    }
    assert summarize_agent_presence(()).to_dict() == {
        "state": "setup_required",
        "reasonCode": "no_authorized_instances",
        "reason": "No authorized OStag instance is configured.",
        "counts": {
            "setup_required": 0,
            "needs_attention": 0,
            "offline": 0,
            "working": 0,
            "queued": 0,
            "available": 0,
        },
    }
