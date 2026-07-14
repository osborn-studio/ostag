from datetime import datetime, timezone

import pytest

from ostag.copilot_relay.models import RelayEnvelope
from ostag.copilot_relay.policy import RelayPolicy
from ostag.copilot_relay.service import CopilotRelayService, RelayRoutingError
from ostag.copilot_relay.store import RelayStore
from ostag.core.identity import Principal
from ostag.work_monitor.store import MonitorStore


OWNER = Principal("feishu", "owner")
MEMBER = Principal("feishu", "member")


def _session_envelope(session_id: str = "sess_1", *, version: int = 3) -> RelayEnvelope:
    return RelayEnvelope.from_dict(
        {
            "schema_version": 1,
            "message_id": f"upsert_{session_id}",
            "binding": {
                "instance_id": "macmini", "task_id": "task_1", "run_id": "run_1",
                "session_id": session_id, "turn_id": "turn_3", "session_version": version,
            },
            "sequence": version,
            "created_at": "2026-07-14T01:00:00Z",
            "expires_at": "2026-07-14T02:00:00Z",
            "sender": OWNER.to_dict(),
            "kind": "session.upsert",
            "payload": {
                "agent_id": "codex-cli", "title": "Relay", "state": "awaiting_decision",
                "resume_supported": True, "latest_summary": "Waiting",
            },
            "correlation_id": None,
            "causation_id": None,
        }
    )


def _service(tmp_path, *, member_scope: bool = False):
    store = RelayStore(MonitorStore(tmp_path / "monitor.db"))
    store.upsert_session(_session_envelope())
    policy = RelayPolicy(
        OWNER,
        instance_scopes={MEMBER: frozenset({"macmini"})} if member_scope else {},
    )
    service = CopilotRelayService(store, policy)
    service.select_session(OWNER, "macmini", "sess_1", selected_at="1")
    return service, store


def test_ordinary_message_routes_only_to_exact_current_session(tmp_path) -> None:
    service, _ = _service(tmp_path)
    result = service.handle_text(OWNER, "继续完成测试", event_id="evt_1")

    assert result.state == "queued"
    assert result.outbound.kind == "instruction.send"
    assert result.outbound.binding.session_id == "sess_1"
    assert service.handle_text(OWNER, "继续完成测试", event_id="evt_1").state == "duplicate"


def test_quoted_decision_reply_uses_original_turn_and_rejects_stale_version(tmp_path) -> None:
    service, store = _service(tmp_path)
    request = _session_envelope().to_dict()
    request.update(message_id="om_decision_7", kind="decision.request")
    request["payload"] = {
        "decision_id": "decision_7", "question": "选择？",
        "choices": [{"choice_id": "b", "label": "B"}],
        "recommended_choice_id": "b", "blocking": True, "safe_work": [],
    }
    decision = RelayEnvelope.from_dict(request)
    store.enqueue(decision, direction="inbound")

    result = service.handle_text(OWNER, "采用 B", quote_message_id="om_decision_7")
    assert result.outbound.kind == "decision.answer"
    assert result.outbound.binding.turn_id == "turn_3"

    store.upsert_session(_session_envelope(version=4))
    with pytest.raises(RelayRoutingError, match="stale"):
        service.handle_text(OWNER, "再次回答", quote_message_id="om_decision_7")


def test_no_current_or_out_of_scope_actor_never_gets_guessed_target(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(RelayRoutingError, match="no current"):
        service.handle_text(MEMBER, "继续")
    with pytest.raises(PermissionError):
        service.select_session(MEMBER, "macmini", "sess_1", selected_at="2")


def test_scoped_member_can_select_only_granted_instance(tmp_path) -> None:
    service, _ = _service(tmp_path, member_scope=True)
    assert service.select_session(MEMBER, "macmini", "sess_1", selected_at="2").session_id == "sess_1"
    with pytest.raises(PermissionError):
        service.select_session(MEMBER, "employee-laptop", "sess_1", selected_at="3")


def test_new_task_requires_confirm_before_mailbox_delivery(tmp_path) -> None:
    service, store = _service(tmp_path)
    proposal = service.handle_text(OWNER, "新任务：修复登录回归", event_id="new_1")
    assert proposal.state == "requires_confirmation"
    assert store.claim_messages(
        direction="outbound", owner="w", now="2099-01-01T00:00:00Z",
        lease_until="2099-01-01T00:01:00Z",
    ) == ()

    confirmed = service.confirm_task_proposal(
        OWNER, proposal.outbound, confirmed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    assert confirmed.outbound.kind == "task.create.confirm"

    forged = proposal.outbound.to_dict()
    forged["binding"]["session_id"] = "other_session"
    with pytest.raises(RelayRoutingError, match="stored"):
        service.confirm_task_proposal(
            OWNER, RelayEnvelope.from_dict(forged), confirmed_at="2026-07-14T01:01:00Z"
        )


def test_snapshot_request_is_bound_to_current_session_without_exposing_local_ids(tmp_path) -> None:
    service, _ = _service(tmp_path)
    result = service.request_snapshot(OWNER, event_id="snapshot_1")
    assert result.outbound.kind == "assistant.snapshot"
    assert result.outbound.binding.session_id == "sess_1"
    assert result.outbound.payload["assistant_text"] == ""


@pytest.mark.parametrize(
    "text",
    ["部署到生产", "删除生成文件", "更新 token", "购买云资源", "给客户发消息", "修改权限"],
)
def test_high_risk_instruction_requires_approval_and_never_enters_mailbox(tmp_path, text) -> None:
    service, store = _service(tmp_path)
    result = service.handle_text(OWNER, text)
    assert result.state == "requires_approval"
    assert store.message_count() == 0
