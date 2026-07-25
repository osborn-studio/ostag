import sqlite3

import pytest

from ostag.copilot_relay.models import RelayEnvelope
from ostag.copilot_relay.store import RelayStore, StaleSessionError
from ostag.work_monitor.store import MonitorStore


def _envelope(
    kind: str,
    message_id: str,
    *,
    session_id: str = "sess_1",
    version: int = 1,
    turn_id: str = "turn_1",
    instance_id: str = "macmini",
    runtime_id: str = "legacy",
) -> RelayEnvelope:
    payloads: dict[str, dict[str, object]] = {
        "session.upsert": {
            "agent_id": "codex-cli",
            "title": "Relay implementation",
            "state": "running",
            "resume_supported": True,
            "latest_summary": "Implementing persistence",
        },
        "instruction.send": {"text": "继续"},
        "decision.request": {
            "decision_id": "decision_1",
            "question": "采用哪个方案？",
            "choices": [{"choice_id": "a", "label": "方案 A"}],
            "recommended_choice_id": "a",
            "blocking": True,
            "safe_work": [],
        },
        "decision.answer": {
            "decision_id": "decision_1",
            "choice_id": "a",
            "answer": "采用方案 A",
        },
        "task.create.proposal": {
            "proposal_id": "proposal_1",
            "objective": "Add relay UI",
            "repository": "ostag",
            "workspace": "relay-worktree",
            "suggested_agent": "codex-cli",
            "acceptance_criteria": ["tests pass"],
            "sensitive_operations": [],
        },
        "task.create.confirm": {"proposal_id": "proposal_1", "confirmed": True},
    }
    return RelayEnvelope.from_dict(
        {
            "schema_version": 1 if runtime_id == "legacy" else 2,
            "message_id": message_id,
            "binding": {
                "instance_id": instance_id,
                **({} if runtime_id == "legacy" else {"runtime_id": runtime_id}),
                "task_id": f"task_{session_id}",
                "run_id": f"run_{session_id}",
                "session_id": session_id,
                "turn_id": turn_id,
                "session_version": version,
            },
            "sequence": version,
            "created_at": "2026-07-14T01:00:00Z",
            "expires_at": "2026-07-14T02:00:00Z",
            "sender": {"platform": "feishu", "actor_id": "ou_owner"},
            "kind": kind,
            "payload": payloads[kind],
            "correlation_id": None,
            "causation_id": None,
        }
    )


def test_relay_schema_is_owned_by_monitor_store_and_restart_safe(tmp_path) -> None:
    path = tmp_path / "monitor.db"
    MonitorStore(path)
    MonitorStore(path)

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "relay_sessions",
        "relay_messages",
        "relay_decisions",
        "relay_current_tasks",
        "relay_task_proposals",
    } <= tables


def test_duplicate_ingest_is_idempotent_and_quote_resolves_exact_binding(tmp_path) -> None:
    relay = RelayStore(MonitorStore(tmp_path / "monitor.db"))
    envelope = _envelope("instruction.send", "msg_1")

    assert relay.enqueue(envelope, direction="outbound") is True
    assert relay.enqueue(envelope, direction="outbound") is False
    assert relay.message_count() == 1
    assert relay.binding_for_message("msg_1") == envelope.binding


def test_two_drainers_are_exclusive_and_expired_lease_is_reclaimed(tmp_path) -> None:
    relay = RelayStore(MonitorStore(tmp_path / "monitor.db"))
    relay.enqueue(_envelope("instruction.send", "msg_lease"), direction="outbound")

    first = relay.claim_messages(
        direction="outbound",
        owner="worker_1",
        now="2026-07-14T01:00:01Z",
        lease_until="2026-07-14T01:00:05Z",
    )
    blocked = relay.claim_messages(
        direction="outbound",
        owner="worker_2",
        now="2026-07-14T01:00:02Z",
        lease_until="2026-07-14T01:00:06Z",
    )
    reclaimed = relay.claim_messages(
        direction="outbound",
        owner="worker_2",
        now="2026-07-14T01:00:06Z",
        lease_until="2026-07-14T01:00:10Z",
    )

    assert [(item.message_id, item.attempts) for item in first] == [("msg_lease", 1)]
    assert blocked == ()
    assert [(item.message_id, item.attempts) for item in reclaimed] == [("msg_lease", 2)]
    assert relay.mark_delivered(
        "msg_lease", owner="worker_1", delivered_at="2026-07-14T01:00:07Z"
    ) is False
    assert relay.mark_delivered(
        "msg_lease", owner="worker_2", delivered_at="2026-07-14T01:00:07Z"
    ) is True


def test_session_upsert_uses_monotonic_compare_and_swap(tmp_path) -> None:
    relay = RelayStore(MonitorStore(tmp_path / "monitor.db"))

    assert relay.upsert_session(_envelope("session.upsert", "msg_s7", version=7)) is True
    assert relay.upsert_session(_envelope("session.upsert", "msg_s7_dup", version=7)) is False
    conflicting = _envelope("session.upsert", "msg_s7_conflict", version=7).to_dict()
    conflicting["payload"]["title"] = "Conflicting title"
    with pytest.raises(StaleSessionError, match="conflicting"):
        relay.upsert_session(RelayEnvelope.from_dict(conflicting))
    with pytest.raises(StaleSessionError):
        relay.upsert_session(_envelope("session.upsert", "msg_s6", version=6))
    assert relay.get_session("macmini", "sess_1").session_version == 7


def test_same_session_id_is_isolated_by_device_and_runtime(tmp_path) -> None:
    relay = RelayStore(MonitorStore(tmp_path / "monitor.db"))
    macbook = _envelope(
        "session.upsert", "msg_macbook", instance_id="device_macbook",
        runtime_id="codex-cli",
    )
    macmini = _envelope(
        "session.upsert", "msg_macmini", instance_id="device_macmini",
        runtime_id="claude-code",
    )

    assert relay.upsert_session(macbook) is True
    assert relay.upsert_session(macmini) is True
    sessions = relay.list_sessions()
    assert {(item.instance_id, item.runtime_id, item.session_id) for item in sessions} == {
        ("device_macbook", "codex-cli", "sess_1"),
        ("device_macmini", "claude-code", "sess_1"),
    }


def test_message_lease_requires_exact_device_runtime_target(tmp_path) -> None:
    relay = RelayStore(MonitorStore(tmp_path / "monitor.db"))
    relay.enqueue(
        _envelope(
            "instruction.send", "msg_target", instance_id="device_macmini",
            runtime_id="claude-code",
        ),
        direction="outbound",
    )

    assert relay.claim_messages(
        direction="outbound", owner="macbook", instance_id="device_macbook",
        runtime_id="codex-cli", now="2026-07-14T01:00:00Z",
        lease_until="2026-07-14T01:00:30Z",
    ) == ()
    claimed = relay.claim_messages(
        direction="outbound", owner="macmini", instance_id="device_macmini",
        runtime_id="claude-code", now="2026-07-14T01:00:00Z",
        lease_until="2026-07-14T01:00:30Z",
    )
    assert [item.message_id for item in claimed] == ["msg_target"]


def test_current_task_switch_and_registry_survive_restart(tmp_path) -> None:
    path = tmp_path / "monitor.db"
    relay = RelayStore(MonitorStore(path))
    relay.upsert_session(_envelope("session.upsert", "msg_a", session_id="sess_a"))
    relay.upsert_session(_envelope("session.upsert", "msg_b", session_id="sess_b"))

    relay.select_current_task("feishu", "ou_owner", "macmini", "sess_a", selected_at="1")
    relay.select_current_task("feishu", "ou_owner", "macmini", "sess_b", selected_at="2")

    reopened = RelayStore(MonitorStore(path))
    assert reopened.current_session("feishu", "ou_owner").session_id == "sess_b"


def test_decisions_and_task_proposals_are_idempotent_and_cas_guarded(tmp_path) -> None:
    relay = RelayStore(MonitorStore(tmp_path / "monitor.db"))
    request = _envelope("decision.request", "msg_decision", version=3)
    proposal = _envelope("task.create.proposal", "msg_proposal", version=3)

    assert relay.record_decision(request) is True
    assert relay.record_decision(request) is False
    assert relay.answer_decision(
        _envelope("decision.answer", "msg_answer", version=3), answered_at="2026-07-14T01:01:00Z"
    ) is True
    assert relay.answer_decision(
        _envelope("decision.answer", "msg_answer_dup", version=3),
        answered_at="2026-07-14T01:01:01Z",
    ) is False

    assert relay.record_proposal(proposal) is True
    assert relay.confirm_proposal(
        _envelope("task.create.confirm", "msg_confirm", version=3),
        confirmed_at="2026-07-14T01:02:00Z",
    ) is True
    assert relay.confirm_proposal(
        _envelope("task.create.confirm", "msg_confirm_dup", version=3),
        confirmed_at="2026-07-14T01:02:01Z",
    ) is False
